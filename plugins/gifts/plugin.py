"""Gifts plugin — Phase 3 (PRD.md S4: gift vault).

Capture present ideas for the wife with minimal friction, keep them visible
until they stop being ideas:

- ``gift add`` / tools: one line of text → a vault file
  (``gifts/<date>-<slug>.md``) with occasion, budget, status, notes.
- ``gift list`` / ``gift show`` / ``gift set``: the lifecycle
  ``idea → shortlist → bought → given``.
- ``gifts.review`` (Sunday 19:00, ahead of the Phase-5 checkup): upcoming
  configured occasions, what's in flight per occasion, and ideas that have
  gone stale — deterministic, no LLM (this is the degraded mode by design;
  the checkup in Phase 5 adds the prose).

Gifts live in the markdown vault, so the owner can edit any of them in an
editor or Obsidian; a human edit wins over any daemon write (kernel vault).
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

from plp.kernel.config import resolve
from plp.kernel.plugin import Command, Job, Plugin, load_sibling, tool
from plp.kernel.vault import Vault, VaultConflict

_HERE = Path(__file__).resolve().parent
gifts_mod = load_sibling("plp.plugins.gifts.gifts", _HERE / "gifts.py")

log = logging.getLogger("plp.gifts")


class GiftsPlugin(Plugin):
    name = "gifts"

    def __init__(self) -> None:
        self._store: gifts_mod.GiftStore | None = None
        self._cfg = None

    # ---------------------------------------------------------------- setup

    def setup(self, ctx) -> None:
        self._cfg = ctx.config.gifts
        self._store = gifts_mod.GiftStore(Vault(ctx.vault_dir(), ctx.store))
        log.info(
            "gifts ready (occasions: %d, review window: %dd)",
            len(self._cfg.occasions),
            self._cfg.review_window_days,
        )

    def _gifts(self) -> gifts_mod.GiftStore:
        assert self._store is not None, "setup() not called"
        return self._store

    # ----------------------------------------------------------------- tools

    def tools(self) -> list:
        store = self._store

        @tool("Capture a gift idea for the wife. occasion is a short label "
              "(e.g. 'birthday', 'anniversary', 'just because'); budget is "
              "an optional number. Returns the vault file created.")
        def gift_add(idea: str, occasion: str = "just because", budget: float = 0.0) -> dict:
            rel, meta = store.add(idea, occasion=occasion, budget=budget or None)
            return {"file": rel, "idea": meta["idea"], "occasion": meta["occasion"],
                    "status": meta["status"]}

        @tool("List gift ideas. Optional filters: occasion label, and status "
              "(idea | shortlist | bought | given).")
        def gifts_list(occasion: str | None = None, status: str | None = None) -> list:
            rows = store.list(occasion=occasion, status=status)
            return [
                {"file": r["file"], "idea": r.get("idea"), "occasion": r.get("occasion"),
                 "status": r.get("status"), "budget": r.get("budget"),
                 "created": r.get("created")}
                for r in rows
            ]

        @tool("Change a gift's lifecycle status: idea | shortlist | bought | given. "
              "`gift` is the filename stem shown by gifts_list. price is only "
              "meaningful when setting 'bought'.")
        def gift_set_status(gift: str, status: str, price: float | None = None) -> dict:
            rel, meta = store.set_status(gift, status, price=price)
            return {"file": rel, "status": meta["status"],
                    "bought_at": meta.get("bought_at"), "given_at": meta.get("given_at")}

        @tool("Upcoming configured occasions (birthdays, anniversaries) within the "
              "next N days, with how many gifts are still in flight for each. "
              "Returns a JSON array.")
        def upcoming(days: int = 90) -> list:
            today = dt.datetime.now().date()
            all_gifts = store.list()
            out = []
            for occ in self._cfg.occasions:
                nxt = gifts_mod.next_occurrence(occ.month, occ.day, today, days)
                if nxt is None:
                    continue
                inflight = [
                    g
                    for g in all_gifts
                    if g.get("occasion") == occ.name
                    and g.get("status") in ("idea", "shortlist")
                ]
                out.append(
                    {
                        "occasion": occ.name,
                        "date": nxt.isoformat(),
                        "in_days": (nxt - today).days,
                        "in_flight": len(inflight),
                    }
                )
            return out

        return [gift_add, gifts_list, gift_set_status, upcoming]

    # ------------------------------------------------------------------ jobs

    def jobs(self) -> list[Job]:
        def _review(ctx, args: dict) -> dict:
            cfg = ctx.config.gifts
            now = ctx.config.default_now_factory()()
            today = now.date()
            all_gifts = self._gifts().list()

            lines = [f"Gifts review — {today.isoformat()}"]

            upcoming = []
            for occ in cfg.occasions:
                nxt = gifts_mod.next_occurrence(occ.month, occ.day, today, cfg.review_window_days)
                if nxt is not None:
                    upcoming.append((nxt, occ))
            if not cfg.occasions:
                lines.append(
                    f"No occasions configured — add her birthday/anniversary under "
                    f"gifts.occasions in config/plp.yaml so upcoming ones surface here."
                )
            elif upcoming:
                lines.append(f"Upcoming occasions (next {cfg.review_window_days} days):")
                for nxt, occ in sorted(upcoming):
                    inflight = [
                        g for g in all_gifts
                        if g.get("occasion") == occ.name and g.get("status") in ("idea", "shortlist")
                    ]
                    lines.append(
                        f"  - {occ.name}: {nxt.isoformat()} (in {(nxt - today).days} days)"
                        f" — {len(inflight)} gift(s) in flight"
                    )
            else:
                lines.append(f"No configured occasions in the next {cfg.review_window_days} days.")

            inflight = [g for g in all_gifts if g.get("status") in ("idea", "shortlist")]
            if inflight:
                lines.append("In flight:")
                for g in sorted(inflight, key=lambda r: r.get("created", "")):
                    age = max(0, (today - self._parse_date(g.get("created"))).days)
                    extra = f", budget ${g['budget']:g}" if g.get("budget") else ""
                    lines.append(
                        f"  [{g.get('status')}] {g['file'].rsplit('/', 1)[-1]}"
                        f" — {g.get('occasion')}{extra} ({age}d old)"
                    )
            else:
                lines.append("Nothing in flight.")

            stale = [
                g for g in inflight
                if g.get("status") == "idea"
                and (today - self._parse_date(g.get("created"))).days > cfg.stale_after_days
            ]
            if stale:
                lines.append(
                    f"Stale ideas ({cfg.stale_after_days}+ days, still 'idea') — "
                    f"shortlist them or let them go: "
                    + ", ".join(g["file"].rsplit("/", 1)[-1] for g in stale)
                )

            text = "\n".join(lines)
            delivered = False
            if ctx.delivery is not None:
                ctx.delivery.deliver("gifts", text)
                delivered = True
            ctx.store.save_digest("gifts", text)
            return {
                "delivered": delivered,
                "occasions_upcoming": [
                    {"occasion": o.name, "date": n.isoformat()} for n, o in upcoming
                ],
                "in_flight": len(inflight),
                "stale": len(stale),
            }

        return [
            Job(
                name="gifts.review",
                handler=_review,
                cron="0 19 * * 0",  # Sunday 19:00, ahead of the checkup (Phase 5)
                staleness_h=48.0,
                plugin=self.name,
            )
        ]

    @staticmethod
    def _parse_date(iso: str | None):
        from datetime import date, datetime

        if not iso:
            return date.today()
        try:
            return datetime.fromisoformat(iso).date()
        except ValueError:
            return date.today()

    # -------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        def add_arguments(sp):
            sub = sp.add_subparsers(dest="gift_action", required=True)

            a = sub.add_parser("add", help="capture a gift idea")
            a.add_argument("idea", help="the idea, one line")
            a.add_argument("--occasion", default="just because")
            a.add_argument("--budget", type=float, default=None)
            a.add_argument("--notes", default=None)

            li = sub.add_parser("list", help="list gift ideas")
            li.add_argument("--occasion", default=None)
            li.add_argument("--status", default=None, choices=list(gifts_mod.STATUSES) or None)

            sh = sub.add_parser("show", help="show one gift (file stem or path)")
            sh.add_argument("gift")

            se = sub.add_parser("set", help="change a gift's status")
            se.add_argument("gift")
            se.add_argument("status", choices=list(gifts_mod.STATUSES))
            se.add_argument("--price", type=float, default=None)

        def handler(args, ctx) -> int:
            store = self._gifts()
            try:
                if args.gift_action == "add":
                    rel, meta = store.add(
                        args.idea, occasion=args.occasion, budget=args.budget,
                        notes=args.notes or "",
                    )
                    print(f"gift saved → {rel}")
                    print(f"  occasion {meta['occasion']} · status {meta['status']}"
                          + (f" · budget ${meta['budget']:g}" if meta.get("budget") else ""))
                    return 0
                if args.gift_action == "list":
                    rows = store.list(occasion=args.occasion, status=args.status)
                    if not rows:
                        print("(no gifts match)")
                        return 0
                    print(f"{'STATUS':<10} {'OCCASION':<16} {'BUDGET':>7}  FILE")
                    for r in rows:
                        bud = f"${r['budget']:g}" if r.get("budget") else "-"
                        print(f"{r.get('status', '?'):<10} {str(r.get('occasion', '-')):<16} "
                              f"{bud:>7}  {r['file']}")
                    return 0
                if args.gift_action == "show":
                    got = store.find(args.gift)
                    if got is None:
                        print(f"gift not found: {args.gift}", flush=True)
                        return 1
                    rel, meta, body = got
                    print(f"--- {rel} ---")
                    for k, v in meta.items():
                        if k != "kind":
                            print(f"{k}: {v}")
                    print(body.rstrip())
                    return 0
                if args.gift_action == "set":
                    rel, meta = store.set_status(args.gift, args.status, price=args.price)
                    print(f"{rel} → {meta['status']}")
                    return 0
            except (ValueError, VaultConflict, KeyError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 2

        return [
            Command(
                name="gift",
                help="gift vault: add / list / show / set (idea → shortlist → bought → given)",
                add_arguments=add_arguments,
                handler=handler,
            )
        ]
