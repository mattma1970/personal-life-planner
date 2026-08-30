"""``plp`` — the Personal Life Planner CLI (PRD.md §7).

    plp daemon [--once] [--interval N] [--max-ticks N]   run the daemon
    plp run <job> [json-args]                            run a job on demand
    plp runs [--limit N]                                 recent runs (audit)
    plp plugins                                          plugin registry state
    plp approve <id> [--reject] [--note TEXT]            resolve a proposal
    plp chat                                             (Phase 3/5)
    plp calendar                                         (Phase 4)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from . import __version__
from .kernel.config import ConfigError, load_config

STUB_PHASES = {
    "chat": 5,
    "calendar": 4,
}


def _config_path(args) -> str:
    if getattr(args, "config", None):
        return args.config
    env = os.environ.get("PLP_CONFIG")
    if env:
        return env
    return "config/plp.yaml"


def _build(args):
    from .kernel.runtime import build_runtime

    return build_runtime(load_config(_config_path(args)))


def _report_boot(rt) -> None:
    rt.logger.info("plp %s (root: %s)", __version__, rt.config.root)
    for lp in rt.plugins:
        rt.logger.info(
            "  plugin %-16s ok   %d job(s) %d tool(s)",
            lp.name,
            len(lp.jobs_by_name),
            len(lp.tools),
        )
    for f in rt.plugin_failures:
        rt.logger.warning(
            "  plugin %-16s FAILED  %s", f["plugin"], f["error"]
        )
    if rt.scheduler.jobs:
        rt.logger.info(
            "  scheduler: %d job(s): %s",
            len(rt.scheduler.jobs),
            ", ".join(sorted(rt.scheduler.jobs)),
        )
    llm_up = rt.llm.available()
    rt.logger.info(
        "  LLM %s at %s (model %s)",
        "up" if llm_up else "unreachable — degraded mode",
        rt.llm.base_url,
        rt.llm.model,
    )


# ----------------------------------------------------------------- commands


def _cmd_daemon(args) -> int:
    rt = _build(args)
    _report_boot(rt)
    if args.once:
        fired = rt.scheduler.tick()
        rt.scheduler.wait_idle(timeout=120)
        print(f"[daemon] --once: fired {fired} job(s), now idle", file=sys.stderr)
        return 0
    interval = args.interval if args.interval > 0 else 30.0

    def _handle(signum, _frame):
        print(f"\n[daemon] signal {signum}: stopping…", file=sys.stderr)
        rt.scheduler.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        rt.scheduler.run_forever(
            interval_s=interval, max_ticks=args.max_ticks or None
        )
    except KeyboardInterrupt:
        pass
    rt.scheduler.wait_idle(timeout=120)
    return 0


def _cmd_run(args) -> int:
    rt = _build(args)
    _report_boot(rt)
    kwargs: dict = {}
    if args.args:
        try:
            kwargs = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(f"error: job args must be a JSON object: {exc}", file=sys.stderr)
            return 2
        if not isinstance(kwargs, dict):
            print("error: job args must be a JSON object", file=sys.stderr)
            return 2
    from .kernel.scheduler import JobNotFoundError

    try:
        result = rt.scheduler.run_now(args.job, kwargs)
    except JobNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"job {args.job} → {result['status']}")
    if result.get("error"):
        print(f"  error: {result['error']}", file=sys.stderr)
    if result.get("detail"):
        print(f"  detail: {json.dumps(result['detail'], default=str)}")
    return 0 if result["status"] == "ok" else 1


def _cmd_runs(args) -> int:
    from .kernel.config import resolve
    from .kernel.store import Store

    cfg = load_config(_config_path(args))
    store = Store(resolve(cfg, cfg.state_db.path))
    rows = store.recent_runs(args.limit)
    if not rows:
        print("(no runs recorded yet)")
        return 0
    print(f"{'ID':>4}  {'JOB':<30} {'PLUGIN':<12} {'STATUS':<8} {'DUR':>8}  STARTED (UTC)          ERROR")
    for r in rows:
        dur = f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-"
        print(
            f"{r['id']:>4}  {r['job']:<30} {(r['plugin'] or '-'):<12} "
            f"{r['status']:<8} {dur:>8}  {r['started_at']}  {r['error'] or ''}"
        )
    return 0


def _cmd_plugins(args) -> int:
    rt = _build(args)
    print(f"{'PLUGIN':<16} {'STATE':<8} {'JOBS':<40} TOOLS")
    for lp in rt.plugins:
        jobs = ", ".join(sorted(lp.jobs_by_name)) or "-"
        tools = ", ".join(f"{lp.name}.{t.__name__}" for t in lp.tools) or "-"
        print(f"{lp.name:<16} {'ok':<8} {jobs:<40} {tools}")
    for f in rt.plugin_failures:
        print(f"{f['plugin']:<16} {'FAILED':<8} {f['error']}")
    if not rt.plugins and not rt.plugin_failures:
        print("(no plugins found)")
    return 0


def _cmd_approve(args) -> int:
    rt = _build(args)
    expired = rt.approvals.expire_stale()
    if expired:
        print(f"(expired {expired} stale proposal(s))", file=sys.stderr)
    ok = rt.approvals.resolve(args.id, not args.reject, args.note)
    if not ok:
        print(f"error: approval #{args.id} not found or not pending", file=sys.stderr)
        return 1
    row = rt.approvals.get(args.id)
    verdict = row["status"].upper()
    print(f"approval #{args.id} ({row['kind']}) → {verdict}")
    print(json.dumps(row["payload"], indent=2))
    return 0


def _cmd_stub(args) -> int:
    phase = STUB_PHASES[args.command]
    print(
        f"plp {args.command}: lands in build phase {phase} (see PRD.md §8) — "
        "the kernel (Phase 1) is up; scenarios/commands are registered per plugin."
    )
    return 0


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plp",
        description="Personal Life Planner — a tuned personal assistant (see PRD.md)",
    )
    p.add_argument("--version", action="version", version=f"plp {__version__}")
    p.add_argument(
        "--config",
        help="path to plp.yaml (default: $PLP_CONFIG or ./config/plp.yaml)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("daemon", help="run the daemon (in-daemon cron)")
    d.add_argument(
        "--once", action="store_true", help="fire one tick, wait for jobs, exit"
    )
    d.add_argument(
        "--interval", type=float, default=30.0, help="tick interval seconds (default 30)"
    )
    d.add_argument(
        "--max-ticks", type=int, default=0, help="exit after N ticks (0 = run forever)"
    )

    r = sub.add_parser("run", help="run a job now (on demand)")
    r.add_argument("job", help="job name (see 'plp plugins')")
    r.add_argument("args", nargs="?", default=None, help="JSON object of job arguments")

    u = sub.add_parser("runs", help="recent job runs (audit trail)")
    u.add_argument("--limit", type=int, default=20)

    sub.add_parser("plugins", help="loaded plugins, their jobs and tools")

    a = sub.add_parser("approve", help="approve (or reject) a pending proposal")
    a.add_argument("id", type=int, help="proposal id (see digests / 'plp runs')")
    a.add_argument("--reject", action="store_true", help="reject instead of approve")
    a.add_argument("--note", default=None, help="resolution note")

    sub.add_parser("chat", help="talk to the assistant (Phase 5)")
    sub.add_parser("calendar", help="calendar operations (Phase 4)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "daemon":
            return _cmd_daemon(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "runs":
            return _cmd_runs(args)
        if args.command == "plugins":
            return _cmd_plugins(args)
        if args.command == "approve":
            return _cmd_approve(args)
        return _cmd_stub(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
