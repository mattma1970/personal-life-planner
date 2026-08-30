"""Travel planner store (Phase 3, PRD.md S5) — trip plans as vault docs.

One markdown file per trip: ``travel/<slug>.md`` with frontmatter
``kind: trip / status / destination / dates / budget / created`` and sections
for why-it-fits, ideas, booking deadlines, open questions, feasibility.

Trip lifecycle: ``brainstorm → planning → booked → done``. Preferences live
in a sibling vault file the owner edits freely; the brainstorm reads it.
The LLM only seasons a plan doc (why/ideas prose) — the doc, its status,
its feasibility checks and its booking list all exist without it.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from plp.kernel.vault import Vault, VaultConflict

log = logging.getLogger("plp.travel")

STATUSES = ("brainstorm", "planning", "booked", "done")
_DASHES_RE = re.compile(r"[\s\-/]+")

PREFERENCES_TEMPLATE = """---
kind: preferences
---

# Trip preferences (edit this file — the planner reads it)

## Hard limits
- budget per trip: (fill in, e.g. $3000 total or $150/night)
- trip length: up to 10 days, weekends or a week off
- no: red-eyes, layovers > 3h, (…anything else off the table)

## We like
- pace: (relaxed / packed / mixed)
- weather: (beach / mountains / cities / mix)
- her interests: (…she will read this plan too)
- my interests: (…and so will she)

## Logistics
- home base: (city, airport code)
- travel style: (direct flights only / fine with one stop)
- must-nots: (e.g. no hotels without a window)
"""


def slugify(text: str, max_len: int = 40) -> str:
    text = _DASHES_RE.sub("-", text.strip().lower()).strip("-")
    return (text[:max_len].strip("-") or "trip")


def parse_dates(spec: str | None) -> tuple[date, date, str] | None:
    """Parse ``YYYY-MM-DD..YYYY-MM-DD`` (a single date → one-day trip).

    Returns ``(start, end, raw)`` or None when absent/unparseable.
    """
    if not spec:
        return None
    s, _, e = spec.partition("..")
    try:
        start = datetime.strptime(s.strip(), "%Y-%m-%d").date()
        end = datetime.strptime(e.strip(), "%Y-%m-%d").date() if e else start
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    return start, end, spec


class TravelStore:
    """Trip plans inside the vault (the daemon is the only writer)."""

    def __init__(self, vault: Vault, preferences_rel: str = "travel/preferences.md") -> None:
        self.vault = vault
        self.preferences_rel = preferences_rel

    # ---------------------------------------------------------- preferences

    def ensure_preferences(self) -> None:
        if self.vault.read(self.preferences_rel) is None:
            with self.vault.lock():
                if self.vault.read(self.preferences_rel) is None:
                    self.vault.write(self.preferences_rel, PREFERENCES_TEMPLATE, {})
            log.info("seeded travel preferences at %s", self.preferences_rel)

    def preferences_text(self) -> str:
        self.ensure_preferences()
        got = self.vault.read(self.preferences_rel)
        assert got is not None
        _meta, body = got
        return body.strip()

    # ---------------------------------------------------------------- plans

    def list(self, status: str | None = None) -> list[dict]:
        out = []
        for rel in self.vault.list("travel"):
            if rel == self.preferences_rel:
                continue
            got = self.vault.read(rel)
            if got is None:
                continue
            meta, _body = got
            if meta.get("kind") != "trip":
                continue
            if status and meta.get("status") != status:
                continue
            row = dict(meta)
            row["file"] = rel
            out.append(row)
        out.sort(key=lambda r: r.get("created", ""), reverse=True)
        return out

    def find(self, plan_id: str) -> tuple[str, dict, str] | None:
        rel = plan_id if plan_id.endswith(".md") else f"travel/{plan_id}.md"
        got = self.vault.read(rel)
        if got is None:
            return None
        meta, body = got
        if meta.get("kind") != "trip":
            raise ValueError(f"{rel} is not a trip plan (kind={meta.get('kind')!r})")
        return rel, meta, body

    def create(
        self,
        destination: str,
        dates: str | None,
        budget: float | None,
        sections: dict,
        status: str = "brainstorm",
    ) -> tuple[str, dict]:
        """Write a new plan doc; returns ``(relpath, meta)``.

        ``sections`` maps section title → body text (the plugin decides how
        each is produced: LLM seasoning or deterministic fallback).
        """
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r} (use one of {', '.join(STATUSES)})")
        stem = f"{date.today().isoformat()}-{slugify(destination)}"
        rel = f"travel/{stem}.md"
        with self.vault.lock():
            n = 2
            while self.vault.read(rel) is not None:
                rel = f"travel/{stem}-{n}.md"
                n += 1
            meta: dict = {
                "kind": "trip",
                "destination": destination,
                "status": status,
                "created": date.today().isoformat(),
            }
            if dates:
                meta["dates"] = dates
            if budget is not None and budget > 0:
                meta["budget"] = round(float(budget), 2)
            body = f"# Trip: {destination}\n"
            for title, text in sections.items():
                body += f"\n## {title}\n{str(text).strip()}\n"
            self.vault.write(rel, body.rstrip("\n") + "\n", meta)
        log.info("trip plan written: %s (%s)", rel, status)
        return rel, meta

    def set_status(self, plan_id: str, status: str) -> tuple[str, dict]:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r} (use one of {', '.join(STATUSES)})")
        found = self.find(plan_id)
        if found is None:
            raise KeyError(f"trip plan not found: {plan_id}")
        with self.vault.lock():
            rel, meta, body = self.find(plan_id)
            mtime = (self.vault.root / rel).stat().st_mtime
            meta = dict(meta)
            meta["status"] = status
            self.vault.write(rel, body, meta, expected_mtime=mtime)
        log.info("trip %s → %s", rel, status)
        return rel, meta
