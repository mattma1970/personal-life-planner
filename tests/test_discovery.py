"""Discovery tests: the Phase-1 exit criterion — a failing plugin is isolated
and reported while the good ones boot (PRD.md §6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from plp.kernel.bus import EventBus
from plp.kernel.config import PlpConfig
from plp.kernel.discovery import discover
from plp.kernel.store import Store

GOOD = '''
from plp.kernel.plugin import Job, Plugin, tool

class GoodPlugin(Plugin):
    name = "good"

    def setup(self, ctx):
        self._ctx = ctx

    def jobs(self):
        def ping(ctx, args):
            return {"ping": True}
        return [Job(name="good.ping", cron="0 8 * * *", handler=ping)]

    def tools(self):
        @tool("say hi to someone")
        def hi(name: str = "world") -> str:
            return f"hi {name}"
        return [hi]
'''

BAD_SETUP = '''
from plp.kernel.plugin import Plugin

class BadPlugin(Plugin):
    name = "bad"

    def setup(self, ctx):
        raise RuntimeError("boom from bad plugin")
'''

BAD_IMPORT = "import definitely_not_a_module_xyz\n"

DUPE = '''
from plp.kernel.plugin import Plugin

class Dup(Plugin):
    name = "good"
'''


def _mk(plugins_dir: Path, name: str, code: str):
    d = plugins_dir / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(code)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _mk(plugins_dir, "a_good", GOOD)
    _mk(plugins_dir, "b_bad_setup", BAD_SETUP)
    _mk(plugins_dir, "c_bad_import", BAD_IMPORT)
    (plugins_dir / "not_a_plugin").mkdir()  # no plugin.py → skipped
    store = Store(tmp_path / "data" / "t.db")
    store.migrate_core()
    cfg = PlpConfig(
        state_db={"path": str(tmp_path / "data" / "t.db")},
        plugins={"dir": str(plugins_dir)},
    )
    bus = EventBus()
    return tmp_path, store, cfg, bus


def test_isolation_and_reporting(env):
    tmp_path, store, cfg, bus = env
    events = []
    bus.subscribe("plugin.", lambda e, p: events.append(e))

    loaded, failures = discover(Path(cfg.plugins.dir), store, bus, cfg)

    # declared name wins over directory name
    assert [lp.name for lp in loaded] == ["good"]
    lp = loaded[0]
    assert "good.ping" in lp.jobs_by_name
    assert lp.jobs_by_name["good.ping"].plugin == "good"
    assert len(lp.tools) == 1

    assert [f["plugin"] for f in failures] == ["b_bad_setup", "c_bad_import"]
    assert "boom from bad plugin" in failures[0]["error"]
    assert "ImportError" in failures[1]["error"] or "ModuleNotFound" in failures[1]["error"]

    statuses = {r["name"]: r for r in store.plugin_statuses()}
    assert statuses["good"]["state"] == "ok"
    assert statuses["b_bad_setup"]["state"] == "failed"
    assert "boom from bad plugin" in statuses["b_bad_setup"]["error"]

    assert "plugin.loaded" in events
    assert "plugin.load_failed" in events


def test_missing_plugins_dir_is_not_an_error(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate_core()
    cfg = PlpConfig(
        state_db={"path": str(tmp_path / "t.db")},
        plugins={"dir": str(tmp_path / "nope")},
    )
    loaded, failures = discover(tmp_path / "nope", store, EventBus(), cfg)
    assert loaded == [] and failures == []


def test_duplicate_plugin_names_collide(env):
    tmp_path, store, cfg, bus = env
    _mk(tmp_path / "plugins", "z_dup", DUPE)
    loaded, failures = discover(Path(cfg.plugins.dir), store, bus, cfg)
    assert [lp.name for lp in loaded] == ["good"]
    assert any(f["plugin"] == "z_dup" and "collides" in f["error"] for f in failures)
