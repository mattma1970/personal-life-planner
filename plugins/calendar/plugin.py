"""Calendar steward plugin (PRD.md §8 Phase 4).

The calendar is the spine: category-tagged events are both the delivery
mechanism (a proposed block of time) and the measurement instrument (Phase 5
aggregates hours by category into the scorecard).

Propose-don't-command (PRD.md §2): nothing a model turn touches writes to the
calendar directly. Model paths call :func:`calendar_propose`, which files a
``calendar_block`` approval; when the human approves (``plp approve <id>``),
an ``approval.*.approved`` bus event reaches this plugin's subscriber, which
executes the write through :class:`plp.kernel.host.HostService` (audited as
``host.calendar_write``). Direct CLI writes (``plp calendar add/rm``) are the
human commanding — still routed through the host, so every mutation is
audited the same way. The LLM is not involved at all in v1.
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta

from plp.kernel.calendar import CalendarEvent, open_calendar_store
from plp.kernel.plugin import Command, Plugin, tool

# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _parse_when(spec: str, now: datetime) -> tuple[datetime, datetime]:
    """Tolerant when-parsing for proposed blocks: ISO datetime, date (all-day),
    "tomorrow HH:MM", "today HH:MM", or bare "HH:MM" (today). Returns
    (start, end) with a default one-hour duration."""
    s = (spec or "").strip()
    if not s:
        raise ValueError(f"empty time spec: {spec!r}")
    low = s.lower()
    for prefix, day in (("today", now), ("tomorrow", now + timedelta(days=1))):
        if low.startswith(prefix):
            rest = s[len(prefix):].strip()
            if not rest:
                return day.replace(hour=9, minute=0, second=0, microsecond=0), day.replace(
                    hour=10, minute=0, second=0, microsecond=0
                )
            hhmm = _parse_hhmm(rest)
            start = day.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
            return start, start + timedelta(hours=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None
    if dt is not None:
        if s in (dt.date().isoformat(),):  # "YYYY-MM-DD" → all-day
            return dt.replace(hour=0, minute=0, second=0, microsecond=0), dt.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
        if dt.time() == time(0, 0) and ":" not in s:
            return dt, dt + timedelta(days=1)
        if dt.time() == time(0, 0):
            # "YYYY-MM-DDTHH:MM" style midnight — keep as a timed event
            return dt, dt + timedelta(hours=1)
        return dt, dt + timedelta(hours=1)
    hhmm = _parse_hhmm(s)
    start = now.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return start, start + timedelta(hours=1)


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = [p for p in s.replace("h", ":").replace("am", "").replace("pm", "").split(":") if p]
    if len(parts) < 1 or len(parts) > 2:
        raise ValueError(f"unparseable time: {s!r}")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range: {s!r}")
    return h, m


def _event_from_payload(p: dict) -> CalendarEvent:
    """Build a CalendarEvent from an approval/CLI payload:
    ``{title, start?, end?, when?, category?, notes?}``.
    ``when`` is the tolerant form (used by proposals); explicit
    start/end (ISO) always win."""
    title = p.get("title") or "(untitled)"
    category = p.get("category") or "personal"
    notes = p.get("notes") or ""
    now = datetime.now()
    start_s = p.get("start")
    end_s = p.get("end")
    if start_s:
        start = datetime.fromisoformat(str(start_s))
        if end_s:
            end = datetime.fromisoformat(str(end_s))
        else:
            end = start + timedelta(hours=1)
        if end <= start and start.time() == time(0, 0):
            end = start + timedelta(days=1)
    else:
        start, end = _parse_when(str(p.get("when") or ""), now)
    return CalendarEvent(title=title, start=start, end=end, category=category, notes=notes)


# --------------------------------------------------------------------------
# Google connect (Phase-4 scaffold, docs/google-calendar-setup.md)
# --------------------------------------------------------------------------

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?client_id={cid}&redirect_uri={redir}&response_type=code"
    "&scope=https://www.googleapis.com/auth/calendar.events"
    "&access_type=offline&prompt=consent&state=plp-connect"
)
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CAL_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


def connect_google(
    credentials_path,
    open_browser: bool = True,
    code: str | None = None,
    calendar_id: str | None = None,
    log=None,
) -> dict:
    """One-time Google Calendar connect: OAuth2 authorization-code flow with a
    local-loopback redirect, then write the refresh token + primary calendar
    id back into the credentials file. ``code`` pre-filled skips the redirect
    wait (manual paste / tests). Returns a receipt dict."""
    import json as _json
    import socket
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    if log is None:
        import logging

        log = logging.getLogger("plp.calendar.connect")

    if credentials_path is None or not credentials_path.exists():
        return {
            "status": "error",
            "error": f"credentials file not found: {credentials_path} — "
            "follow docs/google-calendar-setup.md steps 1-4 first",
        }
    # Raw GCP exports nest the client under "installed"; accept both shapes.
    doc = _json.loads(credentials_path.read_text())
    base = dict(doc)
    if "installed" in doc and isinstance(doc["installed"], dict):
        base.update(doc["installed"])
    for key in ("client_id", "client_secret"):
        if not base.get(key):
            return {"status": "error", "error": f"missing {key!r} in {credentials_path}"}

    # Free loopback port for the one-shot redirect listener.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redir = f"http://127.0.0.1:{port}/"

    received: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", ["?"])[0] == "plp-connect" and q.get("code"):
                received["code"] = q["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>PLP connected - you can close this tab.</h2></body></html>"
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *a):  # silence
            pass

    url = GOOGLE_AUTH_URL.format(cid=base["client_id"], redir=redir)
    if code is None:
        server = HTTPServer(("127.0.0.1", port), _Handler)
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        if open_browser:
            webbrowser.open(url)
        log.info("Google connect: open this URL to authorize:\n%s", url)
        t.join(timeout=300)
        server.server_close()
        if "code" not in received:
            return {"status": "error", "error": "no authorization code received (timeout or rejection)"}
        code = received["code"]

    import httpx

    r = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": base["client_id"],
            "client_secret": base["client_secret"],
            "redirect_uri": redir,
        },
        timeout=30.0,
    )
    r.raise_for_status()
    tokens = r.json()
    if "refresh_token" not in tokens:
        return {
            "status": "error",
            "error": "Google did not return a refresh token "
            "(already authorized? revoke access in your Google account, then retry)",
        }

    if not calendar_id:
        r = httpx.get(
            GOOGLE_CAL_LIST_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=30.0,
        )
        r.raise_for_status()
        primary = [
            i for i in r.json().get("items", []) if i.get("primary") or i.get("id") == "primary"
        ]
        calendar_id = primary[0]["id"] if primary else (r.json().get("items") or [{}])[0].get("id")

    out = {
        **base,
        "refresh_token": tokens["refresh_token"],
        "calendar_id": calendar_id or "primary",
    }
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile as _tmp

    fd, tmp = _tmp.mkstemp(dir=str(credentials_path.parent), prefix=".creds-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        _json.dump(out, f, indent=2)
    os.replace(tmp, credentials_path)
    return {"status": "ok", "calendar_id": out["calendar_id"], "credentials_file": str(credentials_path)}


# --------------------------------------------------------------------------
# Plugin
# --------------------------------------------------------------------------


class CalendarPlugin(Plugin):
    name = "calendar"

    def setup(self, ctx) -> None:
        self._ctx = ctx
        self._store = open_calendar_store(ctx.config, ctx.log)
        if ctx.host is not None:
            ctx.host.register("calendar_write", self._host_write)
        ctx.bus.subscribe("approval.", self._on_approval)

    # ------------------------------------------------- approval → side effect

    def _on_approval(self, event: str, payload: dict) -> None:
        """The propose-don't-command join point: ``approval.<id>.approved``
        with kind ``calendar_block`` → host write (audited)."""
        if not event.endswith(".approved") or "." not in event:
            return
        parts = event.split(".")
        if len(parts) < 3 or not parts[1].isdigit():
            return
        if self._ctx.approvals is None:
            return
        row = self._ctx.approvals.get(int(parts[1]))
        if not row or row.get("kind") != "calendar_block" or row.get("status") != "approved":
            return
        if self._ctx.host is None:
            self._ctx.log("calendar: approval #%s but no host attached", row.get("id"))
            return
        self._ctx.host.call("calendar_write", self._ctx.capability, **row["payload"])

    def _host_write(self, **kw) -> dict:
        """Registered executor for the privileged ``calendar_write`` action.
        Payload: ``op`` create|update|delete plus event fields
        (title/start/end/when/category/notes[/uid]); see _event_from_payload."""
        op = kw.get("op", "create")
        if op == "delete":
            if not kw.get("uid") or not self._store.delete(kw["uid"]):
                return {"status": "error", "error": f"no calendar event with uid {kw.get('uid')!r}"}
            return {"status": "ok", "op": "delete", "uid": kw["uid"]}
        if op == "update":
            if not kw.get("uid"):
                return {"status": "error", "error": "update requires uid"}
            fields = self._fields_from_payload(kw)
            if not fields:
                return {"status": "error", "error": "no fields to update"}
            updated = self._store.update(kw["uid"], **fields)
            if updated is None:
                return {"status": "error", "error": f"no calendar event with uid {kw['uid']!r}"}
            return {"status": "ok", "op": "update", "uid": updated.uid, "title": updated.title}
        event = _event_from_payload(kw)
        try:
            created = self._store.create(event)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "op": "create", "uid": created.uid, "title": created.title}

    @staticmethod
    def _fields_from_payload(kw: dict) -> dict:
        """The subset of event fields actually present in an update payload."""
        fields: dict = {}
        if kw.get("title"):
            fields["title"] = kw["title"]
        if kw.get("when") or (kw.get("start") and kw.get("end")):
            e = _event_from_payload(kw)
            fields["start"], fields["end"] = e.start, e.end
        elif kw.get("start"):
            start = datetime.fromisoformat(str(kw["start"]))
            end = (
                datetime.fromisoformat(str(kw["end"]))
                if kw.get("end")
                else start + timedelta(hours=1)
            )
            fields["start"], fields["end"] = start, end
        if kw.get("category"):
            fields["category"] = kw["category"]
        if kw.get("notes") is not None:
            fields["notes"] = kw["notes"]
        return fields

    # --------------------------------------------------------------- tools

    def tools(self) -> list:
        store = self._store

        @tool("List calendar events in a time window. start/end are ISO strings "
              "(e.g. '2026-09-01T09:00' or '2026-09-01'). Optional category filter.")
        def calendar_list(start: str, end: str, category: str | None = None) -> list:
            lo = datetime.fromisoformat(start)
            hi = datetime.fromisoformat(end)
            if hi.time() == time(0, 0) and ":" not in end:
                hi += timedelta(days=1)
            return [
                {
                    "title": e.title,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "category": e.category,
                    "notes": e.notes,
                    "uid": e.uid,
                }
                for e in store.list(lo, hi, category=category)
            ]

        @tool("Find calendar events that would conflict with a proposed window "
              "(ISO start/end). Use before proposing time so the human isn't "
              "asked to approve something already busy.")
        def calendar_conflicts(start: str, end: str) -> list:
            lo = datetime.fromisoformat(start)
            hi = datetime.fromisoformat(end)
            if hi.time() == time(0, 0) and ":" not in end:
                hi += timedelta(days=1)
            return [
                {
                    "title": e.title,
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "category": e.category,
                    "uid": e.uid,
                }
                for e in store.conflicts(lo, hi)
            ]

        @tool("Propose a calendar block (does NOT write anything yet). Files an "
              "approval the human must accept; only then does the event land on "
              "the calendar. start/end ISO, or 'when' in tolerant form "
              "('tomorrow 09:00', 'YYYY-MM-DD').")
        def calendar_propose(
            title: str,
            start: str | None = None,
            end: str | None = None,
            when: str | None = None,
            category: str | None = None,
            notes: str | None = None,
        ) -> dict:
            if not start and not when:
                return {"error": "give start/end (ISO) or a 'when' string"}
            payload = {
                "title": title,
                "category": category or "personal",
            }
            if start:
                payload["start"] = start
                payload["end"] = end or ""
            else:
                payload["when"] = when
            if notes:
                payload["notes"] = notes
            if self._ctx.approvals is None:
                return {"error": "approvals service unavailable"}
            aid = self._ctx.approvals.propose("calendar_block", payload, note=title)
            return {
                "proposal": aid,
                "note": "pending — the human approves with: plp approve "
                f"{aid} (reject: plp approve {aid} --reject)",
            }

        return [calendar_list, calendar_conflicts, calendar_propose]

    # ------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        def add_arguments(sp) -> None:
            sub = sp.add_subparsers(dest="cal_cmd", required=True)
            w = sub.add_parser("week", help="show the week's calendar")
            w.add_argument("--date", default=None, help="a date inside the week (default: today)")
            w.add_argument("--category", default=None, help="filter by category")
            a = sub.add_parser("add", help="add an event directly (audited)")
            a.add_argument("--title", required=True)
            a.add_argument("--start", required=True, help="ISO date or datetime")
            a.add_argument("--end", default=None, help="ISO date or datetime (default: +1h)")
            a.add_argument("--category", default="personal")
            a.add_argument("--notes", default="")
            r = sub.add_parser("rm", help="remove an event by uid")
            r.add_argument("uid")
            c = sub.add_parser(
                "connect",
                help="one-time Google Calendar OAuth connect (see docs/google-calendar-setup.md)",
            )
            c.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
            c.add_argument("--code", default=None, help="paste the code manually (skips the local redirect wait)")
            c.add_argument("--calendar-id", default=None, help="calendar id (default: your primary calendar)")

        def handler(args, ctx) -> int:
            store = open_calendar_store(ctx.config, ctx.log)
            cal = ctx.config.calendar
            if args.cal_cmd == "connect":
                from plp.kernel.config import resolve

                cred = (
                    resolve(ctx.config, cal.google.credentials_file)
                    if cal.google.credentials_file
                    else ctx.config.root / "data/calendar/credentials.json"
                )
                receipt = connect_google(
                    cred,
                    open_browser=not getattr(args, "no_browser", False),
                    code=getattr(args, "code", None),
                    calendar_id=getattr(args, "calendar_id", None),
                    log=ctx.log,
                )
                if receipt.get("status") != "ok":
                    print(f"error: {receipt.get('error')}", flush=True)
                    return 1
                print(
                    f"connected to Google calendar {receipt['calendar_id']} "
                    f"(credentials: {receipt['credentials_file']})\n"
                    "now set calendar.google.enabled: true in config/plp.yaml to use it",
                    flush=True,
                )
                return 0
            if args.cal_cmd == "week":
                return self._cmd_week(store, cal, args)
            if args.cal_cmd == "add":
                if ctx.host is None:
                    print("error: host service unavailable", flush=True)
                    return 2
                receipt = ctx.host.call(
                    "calendar_write",
                    ctx.capability,
                    op="create",
                    title=args.title,
                    start=args.start,
                    end=args.end or "",
                    category=args.category,
                    notes=args.notes or "",
                )
                if receipt.get("status") != "ok":
                    print(f"error: {receipt.get('error') or receipt}", flush=True)
                    return 1
                print(f"added {receipt['uid']} (audited as host.calendar_write)")
                return 0
            if args.cal_cmd == "rm":
                if ctx.host is None:
                    print("error: host service unavailable", flush=True)
                    return 2
                receipt = ctx.host.call(
                    "calendar_write",
                    ctx.capability,
                    op="delete",
                    uid=args.uid,
                )
                if receipt.get("status") != "ok":
                    print(f"error: {receipt.get('error') or receipt}", flush=True)
                    return 1
                print(f"removed {args.uid} (audited as host.calendar_write)")
                return 0
            return 2

        return [
            Command(
                name="calendar",
                help="calendar steward: week view, add/rm events, approvals",
                handler=handler,
                add_arguments=add_arguments,
            )
        ]

    def _cmd_week(self, store, cal, args) -> int:
        today = datetime.now().date()
        ref = datetime.fromisoformat(args.date).date() if args.date else today
        ws = ref - timedelta(days=(ref.weekday() - cal.week_start) % 7)
        we = ws + timedelta(days=7)
        events = store.list(ws, we, category=args.category)
        print(f"week of {ws.isoformat()}  (categories: "
              f"{', '.join(store.categories()) or 'none yet'})")
        by_day: dict = {}
        for e in events:
            by_day.setdefault(e.start.date(), []).append(e)
        for i in range(7):
            day = ws + timedelta(days=i)
            day_events = sorted(by_day.get(day, []), key=lambda e: e.start)
            marker = " ← today" if day == today else ""
            if not day_events:
                print(f"  {day.strftime('%a %Y-%m-%d')}{marker}  (free)")
                continue
            print(f"  {day.strftime('%a %Y-%m-%d')}{marker}")
            for e in day_events:
                if e.all_day:
                    span = f"ALL DAY"
                else:
                    span = f"{e.start.strftime('%H:%M')}-{e.end.strftime('%H:%M')}"
                cat = f"  [{e.category}]" if e.category else ""
                print(f"    {span:<14} {e.title}{cat}  ({e.uid})")
        return 0
