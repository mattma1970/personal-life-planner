"""Kernel vault tests (Phase 3): frontmatter round-trip, atomic writes,
human-wins conflict, mtime-scan index sync + search. No network, no LLM."""

from __future__ import annotations

import os
import time

import pytest

from plp.kernel.store import Store
from plp.kernel.vault import (
    Vault,
    VaultConflict,
    dump_frontmatter,
    parse_frontmatter,
)


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "plp.db")
    s.migrate_core()
    return s


def _vault(tmp_path) -> Vault:
    return Vault(tmp_path / "vault", _store(tmp_path))


# ------------------------------------------------------------- frontmatter


def test_frontmatter_roundtrip():
    text = dump_frontmatter(
        {"occasion": "anniversary", "status": "idea", "budget": 150},
        "A vinyl record player.\n\nNotes: she loves jazz.",
    )
    meta, body = parse_frontmatter(text)
    assert meta == {"occasion": "anniversary", "status": "idea", "budget": 150}
    assert body.startswith("A vinyl record player.")
    assert meta["budget"] == 150  # stays a number, not a string


def test_frontmatter_absent():
    meta, body = parse_frontmatter("just plain markdown\n")
    assert meta == {}
    assert body == "just plain markdown\n"


def test_frontmatter_malformed_yaml_degrades_to_plain():
    text = "---\n: broken: yaml:\n---\nbody here"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text  # whole file preserved, nothing crashes


# ------------------------------------------------------------------- write


def test_write_creates_nested_file_with_frontmatter(tmp_path):
    v = _vault(tmp_path)
    p = v.write("gifts/2026-birthday.md", "projector idea", {"kind": "gift"})
    assert p.is_file()
    meta, body = v.read("gifts/2026-birthday.md")
    assert meta == {"kind": "gift"}
    assert body.startswith("projector idea")


def test_read_missing_is_none(tmp_path):
    v = _vault(tmp_path)
    assert v.read("nope.md") is None


def test_list_orders_relative_paths(tmp_path):
    v = _vault(tmp_path)
    v.write("b/x.md", "x", {})
    v.write("a/y.md", "y", {})
    v.write("a/z.md", "z", {})
    assert v.list() == ["a/y.md", "a/z.md", "b/x.md"]
    assert v.list("a") == ["a/y.md", "a/z.md"]
    assert v.list("missing") == []


# ------------------------------------------------------------ human-wins


def test_conflict_when_file_changed_since_read(tmp_path):
    v = _vault(tmp_path)
    p = v.write("n.md", "daemon draft", {"k": "v"})
    mtime = p.stat().st_mtime

    # a human edits the file on disk afterwards
    time.sleep(0.01)
    p.write_text("human version\n")
    assert p.stat().st_mtime != mtime

    with pytest.raises(VaultConflict):
        v.write("n.md", "daemon clobber", {"k": "v"}, expected_mtime=mtime)

    # the human's edit stands untouched
    assert p.read_text() == "human version\n"


def test_write_ok_when_mtime_unchanged(tmp_path):
    v = _vault(tmp_path)
    p = v.write("n.md", "v1", {})
    p2 = v.write("n.md", "v2", {"s": 1}, expected_mtime=p.stat().st_mtime)
    assert p2.read_text().startswith("---")
    meta, body = v.read("n.md")
    assert meta == {"s": 1}
    assert body == "v2"


# ----------------------------------------------------------------- index


def test_sync_index_adds_updates_removes(tmp_path):
    v = _vault(tmp_path)
    a = v.write("gifts/gift-a.md", "projector for her birthday", {})
    v.write("travel/hawaii.md", "island trip budget 3000", {})

    assert v.sync_index() == 2
    hits = v.search("projector")
    assert len(hits) == 1 and hits[0]["path"] == "gifts/gift-a.md"

    # unchanged resync is a no-op
    assert v.sync_index() == 0

    # human edits a file -> picked up on next scan
    a.write_text("---\noccasion: birthday\n---\nnew idea: vinyl turntable\n")
    assert v.sync_index() == 1
    assert v.search("turntable")[0]["path"] == "gifts/gift-a.md"
    # stale word is gone from the index
    assert v.search("projector") == []

    # deleted file -> dropped from index
    assert v.remove("travel/hawaii.md")
    assert v.sync_index() == 1
    assert v.search("island") == []


def test_index_isolates_vault_paths(tmp_path):
    v = _vault(tmp_path)
    v.write("notes/x.md", "zebra stripes", {})
    v.sync_index()
    rows = v.store.query("SELECT path FROM docs")
    assert [r["path"] for r in rows] == ["notes/x.md"]


def test_lock_is_reentrant_safe_and_serializes(tmp_path):
    v = _vault(tmp_path)
    with v.lock():
        with v.lock():  # same thread re-enters fine (flock is per-fd)
            v.write("x.md", "ok", {})
    assert v.read("x.md")[1] == "ok"
