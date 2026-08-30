"""Gifts plugin tests (Phase 3): vault-backed records, lifecycle,
human-wins on concurrent edits, review job, CLI command. No LLM."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from plp.kernel.bus import EventBus
from plp.kernel.capability import Capability
from plp.kernel.config import OccasionCfg, PlpConfig
from plp.kernel.context import PluginContext
from plp.kernel.plugin import load_sibling
from plp.kernel.store import Store
from plp.kernel.vault import Vault, VaultConflict

PLUGINS = Path(__file__).resolve().parent.parent / "plugins" / "gifts"

gifts_mod = load_sibling("plp.plugins.gifts.gifts", PLUGINS / "gifts.py")
plugin_mod = load_sibling("plp.plugins.gifts.plugin", PLUGINS / "plugin.py")


# --------------------------------------------------------------- helpers


def _vault(tmp_path) -> Vault:
    store = Store(tmp_path / "plp.db")
    store.migrate_core()
    return Vault(tmp_path / "vault", store)


def _store_only(tmp_path) -> Store:
    s = Store(tmp_path / "plp.db")
    s.migrate_core()
    return s


def _ctx(tmp_path) -> PluginContext:
    cfg = PlpConfig()
    cfg.root = tmp_path
    cfg.vault.path = str(tmp_path / "vault")
    return PluginContext(
        store=_store_only(tmp_path),
        bus=EventBus(),
        config=cfg,
        delivery=None,
        capability=Capability.permissive(8),
    )


def _plugin(ctx) -> plugin_mod.GiftsPlugin:
    p = plugin_mod.GiftsPlugin()
    p.setup(ctx)
    return p


# ------------------------------------------------------------------- slug


def test_slugify():
    assert gifts_mod.slugify("Wifi Projector for the Deck") == "wifi-projector-for-the-deck"
    assert gifts_mod.slugify("  Café  ") == "cafe"
    assert gifts_mod.slugify("???") == "gift"
    assert len(gifts_mod.slugify("a" * 80)) == 40


# ------------------------------------------------------------- occasions


def test_next_occurrence_basic():
    assert gifts_mod.next_occurrence(12, 25, date(2026, 1, 1), 365) == date(2026, 12, 25)


def test_next_occurrence_next_year():
    # just past the date this year → next year, if within horizon
    assert gifts_mod.next_occurrence(1, 1, date(2026, 1, 2), 365) == date(2027, 1, 1)
    assert gifts_mod.next_occurrence(1, 1, date(2026, 1, 2), 300) is None


def test_next_occurrence_invalid_date_skipped():
    assert gifts_mod.next_occurrence(2, 30, date(2026, 1, 1), 365) is None
    # 2028 is a leap year; 2028-02-29 is 790 days after 2026-01-01
    assert gifts_mod.next_occurrence(2, 29, date(2026, 1, 1), 790) == date(2028, 2, 29)


# ------------------------------------------------------------------- store


def test_add_creates_vault_file(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, meta = s.add("A vinyl record player", occasion="birthday", budget=200)
    assert rel == f"gifts/{date.today().isoformat()}-a-vinyl-record-player.md"
    assert meta["status"] == "idea"
    text = (tmp_path / "vault" / rel).read_text()
    assert "occasion: birthday" in text and "budget: 200.0" in text


def test_add_same_idea_twice_gets_suffix(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel1, _ = s.add("Board games")
    rel2, _ = s.add("Board games")
    assert rel1 != rel2 and rel2.endswith("-2.md")


def test_list_filters(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    s.add("Turntable", occasion="birthday")
    s.add("Scarf", occasion="anniversary")
    s.add("Mug", occasion="birthday")
    s.set_status(s.list(status="idea")[0]["file"], "bought")
    assert len(s.list(occasion="birthday")) == 2
    assert len(s.list(occasion="birthday", status="idea")) == 1
    assert len(s.list(status="bought")) == 1


def test_find_by_stem_and_path(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, _ = s.add("Coffee table book")
    stem = Path(rel).stem
    got = s.find(stem)
    assert got is not None and got[0] == rel
    assert s.find(rel) is not None
    assert s.find("nope-nope") is None


def test_find_missing_or_not_a_gift(tmp_path):
    v = _vault(tmp_path)
    v.write("notes/random.md", "not a gift", {})
    s = gifts_mod.GiftStore(v)
    assert s.find("random") is None  # no such gift file
    v.write("gifts/evil.md", "hi", {})  # a real file that is not a gift
    with pytest.raises(ValueError):
        s.find("gifts/evil.md")


def test_set_status_lifecycle(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, _ = s.add("Projector")
    stem = Path(rel).stem
    _, meta = s.set_status(stem, "shortlist")
    assert meta["status"] == "shortlist"
    _, meta = s.set_status(stem, "bought", price=189.99)
    assert meta["status"] == "bought"
    assert meta["bought_at"] == date.today().isoformat()
    assert meta["price_paid"] == 189.99
    _, meta = s.set_status(stem, "given")
    assert meta["status"] == "given" and meta["given_at"] == date.today().isoformat()


def test_set_status_bad_input(tmp_path):
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, _ = s.add("Thing")
    with pytest.raises(ValueError):
        s.set_status(Path(rel).stem, "sailing")
    with pytest.raises(KeyError):
        s.set_status("does-not-exist", "bought")


def test_human_edit_wins_over_daemon_write(tmp_path, monkeypatch):
    """A human edit landing between the daemon's read and write must not be
    clobbered: the write is skipped, the human's file stands."""
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, _ = s.add("Lamp")
    p = tmp_path / "vault" / rel
    vault = s.vault
    real_write = vault.write

    def interleaved(rel_, body, meta=None, expected_mtime=None):
        import time

        time.sleep(0.005)  # > 1ms: land on a different clock tick than the stat
        (vault.root / rel_).write_text("human note landed mid-flight\n")
        return real_write(rel_, body, meta=meta, expected_mtime=expected_mtime)

    monkeypatch.setattr(vault, "write", interleaved)
    with pytest.raises(VaultConflict):
        s.set_status(Path(rel).stem, "bought")
    assert p.read_text() == "human note landed mid-flight\n"  # untouched


def test_daemon_writes_human_version_read_beforehand(tmp_path):
    """If the human edit predates the daemon's read, the daemon works on top
    of it (no conflict — it is reading the newest truth)."""
    s = gifts_mod.GiftStore(_vault(tmp_path))
    rel, _ = s.add("Lamp")
    p = tmp_path / "vault" / rel
    p.write_text("---\nkind: gift\nidea: Lamp\noccasion: x\nstatus: idea\ncreated: 2026-01-01\n---\n\nhuman note\n")
    rel2, meta = s.set_status(Path(rel).stem, "bought")
    text = p.read_text()
    assert "human note" in text and "status: bought" in text


# ------------------------------------------------------------------- plugin


def test_tools_roundtrip(tmp_path):
    ctx = _ctx(tmp_path)
    p = _plugin(ctx)
    tools = {t.__name__: t for t in p.tools()}
    r = tools["gift_add"](idea="Trip to Japan", occasion="anniversary", budget=2500)
    assert r["status"] == "idea" and r["file"].startswith("gifts/")
    rows = tools["gifts_list"](occasion="anniversary")
    assert len(rows) == 1 and rows[0]["idea"] == "Trip to Japan"
    out = tools["gift_set_status"](gift=Path(r["file"]).stem, status="bought", price=2400)
    assert out["status"] == "bought"


def test_review_job_no_occasions(tmp_path):
    ctx = _ctx(tmp_path)
    p = _plugin(ctx)
    p._gifts().add("Mug", occasion="just because")
    job = p.jobs()[0]
    assert job.name == "gifts.review"
    result = job.handler(ctx, {})
    assert result["in_flight"] == 1 and result["occasions_upcoming"] == []
    rows = ctx.store.query("SELECT content FROM digests WHERE kind='gifts'")
    assert len(rows) == 1
    assert "No occasions configured" in rows[0]["content"]


def test_review_job_upcoming_and_stale(tmp_path, monkeypatch):
    cfg = PlpConfig()
    cfg.root = tmp_path
    cfg.vault.path = str(tmp_path / "vault")
    cfg.gifts.occasions = [OccasionCfg(name="birthday", month=12, day=25)]
    cfg.gifts.stale_after_days = 30
    cfg.gifts.review_window_days = 150  # Dec 25 is >90d out from late August
    ctx = PluginContext(
        store=_store_only(tmp_path),
        bus=EventBus(),
        config=cfg,
        delivery=None,
        capability=Capability.permissive(8),
    )
    p = _plugin(ctx)
    rel, meta = p._gifts().add("Old idea", occasion="birthday")
    # backdate it 40 days
    import time

    old = (date.today().toordinal() - 40)
    meta["created"] = date.fromordinal(old).isoformat()
    p._gifts().vault.write(rel, "# Old idea\n", dict(meta))

    result = p.jobs()[0].handler(ctx, {})
    assert result["occasions_upcoming"][0]["occasion"] == "birthday"
    assert result["stale"] == 1
    rows = ctx.store.query("SELECT content FROM digests WHERE kind='gifts'")
    assert "Stale ideas" in rows[0]["content"]


# ------------------------------------------------------------------- cli


def test_cli_add_list_show_set(tmp_path, capsys):
    from plp.cli import build_parser, main

    real_plugins = (Path(__file__).resolve().parent.parent / "plugins")
    cfg_path = tmp_path / "plp.yaml"
    cfg_path.write_text(
        f"vault:\n  path: {tmp_path / 'vault'}\n"
        f"state_db:\n  path: {tmp_path / 'plp.db'}\n"
        f"plugins:\n  dir: {real_plugins}\n"
    )
    env_cfg = {"PLP_CONFIG": str(cfg_path)}
    import os

    old = {k: os.environ.get(k) for k in env_cfg}
    os.environ.update(env_cfg)
    try:
        cwd = Path.cwd()
        os.chdir(tmp_path)  # config root derives from the file location
        rc = main(["gift", "add", "A record player", "--occasion", "birthday",
                   "--budget", "200", "--notes", "jazz first"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "gift saved" in out

        rc = main(["gift", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "idea" in out and "birthday" in out and "a-record-player.md" in out

        stem = out.split("a-record-player")
        assert stem[0].strip().endswith("a-record-player") or "a-record-player.md" in out

        # use the real stem:
        import re

        m = re.search(r"gifts/(\S+?\.md)", out)
        assert m
        stem = m.group(1).removesuffix(".md")
        rc = main(["gift", "show", stem])
        assert rc == 0
        out = capsys.readouterr().out
        assert "occasion: birthday" in out and "jazz first" in out

        rc = main(["gift", "set", stem, "bought", "--price", "150"])
        assert rc == 0
        assert "bought" in capsys.readouterr().out

        rc = main(["gift", "set", "missing-gift", "given"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err
    finally:
        os.chdir(cwd)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
