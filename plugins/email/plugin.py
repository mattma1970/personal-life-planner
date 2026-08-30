"""Email scanner plugin (Phase 6, PRD.md §4 S2).

Read-only Gmail triage, daily:

- ``email.scan`` job — fetches the look-back window, runs the deterministic
  triage (``triage.py``), surfaces needs-reply / life-relevant / date /
  RSVP / birthday items, and turns concretely extractable dates into
  **calendar proposals** (approve-don't-command, via the approvals queue).
- LLM thread summarization is **opt-in** (``features.email_summarization``);
  off by default, and it seasons the report only — the deterministic floor
  ships regardless.
- Graceful no-op without credentials: the job logs and returns, no crash,
  no noise in the digest stream.

``plp email connect`` runs the one-time OAuth flow (``gmail.py``); the
refresh token lives in a local gitignored file, scope is ``gmail.readonly``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from plp.kernel.agent import Scenario
from plp.kernel.config import resolve
from plp.kernel.context import PluginContext
from plp.kernel.plugin import Command, Job, Plugin, load_sibling, tool

_HERE = Path(__file__).resolve().parent
log = logging.getLogger("plp.email")

_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """CREATE TABLE IF NOT EXISTS email_seen (
            message_id TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            proposed INTEGER NOT NULL DEFAULT 0,
            subject TEXT
        )""",
    ),
]

#: Narrow LLM summary schema (opt-in). The deterministic floor never needs it.
SUMMARY_SCHEMA: dict = {
    "type": "object",
    "required": ["summary", "action"],
    "properties": {
        "summary": {"type": "string"},
        "action": {"type": "string"},
    },
    "additionalProperties": False,
}

_SUMMARY_PROMPT = (
    "You are a personal assistant reviewing this run's triaged email items "
    "(given in context). Write ONE compact summary (max 500 characters) of "
    "what matters for the owner's life — not a mail list — and suggest at "
    "most ONE concrete life action (e.g. reply to X, add the date to the "
    "calendar). If nothing warrants action, set action to \"none\". "
    "Answer JSON matching the schema exactly; no prose outside JSON."
)


class EmailPlugin(Plugin):
    name = "email"

    def __init__(self) -> None:
        # Sibling modules load without a config; the config arrives in setup()
        # (same convention as the other plugins — discovery constructs bare).
        self._cfg = None
        self._gmail = load_sibling("plp.plugins.email.gmail", _HERE / "gmail.py")
        self._triage = load_sibling("plp.plugins.email.triage", _HERE / "triage.py")
        self._last_items: list = []

    # ---------------------------------------------------------------- setup

    def setup(self, ctx: PluginContext) -> None:
        self._cfg = ctx.config
        ctx.store.migrate_for(self.name, _MIGRATIONS)
        ec = self._cfg.email
        creds = resolve(ctx.config, ec.credentials_file) if ec.credentials_file else None
        if creds is None or not creds.exists():
            log.info(
                "email: Gmail not configured — email.scan will no-op "
                "(run: plp email connect --credentials <google-credentials.json>)"
            )
        elif not self._gmail.load_token(resolve(ctx.config, ec.token_file)):
            log.info("email: credentials present but no token yet — run: plp email connect")
        else:
            log.info("email: Gmail connected (read-only), daily scan at %s", ec.scan_cron)

    # ------------------------------------------------------------------ jobs

    def jobs(self) -> list[Job]:
        ec = self._cfg.email
        return [
            Job(
                name="email.scan",
                handler=self._scan,
                cron=ec.scan_cron,
                timeout_s=600,
                staleness_h=24,
                plugin=self.name,
            )
        ]

    # ----------------------------------------------------------------- scan

    def _credentials(self, ctx: PluginContext) -> Path | None:
        """The resolved credentials path, or None when Gmail isn't configured."""
        ec = self._cfg.email
        if not ec.credentials_file:
            return None
        p = resolve(ctx.config, ec.credentials_file)
        return p if p.exists() else None

    def _no_op(self, ctx: PluginContext, reason: str, result: dict) -> dict:
        """Publish a no-op scan event (and its log line) and return the result."""
        log.info("email.scan: %s", reason)
        payload = {"scanned": 0, "flagged": 0, "noise": 0, "proposed": [], "seasoned": False, "no_credentials": True, **result}
        ctx.bus.publish("email.scan", payload)
        return payload

    def _scan(self, ctx: PluginContext, args: dict) -> dict:
        ec = self._cfg.email
        now = args.get("now") or ctx.config.default_now_factory()()
        days = int(args.get("days") or ec.scan_days)

        creds_path = self._credentials(ctx)
        if creds_path is None:
            return self._no_op(
                ctx,
                "Gmail not configured (email.credentials_file empty or missing) — no-op "
                "(plp email connect --credentials <json>)",
                {"status": "no_credentials"},
            )

        token_file = resolve(ctx.config, ec.token_file)
        try:
            token = self._gmail.get_access_token(token_file, creds_path, log)
        except Exception as exc:  # network / auth — degrade, don't crash the day
            log.error("email.scan: token refresh failed: %s", exc)
            ctx.bus.publish("email.scan", {"scanned": 0, "flagged": 0, "noise": 0, "proposed": [], "seasoned": False, "error": str(exc)})
            return {"status": "auth_error", "scanned": 0, "flagged": 0, "noise": 0, "proposed": [], "seasoned": False}
        if token is None:
            return self._no_op(
                ctx,
                "no token file (connect never completed) — no-op (plp email connect)",
                {"status": "no_credentials"},
            )

        client = self._gmail.GmailClient(self._gmail.GMAIL_API_BASE, lambda: token)

        seen = {
            r["message_id"]: r for r in ctx.store.query_json("SELECT message_id, proposed FROM email_seen")
        }
        ids = client.search(f"newer_than:{days}d", max_results=ec.max_items)

        items: list = []
        noise = 0
        for mid in ids:
            msg = client.fetch(mid)
            item = self._triage.triage_message(
                msg.id,
                msg.sender,
                msg.subject,
                msg.date,
                msg.body,
                now,
                tuple(ec.life_keywords),
            )
            known = seen.get(mid)
            if known is None:
                ctx.store.execute(
                    "INSERT INTO email_seen(message_id, first_seen, proposed, subject) VALUES(?,?,?,?)",
                    (mid, now.isoformat(timespec="seconds"), 0, item.subject[:120]),
                )
                seen[mid] = {"message_id": mid, "proposed": 0}
            if not item.interesting:
                noise += 1
                continue
            items.append(item)
        self._last_items = items

        # Propose calendar blocks — only for first-seen messages (a message
        # never changes; a re-scan must not re-propose the same date).
        proposed_ids: list[int] = []
        for item in items:
            if seen[item.message_id]["proposed"]:
                continue
            payload = self._triage.propose_from_item(item)
            if payload is None:
                continue
            aid = ctx.approvals.propose("calendar_block", payload, note="email scan")
            proposed_ids.append(aid)
            ctx.store.execute(
                "UPDATE email_seen SET proposed=1 WHERE message_id=?", (item.message_id,)
            )
            seen[item.message_id]["proposed"] = 1
            item.proposed_id = aid  # type: ignore[attr-defined]

        summary = None
        seasoned = False
        want_llm = args.get("llm")
        if want_llm is None:
            want_llm = ctx.config.features.email_summarization
        if want_llm and ctx.agent is not None and items:
            summary = self._season(ctx, items)
            seasoned = summary is not None

        text = self._render(items, noise, len(ids), days, now, summary, seasoned)
        if ctx.delivery is not None:
            ctx.delivery.deliver("email", text)
        ctx.store.save_digest("email", text)
        ctx.bus.publish(
            "email.scan",
            {
                "scanned": len(ids),
                "flagged": len(items),
                "noise": noise,
                "proposed": proposed_ids,
                "seasoned": seasoned,
            },
        )
        return {
            "status": "ok",
            "scanned": len(ids),
            "flagged": len(items),
            "noise": noise,
            "proposed": proposed_ids,
            "seasoned": seasoned,
        }

    # --------------------------------------------------------------- season

    def _season(self, ctx: PluginContext, items: list) -> dict | None:
        """Opt-in LLM summary of this run's flagged items (one retry, then the
        deterministic floor ships without it — PRD.md §6.5)."""
        if ctx.agent is None:
            return None
        scenario = Scenario(
            name="email_summarize",
            system_prompt=_SUMMARY_PROMPT,
            tools=["calendar.calendar_list"],
            context_fn=lambda _c: json.dumps(
                [
                    {
                        "subject": i.subject,
                        "from": i.sender,
                        "date": i.date.isoformat(timespec="seconds") if i.date else None,
                        "flags": i.flags,
                        "event": i.event.isoformat(timespec="seconds") if i.event else None,
                        "rsvp_by": i.rsvp_by.isoformat(timespec="seconds") if i.rsvp_by else None,
                    }
                    for i in items
                ],
                ensure_ascii=False,
            ),
            output_schema=SUMMARY_SCHEMA,
        )
        result = ctx.agent.run_turn(scenario, ctx, "Summarize these triaged email items.")
        if not result.get("ok") or result.get("structured") is None:
            err = result.get("schema_error") or result.get("reason") or "unavailable"
            result = ctx.agent.run_turn(
                scenario,
                ctx,
                f"Your previous answer was not accepted ({err}). Reply again with valid JSON only.",
            )
        if not result.get("ok") or result.get("structured") is None:
            log.warning(
                "email summary degraded (%s); shipping the deterministic report",
                result.get("schema_error") or result.get("reason") or "unavailable",
            )
            return None
        s = result["structured"]
        return {"summary": str(s.get("summary", ""))[:600], "action": str(s.get("action", "none"))[:200]}

    # ---------------------------------------------------------------- render

    @staticmethod
    def _line(item) -> str:
        if "date" in item.flags or "birthday" in item.flags:
            kind = "BIRTHDAY" if "birthday" in item.flags else "DATE"
            when = f" — {item.event:%Y-%m-%d %H:%M}" if item.event else ""
            prop = f" → proposal #{item.proposed_id}" if getattr(item, "proposed_id", None) else ""
            return f"· {kind:12} {item.subject[:48]:50} — {item.sender[:32]}{when}{prop}"
        if "rsvp" in item.flags:
            return f"· {'RSVP':12} {item.subject[:48]:50} — {item.sender[:32]} (by {item.rsvp_by:%Y-%m-%d})"
        if "needs_reply" in item.flags:
            return f"· {'NEEDS REPLY':12} {item.subject[:48]:50} — {item.sender[:32]}"
        return f"· {'LIFE':12} {item.subject[:48]:50} — {item.sender[:32]}"

    def _render(
        self,
        items: list,
        noise: int,
        scanned: int,
        days: int,
        now: dt.datetime,
        summary: dict | None,
        seasoned: bool,
    ) -> str:
        lines = [f"Email triage — {now:%Y-%m-%d} (last {days} day(s))", ""]
        if not items:
            lines.append(f"  nothing worth surfacing ({scanned} scanned, {noise} noise)")
        else:
            lines.append(f"  {len(items)} of {scanned} scanned worth surfacing ({noise} noise):")
            lines += ["  " + self._line(i) for i in items]
        if summary and summary.get("summary"):
            lines += ["", "> " + summary["summary"].replace("\n", " "), "> suggested action: " + summary.get("action", "none")]
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------------- tools

    def tools(self) -> list:
        @tool(
            "The current run's triaged email items (life-relevant or needs-reply, "
            "no bodies): JSON array of {subject, from, date, flags, event, rsvp_by}."
        )
        def last_scan() -> str:
            return json.dumps(
                [
                    {
                        "subject": i.subject,
                        "from": i.sender,
                        "date": i.date.isoformat(timespec="seconds") if i.date else None,
                        "flags": i.flags,
                        "event": i.event.isoformat(timespec="seconds") if i.event else None,
                        "rsvp_by": i.rsvp_by.isoformat(timespec="seconds") if i.rsvp_by else None,
                    }
                    for i in self._last_items
                ],
                ensure_ascii=False,
            )

        return [last_scan]

    # --------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        def email_cmd(a, ctx) -> int:
            if a.email == "scan":
                args: dict = {"days": a.days, "llm": None if a.llm is None else a.llm}
                if a.no_llm:
                    args["llm"] = False
                if a.now:
                    args["now"] = dt.datetime.fromisoformat(a.now)
                result = self._scan(ctx, args)
                if result["status"] == "no_credentials":
                    print("Gmail not connected — nothing to do (plp email connect --credentials <json>).")
                    return 0
                text = self._render(
                    self._last_items,
                    result["noise"],
                    result["scanned"],
                    int(a.days or self._cfg.email.scan_days),
                    args.get("now") or ctx.config.default_now_factory()(),
                    None,
                    result.get("seasoned", False),
                )
                print(text, end="")
                print(f"\n({len(result['proposed'])} calendar proposal(s): {result['proposed']})")
                return 0
            if a.email == "recent":
                creds_path = self._credentials(ctx)
                if creds_path is None:
                    print("Gmail not connected (plp email connect --credentials <json>).")
                    return 1
                token = self._gmail.get_access_token(resolve(ctx.config, self._cfg.email.token_file), creds_path, log)
                if token is None:
                    print("No token — run: plp email connect")
                    return 1
                client = self._gmail.GmailClient(
                    self._gmail.GMAIL_API_BASE, lambda: token
                )
                now = ctx.config.default_now_factory()()
                for mid in client.search(f"newer_than:{a.days}d", max_results=a.limit):
                    m = client.fetch(mid)
                    item = self._triage.triage_message(
                        m.id, m.sender, m.subject, m.date, m.body, now
                    )
                    when = f"{m.date:%Y-%m-%d}" if m.date else "?"
                    flags = ",".join(item.flags) or "-"
                    print(f"{when}  [{flags:32}] {m.subject[:60]}  <{m.sender[:40]}>")
                return 0
            if a.email == "connect":
                ec = self._cfg.email
                if a.credentials:
                    creds_path = Path(a.credentials)
                elif ec.credentials_file:
                    creds_path = resolve(ctx.config, ec.credentials_file)
                else:
                    print(
                        "no credentials path — pass --credentials <google-credentials.json> "
                        "or set email.credentials_file in config/plp.yaml "
                        "(create the OAuth client per docs/email-gmail-setup.md)"
                    )
                    return 1
                if not creds_path.exists():
                    print(f"credentials file not found: {creds_path}")
                    return 1
                receipt = self._gmail.connect(
                    creds_path,
                    resolve(ctx.config, ec.token_file),
                    open_browser=not a.no_browser,
                    code=a.code,
                )
                if receipt.get("status") == "error":
                    print("connect failed: " + receipt.get("error", "?"))
                    return 1
                print(f"Gmail connected (read-only) — token: {receipt.get('token_file')}")
                if not ec.credentials_file or a.credentials:
                    print("next: set email.credentials_file in config/plp.yaml to enable daily email.scan")
                return 0
            return 1

        def add_arguments(parser) -> None:
            sub = parser.add_subparsers(dest="email", required=True)
            p_scan = sub.add_parser("scan", help="run a triage scan now (same as the email.scan job)")
            p_scan.add_argument("--days", type=int, default=0, help="look-back days (0 = config default)")
            p_scan.add_argument("--llm", dest="llm", action="store_true", default=None, help="force the opt-in LLM summary on")
            p_scan.add_argument("--no-llm", dest="no_llm", action="store_true", help="skip the LLM summary even if configured")
            p_scan.add_argument("--now", default=None, help="ISO now (tests/replay)")
            p_recent = sub.add_parser("recent", help="peek at raw mail from the last N days")
            p_recent.add_argument("--days", type=int, default=2)
            p_recent.add_argument("--limit", type=int, default=25)
            p_connect = sub.add_parser("connect", help="one-time read-only Gmail OAuth connect")
            p_connect.add_argument("--credentials", default=None, help="path to the Google OAuth client JSON")
            p_connect.add_argument("--no-browser", action="store_true")
            p_connect.add_argument("--code", default=None, help="pre-fetched authorization code (skips the redirect wait)")

        return [
            Command(name="email", help="email scanner (scan / recent / connect)", handler=email_cmd, add_arguments=add_arguments),
        ]
