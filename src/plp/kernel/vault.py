"""The vault — human-first, LLM-first persistence (PRD.md §5, §6.2).

Markdown with YAML frontmatter inside an Obsidian-compatible, git-tracked
directory. Rules (PRD.md §5):

- single automated writer (the daemon); a lockfile serializes write sessions;
- writes are atomic (temp file + ``os.replace``);
- the search index (``docs`` + FTS5 in the state DB) is rebuilt from an
  mtime scan — never from write events, so human edits are picked up too;
- on conflict the **human's file edit wins**: if the file changed on disk
  since the caller last read it, ``write`` raises :class:`VaultConflict`
  instead of clobbering it.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from .store import Store
from .util import utcnow_iso

log = logging.getLogger("plp.kernel.vault")

_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n+", re.DOTALL)

__all__ = ["Vault", "VaultConflict", "parse_frontmatter", "dump_frontmatter"]


class VaultConflict(Exception):
    """The file changed on disk since it was last read — a human edit wins."""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``--- yaml ---`` frontmatter from the body.

    No frontmatter (or unparseable YAML) → ``({}, text)``: a malformed
    human file must never crash the daemon, it just loses its metadata.
    """
    if not text:
        return {}, ""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        log.warning("vault frontmatter is not valid YAML (%s); treating file as plain", exc)
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, text[m.end():]


def dump_frontmatter(meta: dict, body: str) -> str:
    """Render frontmatter + body (stable key order, unicode preserved)."""
    if not meta:
        return body
    yaml_text = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip()
    return f"---\n{yaml_text}\n---\n\n{body.lstrip()}"


class Vault:
    """A markdown directory the daemon may read and (carefully) write."""

    def __init__(self, root: str | Path, store: Store) -> None:
        self.root = Path(root)
        self.store = store
        self.root.mkdir(parents=True, exist_ok=True)
        self.store.migrate_core()
        self._lock_path = self.root / ".plp.lock"
        self._lock_fd: int | None = None
        self._lock_depth = 0

    # ------------------------------------------------------------------ lock

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize daemon write-sessions (daemon tick vs. ``plp run``).

        One flock per process; nested use in the same process is reentrant
        (a depth counter keeps it held until the outermost user releases).
        """
        if self._lock_fd is None:
            self._lock_path.touch(exist_ok=True)
            self._lock_fd = os.open(self._lock_path, os.O_RDWR)
        if self._lock_depth == 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1
            if self._lock_depth == 0:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    # ----------------------------------------------------------------- files

    def read(self, rel: str) -> tuple[dict, str] | None:
        """Return ``(meta, body)`` or ``None`` if the file does not exist."""
        p = self.root / rel
        if not p.is_file():
            return None
        return parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))

    def write(
        self,
        rel: str,
        body: str,
        meta: dict | None = None,
        expected_mtime: float | None = None,
    ) -> Path:
        """Atomic write. If ``expected_mtime`` is given and the on-disk file
        is newer/different, raise :class:`VaultConflict` (human edit wins)."""
        p = self.root / rel
        if expected_mtime is not None and p.exists():
            actual = p.stat().st_mtime
            if actual != expected_mtime:
                raise VaultConflict(
                    f"vault file {rel} changed on disk (mtime {actual} != {expected_mtime}); "
                    "human edit wins, write skipped"
                )
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(dump_frontmatter(meta or {}, body), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def remove(self, rel: str) -> bool:
        p = self.root / rel
        if p.is_file():
            p.unlink()
            return True
        return False

    def list(self, subdir: str | None = None) -> list[str]:
        """Relative paths of all ``.md`` files (optional subdirectory prefix)."""
        base = self.root / subdir if subdir else self.root
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*.md"))

    # ----------------------------------------------------------------- index

    def sync_index(self) -> int:
        """Rebuild the search index from an mtime scan; return # changes.

        Adds/updates ``docs`` rows for changed files, drops deleted ones, and
        rebuilds the FTS5 table. Cheap: only stat() on each file.
        """
        on_disk: dict[str, tuple[float, int]] = {}
        for rel in self.list():
            st = (self.root / rel).stat()
            on_disk[rel] = (st.st_mtime, st.st_size)

        known: dict[str, tuple[float, int]] = {
            r["path"]: (float(r["mtime"]), int(r["size"]))
            for r in self.store.query("SELECT path, mtime, size FROM vault_files")
        }

        removed = [p for p in known if p not in on_disk]
        changed = [p for p in on_disk if known.get(p) != on_disk[p]]

        for rel in removed:
            row = self.store.query_one("SELECT id FROM docs WHERE path = ?", (rel,))
            if row is not None:
                self.store.execute("DELETE FROM docs WHERE path = ?", (rel,))
                if self.store.fts5:
                    self.store.execute("DELETE FROM fts_docs WHERE rowid = ?", (row["id"],))
            self.store.execute("DELETE FROM vault_files WHERE path = ?", (rel,))

        for rel in changed:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            mtime, size = on_disk[rel]
            row = self.store.query_one("SELECT id FROM docs WHERE path = ?", (rel,))
            if row is not None:
                self.store.execute(
                    "UPDATE docs SET body = ?, updated_at = ? WHERE id = ?",
                    (text, utcnow_iso(), row["id"]),
                )
            else:
                self.store.execute(
                    "INSERT INTO docs (path, body, updated_at) VALUES (?, ?, ?)",
                    (rel, text, utcnow_iso()),
                )
            self.store.execute(
                "INSERT INTO vault_files (path, mtime, size, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
                "size=excluded.size, updated_at=excluded.updated_at",
                (rel, mtime, size, utcnow_iso()),
            )

        if self.store.fts5 and (removed or changed):
            self.store.execute("INSERT INTO fts_docs(fts_docs) VALUES ('rebuild')")
        if removed or changed:
            log.info("vault index: %d added/updated, %d removed", len(changed), len(removed))
        return len(removed) + len(changed)

    def search(self, text: str, limit: int = 10) -> list[dict]:
        """Full-text search over the vault (syncs the index first)."""
        self.sync_index()
        return self.store.search_docs(text, limit=limit)
