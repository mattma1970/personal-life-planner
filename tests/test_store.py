"""Store tests: migrations, WAL, FTS5, runs, jobs, approvals, plugin status."""

from __future__ import annotations

import pytest

from plp.kernel.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "t.db")
    s.migrate_core()
    yield s
    s.close()


def test_wal_enabled(store):
    assert store.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"


def test_migrations_idempotent(store):
    store.migrate_core()
    store.migrate_core()
    rows = store.query("SELECT version FROM schema_version WHERE ns='core' ORDER BY version")
    assert [r[0] for r in rows] == [1, 2, 3]


def test_fts5_available(store):
    # This build has FTS5; on a build without it, Store degrades to LIKE.
    assert store.fts5 is True


def test_plugin_migrations_ns(store):
    store.migrate_for("gifts", [(1, "CREATE TABLE gifts_demo (id INTEGER PRIMARY KEY);")])
    store.migrate_for("gifts", [(1, "CREATE TABLE gifts_demo (id INTEGER PRIMARY KEY);")])
    rows = store.query("SELECT version FROM schema_version WHERE ns='plugin.gifts'")
    assert len(rows) == 1


def test_runs_lifecycle(store):
    rid = store.start_run("news.collect", "news")
    store.finish_run(rid, "ok", detail='{"n": 3}')
    r = store.recent_runs(10)[0]
    assert r["job"] == "news.collect"
    assert r["plugin"] == "news"
    assert r["status"] == "ok"
    assert r["duration_ms"] >= 0
    assert r["detail"] == '{"n": 3}'


def test_jobs_upsert_preserves_last_fired(store):
    store.upsert_job("j", "cron", "0 7 * * *", "p")
    store.set_job_fired("j", "2026-07-01T00:00:00.000+00:00")
    store.upsert_job("j", "cron", "5 7 * * *", "p")  # rescheduled
    j = store.get_job("j")
    assert j["spec"] == "5 7 * * *"
    assert j["last_fired_at"] == "2026-07-01T00:00:00.000+00:00"


def test_set_job_fired_never_moves_backwards(store):
    store.upsert_job("j", "cron", "0 7 * * *", "p")
    store.set_job_fired("j", "2026-07-05T00:00:00.000+00:00")
    store.set_job_fired("j", "2026-07-01T00:00:00.000+00:00")
    assert store.get_job("j")["last_fired_at"] == "2026-07-05T00:00:00.000+00:00"


def test_approvals_lifecycle(store):
    aid = store.propose("calendar_block", {"title": "x"}, note="n")
    p = store.pending_approvals()
    assert len(p) == 1
    assert p[0]["id"] == aid
    assert p[0]["payload"]["title"] == "x"
    assert p[0]["note"] == "n"
    assert store.resolve_approval(aid, True, "ok") is True
    assert store.resolve_approval(aid, False) is False  # already resolved
    row = store.get_approval(aid)
    assert row["status"] == "approved"
    assert store.pending_approvals() == []


def test_expire_stale_approvals(store):
    from datetime import datetime, timedelta, timezone

    aid = store.propose("calendar_block", {})
    stale = (
        datetime.now(timezone.utc) - timedelta(hours=48)
    ).isoformat(timespec="milliseconds")
    fresh = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat(timespec="milliseconds")
    store.execute("UPDATE approvals SET proposed_at=? WHERE id=?", (stale, aid))
    fresh_id = store.propose("calendar_block", {})
    store.execute(
        "UPDATE approvals SET proposed_at=? WHERE id=?", (fresh, fresh_id)
    )
    assert store.expire_stale_approvals(24) == 1
    assert store.get_approval(aid)["status"] == "expired"
    assert store.get_approval(fresh_id)["status"] == "pending"


def test_plugin_status_upsert(store):
    store.set_plugin_status("demo", "ok", None)
    store.set_plugin_status("demo", "failed", "boom")
    rows = store.plugin_statuses()
    assert rows[0]["state"] == "failed"
    assert rows[0]["error"] == "boom"


def test_digests(store):
    did = store.save_digest("daily", "hello digest")
    rows = store.query("SELECT * FROM digests WHERE id=?", (did,))
    assert rows[0]["content"] == "hello digest"


def test_search_docs_fts(store):
    store.execute(
        "INSERT INTO docs(path, body, updated_at) VALUES (?, ?, ?)",
        ("notes/one.md", "the gift idea is a telescope", "2026-07-09T00:00:00.000+00:00"),
    )
    if store.fts5:
        store.execute("INSERT INTO fts_docs(rowid, path, body) VALUES (1, 'notes/one.md', 'the gift idea is a telescope')")
    rows = store.search_docs("telescope")
    assert len(rows) == 1
    assert rows[0]["path"] == "notes/one.md"
