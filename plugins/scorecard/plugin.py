"""Life scorecard + weekly checkup plugin (Phase 5, PRD.md §3/§8/§11).

- Goals live in the vault (``plp-vault/goals.md``); seeded on first boot,
  edited by the owner, interview-seeded via ``plp scorecard onboarding``.
- Measurement is deterministic: calendar hours per category + vault activity
  per week, trended in the state DB (LLM-free floor, always works).
- ``checkup.weekly`` (default Sunday 20:00) seasons the numbers with the
  bounded agent — narrow tool mount, strict JSON, one retry, floor fallback —
  and ends in 2–3 approvable calendar proposals (propose-don't-command).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from plp.kernel.calendar import open_calendar_store
from plp.kernel.plugin import Command, Job, Plugin, load_sibling, tool
from plp.kernel.vault import Vault

_HERE = Path(__file__).resolve().parent
log = logging.getLogger("plp.scorecard")


class ScorecardPlugin(Plugin):
    name = "scorecard"

    def __init__(self) -> None:
        # Sibling modules are plugin-wired (kernel rule: no sibling imports).
        self._goals_mod = load_sibling("plp.plugins.scorecard.goals", _HERE / "goals.py")
        self._scorecard_mod = load_sibling(
            "plp.plugins.scorecard.scorecard", _HERE / "scorecard.py"
        )
        self._checkup_mod = load_sibling("plp.plugins.scorecard.checkup", _HERE / "checkup.py")
        self._gifts_ctx_mod = load_sibling(
            "plp.plugins.scorecard.gifts_context", _HERE / "gifts_context.py"
        )
        self._ctx = None
        self._cfg = None
        self._store = None

    # ----------------------------------------------------------------- setup

    def setup(self, ctx) -> None:
        self._ctx = ctx
        self._cfg = ctx.config.scorecard
        self._store = self._scorecard_mod.ScorecardStore(ctx.store)
        vault = Vault(ctx.vault_dir(), ctx.store)
        if vault.read(self._cfg.goals_file) is None:
            vault.write(self._cfg.goals_file, self._goals_mod.seed_goals())
            ctx.log(
                "seeded %s — edit it; or run `plp scorecard onboarding`",
                self._cfg.goals_file,
            )
        else:
            ctx.log(
                "scorecard ready (goals file: %s, checkup cron: %s)",
                self._cfg.goals_file,
                self._cfg.checkup_cron,
            )

    # ---------------------------------------------------------------- helpers

    def _vault(self) -> Vault:
        assert self._ctx is not None, "setup() not called"
        return Vault(self._ctx.vault_dir(), self._ctx.store)

    def _calendar(self):
        assert self._ctx is not None, "setup() not called"
        return open_calendar_store(self._ctx.config, log)

    def _goals(self) -> list:
        row = self._vault().read(self._cfg.goals_file)
        return self._goals_mod.parse_goals(row[1]) if row else []

    def _window(self, ref: str | None):
        """Week window containing ``ref`` (``YYYY-MM-DD``), else the most
        recently completed week relative to now."""
        ws = self._cfg.week_start
        if ref:
            d = dt.date.fromisoformat(ref)
            ref_dt = dt.datetime(d.year, d.month, d.day)
        else:
            now = self._ctx.config.default_now_factory()().replace(tzinfo=None)
            ref_dt = now
            end, _ = self._scorecard_mod.window_for(ref_dt, ws)
            return end - dt.timedelta(days=7), end
        return self._scorecard_mod.window_for(ref_dt, ws)

    # ------------------------------------------------------------------ tools

    def tools(self) -> list:
        scorecard_mod = self._scorecard_mod
        goals_mod = self._goals_mod
        sstore = self._store
        cfg = self._cfg

        @tool(
            "The life scorecard for one week (default: the most recently completed "
            "week; pass a date 'YYYY-MM-DD' inside any week). Returns JSON: hours "
            "per calendar category, each goal's actual vs target hours, vault activity."
        )
        def week(week: str | None = None) -> str:
            window = self._window(week)
            data = scorecard_mod.aggregate_week(self._vault(), self._calendar(), self._goals(), window)
            return json.dumps(
                {
                    "window": [data["window"][0].isoformat(), data["window"][1].isoformat()],
                    "hours_by_category": data["hours_by_category"],
                    "events": data["events"],
                    "goals": data["goals"],
                    "vault_created": data["vault_created"],
                    "vault_updated": data["vault_updated"],
                },
                ensure_ascii=False,
            )

        @tool(
            "Trend for one goal category: hours per week, oldest → newest, with "
            "missed weeks shown as 0. Returns a JSON array."
        )
        def trend(category: str, weeks: int = 8) -> str:
            return json.dumps(sstore.trend(category, weeks=weeks))

        @tool(
            "The stated life goals from the vault (title, category, target hours "
            "per week). Returns a JSON array."
        )
        def goals() -> str:
            row = self._vault().read(cfg.goals_file)
            gs = goals_mod.parse_goals(row[1]) if row else []
            return json.dumps(
                [
                    {
                        "title": g.title,
                        "category": g.category,
                        "target_hours_week": g.target_hours_week,
                    }
                    for g in gs
                ]
            )

        return [week, trend, goals]

    # ------------------------------------------------------------------- jobs

    def jobs(self) -> list[Job]:
        def _checkup(ctx, args: dict) -> dict:
            window = None
            if args.get("week"):
                d = dt.date.fromisoformat(args["week"])
                window = self._scorecard_mod.window_for(
                    dt.datetime(d.year, d.month, d.day), ctx.config.scorecard.week_start
                )
            return self._checkup_mod.build_checkup(
                ctx,
                window,
                self._goals_mod,
                self._gifts_ctx_mod,
                self._scorecard_mod,
                use_llm=not args.get("no_llm"),
            )

        return [
            Job(
                name="checkup.weekly",
                handler=_checkup,
                cron=self._cfg.checkup_cron if self._cfg else "0 20 * * 0",  # Sunday 20:00
                timeout_s=900,  # CPU LLM runs measured ~4 min; keep big headroom
                staleness_h=48.0,
                plugin=self.name,
            )
        ]

    # ---------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        def add_arguments(sp) -> None:
            sub = sp.add_subparsers(dest="sc_cmd", required=True)
            w = sub.add_parser("week", help="scorecard for one week (no LLM)")
            w.add_argument("--date", default=None, help="a date inside the week (default: last completed week)")
            c = sub.add_parser(
                "checkup", help="run the weekly checkup now (LLM-seasoned; --no-llm for the floor)"
            )
            c.add_argument("--date", default=None, help="a date inside the week to review (default: last completed week)")
            c.add_argument("--no-llm", action="store_true", help="deterministic floor only")
            g = sub.add_parser("goals", help="show the life goals file")
            g.add_argument("--init", action="store_true", help="seed the goals file if missing")
            sub.add_parser(
                "onboarding", help="interactive interview to (re)seed goals.md"
            )

        def handler(args, ctx) -> int:
            if args.sc_cmd == "week":
                window = self._window(args.date)
                data = self._scorecard_mod.aggregate_week(
                    self._vault(), self._calendar(), self._goals(), window
                )
                trends = {
                    r["category"]: self._store.trend(r["category"], weeks=8)
                    for r in data["goals"]
                    if r["target"] is not None
                }
                wins, drift = self._checkup_mod.floor_wins_drift(data, trends)
                print(
                    self._checkup_mod.render(
                        data, wins, drift, [], [], False, None, trends
                    )
                )
                return 0
            if args.sc_cmd == "checkup":
                window = self._window(args.date)
                result = self._checkup_mod.build_checkup(
                    ctx,
                    window,
                    self._goals_mod,
                    self._gifts_ctx_mod,
                    self._scorecard_mod,
                    use_llm=not args.no_llm,
                )
                print(result["text"])
                for aid in result["proposals"]:
                    print(f"  proposal awaiting approval: plp approve {aid}")
                return 0
            if args.sc_cmd == "goals":
                vault = self._vault()
                row = vault.read(self._cfg.goals_file)
                if row is None:
                    if args.init:
                        vault.write(self._cfg.goals_file, self._goals_mod.seed_goals())
                        print(f"seeded {self._cfg.goals_file}")
                        return 0
                    print(f"no goals file yet — run: plp scorecard goals --init")
                    return 1
                print(row[1].rstrip())
                return 0
            # onboarding
            return self._onboarding(ctx)
        
        return [
            Command(
                name="scorecard",
                help="life scorecard & weekly checkup (goals in the vault)",
                handler=handler,
                add_arguments=add_arguments,
            )
        ]

    def _onboarding(self, ctx) -> int:
        """The PRD §11 onboarding interview: walk the default goals, take a
        target hours/week for each (Enter = default, 0 = skip/unmeasured),
        write goals.md (human-edit-wins guard: re-read before writing)."""
        vault = self._vault()
        row = vault.read(self._cfg.goals_file)
        existing = self._goals_mod.parse_goals(row[1]) if row else []
        by_cat = {g.category: g for g in existing}
        print("Life goals onboarding — set a target hours/week per category.")
        print("(Enter = suggested default, 0 = unmeasured, 'q' = skip)\n")
        goals = []
        for title, cat, default in self._goals_mod.DEFAULT_GOALS:
            g = by_cat.get(cat)
            cur = g.target_hours_week if g else None
            prompt = f"  {title} [{cat}] — target hours/week [{cur if cur is not None else default}]: "
            raw = input(prompt).strip()
            if raw.lower() == "q":
                continue
            try:
                val = float(raw) if raw else float(default if cur is None else cur)
            except ValueError:
                print(f"  (not a number — kept {cur if cur is not None else default})")
                continue
            goals.append(
                self._goals_mod.Goal(
                    title=title,
                    category=cat,
                    target_hours_week=None if val == 0 else val,
                    notes=(g.notes if g and g.notes else ""),
                )
            )
        # keep goals the owner added beyond the default set (they were not
        # asked about); a 'q' answer to a default means "not my goal" → dropped
        default_cats = {c for _, c, _ in self._goals_mod.DEFAULT_GOALS}
        for g in existing:
            if g.category not in default_cats:
                goals.append(g)
        p = vault.root / self._cfg.goals_file
        expected = p.stat().st_mtime if p.exists() else None
        vault.write(
            self._cfg.goals_file,
            self._goals_mod.dump_goals(goals),
            expected_mtime=expected,
        )
        print(f"\nwrote {self._cfg.goals_file} — edit it any time; the checkup reads it weekly.")
        return 0
