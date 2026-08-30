"""Travel plugin — Phase 3 (PRD.md S5: holiday planner).

- ``plp travel brainstorm "Hawaii" [--dates …] [--budget N]`` — turns stated
  preferences (``travel/preferences.md`` in the vault) plus the request into
  a plan doc (``travel/<date>-<slug>.md``) with sections: why it fits,
  ideas, what-to-book-and-when, open questions, feasibility.
- ``plp travel plans`` / ``travel show`` / ``travel set`` — the lifecycle
  ``brainstorm → planning → booked → done``.
- ``plp travel prefs`` — show the preferences file (seeded on first use).

The Qwen 27B seasons the plan (why/ideas prose) when available; without it
the doc is still complete — deterministic feasibility, booking deadlines
derived from the dates, and open questions for the human (PRD.md §2.3:
LLM is seasoning, never load-bearing).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from plp.kernel.llm import LLMClient
from plp.kernel.plugin import Command, Job, Plugin, load_sibling, tool
from plp.kernel.vault import Vault, VaultConflict

_HERE = Path(__file__).resolve().parent
travel_mod = load_sibling("plp.plugins.travel.travel", _HERE / "travel.py")

log = logging.getLogger("plp.travel")

#: Booking lead times used for the deterministic "what to book" section.
FLIGHTS_WEEKS = 6
LODGING_WEEKS = 3


def _season_with_llm(llm: LLMClient, preferences: str, request: str) -> dict | None:
    """One bounded LLM call → ``{"why": str, "ideas": [str, ...]}``.

    Strict-JSON demand, fence stripping, one retry, then None (degrade).
    """
    prompt = (
        "You are planning a personal trip for a couple, from their preferences.\n"
        f"PREFERENCES (their own words):\n{preferences[:1500]}\n\n"
        f"REQUEST: {request}\n\n"
        "Reply with STRICT JSON only, no markdown fences: "
        '{"why": "<=60 words on why this trip fits their stated preferences and limits", '
        '"ideas": ["3 to 5 short concrete ideas, each <=12 words"]}.'
    )
    system = (
        "You are a pragmatic travel assistant. You output only valid JSON matching "
        "the requested schema. No prose outside the JSON object."
    )
    for _attempt in range(2):
        try:
            msg = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.7,
            )
            text = (msg.get("content") or "").strip()
            # The schema is flat (no nested objects), so prefer the LAST
            # balanced `{...}` that parses — a thinking preamble (if any)
            # with its own braces won't mask the real JSON.
            data = None
            for m in re.finditer(r"\{[^{}]*\}", text):
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
            if data is None:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
            if isinstance(data, dict):
                why = str(data.get("why", "")).strip()
                ideas = [str(x).strip() for x in data.get("ideas", []) if str(x).strip()]
                if why:
                    return {"why": why, "ideas": ideas[:5]}
        except Exception as exc:  # LLMError, JSON, timeout — all degrade
            log.warning("travel brainstorm LLM step failed (attempt %d): %s", _attempt + 1, exc)
    return None


class TravelPlugin(Plugin):
    name = "travel"

    def __init__(self) -> None:
        self._store: travel_mod.TravelStore | None = None
        self._ctx = None

    # ---------------------------------------------------------------- setup

    def setup(self, ctx) -> None:
        self._ctx = ctx
        self._store = travel_mod.TravelStore(
            Vault(ctx.vault_dir(), ctx.store),
            preferences_rel=ctx.config.travel.preferences,
        )
        self._store.ensure_preferences()
        log.info("travel ready (max budget: %s, max days: %d)",
                 ctx.config.travel.max_budget or "none stated",
                 ctx.config.travel.max_trip_days)

    def _vault_store(self) -> travel_mod.TravelStore:
        assert self._store is not None, "setup() not called"
        return self._store

    # ------------------------------------------------------------- internals

    def _feasibility(self, dates: str | None, budget: float | None) -> list[str]:
        """Deterministic checks. Never blocks the doc — warnings only."""
        cfg = self._ctx.config.travel
        out = []
        parsed = travel_mod.parse_dates(dates)
        if dates and parsed is None:
            out.append(f"dates {dates!r} not understood (use YYYY-MM-DD..YYYY-MM-DD) — treating as undated")
            parsed = None
        if parsed:
            start, end, _ = parsed
            days = (end - start).days + 1
            if days > cfg.max_trip_days:
                out.append(f"trip is {days} days — above your stated max of {cfg.max_trip_days}")
            if start < date.today():
                out.append(f"start date {start} is in the past")
        if cfg.max_budget > 0 and budget is not None and budget > 0 and budget > cfg.max_budget:
            out.append(f"budget ${budget:g} is above the stated ceiling of ${cfg.max_budget:g}")
        out.append("calendar conflict check: pending (calendar plugin lands in Phase 4)")
        return out

    def _open_questions(self, dates: str | None, budget: float | None, seasoned: bool) -> list[str]:
        q = []
        if not dates:
            q.append("Which weeks are actually free? (calendar check lands in Phase 4)")
        if not budget or budget <= 0:
            q.append("What's the budget ceiling for this trip?")
        if not seasoned:
            q.append("Answer the 'why it fits' + ideas above, or re-run the brainstorm with the LLM up")
        return q

    def brainstorm(
        self, destination: str, dates: str | None = None, budget: float | None = None
    ) -> dict:
        """Build one plan doc. Returns {file, seasoned, warnings}."""
        ctx = self._ctx
        cfg = ctx.config.travel
        store = self._vault_store()

        prefs = store.preferences_text()
        eff_budget = budget if (budget and budget > 0) else (cfg.max_budget or None)
        request = (
            f"{destination}"
            + (f", dates {dates}" if dates else "")
            + (f", budget ${eff_budget:g}" if eff_budget else ", no budget stated")
        )

        llm = LLMClient(ctx.config.llm)
        seasoned_data = None
        if llm.available():
            seasoned_data = _season_with_llm(llm, prefs, request)
        seasoned = seasoned_data is not None

        why = (
            seasoned_data["why"]
            if seasoned_data
            else (
                f"Not seasoned yet (LLM unavailable at brainstorm time). Preferences on "
                f"file say: see '{store.preferences_rel}'. Fill in why {destination} fits, "
                f"or re-run the brainstorm with the LLM up."
            )
        )
        ideas = seasoned_data.get("ideas") if seasoned_data else None
        ideas_text = (
            "\n".join(f"- {i}" for i in ideas) if ideas else "- (none yet — add your own)"
        )

        parsed = travel_mod.parse_dates(dates)
        booking = []
        if parsed:
            start, _end, _ = parsed
            booking.append(
                f"- flights: look {FLIGHTS_WEEKS}+ weeks before {start} "
                f"(by ~{(start - timedelta(weeks=FLIGHTS_WEEKS)).isoformat()})"
            )
            booking.append(
                f"- lodging: book by ~{(start - timedelta(weeks=LODGING_WEEKS)).isoformat()}"
            )
        else:
            booking.append(
                f"- once dates are fixed: flights ~{FLIGHTS_WEEKS} weeks out, "
                f"lodging ~{LODGING_WEEKS} weeks out"
            )

        warnings = self._feasibility(dates, eff_budget)
        questions = self._open_questions(dates, eff_budget, seasoned)

        sections = {
            "Why it fits": why,
            "Ideas": ideas_text,
            "What to book, and when": "\n".join(booking),
            "Open questions": "\n".join(f"- {q}" for q in questions) or "- none",
            "Feasibility": "\n".join(f"- {w}" for w in warnings),
        }
        rel, _meta = store.create(destination, dates, eff_budget, sections)
        if ctx.delivery is not None:
            ctx.delivery.deliver("travel", f"Trip plan drafted → {rel}\n{why[:200]}")
        return {"file": rel, "seasoned": seasoned, "warnings": warnings}

    # ----------------------------------------------------------------- tools

    def tools(self) -> list:
        plugin = self

        @tool("Brainstorm a trip from the couple's saved preferences. Returns the "
              "plan doc path. Optional dates as 'YYYY-MM-DD..YYYY-MM-DD' and a budget.")
        def travel_brainstorm(destination: str, dates: str | None = None,
                              budget: float | None = None) -> dict:
            return plugin.brainstorm(destination, dates=dates, budget=budget)

        @tool("List trip plans. Optional status filter: brainstorm | planning | booked | done.")
        def travel_plans(status: str | None = None) -> list:
            return [
                {"file": r["file"], "destination": r.get("destination"),
                 "status": r.get("status"), "dates": r.get("dates"),
                 "budget": r.get("budget"), "created": r.get("created")}
                for r in plugin._vault_store().list(status=status)
            ]

        @tool("Change a trip plan's status: brainstorm | planning | booked | done. "
              "`plan` is the filename stem from travel_plans.")
        def travel_set_status(plan: str, status: str) -> dict:
            rel, meta = plugin._vault_store().set_status(plan, status)
            return {"file": rel, "status": meta["status"]}

        return [travel_brainstorm, travel_plans, travel_set_status]

    # ------------------------------------------------------------------ jobs

    def jobs(self) -> list[Job]:
        # Nudging existing plans ("book by X") is the Phase-5 checkup's job;
        # Phase 3 is on-demand by design (PRD.md S5: "on demand + nudge").
        return []

    # -------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        def add_arguments(sp):
            sub = sp.add_subparsers(dest="travel_action", required=True)

            b = sub.add_parser("brainstorm", help="draft a trip plan doc")
            b.add_argument("destination", help="where (or 'somewhere warm' — be as vague as you like)")
            b.add_argument("--dates", default=None, help="YYYY-MM-DD..YYYY-MM-DD")
            b.add_argument("--budget", type=float, default=None)

            li = sub.add_parser("plans", help="list trip plans")
            li.add_argument("--status", default=None, choices=list(travel_mod.STATUSES))

            sh = sub.add_parser("show", help="show one plan (file stem or path)")
            sh.add_argument("plan")

            se = sub.add_parser("set", help="change a plan's status")
            se.add_argument("plan")
            se.add_argument("status", choices=list(travel_mod.STATUSES))

            sub.add_parser("prefs", help="show the trip-preferences file (edit it freely)")

        def handler(args, ctx) -> int:
            store = self._vault_store()
            try:
                if args.travel_action == "brainstorm":
                    r = self.brainstorm(args.destination, dates=args.dates, budget=args.budget)
                    print(f"plan drafted → {r['file']}  ({'LLM-seasoned' if r['seasoned'] else 'deterministic — LLM unavailable'})")
                    for w in r["warnings"]:
                        print(f"  ! {w}")
                    return 0
                if args.travel_action == "plans":
                    rows = store.list(status=args.status)
                    if not rows:
                        print("(no trip plans yet — try: plp travel brainstorm \"Hawaii\")")
                        return 0
                    print(f"{'STATUS':<11} {'DATES':<27} {'BUDGET':>8}  FILE")
                    for r in rows:
                        bud = f"${r['budget']:g}" if r.get("budget") else "-"
                        print(f"{r.get('status', '?'):<11} {str(r.get('dates', '-')):<27} "
                              f"{bud:>8}  {r['file']}")
                    return 0
                if args.travel_action == "show":
                    got = store.find(args.plan)
                    if got is None:
                        print(f"trip plan not found: {args.plan}", file=sys.stderr)
                        return 1
                    rel, meta, body = got
                    print(f"--- {rel} ---")
                    for k, v in meta.items():
                        if k != "kind":
                            print(f"{k}: {v}")
                    print(body.rstrip())
                    return 0
                if args.travel_action == "set":
                    rel, meta = store.set_status(args.plan, args.status)
                    print(f"{rel} → {meta['status']}")
                    return 0
                if args.travel_action == "prefs":
                    p = ctx.vault_dir() / store.preferences_rel
                    print(f"--- {p} (edit freely; the planner re-reads it each brainstorm) ---")
                    print(store.preferences_text())
                    return 0
            except (ValueError, VaultConflict, KeyError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 2

        return [
            Command(
                name="travel",
                help="holiday planner: brainstorm / plans / show / set / prefs",
                add_arguments=add_arguments,
                handler=handler,
            )
        ]
