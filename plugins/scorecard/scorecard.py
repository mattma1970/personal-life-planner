"""Scorecard core (Phase 5): deterministic, LLM-free.

- ``window_for`` — a half-open week window [Monday … next Monday) in
  *naive local* time (the ICS backend stores floating local datetimes; the
  vault stores local mtimes — everything compares cleanly).
- ``aggregate_week`` — hours per calendar category (clipped to the window),
  per-goal actual-vs-target rows, and vault activity (files created/updated
  inside the window by mtime/ctime).
- ``ScorecardStore`` — the week's numbers in the state DB (``scorecard_week``
  + ``vault_activity``), upserted by week, trended over ``history_weeks``,
  pruned beyond that (PRD.md §4: the state DB owns the scorecard time series).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from plp.kernel.calendar import CalendarEvent, CalendarStore
from plp.kernel.store import Store
from plp.kernel.vault import Vault

log = logging.getLogger("plp.scorecard")

_MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS scorecard_week (
            week_start TEXT NOT NULL,
            category   TEXT NOT NULL,
            hours      REAL NOT NULL DEFAULT 0,
            target_hours REAL,
            PRIMARY KEY (week_start, category)
        );
        CREATE TABLE IF NOT EXISTS vault_activity (
            week_start TEXT NOT NULL PRIMARY KEY,
            created    INTEGER NOT NULL DEFAULT 0,
            updated    INTEGER NOT NULL DEFAULT 0,
            notes      TEXT
        );
        """,
    )
]


# ------------------------------------------------------------------- windows


def window_for(
    ref: dt.datetime, week_start: int = 0
) -> tuple[dt.datetime, dt.datetime]:
    """The (naive) half-open week window containing ``ref``.
    ``week_start`` is the Python weekday of the week's first day (0=Monday).
    Windows are midnight-pinned (Mon 00:00 → Sun 24:00 for week_start=0),
    independent of the ref's clock time."""
    ref = ref.replace(tzinfo=None)
    start = ref - dt.timedelta(days=(ref.weekday() - week_start) % 7)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=7)


def previous_window(
    now: dt.datetime, week_start: int = 0
) -> tuple[dt.datetime, dt.datetime]:
    """The week a checkup run at ``now`` reports on.

    On the week's last day (week_start+6, Sunday by default) the current
    week is treated as done (a Sunday-evening checkup reports on the week
    that ends at midnight); on any other day, the last fully-completed
    week — never a half-finished one."""
    now = now.replace(tzinfo=None)
    start, _ = window_for(now, week_start)
    if now.weekday() == (week_start + 6) % 7:
        return start, start + dt.timedelta(days=7)
    return start - dt.timedelta(days=7), start


# ----------------------------------------------------------------- aggregation


def _clip_hours(event: CalendarEvent, start: dt.datetime, end: dt.datetime) -> float:
    s = max(event.start, start)
    e = min(event.end, end)
    return max(0.0, (e - s).total_seconds()) / 3600.0


def aggregate_week(
    vault: Vault,
    calendar: CalendarStore,
    goals: list,
    window: tuple[dt.datetime, dt.datetime],
) -> dict:
    """One week of measured life, as plain data (never raises on a missing
    calendar file — an empty week is a valid measurement)."""
    start, end = window
    # Only goal categories stay named; everything else (work, ???, blank)
    # folds into "uncategorized" so the scorecard stays small and readable.
    goal_cats = {g.category.strip().lower() for g in goals}
    hours_by_cat: dict[str, float] = {}
    events: list[CalendarEvent] = []
    try:
        events = calendar.list(start, end)
    except Exception as exc:  # noqa: BLE001 - measurement must not die
        log.warning("calendar unreadable during scorecard: %s", exc)
    for e in events:
        cat = (e.category or "uncategorized").strip().lower() or "uncategorized"
        if cat not in goal_cats:
            cat = "uncategorized"
        hours_by_cat[cat] = hours_by_cat.get(cat, 0.0) + _clip_hours(e, start, end)
    hours_by_cat = {k: round(v, 1) for k, v in hours_by_cat.items()}

    goal_rows = []
    for g in goals:
        actual = hours_by_cat.get(g.category.lower(), 0.0)
        target = g.target_hours_week
        goal_rows.append(
            {
                "title": g.title,
                "category": g.category,
                "target": target,
                "actual": actual,
                "delta": None if target is None else round(actual - target, 1),
                "notes": g.notes,
            }
        )

    created, updated = 0, 0
    for rel in vault.list():
        p = vault.root / rel
        try:
            st = p.stat()
        except OSError:
            continue
        m = dt.datetime.fromtimestamp(st.st_mtime)
        c = dt.datetime.fromtimestamp(st.st_ctime)
        if start <= m < end:
            if start <= c < end:
                created += 1
            else:
                updated += 1

    return {
        "window": (start, end),
        "hours_by_category": hours_by_cat,
        "events": len(events),
        "goals": goal_rows,
        "vault_created": created,
        "vault_updated": updated,
    }


# -------------------------------------------------------------------- storage


class ScorecardStore:
    """Trend history over the shared state DB (namespaced migration)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        store.migrate_for("scorecard", _MIGRATIONS)

    def save(
        self,
        window: tuple[dt.datetime, dt.datetime],
        hours_by_category: dict[str, float],
        goal_rows: list[dict],
        vault_created: int,
        vault_updated: int,
        notes: str | None = None,
        history_weeks: int = 26,
    ) -> None:
        if isinstance(window, dt.datetime):
            window = (window, window + dt.timedelta(days=7))
        ws = window[0].date().isoformat()
        for cat, hours in hours_by_category.items():
            self._store.execute(
                "INSERT INTO scorecard_week (week_start, category, hours) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(week_start, category) DO UPDATE SET "
                "hours=excluded.hours, target_hours=COALESCE(excluded.target_hours, target_hours)",
                (ws, cat, hours),
            )
        # attach targets (a category's target is its goal's, if any)
        for row in goal_rows:
            if row.get("target") is None:
                continue
            self._store.execute(
                "INSERT INTO scorecard_week (week_start, category, hours, target_hours) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(week_start, category) DO UPDATE SET target_hours=excluded.target_hours",
                (ws, row["category"].lower(), row["target"]),
            )
        self._store.execute(
            "INSERT INTO vault_activity (week_start, created, updated, notes) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(week_start) DO UPDATE SET created=excluded.created, "
            "updated=excluded.updated, notes=COALESCE(excluded.notes, vault_activity.notes)",
            (ws, vault_created, vault_updated, notes),
        )
        self._prune(ws, history_weeks)

    def _prune(self, keep_anchor: str, weeks: int) -> None:
        anchor = dt.date.fromisoformat(keep_anchor)
        cutoff = anchor - dt.timedelta(weeks=weeks - 1)
        self._store.execute(
            "DELETE FROM scorecard_week WHERE week_start < ?",
            (cutoff.isoformat(),),
        )
        self._store.execute(
            "DELETE FROM vault_activity WHERE week_start < ?",
            (cutoff.isoformat(),),
        )

    def trend(self, category: str, weeks: int = 8) -> list[dict]:
        """Latest ``weeks`` rows for a category, oldest first, with gaps kept
        as zeros so drift (two quiet weeks in a row) is visible."""
        rows = self._store.query_json(
            "SELECT week_start, hours, target_hours FROM scorecard_week "
            "WHERE category = ? ORDER BY week_start DESC LIMIT ?",
            (category.lower(), weeks),
        )
        if not rows:
            return []
        rows = list(reversed(rows))
        first = dt.date.fromisoformat(rows[0]["week_start"])
        last = dt.date.fromisoformat(rows[-1]["week_start"])
        out, cur = [], first
        have = {r["week_start"]: r for r in rows}
        while cur <= last:
            k = cur.isoformat()
            r = have.get(k)
            out.append(
                {
                    "week": k,
                    "hours": r["hours"] if r else 0.0,
                    "target": (r or {}).get("target_hours"),
                }
            )
            cur += dt.timedelta(weeks=1)
        return out

    def recent_vault(self, weeks: int = 4) -> list[dict]:
        """Latest ``weeks`` vault-activity rows, oldest first (like trend())."""
        rows = self._store.query_json(
            "SELECT week_start, created, updated FROM vault_activity "
            "ORDER BY week_start DESC LIMIT ?",
            (weeks,),
        )
        return list(reversed(rows))
