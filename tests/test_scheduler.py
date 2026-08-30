"""Scheduler tests: cron due-logic, catch-up bounds, coalescing, retry, timeout,
one-shots, config overrides — all with a mocked clock (PRD.md §6.4)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from plp.kernel.bus import EventBus
from plp.kernel.config import PlpConfig
from plp.kernel.plugin import Job
from plp.kernel.scheduler import JobNotFoundError, Scheduler
from plp.kernel.store import Store

UTC = timezone.utc


def dt(*args):
    return datetime(*args, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, *args):
        self.t = datetime(*args, tzinfo=UTC)


def make_sched(tmp_path, clock: Clock, jobs: list[Job], schedules: dict | None = None):
    store = Store(tmp_path / "data" / "t.db")
    store.migrate_core()
    cfg = PlpConfig(
        timezone="UTC",
        state_db={"path": str(tmp_path / "data" / "t.db")},
        plugins={"dir": str(tmp_path / "plugins")},
        schedules=schedules or {},
    )
    sched = Scheduler(
        store,
        EventBus(),
        cfg,
        ctx_factory=lambda row, args: object(),
        plugins_by_name={},
        now_fn=clock,
        sleep_fn=lambda _s: None,
    )
    if jobs:
        sched.register_from_plugins(jobs)
    return sched, store


def test_cron_never_fired_fires_once(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    calls = []

    def h(ctx, args):
        calls.append(clock.t)

    sched, _ = make_sched(
        tmp_path, clock, [Job(name="j", handler=h, cron="0 7 * * *")]
    )
    assert sched.tick() == 1  # never fired; today's 07:00 slot is within 36h
    sched.wait_idle()
    assert len(calls) == 1
    # second tick, same instant: slot already covered
    assert sched.tick() == 0
    assert len(calls) == 1


def test_cron_next_slot_fires(tmp_path):
    clock = Clock(dt(2026, 7, 10, 7, 30, 0))
    sched, store = make_sched(
        tmp_path, clock, [Job(name="j", handler=lambda c, a: {}, cron="0 7 * * *")]
    )
    store.set_job_fired("j", "2026-07-09T07:00:00.000+00:00")
    assert sched.tick() == 1  # yesterday fired; today's slot is due
    sched.wait_idle()
    assert sched.tick() == 0  # now last_fired covers today's slot


def test_catchup_weekly_bounded_by_staleness(tmp_path):
    # Monday 10:00; weekly job is Sunday 20:00 → the newest slot is 14h old.
    # With a 12h staleness window that slot is too stale → no catch-up, even
    # though the job last ran 2 months ago.
    clock = Clock(dt(2026, 7, 13, 10, 0, 0))
    sched, store = make_sched(
        tmp_path,
        clock,
        [Job(name="w", handler=lambda c, a: {}, cron="0 20 * * 0", staleness_h=12)],
    )
    store.set_job_fired("w", "2026-05-01T00:00:00.000+00:00")
    assert sched.tick() == 0

    # Same clock, wider (48h) window: Sunday's 14h-old slot catches up.
    clock2 = Clock(dt(2026, 7, 13, 10, 0, 0))
    sched2, store2 = make_sched(
        tmp_path / "b",
        clock2,
        [Job(name="w", handler=lambda c, a: {}, cron="0 20 * * 0", staleness_h=48)],
    )
    store2.set_job_fired("w", "2026-07-11T00:00:00.000+00:00")
    assert sched2.tick() == 1
    sched2.wait_idle()


def test_never_fired_weekly_stale_does_not_catch_up(tmp_path):
    # Fresh install on a Monday: Sunday's slot (14h ago) exceeds a 12h
    # staleness window → no stale catch-up blast; normal cadence resumes.
    clock = Clock(dt(2026, 7, 13, 10, 0, 0))
    sched, _ = make_sched(
        tmp_path,
        clock,
        [Job(name="w", handler=lambda c, a: {}, cron="0 20 * * 0", staleness_h=12)],
    )
    assert sched.tick() == 0


def test_oneshot_fires_once(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    calls = []

    def h(ctx, args):
        calls.append(1)

    sched, _ = make_sched(
        tmp_path,
        clock,
        [Job(name="os", handler=h, fire_at="2026-07-09T08:00:00+00:00")],
    )
    assert sched.tick() == 1
    sched.wait_idle()
    assert len(calls) == 1
    assert sched.tick() == 0  # one-shots do not repeat

    # a future one-shot is not due
    clock2 = Clock(dt(2026, 7, 9, 9, 0, 0))
    sched2, _ = make_sched(
        tmp_path / "b",
        clock2,
        [Job(name="os2", handler=lambda c, a: None, fire_at="2026-07-09T10:00:00+00:00")],
    )
    assert sched2.tick() == 0


def test_coalescing_drops_overlapping_tick(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 30))
    gate = threading.Event()
    calls = []

    def h(ctx, args):
        calls.append(1)
        gate.wait(timeout=10)

    sched, _ = make_sched(
        tmp_path,
        clock,
        [Job(name="m", handler=h, cron="* * * * *", staleness_h=1)],
    )
    assert sched.tick() == 1  # fires for minute 10:00
    clock.advance(2026, 7, 9, 10, 1, 30)  # next minute, job still blocked
    assert sched.tick() == 0  # dropped (coalesce), not queued
    gate.set()
    sched.wait_idle(timeout=30)
    assert len(calls) == 1


def test_retry_then_success(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    attempts = []

    def h(ctx, args):
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("flaky")
        return {"ok": True}

    sched, _ = make_sched(
        tmp_path, clock, [Job(name="j", handler=h, cron="0 7 * * *", retries=2)]
    )
    res = sched.run_now("j")
    assert res["status"] == "ok"
    assert len(attempts) == 3


def test_retry_exhausted(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    attempts = []

    def h(ctx, args):
        attempts.append(1)
        raise ValueError("always broken")

    sched, _ = make_sched(
        tmp_path, clock, [Job(name="j", handler=h, cron="0 7 * * *", retries=1)]
    )
    res = sched.run_now("j")
    assert res["status"] == "failed"
    assert "ValueError" in res["error"]
    assert len(attempts) == 2


def test_timeout(tmp_path):
    import time

    clock = Clock(dt(2026, 7, 9, 10, 0, 0))

    def h(ctx, args):
        time.sleep(1.0)
        return {}

    sched, _ = make_sched(
        tmp_path,
        clock,
        [Job(name="j", handler=h, cron="0 7 * * *", retries=0, timeout_s=0.15)],
    )
    res = sched.run_now("j")
    assert res["status"] == "timeout"
    assert "timeout" in res["error"]


def test_config_overrides_manifest_cron(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    sched, store = make_sched(
        tmp_path,
        clock,
        [Job(name="j", handler=lambda c, a: {}, cron="0 7 * * *")],
        schedules={"j": "5 7 * * *"},
    )
    assert store.get_job("j")["spec"] == "5 7 * * *"

    # invalid override is a registration error
    with pytest.raises(ValueError, match="invalid cron override"):
        make_sched(
            tmp_path / "b",
            clock,
            [Job(name="j", handler=lambda c, a: {}, cron="0 7 * * *")],
            schedules={"j": "not a cron"},
        )


def test_invalid_cron_rejected(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    with pytest.raises(ValueError, match="invalid cron"):
        make_sched(
            tmp_path, clock, [Job(name="j", handler=lambda c, a: {}, cron="99 99 * * *")]
        )


def test_run_now_unknown_job(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    sched, _ = make_sched(tmp_path, clock, [])
    with pytest.raises(JobNotFoundError):
        sched.run_now("nope")


def test_job_whose_plugin_did_not_load_fails(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    j = Job(name="j", handler=lambda c, a: {}, cron="0 7 * * *")
    j.plugin = "ghost"
    sched, _ = make_sched(tmp_path, clock, [j])
    res = sched.run_now("j")
    assert res["status"] == "failed"
    assert "not loaded" in res["error"]


def test_run_logs_to_store(tmp_path):
    clock = Clock(dt(2026, 7, 9, 10, 0, 0))
    sched, store = make_sched(
        tmp_path, clock, [Job(name="j", handler=lambda c, a: {"x": 1}, cron="0 7 * * *")]
    )
    sched.run_now("j")
    runs = store.recent_runs(5)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["detail"] == '{"x": 1}'
