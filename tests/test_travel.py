"""Travel plugin tests (Phase 3): plan-doc lifecycle, preferences seeding,
deterministic feasibility, LLM seasoning with clean degradation. No network."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from plp.kernel.bus import EventBus
from plp.kernel.capability import Capability
from plp.kernel.config import PlpConfig
from plp.kernel.context import PluginContext
from plp.kernel.plugin import load_sibling
from plp.kernel.store import Store
from plp.kernel.vault import Vault, VaultConflict

PLUGINS = Path(__file__).resolve().parent.parent / "plugins" / "travel"

travel_mod = load_sibling("plp.plugins.travel.travel", PLUGINS / "travel.py")
plugin_mod = load_sibling("plp.plugins.travel.plugin", PLUGINS / "plugin.py")


# ------------------------------------------------------------------- helpers


def _vault(tmp_path) -> Vault:
    store = Store(tmp_path / "plp.db")
    store.migrate_core()
    return Vault(tmp_path / "vault", store)


def _cfg(tmp_path) -> PlpConfig:
    cfg = PlpConfig()
    cfg.root = tmp_path
    cfg.vault.path = str(tmp_path / "vault")
    cfg.travel.max_budget = 5000
    cfg.travel.max_trip_days = 10
    return cfg


def _ctx(tmp_path, cfg: PlpConfig | None = None) -> PluginContext:
    cfg = cfg or _cfg(tmp_path)
    return PluginContext(
        store=Store(cfg.root / "data" / "plp.db"),
        bus=EventBus(),
        config=cfg,
        delivery=None,
        capability=Capability.permissive(8),
    )


def _plugin(tmp_path, cfg: PlpConfig | None = None) -> plugin_mod.TravelPlugin:
    p = plugin_mod.TravelPlugin()
    p.setup(_ctx(tmp_path, cfg))
    return p


class FakeLLM:
    def __init__(self, payload: str | Exception):
        self.payload = payload

    def available(self) -> bool:
        return not isinstance(self.payload, Exception)

    def chat(self, messages, **kw) -> dict:
        if isinstance(self.payload, Exception):
            raise self.payload
        return {"role": "assistant", "content": self.payload}


def _with_llm(monkeypatch, payload: str | Exception) -> None:
    monkeypatch.setattr(
        plugin_mod, "LLMClient", lambda cfg: FakeLLM(payload)
    )


# ------------------------------------------------------------------- dates


def test_parse_dates():
    s, e, raw = travel_mod.parse_dates("2026-03-10..2026-03-17")
    assert (s, e) == (date(2026, 3, 10), date(2026, 3, 17))
    s2, e2, _ = travel_mod.parse_dates("2026-03-10")
    assert s2 == e2 == date(2026, 3, 10)
    s3, e3, _ = travel_mod.parse_dates("2026-03-20..2026-03-10")  # reversed
    assert (s3, e3) == (date(2026, 3, 10), date(2026, 3, 20))
    assert travel_mod.parse_dates("nope") is None
    assert travel_mod.parse_dates(None) is None
    assert travel_mod.parse_dates("2026-13-40..2026-01-01") is None


# -------------------------------------------------------------- preferences


def test_preferences_seeded_once(tmp_path):
    p = _plugin(tmp_path)
    rel = p._vault_store().preferences_rel
    assert (tmp_path / "vault" / rel).is_file()
    text = p._vault_store().preferences_text()
    assert "Hard limits" in text
    # second call does not reseed/clobber
    p._vault_store().vault.write(rel, "# mine\n", {"kind": "preferences"})
    assert p._vault_store().preferences_text() == "# mine"


# ------------------------------------------------------------------- store


def test_create_plan_doc(tmp_path):
    p = _plugin(tmp_path)
    rel, meta = p._vault_store().create(
        "Hawaii", "2026-03-10..2026-03-17", 3000,
        {"Why it fits": "warm water"},
    )
    assert rel == f"travel/{date.today().isoformat()}-hawaii.md"
    assert meta["status"] == "brainstorm"
    text = (tmp_path / "vault" / rel).read_text()
    assert "destination: Hawaii" in text and "## Why it fits" in text


def test_list_and_find(tmp_path):
    p = _plugin(tmp_path)
    p._vault_store().create("Japan", None, None, {"Ideas": "x"})
    p._vault_store().create("Hawaii", None, None, {"Ideas": "y"}, status="planning")
    assert len(p._vault_store().list()) == 2
    assert len(p._vault_store().list(status="planning")) == 1
    assert p._vault_store().find("nope") is None
    # preferences file is never listed as a plan
    p._vault_store().vault.write(p._vault_store().preferences_rel, "prefs", {})
    assert all(r["file"] != p._vault_store().preferences_rel for r in p._vault_store().list())


def test_set_status_and_conflict(tmp_path, monkeypatch):
    p = _plugin(tmp_path)
    rel, _ = p._vault_store().create("Hawaii", None, None, {})
    stem = Path(rel).stem
    _, meta = p._vault_store().set_status(stem, "planning")
    assert meta["status"] == "planning"
    with pytest.raises(ValueError):
        p._vault_store().set_status(stem, "sailing")
    with pytest.raises(KeyError):
        p._vault_store().set_status("ghost", "booked")

    # human edit mid-write wins
    vault = p._vault_store().vault
    real_write = vault.write

    def interleaved(rel_, body, meta=None, expected_mtime=None):
        import time

        time.sleep(0.005)  # > 1ms: land on a different clock tick than the stat
        (vault.root / rel_).write_text("human edit\n")
        return real_write(rel_, body, meta=meta, expected_mtime=expected_mtime)

    monkeypatch.setattr(vault, "write", interleaved)
    with pytest.raises(VaultConflict):
        p._vault_store().set_status(stem, "booked")


# -------------------------------------------------------------- feasibility


def test_feasibility_warnings(tmp_path):
    p = _plugin(tmp_path)
    w = p._feasibility("2026-01-01..2026-03-01", 9000)
    assert any("60 days" in x for x in w)          # > 10-day max
    assert any("past" in x for x in w)             # Jan 2026 < today (Aug 2026)
    assert any("ceiling" in x for x in w)          # 9000 > 5000
    assert any("no calendar conflicts" in x for x in w)  # empty calendar (Phase 4)
    w2 = p._feasibility("not-a-date", 100)
    assert any("not understood" in x for x in w2)
    w3 = p._feasibility("2026-09-01..2026-09-05", 100)
    assert w3 == ["no calendar conflicts for 2026-09-01 → 2026-09-05"]


def test_feasibility_calendar_conflict(tmp_path):
    from datetime import datetime

    from plp.kernel.calendar import CalendarEvent, IcsCalendarStore

    p = _plugin(tmp_path)
    # _calendar_check opens the plugin's configured store: default ICS path
    ics = IcsCalendarStore(tmp_path / "data" / "calendar" / "main.ics")
    ics.create(
        CalendarEvent(
            title="Focus block",
            start=datetime(2026, 9, 2, 9, 0),
            end=datetime(2026, 9, 2, 10, 0),
        )
    )
    w = p._feasibility("2026-09-01..2026-09-05", 100)
    assert any("OVERLAPS" in x and "Focus block" in x for x in w)


def test_open_questions(tmp_path):
    p = _plugin(tmp_path)
    q = p._open_questions(None, None, False)
    assert any("weeks" in x for x in q)
    assert any("budget" in x for x in q)
    assert any("LLM" in x for x in q)
    assert p._open_questions("2026-09-01..2026-09-05", 100, True) == []


# --------------------------------------------------------------- brainstorm


def test_brainstorm_degraded_when_llm_down(tmp_path, monkeypatch):
    _with_llm(monkeypatch, Exception("endpoint down"))
    p = _plugin(tmp_path)
    r = p.brainstorm("Hawaii", dates="2026-11-20..2026-11-27", budget=3000)
    assert r["seasoned"] is False
    text = (tmp_path / "vault" / r["file"]).read_text()
    assert "Not seasoned yet" in text
    assert "by ~2026-10-09" in text        # flights: 6 weeks before Nov 20
    assert "lodging: book by" in text
    assert "Open questions" in text


def test_brainstorm_seasoned_with_llm(tmp_path, monkeypatch):
    _with_llm(
        monkeypatch,
        '```json\n{"why": "warm seas and her favourite jazz festivals", '
        '"ideas": ["snorkel day trip", "sunset surf lesson", "old-town food walk", "a" ]}\n```',
    )
    p = _plugin(tmp_path)
    r = p.brainstorm("Hawaii", dates="2026-11-20..2026-11-27", budget=3000)
    assert r["seasoned"] is True
    text = (tmp_path / "vault" / r["file"]).read_text()
    assert "warm seas" in text
    assert "- snorkel day trip" in text and "- a" in text  # up to 5 ideas kept


def test_brainstorm_llm_junk_degrades(tmp_path, monkeypatch):
    _with_llm(monkeypatch, "I would rather talk about it in prose, sorry.")
    p = _plugin(tmp_path)
    r = p.brainstorm("Japan")
    assert r["seasoned"] is False
    assert "Not seasoned yet" in (tmp_path / "vault" / r["file"]).read_text()
    # budget falls back to the configured ceiling
    assert "budget: 5000.0" in (tmp_path / "vault" / r["file"]).read_text()


def test_tools(tmp_path, monkeypatch):
    _with_llm(monkeypatch, Exception("down"))
    p = _plugin(tmp_path)
    tools = {t.__name__: t for t in p.tools()}
    r = tools["travel_brainstorm"]("Bali", dates="2026-12-01..2026-12-08")
    assert r["seasoned"] is False
    rows = tools["travel_plans"]()
    assert rows[0]["destination"] == "Bali"
    out = tools["travel_set_status"](Path(r["file"]).stem, "planning")
    assert out["status"] == "planning"


# --------------------------------------------------------------------- cli


def test_cli_prefs_and_brainstorm(tmp_path, capsys, monkeypatch):
    _with_llm(monkeypatch, Exception("down"))
    p = _plugin(tmp_path)
    ctx = p._ctx

    class A:
        pass

    # prefs
    args = A(); args.travel_action = "prefs"
    assert p.commands()[0].handler(args, ctx) == 0
    assert "Trip preferences" in capsys.readouterr().out

    # brainstorm prints the file and the warnings
    args = A(); args.travel_action = "brainstorm"
    args.destination = "Hawaii"; args.dates = "2026-11-20..2026-11-27"; args.budget = 3000
    assert p.commands()[0].handler(args, ctx) == 0
    out = capsys.readouterr().out
    assert "plan drafted" in out and "deterministic" in out

    # plans table
    args = A(); args.travel_action = "plans"; args.status = None
    assert p.commands()[0].handler(args, ctx) == 0
    assert "hawaii.md" in capsys.readouterr().out

    # show + set
    import re

    m = re.search(r"travel/(\S+?\.md)", out)
    stem = m.group(1).removesuffix(".md")
    args = A(); args.travel_action = "show"; args.plan = stem
    assert p.commands()[0].handler(args, ctx) == 0
    assert "## Feasibility" in capsys.readouterr().out
    args = A(); args.travel_action = "set"; args.plan = stem; args.status = "booked"
    assert p.commands()[0].handler(args, ctx) == 0
    assert "booked" in capsys.readouterr().out

    # missing plan
    args = A(); args.travel_action = "show"; args.plan = "ghost"
    assert p.commands()[0].handler(args, ctx) == 1
