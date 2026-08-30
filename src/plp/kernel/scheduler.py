"""The in-daemon cron scheduler — *the* cron (PRD.md §6.4).

Design decisions (from the architecture workshop):

- **No system crontab for jobs.** System cron may supervise the *process*;
  this is the scheduler. Ticks every N seconds (default 30).
- **Due-logic** per job, all persisted in the ``jobs`` table:
  * cron job: due when a cron slot has passed since the last fire — i.e.
    normal cadence *and* catch-up-on-wake after downtime, in one condition;
    never-fired jobs only catch up if the newest missed slot is within the
    job's ``staleness_h`` window (no 3-week-stale news blasts);
  * one-shot job: due when its ``fire_at`` has elapsed and it hasn't fired.
- **Coalescing, not queueing:** a job that is running when its slot arrives
  drops the tick (logged) — a late run is never more useful than the current
  one for these features.
- **Supervisor:** every run is logged to ``runs`` (the audit trail,
  PRD.md §2), runs under a per-job timeout, retries with linear backoff,
  and publishes bus events. A failing job is reported, never fatal.
- **Config beats manifest:** ``schedules:`` in plp.yaml overrides any job's
  cron expression at registration time.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable

from croniter import croniter

from .bus import EventBus
from .config import PlpConfig
from .plugin import Job
from .store import Store
from .util import parse_ts, utcnow_iso

log = logging.getLogger("plp.kernel.scheduler")


class JobNotFoundError(KeyError):
    pass


CtxFactory = Callable[[dict, dict], Any]  # (job row, args) -> PluginContext


class Scheduler:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        config: PlpConfig,
        ctx_factory: CtxFactory,
        plugins_by_name: dict[str, Any],
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.config = config
        self.ctx_factory = ctx_factory
        self.plugins = plugins_by_name
        self.now_fn = now_fn or config.default_now_factory()
        self.sleep_fn = sleep_fn or time.sleep
        self._cv = threading.Condition()
        self._running: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plp-job")
        self._stop = threading.Event()
        #: In-memory job policy objects (name -> Job); rebuilt each boot.
        self.jobs: dict[str, Job] = {}

    # ---------------------------------------------------------- registration

    def register_from_plugins(self, jobs: list[Job]) -> list[str]:
        """Upsert manifest-declared jobs into the DB and keep policy in memory.

        Applies ``config.schedules`` overrides (config beats manifest).
        Raises ValueError on invalid cron expressions or one-shot timestamps.
        """
        names: list[str] = []
        for j in jobs:
            kind, spec = j.kind, j.spec
            if kind == "cron":
                if not croniter.is_valid(spec):
                    raise ValueError(f"job {j.name!r}: invalid cron expression {spec!r}")
            else:
                parse_ts(spec)  # raises ValueError if unparseable
            override = self.config.schedules.get(j.name)
            if override is not None:
                if kind == "cron":
                    if not croniter.is_valid(override):
                        raise ValueError(
                            f"job {j.name!r}: invalid cron override {override!r}"
                        )
                    j.cron = override
                    spec = override
                else:
                    parse_ts(override)
                    j.fire_at = override
                    spec = override
            self.store.upsert_job(
                j.name,
                kind,
                spec,
                j.plugin,
                retries=j.retries,
                timeout_s=j.timeout_s,
                staleness_h=j.staleness_h,
            )
            self.jobs[j.name] = j
            names.append(j.name)
        self.bus.publish("scheduler.jobs_registered", {"count": len(names)})
        return names

    def add_one_shot(
        self,
        name: str,
        fire_at: str,
        handler: Callable[[Any, dict], Any],
        plugin: str | None = None,
        retries: int = 0,
        timeout_s: float = 300.0,
    ) -> None:
        """Create a one-shot job (the DB row) the agent can schedule at runtime."""
        parse_ts(fire_at)
        j = Job(
            name=name,
            fire_at=fire_at,
            handler=handler,
            plugin=plugin,
            retries=retries,
            timeout_s=timeout_s,
        )
        self.store.add_one_shot(name, fire_at, plugin, retries, timeout_s)
        self.jobs[name] = j
        self.bus.publish("scheduler.one_shot", {"job": name, "fire_at": fire_at})

    # -------------------------------------------------------------- due logic

    def _due(self, row: dict) -> bool:
        now = self.now_fn()
        last = row["last_fired_at"]
        if row["kind"] == "oneshot":
            return last is None and parse_ts(row["spec"]) <= now
        prev = croniter(row["spec"], now).get_prev(datetime)
        if prev.tzinfo is None:  # defensive: keep comparisons tz-aware
            prev = prev.replace(tzinfo=now.tzinfo)
        staleness = timedelta(hours=float(row["staleness_h"] or 36.0))
        if last is None:
            # Never fired: catch up only if the newest missed slot is recent.
            return (now - prev) <= staleness
        # Fired before: a slot passed after the last fire — and that slot is
        # not so old that the catch-up would be stale (bounded catch-up).
        return prev > parse_ts(last) and (now - prev) <= staleness

    def tick(self) -> int:
        """Fire all due jobs (one worker thread each). Returns count fired."""
        fired = 0
        for row in self.store.all_jobs():
            if not self._due(row):
                continue
            with self._cv:
                if row["name"] in self._running:
                    log.info("job %s still running; dropping tick (coalesce)", row["name"])
                    continue
                self._running.add(row["name"])
            self.store.set_job_fired(row["name"], utcnow_iso())
            threading.Thread(
                target=self._supervised,
                args=(row,),
                daemon=True,
                name=f"plp-job-{row['name']}",
            ).start()
            fired += 1
        return fired

    def wait_idle(self, timeout: float = 300.0) -> bool:
        """Block until no job is running (used by ``daemon --once`` and tests)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._running, timeout)

    # ---------------------------------------------------------- on-demand run

    def run_now(self, job_name: str, args: dict | None = None) -> dict:
        """Run a job immediately (``plp run``): same supervisor, bypasses due-logic."""
        row = self.store.get_job(job_name)
        if row is None:
            raise JobNotFoundError(f"unknown job {job_name!r}")
        with self._cv:
            if job_name in self._running:
                raise JobNotFoundError(f"job {job_name!r} is already running")
            self._running.add(job_name)
        self.store.set_job_fired(job_name, utcnow_iso())
        try:
            return self._supervised(row, args=args or {}, manual=True)
        finally:
            with self._cv:
                self._running.discard(job_name)
                self._cv.notify_all()

    # -------------------------------------------------------------- execution

    def _policy(self, row: dict) -> tuple[float, int]:
        j = self.jobs.get(row["name"])
        timeout = j.timeout_s if j is not None else float(row["timeout_s"])
        retries = j.retries if j is not None else int(row["retries"])
        return timeout, retries

    def _supervised(self, row: dict, args: dict | None = None, manual: bool = False) -> dict:
        name = row["name"]
        args = args or {}
        timeout, retries = self._policy(row)
        run_id = self.store.start_run(name, row["plugin"])
        started = utcnow_iso()
        self.bus.publish(f"job.{name}.started", {"job": name, "manual": manual})
        status, err, detail = "failed", None, None
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    detail = self._call(row, timeout, args)
                    status, err = "ok", None
                    break
                except TimeoutError:
                    status, err = "timeout", f"exceeded {timeout}s timeout"
                    break  # no retry on timeout: a stuck job should not be re-stuck
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    err = f"{type(exc).__name__}: {exc}"
                    if attempt <= retries:
                        log.warning(
                            "job %s attempt %d/%d failed: %s — retrying",
                            name,
                            attempt,
                            retries + 1,
                            err,
                        )
                        self.sleep_fn(2.0 * attempt)
                        continue
                    status = "failed"
                    break
        finally:
            duration = int((self.now_fn() - parse_ts(started)).total_seconds() * 1000)
            self.store.finish_run(
                run_id,
                status,
                err,
                json.dumps(detail, default=str) if detail is not None else None,
            )
            event = "finished" if status == "ok" else "failed"
            self.bus.publish(
                f"job.{name}.{event}",
                {"status": status, "error": err, "duration_ms": duration, "manual": manual},
            )
            log.log(
                logging.INFO if status == "ok" else logging.ERROR,
                "job %s %s in %dms%s",
                name,
                status,
                duration,
                f" ({err})" if err else "",
            )
            with self._cv:
                self._running.discard(name)
                self._cv.notify_all()
        return {"job": name, "status": status, "error": err, "detail": detail}

    def _call(self, row: dict, timeout: float, args: dict) -> Any:
        plugin = self.plugins.get(row["plugin"]) if row["plugin"] else None
        if row["plugin"] and plugin is None:
            raise RuntimeError(
                f"plugin {row['plugin']!r} is not loaded; job {row['name']!r} cannot run "
                "(see 'plp plugins')"
            )
        job = self.jobs.get(row["name"])
        if job is None:
            raise RuntimeError(f"no in-memory job policy for {row['name']!r}")
        ctx = self.ctx_factory(row, dict(args))
        future = self._executor.submit(job.handler, ctx, dict(args))
        return future.result(timeout=timeout)

    # ----------------------------------------------------------------- daemon

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, interval_s: float = 30.0, max_ticks: int | None = None) -> None:
        """The daemon loop: immediate first tick (catch-up on wake), then N-second ticks."""
        log.info("scheduler: ticking every %.0fs", interval_s)
        ticks = 0
        while not self._stop.is_set():
            ticks += 1
            self.tick()
            if max_ticks is not None and ticks >= max_ticks:
                break
            self._stop.wait(interval_s)
        self._executor.shutdown(wait=False)
        log.info("scheduler: stopped after %d tick(s)", ticks)
