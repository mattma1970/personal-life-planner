"""Gift context for the checkup (Phase 5) — read-only, kernel-only imports.

Reads the gift vault (``plp-vault/gifts/*.md`` frontmatter) and the configured
occasions; never writes. The gifts plugin keeps its own store; this module is
the scorecard's side of that boundary.
"""

from __future__ import annotations

import calendar as _cal
import datetime as dt

from plp.kernel.config import PlpConfig
from plp.kernel.vault import Vault

MAX_LINES = 8


def next_occurrence(month: int, day: int, today: dt.date, window_days: int) -> dt.date | None:
    """Next occurrence of month/day within ``window_days`` (a day that does
    not exist in some years is skipped)."""
    for y in (today.year, today.year + 1):
        last = _cal.monthrange(y, month)[1]
        if day > last:
            continue
        d = dt.date(y, month, day)
        delta = (d - today).days
        if 0 <= delta <= window_days:
            return d
    return None


def upcoming_occasions(cfg: PlpConfig, now: dt.datetime) -> list[str]:
    """'anniversary 2026-10-02 (33d)' style lines for occasions inside the
    configured review window."""
    gc = cfg.gifts
    today = now.date()
    out = []
    for occ in gc.occasions:
        nxt = next_occurrence(occ.month, occ.day, today, gc.review_window_days)
        if nxt is not None:
            out.append(f"{occ.name} {nxt.isoformat()} ({(nxt - today).days}d)")
    return out[:MAX_LINES]


def in_flight_gifts(vault: Vault, now: dt.datetime) -> list[str]:
    """'2026-anniversary.md — anniversary (shortlist, $150, 12d)' for every
    gift still idea/shortlist."""
    today = now.date()
    out = []
    for rel in vault.list("gifts"):
        row = vault.read(rel)
        if row is None:
            continue
        meta, _body = row
        status = (meta.get("status") or "").strip()
        if status not in ("idea", "shortlist"):
            continue
        bits = [rel.rsplit("/", 1)[-1], "—", str(meta.get("occasion") or "?"), "(", status]
        if meta.get("budget"):
            bits.append(f", ${meta['budget']:g}")
        created = (meta.get("created") or "")[:10]
        if created:
            try:
                bits.append(f", {(today - dt.date.fromisoformat(created)).days}d")
            except ValueError:
                pass
        bits.append(")")
        out.append(" ".join(bits))
    return out[:MAX_LINES]
