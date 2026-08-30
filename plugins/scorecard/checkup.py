"""The weekly checkup (PRD.md §3, Phase 5): scorecard, wins & drift, 2–3
approvable proposals — one honest read on the week, ending in things to do.

Two layers, the LLM one optional (PRD.md §6.5):

- **Floor** (always works, no LLM): measured numbers, rule-based wins/drift,
  and deterministic proposals (a recurring slot each week per category) for
  every under-targeted goal.
- **Seasoning** (self-hosted Qwen via the bounded agent): a narrow scenario
  mounts ≤8 tools (scorecard/calendar/gifts) and must answer with strict JSON
  (validate → one retry hint → fall back to the floor). The LLM *judges*
  (phrasing, prioritization, what to say); it never invents numbers — all
  measurements come from the store in the context.

Sibling rule: this module imports only stdlib + ``plp.kernel``. The plugin
passes the ``goals`` and ``gifts_context`` modules in.
"""

from __future__ import annotations

import datetime as dt
import logging

from plp.kernel.agent import Agent, Scenario
from plp.kernel.calendar import CalendarStore, open_calendar_store
from plp.kernel.context import PluginContext
from plp.kernel.vault import Vault

log = logging.getLogger("plp.checkup")

#: Default recurring proposal slots (weekday, hour, duration h) per category.
#: Weekday is Python convention: 0=Monday … 6=Sunday.
PROPOSAL_SLOTS: dict[str, tuple[int, int, float]] = {
    "wife": (2, 19, 2),      # Wed 19:00, 2h
    "family": (2, 19, 2),
    "gifts": (6, 19, 1),     # Sun 19:00, 1h (with gifts.review, 19:00)
    "travel": (5, 10, 1),    # Sat 10:00, 1h
    "deep-work": (0, 9, 2),  # Mon 09:00, 2h (plan the week's work)
}
_DEFAULT_SLOT = (0, 18, 2)

CHECKUP_SYSTEM_PROMPT = """You are the weekly checkup of a local-first personal
assistant. You receive last week's measured scorecard (hours per life category
vs. stated goals, trends, vault activity, upcoming occasions, in-flight gifts,
what is already scheduled next week) as context. You may call the mounted
tools to look at more of the calendar, but you must not invent facts or
numbers that are not in the context or tool results.

Reply with ONE JSON object only — no prose, no markdown fences — with:
- "wins": 2-4 short, concrete sentences about what went well (quote real
  numbers: "6.0h with her" beats "good time with family").
- "drift": 0-3 honest sentences about where the week drifted from stated
  goals. Plain, unsentimental, never preachy, never blaming.
- "proposals": 2-3 concrete calendar blocks for the coming 7 days that would
  move an under-targeted goal forward. Each: {"title": short imperative,
  "when": "YYYY-MM-DDTHH:MM", "category": one of the goal categories,
  "notes": one line of context}. Prefer personal categories over work and
  spread the blocks across the week; avoid the times already scheduled.
- "summary": one sentence that captures the week in the roundest way.
"""

CHECKUP_SCHEMA: dict = {
    "type": "object",
    "required": ["wins", "drift", "proposals"],
    "additionalProperties": False,
    "properties": {
        "wins": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {"type": "string"},
        },
        "drift": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "proposals": {
            "type": "array",
            "minItems": 2,
            "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["title", "when", "category", "notes"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "when": {"type": "string"},
                    "category": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


# ------------------------------------------------------------------ floor (no LLM)


def floor_wins_drift(data: dict, trends: dict[str, list[dict]]) -> tuple[list[str], list[str]]:
    """Deterministic wins/drift from the measured week plus trend history.

    A goal two weeks short gets the streak folded into its own line
    (one line per drifted goal, so the cap can't bury the signal); a
    zero-streak category without a measured goal still gets its own line."""
    wins, drift = [], []
    two_zero = {
        cat.lower()
        for cat, tr in trends.items()
        if len(tr) >= 2 and tr[-1]["hours"] == 0 and tr[-2]["hours"] == 0
    }
    for row in data["goals"]:
        if row["delta"] is None:
            continue
        if row["delta"] > 0:
            wins.append(
                f"{row['title']}: {row['actual']:g}h vs {row['target']:g}h target"
                f" (+{row['delta']:g}h)"
            )
        elif row["delta"] < 0:
            line = (
                f"{row['title']}: {row['actual']:g}h vs {row['target']:g}h target"
                f" ({row['delta']:g}h short)"
            )
            if row["category"].lower() in two_zero:
                line += f" — no {row['category']} time for two weeks running"
            drift.append(line)
        else:
            wins.append(f"{row['title']}: exactly on target ({row['actual']:g}h)")
    for cat in sorted(two_zero):
        has_goal = any(
            r["category"].lower() == cat and r["delta"] is not None and r["delta"] < 0
            for r in data["goals"]
        )
        if not has_goal:
            drift.append(f"No {cat} time at all for two weeks running.")
    if not wins and data["vault_created"]:
        wins.append(f"{data['vault_created']} vault note(s) written this week.")
    return wins[:4], drift[:4]


def floor_proposals(
    data: dict,
    next_window: tuple[dt.datetime, dt.datetime],
    personal: list[str],
    proposal_max: int,
) -> list[dict]:
    """Deterministic: every under-targeted goal gets its recurring slot in the
    coming week, most-personal first, up to ``proposal_max``."""
    order = {c: i for i, c in enumerate(personal)}
    under = [r for r in data["goals"] if r["target"] is not None and (r["delta"] or 0) < 0]
    under.sort(key=lambda r: (order.get(r["category"], 99), r["delta"] or 0))
    out = []
    for r in under[:proposal_max]:
        wd, hour, dur = PROPOSAL_SLOTS.get(r["category"], _DEFAULT_SLOT)
        slot = next_slot(next_window[0], wd, hour)
        out.append(
            {
                "title": f"Checkup: {r['title']}",
                "when": slot.isoformat(sep=" "),
                "category": r["category"],
                "notes": (
                    f"recurring {r['category']} block — was "
                    f"{r['actual']:g}h vs {r['target']:g}h target last week"
                ),
                "duration_h": dur,
            }
        )
    return out


def next_slot(anchor: dt.datetime, weekday: int, hour: int) -> dt.datetime:
    days_ahead = (weekday - anchor.weekday()) % 7
    return (anchor + dt.timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


# ---------------------------------------------------------------- seasoning (LLM)


def context_text(data: dict, trends: dict[str, list[dict]], extras: dict) -> str:
    lines = [
        f"Last week: {data['window'][0].date()} → {data['window'][1].date()} "
        f"({data['events']} calendar events).",
        "Scorecard (hours per goal category):",
    ]
    for r in data["goals"]:
        t = f"{r['target']:g}h" if r["target"] is not None else "n/a"
        lines.append(f"  - {r['title']} [{r['category']}]: {r['actual']:g}h vs {t} target")
    for cat, tr in trends.items():
        lines.append(f"Trend [{cat}]: " + " → ".join(f"{x['hours']:g}h" for x in tr))
    lines.append(
        f"Vault: {data['vault_created']} note(s) created, "
        f"{data['vault_updated']} updated this week."
    )
    if extras.get("occasions"):
        lines.append("Upcoming occasions: " + "; ".join(extras["occasions"]))
    if extras.get("gifts"):
        lines.append("Gifts in flight: " + "; ".join(extras["gifts"]))
    if extras.get("calendar_next"):
        lines.append("Already scheduled next week: " + "; ".join(extras["calendar_next"]))
    return "\n".join(lines)


def season(
    agent: Agent | None,
    ctx: PluginContext,
    data: dict,
    trends: dict[str, list[dict]],
    extras: dict,
) -> dict | None:
    """Run the narrow checkup scenario; return the validated structured
    answer or None (degraded / schema-failed after one retry)."""
    if agent is None:
        return None
    scenario = Scenario(
        name="checkup",
        system_prompt=CHECKUP_SYSTEM_PROMPT,
        tools=[
            "scorecard.week",
            "scorecard.trend",
            "scorecard.goals",
            "calendar.calendar_list",
            "gifts.upcoming",
            "gifts.gifts_list",
        ],
        context_fn=lambda _c: context_text(data, trends, extras),
        output_schema=CHECKUP_SCHEMA,
    )
    result = agent.run_turn(scenario, ctx, "Produce the weekly checkup JSON.")
    if not result.get("ok") or result.get("structured") is None:
        err = result.get("schema_error") or result.get("reason") or "unavailable"
        result = agent.run_turn(
            scenario,
            ctx,
            f"Your previous answer was not accepted ({err}). Reply again with "
            "valid JSON only, matching the schema exactly.",
        )
    if not result.get("ok") or result.get("structured") is None:
        log.warning(
            "checkup seasoning degraded (%s); using the deterministic floor",
            result.get("schema_error") or result.get("reason") or "unavailable",
        )
        return None
    return result["structured"]


# ------------------------------------------------------------------- assembly


def build_checkup(
    ctx: PluginContext,
    window: tuple[dt.datetime, dt.datetime] | None,
    goals_mod,
    gifts_ctx_mod,
    scorecard_mod,
    *,
    use_llm: bool = True,
    now: dt.datetime | None = None,
) -> dict:
    """Measure → trend → floor → season → proposals → deliver.
    ``window`` None = most recently completed week. Returns a result dict
    (``proposals`` = created approval ids). Sibling modules are passed in
    (plugin-wired) so this file imports only stdlib + plp.kernel."""
    now = now or ctx.config.default_now_factory()()
    now_naive = now.replace(tzinfo=None)
    if window is None:
        window = scorecard_mod.previous_window(now_naive, ctx.config.scorecard.week_start)

    cfg = ctx.config
    vault = Vault(ctx.vault_dir(), ctx.store)
    calendar = open_calendar_store(cfg, log)
    sstore = scorecard_mod.ScorecardStore(ctx.store)

    goal_text = vault.read(cfg.scorecard.goals_file)
    gs = goals_mod.parse_goals(goal_text[1]) if goal_text else []
    data = scorecard_mod.aggregate_week(vault, calendar, gs, window)
    trends = {
        r["category"]: sstore.trend(r["category"], weeks=min(8, cfg.scorecard.history_weeks))
        for r in data["goals"]
        if r["target"] is not None
    }
    sstore.save(
        data["window"],
        data["hours_by_category"],
        data["goals"],
        data["vault_created"],
        data["vault_updated"],
        notes=None,
        history_weeks=cfg.scorecard.history_weeks,
    )

    wins, drift = floor_wins_drift(data, trends)
    next_window = (window[1], window[1] + dt.timedelta(days=7))
    proposals = floor_proposals(
        data,
        next_window,
        cfg.scorecard.personal_categories,
        cfg.scorecard.proposal_max,
    )
    seasoned = False
    summary = None
    if use_llm:
        extras = _extras(ctx, calendar, window, gifts_ctx_mod, now_naive)
        structured = season(ctx.agent, ctx, data, trends, extras)
        if structured:
            wins = structured["wins"]
            drift = structured.get("drift", [])
            summary = structured.get("summary")
            llm_props = [
                p for p in structured["proposals"] if _valid_when(p.get("when"), next_window)
            ]
            if llm_props:
                proposals = llm_props
            seasoned = True

    ids: list[int] = []
    placed: list[dict] = []  # proposals as finally placed (post-nudge)
    if ctx.approvals is not None:
        taken: list[tuple[dt.datetime, dt.datetime]] = []  # this run's siblings

        def _busy(s: dt.datetime, e: dt.datetime) -> bool:
            if calendar.conflicts(s, e):
                return True
            return any(s < te and e > ts for ts, te in taken)

        def _nudge_free(s: dt.datetime, e: dt.datetime):
            """Shift the slot's START forward (hourly, up to 3 tries), keeping
            its duration; returns the new start or None when none is free."""
            dur = e - s
            for _ in range(3):
                s += dt.timedelta(hours=1)
                if not _busy(s, s + dur):
                    return s
            return None

        for p in proposals:
            when = parse_when(p["when"])
            if when is None:
                continue
            dur = float(p.get("duration_h") or 2)
            end = when + dt.timedelta(hours=dur)
            if _busy(when, end):
                new_start = _nudge_free(when, end)
                if new_start is None:
                    log.info(
                        "skipped proposal %r: no free slot near %s",
                        p["title"], p["when"],
                    )
                    continue
                when, end = new_start, new_start + dt.timedelta(hours=dur)
            taken.append((when, end))
            ids.append(
                ctx.approvals.propose(
                    "calendar_block",
                    {
                        "title": p["title"],
                        "start": when.isoformat(sep=" "),
                        "end": end.isoformat(sep=" "),
                        "category": p.get("category") or "personal",
                        "notes": p.get("notes", ""),
                    },
                    note="weekly checkup",
                )
            )
            q = dict(p)
            q["when"] = when.isoformat(sep=" ")  # render shows the placed slot
            placed.append(q)
        proposals = placed

    text = render(
        data, wins, drift, proposals, ids, seasoned, summary, trends
    )
    delivered = False
    if ctx.delivery is not None:
        ctx.delivery.deliver("checkup", text)
        delivered = True
    ctx.store.save_digest("checkup", text)
    ctx.bus.publish(
        "checkup.weekly",
        {
            "week": data["window"][0].date().isoformat(),
            "seasoned": seasoned,
            "proposals": ids,
        },
    )
    return {
        "delivered": delivered,
        "seasoned": seasoned,
        "summary": summary,
        "wins": wins,
        "drift": drift,
        "proposals": ids,
        "scorecard": data["goals"],
        "text": text,
    }


def _extras(
    ctx: PluginContext,
    calendar: CalendarStore,
    window: tuple[dt.datetime, dt.datetime],
    gifts_ctx_mod,
    now_naive: dt.datetime,
) -> dict:
    """Upcoming occasions / in-flight gifts / next week's schedule — from the
    store, never from the model's imagination (best-effort)."""
    out: dict = {}
    try:
        occ = gifts_ctx_mod.upcoming_occasions(ctx.config, now_naive)
        if occ:
            out["occasions"] = occ
        gf = gifts_ctx_mod.in_flight_gifts(Vault(ctx.vault_dir(), ctx.store), now_naive)
        if gf:
            out["gifts"] = gf
    except Exception as exc:  # noqa: BLE001 - context enrichment only
        log.debug("gifts context unavailable: %s", exc)
    try:
        nxt = (window[1], window[1] + dt.timedelta(days=7))
        events = calendar.list(nxt[0], nxt[1])
        if events:
            out["calendar_next"] = [
                f"{e.start.date()} {e.title} [{e.category}]" for e in events[:8]
            ]
    except Exception as exc:  # noqa: BLE001
        log.debug("next-week calendar unavailable: %s", exc)
    return out


# ----------------------------------------------------------------- helpers


def parse_when(spec: str | None) -> dt.datetime | None:
    if not spec:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return dt.datetime.strptime(spec, fmt)
        except ValueError:
            continue
    return None


def _valid_when(spec: str | None, window: tuple[dt.datetime, dt.datetime]) -> bool:
    w = parse_when(spec)
    return w is not None and window[0] <= w < window[1]


# ------------------------------------------------------------------- rendering


def scorecard_block(data: dict, trends: dict[str, list[dict]]) -> list[str]:
    lines = [
        "SCORECARD",
        f"{'goal':<22}{'category':<14}{'target':>8}{'actual':>8}{'delta':>9}",
    ]
    for r in data["goals"]:
        tgt = f"{r['target']:g}h" if r["target"] is not None else "—"
        delta = "—" if r["delta"] is None else f"{r['delta']:+g}h"
        mark = ""
        if r["delta"] is not None:
            mark = " ▲" if r["delta"] > 0 else (" ▼" if r["delta"] < 0 else " =")
        lines.append(
            f"{r['title'][:22]:<22}{r['category'][:14]:<14}{tgt:>8}"
            f"{r['actual']:>7g}h{delta:>9}{mark}"
        )
    lines.append(
        f"Vault: {data['vault_created']} created / {data['vault_updated']} updated · "
        f"{data['events']} calendar events"
    )
    for cat, tr in trends.items():
        if len(tr) > 1:
            lines.append(f"Trend [{cat}]: " + " → ".join(f"{x['hours']:g}h" for x in tr))
    return lines


def render(
    data: dict,
    wins: list[str],
    drift: list[str],
    proposals: list[dict],
    ids: list[int],
    seasoned: bool,
    summary: str | None,
    trends: dict[str, list[dict]] | None = None,
) -> str:
    lines = [
        f"Weekly checkup — week of {data['window'][0].date().isoformat()} "
        f"{'(LLM-seasoned)' if seasoned else '(deterministic — LLM unavailable)'}"
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append("")
    lines.extend(scorecard_block(data, trends or {}))
    lines.append("")
    lines.append("WINS")
    lines.extend(f"  + {w}" for w in (wins or ["(none recorded)"]))
    lines.append("")
    lines.append("DRIFT")
    lines.extend(f"  · {d}" for d in (drift or ["(none)"]))
    lines.append("")
    lines.append("PROPOSALS — approve with: plp approve <id>")
    if not ids:
        lines.append("  (none — nothing under target, or no free slots)")
    for i, (p, aid) in enumerate(zip(proposals, ids), 1):
        w = parse_when(p["when"])
        when_s = w.strftime("%a %Y-%m-%d %H:%M") if w else p["when"]
        lines.append(
            f"  {aid:>4}. [{i}] {p['title']}  ({when_s}, {p.get('category', 'personal')})"
        )
    return "\n".join(lines)
