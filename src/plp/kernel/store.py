"""SQLite (WAL + FTS5) persistence — the machine-first tier (PRD.md §5, §6.2).

Core owns a numbered migration chain; plugins register their own chains under
``plugin.<name>`` (the mechanism lands in use with Phase 3). One connection per
thread; all timestamps are UTC ISO-8601 (see ``util.utcnow_iso``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from .util import utcnow_iso

log = logging.getLogger("plp.kernel.store")

CORE_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            ns TEXT NOT NULL,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY (ns, version));

        CREATE TABLE IF NOT EXISTS jobs (
            name TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('cron', 'oneshot')),
            spec TEXT NOT NULL,
            plugin TEXT,
            last_fired_at TEXT,
            retries INTEGER NOT NULL DEFAULT 2,
            timeout_s REAL NOT NULL DEFAULT 300,
            staleness_h REAL NOT NULL DEFAULT 36,
            created_at TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job TEXT NOT NULL,
            plugin TEXT,
            status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'timeout')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_ms INTEGER,
            error TEXT,
            detail TEXT);

        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
            proposed_at TEXT NOT NULL,
            resolved_at TEXT,
            note TEXT);

        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS plugin_status (
            name TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('ok', 'failed')),
            error TEXT,
            updated_at TEXT NOT NULL);
        """,
    ),
    (
        2,
        """
        -- Search-target documents (Phase 3 indexes the vault here; the FTS5
        -- external-content table is created in code once availability is known).
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            body TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        """,
    ),
]


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.fts5 = False

    # ------------------------------------------------------------------ conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------ migrations

    def _migrate_ns(self, ns: str, migrations: list[tuple[int, str]]) -> None:
        conn = self._conn()
        try:
            applied = {
                r[0]
                for r in conn.execute(
                    "SELECT version FROM schema_version WHERE ns=?", (ns,)
                )
            }
        except sqlite3.OperationalError:
            applied = set()  # fresh database: schema_version does not exist yet
        for version, sql in sorted(migrations):
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(ns, version, applied_at) VALUES (?,?,?)",
                (ns, version, utcnow_iso()),
            )
            log.info("migration %s@%d applied", ns, version)
        conn.commit()

    def migrate_core(self) -> None:
        self._migrate_ns("core", CORE_MIGRATIONS)
        self._ensure_fts()

    def migrate_for(self, plugin: str, migrations: list[tuple[int, str]]) -> None:
        """Apply a plugin-owned migration chain (PRD.md §5: plugins own their tables)."""
        self._migrate_ns(f"plugin.{plugin}", migrations)

    def _ensure_fts(self) -> None:
        if self.fts5:
            return
        conn = self._conn()
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5("
                "path, body, content='docs', content_rowid='id')"
            )
            conn.commit()
            self.fts5 = True
        except sqlite3.OperationalError as exc:
            log.warning("FTS5 unavailable (%s); search degrades to LIKE", exc)
            self.fts5 = False

    # ------------------------------------------------------- generic helpers

    def execute(self, sql: str, params: tuple = ()) -> None:
        conn = self._conn()
        conn.execute(sql, params)
        conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def query_json(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.query(sql, params)]

    def search_docs(self, text: str, limit: int = 20) -> list[dict]:
        """Full-text search over the docs table (FTS5, or LIKE fallback)."""
        like = f"%{text}%"
        if self.fts5:
            safe = text.replace('"', '""')
            rows = self.query(
                "SELECT d.path, d.body, d.updated_at FROM fts_docs f "
                "JOIN docs d ON d.id = f.rowid WHERE fts_docs MATCH ? "
                "ORDER BY rank LIMIT ?",
                (f'"{safe}"', limit),
            )
        else:
            rows = self.query(
                "SELECT path, body, updated_at FROM docs WHERE body LIKE ? LIMIT ?",
                (like, limit),
            )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- runs

    def start_run(self, job: str, plugin: str | None) -> int:
        cur = self._conn().execute(
            "INSERT INTO runs(job, plugin, status, started_at) VALUES (?,?, 'running', ?)",
            (job, plugin, utcnow_iso()),
        )
        self._conn().commit()
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, status: str, error: str | None = None, detail: str | None = None
    ) -> None:
        self.execute(
            "UPDATE runs SET status=?, ended_at=?, duration_ms=?,"
            " error=COALESCE(?, error), detail=COALESCE(?, detail) WHERE id=?",
            (
                status,
                utcnow_iso(),
                self._elapsed_ms(run_id),
                error,
                detail,
                run_id,
            ),
        )

    def _elapsed_ms(self, run_id: int) -> int:
        row = self.query_one("SELECT started_at FROM runs WHERE id=?", (run_id,))
        if row is None:
            return 0
        from .util import parse_ts

        started = parse_ts(row["started_at"])
        return int((parse_ts(utcnow_iso()) - started).total_seconds() * 1000)

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self.query(
            "SELECT id, job, plugin, status, started_at, ended_at, duration_ms,"
            " error, detail FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- jobs

    def upsert_job(
        self,
        name: str,
        kind: str,
        spec: str,
        plugin: str | None = None,
        retries: int = 2,
        timeout_s: float = 300.0,
        staleness_h: float = 36.0,
    ) -> None:
        self.execute(
            """
            INSERT INTO jobs(name, kind, spec, plugin, retries, timeout_s,
                             staleness_h, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                kind=excluded.kind, spec=excluded.spec, plugin=excluded.plugin,
                retries=excluded.retries, timeout_s=excluded.timeout_s,
                staleness_h=excluded.staleness_h
            """,
            (name, kind, spec, plugin, retries, timeout_s, staleness_h, utcnow_iso()),
        )

    def add_one_shot(
        self,
        name: str,
        fire_at: str,
        plugin: str | None = None,
        retries: int = 0,
        timeout_s: float = 300.0,
    ) -> None:
        self.upsert_job(name, "oneshot", fire_at, plugin, retries, timeout_s, 0.0)

    def set_job_fired(self, name: str, at: str) -> None:
        self.execute(
            "UPDATE jobs SET last_fired_at=? WHERE name=? AND (last_fired_at IS NULL OR last_fired_at<?)",
            (at, name, at),
        )

    def all_jobs(self) -> list[dict]:
        return self.query_json("SELECT * FROM jobs ORDER BY name")

    def get_job(self, name: str) -> dict | None:
        row = self.query_one("SELECT * FROM jobs WHERE name=?", (name,))
        return dict(row) if row else None

    # ------------------------------------------------------------- approvals

    def propose(self, kind: str, payload: dict, note: str | None = None) -> int:
        cur = self._conn().execute(
            "INSERT INTO approvals(kind, payload, status, proposed_at, note)"
            " VALUES (?,?, 'pending', ?, ?)",
            (kind, json.dumps(payload), utcnow_iso(), note),
        )
        self._conn().commit()
        return int(cur.lastrowid)

    def pending_approvals(self, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.query(
                "SELECT * FROM approvals WHERE status='pending' AND kind=? ORDER BY id",
                (kind,),
            )
        else:
            rows = self.query(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY id"
            )
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def get_approval(self, aid: int) -> dict | None:
        row = self.query_one("SELECT * FROM approvals WHERE id=?", (aid,))
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def resolve_approval(self, aid: int, approved: bool, note: str | None = None) -> bool:
        row = self.query_one(
            "SELECT id FROM approvals WHERE id=? AND status='pending'", (aid,)
        )
        if row is None:
            return False
        self.execute(
            "UPDATE approvals SET status=?, resolved_at=?, note=COALESCE(?, note)"
            " WHERE id=?",
            ("approved" if approved else "rejected", utcnow_iso(), note, aid),
        )
        return True

    def expire_stale_approvals(self, max_age_hours: float) -> int:
        from .util import parse_ts

        cutoff = parse_ts(utcnow_iso())
        import datetime as _dt

        cutoff -= _dt.timedelta(hours=max_age_hours)
        rows = self.query(
            "SELECT id, proposed_at FROM approvals WHERE status='pending'"
        )
        n = 0
        for r in rows:
            if parse_ts(r["proposed_at"]) < cutoff:
                self.execute(
                    "UPDATE approvals SET status='expired', resolved_at=? WHERE id=?",
                    (utcnow_iso(), r["id"]),
                )
                n += 1
        return n

    # --------------------------------------------------------------- digests

    def save_digest(self, kind: str, content: str) -> int:
        cur = self._conn().execute(
            "INSERT INTO digests(kind, created_at, content) VALUES (?,?,?)",
            (kind, utcnow_iso(), content),
        )
        self._conn().commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------- plugin status

    def set_plugin_status(self, name: str, state: str, error: str | None = None) -> None:
        self.execute(
            "INSERT INTO plugin_status(name, state, error, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
            " error=excluded.error, updated_at=excluded.updated_at",
            (name, state, error, utcnow_iso()),
        )

    def plugin_statuses(self) -> list[dict]:
        return self.query_json("SELECT * FROM plugin_status ORDER BY name")
