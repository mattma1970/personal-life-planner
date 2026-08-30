"""Gift vault store (Phase 3, PRD.md S4) — gift records as vault documents.

One markdown file per gift: ``gifts/<YYYY-MM-DD>-<slug>.md`` with frontmatter
``kind/idea/occasion/status/budget/created`` and a freeform notes body
(including, when the owner writes it, "what to say when I give it").

Lifecycle: ``idea → shortlist → bought → given`` (PRD.md S4). Every
read-modify-write happens under the vault lock with an mtime check, so a
concurrent human edit of the same file wins (kernel.vault.VaultConflict).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta

from plp.kernel.vault import Vault, VaultConflict

log = logging.getLogger("plp.gifts")

STATUSES = ("idea", "shortlist", "bought", "given")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase hyphen slug; empty input → 'gift'."""
    text = unicodedata.normalize("NFKD", text).lower()
    text = _SLUG_RE.sub("-", text).strip("-")
    return text[:max_len].strip("-") or "gift"


def next_occurrence(month: int, day: int, from_date: date, horizon_days: int) -> date | None:
    """Next ``month/day`` on/after ``from_date`` within the horizon, or None.

    Years where the date does not exist (e.g. 02-30) are skipped.
    """
    horizon = from_date + timedelta(days=horizon_days)
    # a few years of headroom: 02-29 skips non-leap years, and a far horizon
    # can span more than a year after the first candidate.
    for year in range(from_date.year, from_date.year + 4):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if from_date <= cand <= horizon:
            return cand
    return None


class GiftStore:
    """Gift records inside the vault (the daemon is the only writer)."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    # ----------------------------------------------------------------- read

    def list(self, occasion: str | None = None, status: str | None = None) -> list[dict]:
        """Gifts (newest first), optionally filtered by occasion/status."""
        out = []
        for rel in self.vault.list("gifts"):
            got = self.vault.read(rel)
            if got is None:
                continue
            meta, _body = got
            if meta.get("kind") != "gift":
                continue
            if occasion and meta.get("occasion") != occasion:
                continue
            if status and meta.get("status") != status:
                continue
            row = dict(meta)
            row["file"] = rel
            out.append(row)
        out.sort(key=lambda r: r.get("created", ""), reverse=True)
        return out

    def find(self, gift_id: str) -> tuple[str, dict, str] | None:
        """Locate a gift by filename stem (``2026-08-30-vinyl-turntable``) or
        vault-relative path; returns ``(rel, meta, body)`` or None."""
        rel = gift_id if gift_id.endswith(".md") else f"gifts/{gift_id}.md"
        got = self.vault.read(rel)
        if got is None:
            return None
        meta, body = got
        if meta.get("kind") != "gift":
            raise ValueError(f"{rel} is not a gift record (kind={meta.get('kind')!r})")
        return rel, meta, body

    # ----------------------------------------------------------------- write

    def add(
        self,
        idea: str,
        occasion: str = "just because",
        budget: float | None = None,
        notes: str = "",
    ) -> tuple[str, dict]:
        """Capture a gift idea; returns ``(relpath, meta)``.

        Low-friction on purpose: one line of text is enough.
        """
        idea = " ".join(idea.split())
        if not idea:
            raise ValueError("gift idea is empty")
        today = date.today().isoformat()
        stem = f"{today}-{slugify(idea)}"
        rel = f"gifts/{stem}.md"
        with self.vault.lock():
            n = 2
            while self.vault.read(rel) is not None:
                rel = f"gifts/{stem}-{n}.md"
                n += 1
            meta: dict = {
                "kind": "gift",
                "idea": idea,
                "occasion": occasion,
                "status": "idea",
                "created": today,
            }
            if budget is not None and budget > 0:
                meta["budget"] = round(float(budget), 2)
            body = f"# {idea}\n"
            if notes:
                body += f"\n{notes.strip()}\n"
            self.vault.write(rel, body.rstrip("\n") + "\n", meta)
        log.info("gift added: %s (%s)", rel, occasion)
        return rel, meta

    def set_status(self, gift_id: str, status: str, price: float | None = None) -> tuple[str, dict]:
        """Advance (or correct) a gift's lifecycle state; stamps dates.

        Raises ValueError for an unknown status, KeyError if the gift is
        missing, VaultConflict if a human edited the file meanwhile.
        """
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r} (use one of {', '.join(STATUSES)})")
        found = self.find(gift_id)
        if found is None:
            raise KeyError(f"gift not found: {gift_id}")
        with self.vault.lock():
            rel, meta, body = self.find(gift_id)  # re-read fresh under the lock
            mtime = (self.vault.root / rel).stat().st_mtime
            meta = dict(meta)
            meta["status"] = status
            if status == "bought":
                meta["bought_at"] = datetime.now().date().isoformat()
                if price is not None and price > 0:
                    meta["price_paid"] = round(float(price), 2)
            if status == "given":
                meta["given_at"] = datetime.now().date().isoformat()
            self.vault.write(rel, body, meta, expected_mtime=mtime)
        log.info("gift %s → %s", rel, status)
        return rel, meta
