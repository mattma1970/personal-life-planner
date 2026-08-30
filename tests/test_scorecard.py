"""Scorecard + weekly checkup tests (Phase 5).

Covers: goals parse/dump/upsert (human-edit-wins); week-window math under the
0=Monday convention; weekly aggregation (event clipping, vault created vs
updated, goal rows); the scorecard store (upsert, trend gap-fill, prune);
deterministic floors (wins/drift, recurring-slot proposals, clash nudges,
skip-when-blocked); LLM seasoning (strict JSON, one retry, floor fallback,
narrow tool mount); the exit criterion end-to-end (checkup → pending
approvals → approve → ICS event, audited); gifts context; the CLI surface.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from pathlib import Path



from plp.kernel.agent import Agent
from plp.kernel.approvals import Approvals
from plp.kernel.bus import EventBus
from plp.kernel.calendar import CalendarEvent, IcsCalendarStore
from plp.kernel.capability import Capability
from plp.kernel.config import LLMConfig, OccasionCfg, PlpConfig, resolve
from plp.kernel.context import PluginContext
from plp.kernel.host import HostService
from plp.kernel.llm import LLMClient
from plp.kernel.plugin import load_sibling
from plp.kernel.registry import ToolRegistry
from plp.kernel.store import Store
from plp.kernel.vault import Vault

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
PLUGINS = PLUGINS_DIR / "scorecard"
goals_mod = load_sibling("plp.plugins.scorecard.goals", PLUGINS / "goals.py")
scorecard_mod = load_sibling("plp.plugins.scorecard.scorecard", PLUGINS / "scorecard.py")
checkup_mod = load_sibling("plp.plugins.scorecard.checkup", PLUGINS / "checkup.py")
gifts_ctx_mod = load_sibling("plp.plugins.scorecard.gifts_context", PLUGINS / "gifts_context.py")
plugin_mod = load_sibling("plp.plugins.scorecard.plugin", PLUGINS / "plugin.py")
calendar_plugin_mod = load_sibling(
    "plp.plugins.calendar.plugin", PLUGINS_DIR / "calendar" / "plugin.py"
)
gifts_plugin_mod = load_sibling(
    "plp.plugins.gifts.plugin", PLUGINS_DIR / "gifts" / "plugin.py"
)

log = logging.getLogger("plp.test.scorecard")

# Reference week: Mon 2026-08-24 .. Mon 2026-08-31 (naive local).
W = (dt.datetime(2026, 8, 24), dt.datetime(2026, 8, 31))
N = (dt.datetime(2026, 8, 31), dt.datetime(2026, 9, 7))
NOW_SUN = dt.datetime(2026, 8, 30, 20, 0)


# ------------------------------------------------------------------- helpers


def _cfg(tmp_path) -> PlpConfig:
    cfg = PlpConfig()
    cfg.root = str(tmp_path)
    cfg.calendar.ics_file = "cal/test.ics"
    cfg.gifts.occasions = [OccasionCfg(name="her birthday", month=9, day=15)]
    return cfg


def _full_ctx(tmp_path, llm=None):
    """Runtime-shaped context (as build_runtime builds it): store + bus +
    approvals + host + tools registry + optional agent."""
    cfg = _cfg(tmp_path)
    store = Store(tmp_path / "data" / "plp.db")
    store.migrate_core()
    bus = EventBus()
    approvals = Approvals(store, bus, log)
    host = HostService(store, bus, log)
    tools = ToolRegistry()
    agent = Agent(llm, tools, bus) if llm is not None else None
    ctx = PluginContext(
        store=store,
        bus=bus,
        config=cfg,
        delivery=None,
        capability=Capability.permissive(),
        approvals=approvals,
        host=host,
        tools=tools,
        agent=agent,
        job_name=None,
    )
    return cfg, store, bus, approvals, host, ctx


def _ics(ctx_cfg) -> IcsCalendarStore:
    return IcsCalendarStore(resolve(ctx_cfg, ctx_cfg.calendar.ics_file))


def _seed_goals(vault_dir: Path, text: str | None = None) -> None:
    (vault_dir / "goals.md").write_text(text if text is not None else goals_mod.seed_goals())


def _goal_rows(overrides: dict | None = None) -> list[dict]:
    """Default four goals, all zero hours last week (delta = -target)."""
    base = [
        {"title": "Time with wife", "category": "wife", "target": 5.0, "actual": 0.0,
         "delta": -5.0, "notes": ""},
        {"title": "Deep work", "category": "deep-work", "target": 15.0, "actual": 0.0,
         "delta": -15.0, "notes": ""},
        {"title": "Gift work", "category": "gifts", "target": 1.0, "actual": 0.0,
         "delta": -1.0, "notes": ""},
        {"title": "Travel planning", "category": "travel", "target": 2.0, "actual": 0.0,
         "delta": -2.0, "notes": ""},
    ]
    if overrides:
        for row in base:
            row.update(overrides.get(row["category"], {}))
    return base


def _data(overrides: dict | None = None, **kw) -> dict:
    d = {
        "window": W,
        "hours_by_category": {},
        "events": 0,
        "goals": _goal_rows(overrides),
        "vault_created": 0,
        "vault_updated": 0,
    }
    d.update(kw)
    return d


VALID_CHECKUP = json.dumps(
    {
        "summary": "A grounded one-line summary.",
        "wins": ["won real time with wife", "gift is shortlisted and priced"],
        "drift": ["travel planning missed the target"],
        "proposals": [
            {"title": "LLM: Date night", "when": "2026-09-02 18:00",
             "category": "wife", "notes": "make up the missed week"},
            {"title": "LLM: Bad when", "when": "not-a-date",
             "category": "travel", "notes": "must be filtered out"},
            {"title": "LLM: Trip plan", "when": "2026-09-06 10:00",
             "category": "travel", "notes": "kick off the trip"},
        ],
    }
)


class FakeLLM:
    """Duck-typed LLMClient: scripted chat responses, call log, no network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.max_tool_steps = 8

    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeLLM: more chats than scripted responses")
        return self.responses.pop(0)


def _fake_agent(tmp_path, responses, ctx) -> FakeLLM:
    """Register the narrow tool set exactly as build_runtime would (plugin
    name + function name), then hand a FakeLLM-backed Agent to the context."""
    for cls, prefix in (
        (calendar_plugin_mod.CalendarPlugin, "calendar"),
        (gifts_plugin_mod.GiftsPlugin, "gifts"),
        (plugin_mod.ScorecardPlugin, "scorecard"),
    ):
        p = cls()
        p.setup(ctx)
        for t in p.tools():
            ctx.tools.register(f"{prefix}.{t.__name__}", t)
    fake = FakeLLM(responses)
    ctx.agent = Agent(fake, ctx.tools, ctx.bus)
    return fake


def _mounted_names(fake: FakeLLM) -> set:
    names = set()
    for call in fake.calls:
        for t in call["tools"] or []:
            names.add(t["function"]["name"])
    return names


# ------------------------------------------------------------------- goals


def test_goals_parse_dump_roundtrip():
    gs = [
        goals_mod.Goal("A", "cat-a", 3.0, "note a"),
        goals_mod.Goal("B", "cat-b", None, ""),
    ]
    text = goals_mod.dump_goals(gs)
    back = goals_mod.parse_goals(text)
    assert [g.title for g in back] == ["A", "B"]
    assert [g.category for g in back] == ["cat-a", "cat-b"]
    assert back[0].target_hours_week == 3.0
    assert back[1].target_hours_week is None
    assert back[0].notes == "note a"


def test_goals_parse_skips_and_unmeasured():
    text = (
        "## No plp line here\njust prose about something.\n\n"
        "## Weird\nplp: category=x target_hours_week=notanumber\nprose\n\n"
        "## ### looks like subheading\nplp: category=y target_hours_week=2\n"
    )
    gs = goals_mod.parse_goals(text)
    assert [g.title for g in gs] == ["Weird", "### looks like subheading"]
    assert gs[0].target_hours_week is None  # malformed → unmeasured, not a crash


def test_upsert_preserves_human_text(tmp_path):
    text = (
        "# Life goals\n\n"
        "## Time with wife\nplp: category=wife target_hours_week=5\n"
        "My own prose line about dinners.\n\n"
        "## Hobby\nplp: category=hobby target_hours_week=1\n"
        "Keep me.\n"
    )
    goals = [goals_mod.Goal("Time with wife", "wife", 7.5, "new note")]
    out = goals_mod.upsert_plp_lines(text, goals)
    assert "plp: category=wife target_hours_week=7.5" in out
    assert "My own prose line about dinners." in out  # human prose intact
    assert "## Hobby\nplp: category=hobby target_hours_week=1" in out  # untouched
    assert "Keep me." in out


# ----------------------------------------------------------------- windows


def test_window_for_monday_convention():
    thu = dt.datetime(2026, 9, 3, 15, 30)  # a Thursday
    start, end = scorecard_mod.window_for(thu, 0)
    # the Monday-start week containing Thu 09-03 begins Mon 08-31
    assert start == dt.datetime(2026, 8, 31)
    assert end == dt.datetime(2026, 9, 7)
    monday = dt.datetime(2026, 9, 7, 23, 59)
    assert scorecard_mod.window_for(monday, 0)[0] == dt.datetime(2026, 9, 7)


def test_previous_window():
    sun_evening = dt.datetime(2026, 9, 6, 20, 0)
    assert scorecard_mod.previous_window(sun_evening, 0) == (
        dt.datetime(2026, 8, 31),
        dt.datetime(2026, 9, 7),
    )
    # mid-week run reports on the last completed week, not a half-finished one
    wed = dt.datetime(2026, 9, 2, 9, 0)
    assert scorecard_mod.previous_window(wed, 0) == (
        dt.datetime(2026, 8, 24),
        dt.datetime(2026, 8, 31),
    )


# --------------------------------------------------------------- aggregation


def test_aggregate_clips_and_categories(tmp_path):
    cfg = _cfg(tmp_path)
    ics = _ics(cfg)
    s, e = W
    # 4h straddling the window start: 2h inside
    ics.create(CalendarEvent(title="cross-start", start=s - dt.timedelta(hours=2),
                          end=s + dt.timedelta(hours=2), category="Wife"))
    # 4h straddling the window end: 2h inside
    ics.create(CalendarEvent(title="cross-end", start=e - dt.timedelta(hours=2),
                          end=e + dt.timedelta(hours=2), category="deep-work"))
    # fully inside, 3h
    ics.create(CalendarEvent(title="inside", start=s + dt.timedelta(days=1, hours=9),
                          end=s + dt.timedelta(days=1, hours=12), category="travel"))
    # outside entirely: 0h
    ics.create(CalendarEvent(title="outside", start=e + dt.timedelta(days=1),
                          end=e + dt.timedelta(days=1, hours=5), category="travel"))
    vault = Vault(tmp_path / "plp-vault", Store(tmp_path / "data" / "plp.db"))
    data = scorecard_mod.aggregate_week(
        vault, ics, goals_mod.parse_goals(goals_mod.seed_goals()), W
    )
    assert data["hours_by_category"]["wife"] == 2.0  # clipped + lowercased
    assert data["hours_by_category"]["deep-work"] == 2.0
    assert data["hours_by_category"]["travel"] == 3.0
    assert data["events"] == 3  # the outside one doesn't count


def test_aggregate_vault_created_vs_updated(tmp_path, monkeypatch):
    vault_dir = tmp_path / "plp-vault"
    vault_dir.mkdir()
    store = Store(tmp_path / "data" / "plp.db")
    store.migrate_core()
    vault = Vault(vault_dir, store)

    s, e = W
    # (a) created: mtime and ctime both inside the window
    p1 = vault_dir / "created.md"
    p1.write_text("new note")
    t_mid = (s + dt.timedelta(days=3)).timestamp()
    os.utime(p1, (t_mid, t_mid))

    # (b) updated: mtime inside the window, ctime in the previous window
    # (a note written earlier that the owner touched this week).
    p2 = vault_dir / "updated.md"
    p2.write_text("older note")
    t_prev = (s - dt.timedelta(days=2)).timestamp()  # ctime: prev week
    os.utime(p2, (t_prev, t_prev))
    os.utime(p2, (t_mid, t_mid))  # bump mtime only → ctime stays at t_prev

    # (c) outside the window on both clocks
    p3 = vault_dir / "stale.md"
    p3.write_text("ancient")
    os.utime(p3, (t_prev, t_prev))

    # ctime cannot be set backwards on Linux — force it for the (b) file.
    real_stat = os.stat
    target = str(p2)

    def fake_stat(path, *a, **k):
        r = real_stat(path, *a, **k)
        if os.fspath(path) == target:
            vals = list(r)
            vals[8] = t_mid  # mtime
            vals[9] = t_prev  # ctime
            return os.stat_result(vals)
        return r

    monkeypatch.setattr(os, "stat", fake_stat)

    data = scorecard_mod.aggregate_week(
        vault, IcsCalendarStore(tmp_path / "cal" / "none.ics"),
        goals_mod.parse_goals(goals_mod.seed_goals()), W,
    )
    assert data["vault_created"] == 1  # (a)
    assert data["vault_updated"] == 1  # (b)


def test_aggregate_goal_rows(tmp_path):
    cfg = _cfg(tmp_path)
    ics = _ics(cfg)
    s, _ = W
    ics.create(CalendarEvent(title="dinner", start=s + dt.timedelta(days=2, hours=19),
                          end=s + dt.timedelta(days=2, hours=21), category="wife"))
    ics.create(CalendarEvent(title="mystery", start=s + dt.timedelta(days=2, hours=9),
                          end=s + dt.timedelta(days=2, hours=10), category="???"))
    gs = goals_mod.parse_goals(goals_mod.seed_goals())
    gs[1].target_hours_week = None  # deep work → unmeasured
    data = scorecard_mod.aggregate_week(
        Vault(tmp_path / "v", Store(tmp_path / "data" / "plp.db")), ics, gs, W
    )
    by_cat = {r["category"]: r for r in data["goals"]}
    assert by_cat["wife"]["actual"] == 2.0
    assert by_cat["wife"]["delta"] == -3.0
    assert by_cat["deep-work"]["delta"] is None  # unmeasured: no delta
    assert by_cat["travel"]["actual"] == 0.0
    assert data["hours_by_category"].get("uncategorized") == 1.0


# ------------------------------------------------------------------ store


def test_store_upsert_trend_prune(tmp_path):
    store = Store(tmp_path / "plp.db")
    store.migrate_core()
    sstore = scorecard_mod.ScorecardStore(store)
    w1, w2, w3 = (W[0] + dt.timedelta(days=7 * k) for k in range(3))
    sstore.save(W, {"wife": 1.0}, _goal_rows(), 0, 0, None, 26)
    sstore.save(w2, {"wife": 2.0}, _goal_rows(), 1, 0, None, 26)
    sstore.save(w3, {"wife": 3.0}, _goal_rows(), 0, 0, None, 26)
    tr = sstore.trend("wife", 3)  # oldest first
    assert [r["hours"] for r in tr] == [1.0, 2.0, 3.0]
    # gap: save W+21d without W+14d → trend fills the gap with 0
    w4 = W[0] + dt.timedelta(days=21)
    sstore.save(w4, {"gifts": 5.0}, _goal_rows(), 0, 0, None, 26)
    tr = sstore.trend("gifts", 3)
    assert [r["hours"] for r in tr] == [0.0, 0.0, 5.0]
    # upsert: re-saving the same week overwrites
    sstore.save(W, {"wife": 9.0}, _goal_rows(), 0, 0, None, 26)
    assert sstore.trend("wife", 4)[0]["hours"] == 9.0
    # prune: 26-week history. A real checkup always passes goal rows, which
    # anchor each week (a 0-hour goal still writes a row), so prune has
    # something to drop.
    for k in range(30):
        sstore.save(W[0] + dt.timedelta(days=7 * k), {}, _goal_rows(), 0, 0, None, 26)
    first = store.query_one("SELECT MIN(week_start) AS m FROM scorecard_week")["m"]
    assert first == (W[0] + dt.timedelta(days=7 * 4)).date().isoformat()


def test_store_vault_recent(tmp_path):
    store = Store(tmp_path / "plp.db")
    store.migrate_core()
    sstore = scorecard_mod.ScorecardStore(store)
    sstore.save(W, {}, [], 2, 3, None, 26)
    sstore.save(W[0] + dt.timedelta(days=7), {}, [], 4, 0, None, 26)
    rows = sstore.recent_vault(2)
    assert [(r["created"], r["updated"]) for r in rows] == [(2, 3), (4, 0)]


# ------------------------------------------------------------------- floor


def test_floor_wins_drift():
    data = _data({"wife": {"actual": 7.0, "delta": 2.0},
                  "deep-work": {"actual": 15.0, "delta": 0.0}})
    wins, drift = checkup_mod.floor_wins_drift(data, {})
    assert any("Time with wife" in w and "+2h" in w for w in wins)
    assert any("Deep work" in w and "exactly on target" in w for w in wins)
    assert any("Travel planning" in d and "-2h short" in d for d in drift)
    assert len(drift) <= 3
    # vault notes become a win when there is nothing else
    data2 = _data()
    wins2, _ = checkup_mod.floor_wins_drift(data2, {})
    assert any("vault note" in w for w in wins2) or data2["vault_created"] == 0


def test_floor_two_zero_weeks_drift():
    data = _data({"travel": {"actual": 0.0, "delta": -2.0}})
    trends = {"travel": [
        {"week_start": "2026-08-17", "hours": 1.0},
        {"week_start": "2026-08-24", "hours": 0.0},
        {"week_start": "2026-08-31", "hours": 0.0},
    ]}
    _, drift = checkup_mod.floor_wins_drift(data, trends)
    assert any("two weeks running" in d and "travel" in d for d in drift)


def test_floor_proposals_assign_slots():
    """The floor is a pure slot allocator: every under-targeted goal gets its
    recurring slot, most-personal first, capped at proposal_max. (Clash
    resolution happens in build_checkup, which sees the calendar.)"""
    personal = ["wife", "family", "gifts", "travel"]
    data = _data()
    data["goals"].append(
        {"title": "Family time", "category": "family", "target": 3.0,
         "actual": 1.0, "delta": -2.0, "notes": ""}
    )
    props = checkup_mod.floor_proposals(data, N, personal, 3)
    assert [p["category"] for p in props] == ["wife", "family", "gifts"]
    by_cat = {p["category"]: p for p in props}
    assert by_cat["wife"]["when"] == "2026-09-02 19:00:00"   # Wed 19:00
    assert by_cat["family"]["when"] == "2026-09-02 19:00:00"  # same slot, raw
    assert by_cat["gifts"]["when"] == "2026-09-06 19:00:00"   # Sun 19:00
    for p in props:
        assert p["duration_h"] > 0
        assert p["notes"]


# (nudge-by-the-hour is exercised end-to-end in
# test_checkup_nudges_siblings_and_existing; the closure in build_checkup
# is the only nudge implementation.)

# ------------------------------------------------------------ LLM seasoning


def test_build_checkup_llm_seasoned(tmp_path):
    cfg, store, bus, approvals, _host, ctx = _full_ctx(tmp_path)
    (tmp_path / "plp-vault").mkdir()
    _seed_goals(tmp_path / "plp-vault")
    fake = _fake_agent(tmp_path, [VALID_CHECKUP], ctx)

    events = []
    bus.subscribe("checkup", lambda name, payload: events.append((name, payload)))

    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,
    )
    assert res["seasoned"] is True
    assert res["summary"] == "A grounded one-line summary."
    assert res["wins"] == ["won real time with wife", "gift is shortlisted and priced"]
    assert res["drift"] == ["travel planning missed the target"]
    # 'not-a-date' was filtered; the two valid proposals became approvals
    assert len(res["proposals"]) == 2
    pend = approvals.pending()
    titles = sorted(a["payload"]["title"] for a in pend)
    assert titles == ["LLM: Date night", "LLM: Trip plan"]
    # narrow mount: exactly the six scenario tools, nothing else
    assert _mounted_names(fake) == {
        "scorecard.week", "scorecard.trend", "scorecard.goals",
        "calendar.calendar_list", "gifts.upcoming", "gifts.gifts_list",
    }
    # bus: the weekly event carries the ids; digest saved; text returned
    assert events and events[0][0] == "checkup.weekly"
    assert events[0][1]["proposals"] == res["proposals"]
    assert events[0][1]["seasoned"] is True
    assert store.query_json("SELECT content FROM digests WHERE kind='checkup'")
    assert "LLM-seasoned" in res["text"]
    assert "SCORECARD" in res["text"]


def test_llm_retry_after_schema_error(tmp_path):
    cfg, store, bus, approvals, _host, ctx = _full_ctx(tmp_path)
    (tmp_path / "plp-vault").mkdir()
    _seed_goals(tmp_path / "plp-vault")
    fake = _fake_agent(tmp_path, ["sorry, I cannot produce JSON", VALID_CHECKUP], ctx)
    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,
    )
    assert len(fake.calls) == 2
    assert "not accepted" in fake.calls[1]["messages"][-1]["content"]
    assert res["seasoned"] is True
    assert len(res["proposals"]) == 2


def test_llm_unusable_twice_falls_to_floor(tmp_path):
    cfg, store, bus, approvals, _host, ctx = _full_ctx(tmp_path)
    (tmp_path / "plp-vault").mkdir()
    _seed_goals(tmp_path / "plp-vault")
    fake = _fake_agent(tmp_path, ["prose one", "prose two"], ctx)
    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,
    )
    assert res["seasoned"] is False
    assert len(fake.calls) == 2  # one retry, then give up
    # floor still produced 2-3 approvable proposals (the PRD's degraded mode)
    assert 2 <= len(res["proposals"]) <= 3
    assert all(a["kind"] == "calendar_block" for a in approvals.pending())


def test_build_checkup_no_llm_at_all(tmp_path):
    """Agent present but LLM down → degraded, floor delivered, no crash."""
    cfg, store, bus, approvals, _host, ctx = _full_ctx(
        tmp_path, llm=LLMClient(LLMConfig(base_url="http://127.0.0.1:9/v1", model="qwen-test"))
    )
    (tmp_path / "plp-vault").mkdir()
    _seed_goals(tmp_path / "plp-vault")

    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,
    )
    assert res["seasoned"] is False
    assert 2 <= len(res["proposals"]) <= 3  # floor
    text = store.query_json("SELECT content FROM digests WHERE kind='checkup'")[0]["content"]
    assert "deterministic" in text


def test_checkup_nudges_siblings_and_existing(tmp_path):
    """Two floor proposals sharing the Wed 19:00 slot, plus an existing
    calendar event on it: the run must end with distinct, non-clashing slots."""
    cfg, store, bus, approvals, _host, ctx = _full_ctx(tmp_path)
    (tmp_path / "plp-vault").mkdir()
    _seed_goals(tmp_path / "plp-vault")
    ics = _ics(cfg)
    ics.create(CalendarEvent(
        title="existing",
        start=dt.datetime(2026, 9, 2, 19, 0),
        end=dt.datetime(2026, 9, 2, 20, 0),
        category="personal",
    ))
    # only wife + family under target (same slot)
    goals_text = goals_mod.dump_goals([
        goals_mod.Goal("Time with wife", "wife", 5.0, ""),
        goals_mod.Goal("Family time", "family", 3.0, ""),
    ])
    (tmp_path / "plp-vault" / "goals.md").write_text(goals_text)
    fake = _fake_agent(tmp_path, ["prose one", "prose two"], ctx)  # floor

    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,
    )
    # wife → 20:00 (nudged once past the existing event), family → 22:00
    # (nudged past the existing event and past the wife proposal)
    assert len(res["proposals"]) == 2
    starts = sorted(
        dt.datetime.fromisoformat(a["payload"]["start"])
        for a in approvals.pending()
    )
    assert starts == [dt.datetime(2026, 9, 2, 20, 0), dt.datetime(2026, 9, 2, 22, 0)]
    # neither overlaps the existing 19:00-20:00 event
    for a in approvals.pending():
        s = dt.datetime.fromisoformat(a["payload"]["start"])
        assert s >= dt.datetime(2026, 9, 2, 20, 0)


# ------------------------------------------------------------ exit criterion


def test_exit_criterion_checkup_approve_writes_ics(tmp_path):
    """PRD §8 Phase-5 exit: Sunday checkup → scorecard, wins/drift, 2-3
    approvable proposals → approve → calendar entry (audited)."""
    cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
    vault_dir = tmp_path / "plp-vault"
    vault_dir.mkdir()
    _seed_goals(vault_dir)

    sc = plugin_mod.ScorecardPlugin()
    sc.setup(ctx)
    cal = calendar_plugin_mod.CalendarPlugin()
    cal.setup(ctx)  # subscribes the approvals→ICS bus wiring

    # some measured time last week: wife 2h, deep-work 2h
    ics = _ics(cfg)
    s, _ = W
    ics.create(CalendarEvent(title="dinner", start=s + dt.timedelta(days=2, hours=19),
                          end=s + dt.timedelta(days=2, hours=21), category="wife"))
    ics.create(CalendarEvent(title="focus", start=s + dt.timedelta(days=3, hours=9),
                          end=s + dt.timedelta(days=3, hours=11), category="deep-work"))

    events = []
    bus.subscribe("checkup", lambda name, payload: events.append((name, payload)))

    res = checkup_mod.build_checkup(
        ctx, W, goals_mod, gifts_ctx_mod, scorecard_mod,
        use_llm=True, now=NOW_SUN,  # no agent → floor
    )
    assert res["seasoned"] is False
    assert 2 <= len(res["proposals"]) <= 3  # exit: 2-3 approvable proposals
    assert res["wins"] or res["drift"]  # wins/drift present
    assert events[0][1]["seasoned"] is False
    assert events[0][1]["proposals"] == res["proposals"]

    # approve the first proposal → ICS gains the event, audited
    first = res["proposals"][0]
    payload = approvals.get(first)["payload"]
    assert (tmp_path / "cal" / "test.ics").exists()  # has the seeded events
    assert approvals.resolve(first, True, "yes") is True
    start = dt.datetime.fromisoformat(payload["start"])
    end = dt.datetime.fromisoformat(payload["end"])
    got = ics.list(start.replace(minute=0), end)
    assert [e.title for e in got] == [payload["title"]]
    assert got[0].category == payload["category"]
    assert store.query_one(
        "SELECT * FROM runs WHERE job='host.calendar_write'"
    ) is not None

    # and the digest text carries the scorecard + the approve hint
    text = store.query_json("SELECT content FROM digests WHERE kind='checkup'")[0]["content"]
    assert "SCORECARD" in text
    assert "plp approve" in text
    # the pre-seeded event survived the approve's merge write (human-wins)
    assert any(e.title == "dinner" for e in ics.list(W[0], W[1]))


# -------------------------------------------------------------- gifts context


def test_gifts_context_upcoming_and_in_flight(tmp_path):
    cfg = _cfg(tmp_path)
    now = NOW_SUN  # 2026-08-30: her birthday 09-15 is 16 days away
    assert gifts_ctx_mod.upcoming_occasions(cfg, now) == ["her birthday 2026-09-15 (16d)"]
    # an occasion beyond the review window is dropped
    cfg.gifts.review_window_days = 10
    assert gifts_ctx_mod.upcoming_occasions(cfg, now) == []

    vault_dir = tmp_path / "plp-vault"
    (vault_dir / "gifts").mkdir(parents=True)
    store = Store(tmp_path / "data" / "plp.db")
    store.migrate_core()
    vault = Vault(vault_dir, store)
    vault.write(
        "gifts/2026-08-20-vinyl-player.md",
        "Body about the player.",
        {"occasion": "her birthday", "status": "shortlist",
         "budget": 200, "created": "2026-08-20"},
    )
    vault.write(
        "gifts/2026-08-21-bought-already.md",
        "Done.",
        {"occasion": "her birthday", "status": "bought",
         "budget": 50, "created": "2026-08-21"},
    )
    rows = gifts_ctx_mod.in_flight_gifts(vault, now)
    assert len(rows) == 1
    assert "vinyl-player" in rows[0]
    assert "shortlist" in rows[0]
    assert "$200" in rows[0]
    assert "10d" in rows[0]


# --------------------------------------------------------------------- jobs


def test_checkup_job_registered():
    jobs = plugin_mod.ScorecardPlugin().jobs()
    assert [j.name for j in jobs] == ["checkup.weekly"]
    assert jobs[0].cron == "0 20 * * 0"  # Sunday 20:00 default
    assert jobs[0].timeout_s == 900
    assert jobs[0].plugin == "scorecard"


# ---------------------------------------------------------------------- CLI


def _tmp_cfg_file(tmp_path) -> str:
    (tmp_path / "config").mkdir(exist_ok=True)
    p = tmp_path / "config" / "plp.yaml"
    p.write_text(
        "llm:\n"
        "  base_url: \"http://127.0.0.1:9/v1\"\n"
        "  model: \"none\"\n"
        "vault:\n"
        f"  path: \"{tmp_path / 'plp-vault'}\"\n"
        "state_db:\n"
        f"  path: \"{tmp_path / 'data' / 'plp.db'}\"\n"
        "plugins:\n"
        f"  dir: {PLUGINS_DIR}\n"
    )
    return str(p)


def test_cli_scorecard_week(tmp_path, capsys):
    from plp.cli import main

    cfgf = _tmp_cfg_file(tmp_path)
    rc = main(["--config", cfgf, "scorecard", "week", "--date", "2026-08-24"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SCORECARD" in out
    assert "Time with wife" in out


def test_cli_scorecard_checkup_no_llm(tmp_path, capsys):
    from plp.cli import main

    cfgf = _tmp_cfg_file(tmp_path)
    rc = main(["--config", cfgf, "scorecard", "checkup", "--no-llm",
               "--date", "2026-08-24"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deterministic" in out
    assert "PROPOSALS" in out
    assert "plp approve" in out


def test_cli_goals_init_and_show(tmp_path, capsys):
    from plp.cli import main

    cfgf = _tmp_cfg_file(tmp_path)
    assert main(["--config", cfgf, "scorecard", "goals", "--init"]) == 0
    gf = tmp_path / "plp-vault" / "goals.md"
    assert gf.exists()
    assert "Time with wife" in gf.read_text()
    out = capsys.readouterr().out
    assert "plp:" in out


def test_cli_onboarding(tmp_path, capsys, monkeypatch):
    from plp.cli import main

    cfgf = _tmp_cfg_file(tmp_path)
    # answers: keep wife default, set deep work to 0 (unmeasured), 4h gifts, q (skip travel)
    answers = iter(["", "0", "4", "q"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert main(["--config", cfgf, "scorecard", "onboarding"]) == 0
    text = (tmp_path / "plp-vault" / "goals.md").read_text()
    assert "plp: category=wife target_hours_week=5" in text
    # 0 → unmeasured: the plp line exists without a target
    m = re.search(r"## Deep work\nplp: ([^\n]*)", text)
    assert m and "target_hours_week" not in m.group(1)
    assert "plp: category=gifts target_hours_week=4" in text
    assert "## Travel planning" not in text  # skipped via 'q'
