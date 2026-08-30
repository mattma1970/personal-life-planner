"""Email scanner tests (Phase 6, PRD.md §4 S2 / §8 Phase 6).

Covers the deterministic floor (triage.py: date extraction — ISO / US /
month-name / relative / weekday, with year-less dates rolling to the next
occurrence; RSVP deadlines; birthdays; needs-reply / life / noise flags;
proposal payloads), the read-only Gmail layer (connect flow with a
pre-filled code, access-token refresh, message decoding — all HTTP via
monkeypatched httpx, no network), and the plugin end-to-end:

- exit criterion: ``email.scan`` on a fixed fixture flags life items and
  files calendar *proposals* (approve-don't-command); approving one writes
  the ICS through the audited host path;
- graceful no-op without credentials (and without a completed connect);
- idempotency: a re-scan never re-proposes the same message;
- opt-in LLM summarization: on → report seasoned (narrow tool mount,
  one retry, strict JSON); always degrades to the deterministic floor.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import json
import logging
import quopri
import time
import types
from pathlib import Path

import httpx
import pytest

from plp.kernel.agent import Agent
from plp.kernel.approvals import Approvals
from plp.kernel.bus import EventBus
from plp.kernel.calendar import CalendarEvent, IcsCalendarStore
from plp.kernel.capability import Capability
from plp.kernel.config import PlpConfig, resolve
from plp.kernel.context import PluginContext
from plp.kernel.host import HostService
from plp.kernel.plugin import load_sibling
from plp.kernel.registry import ToolRegistry
from plp.kernel.store import Store

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
EMAIL = PLUGINS_DIR / "email"
gmail_mod = load_sibling("plp.plugins.email.gmail", EMAIL / "gmail.py")
triage_mod = load_sibling("plp.plugins.email.triage", EMAIL / "triage.py")
plugin_mod = load_sibling("plp.plugins.email.plugin", EMAIL / "plugin.py")
calendar_plugin_mod = load_sibling(
    "plp.plugins.calendar.plugin", PLUGINS_DIR / "calendar" / "plugin.py"
)

log = logging.getLogger("plp.test.email")

# Reference "now": Saturday 2026-08-29 07:00, naive local.
NOW = dt.datetime(2026, 8, 29, 7, 0)
GMAIL_BASE = "https://www.googleapis.com/gmail/v1"
SEARCH_URL = f"{GMAIL_BASE}/users/me/messages"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _rfc2822(when: dt.datetime) -> str:
    return email.utils.format_datetime(when.replace(tzinfo=dt.timezone.utc), usegmt=True)


# ------------------------------------------------------------------- helpers


def _cfg(tmp_path) -> PlpConfig:
    cfg = PlpConfig()
    cfg.root = str(tmp_path)
    cfg.calendar.ics_file = "cal/test.ics"
    return cfg


def _full_ctx(tmp_path, llm=None, delivery=None):
    """Runtime-shaped context (as build_runtime builds it)."""
    cfg = _cfg(tmp_path)
    store = Store(tmp_path / "data" / "plp.db")
    store.migrate_core()
    bus = EventBus()
    approvals = Approvals(store, bus, log)
    host = HostService(store, bus, log)
    tools = ToolRegistry()
    agent = Agent(llm, tools, bus) if llm is not None else None
    ctx = PluginContext(
        store=store,
        bus=bus,
        config=cfg,
        delivery=delivery,
        capability=Capability.permissive(),
        approvals=approvals,
        host=host,
        tools=tools,
        agent=agent,
        job_name=None,
    )
    return cfg, store, bus, approvals, host, ctx


class _FakeDelivery:
    def __init__(self):
        self.delivered: list[tuple] = []

    def deliver(self, kind: str, text: str) -> None:
        self.delivered.append((kind, text))


def _write_creds(path: Path, nested: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    core = {"client_id": "cid", "client_secret": "sec"}
    path.write_text(json.dumps({"installed": core} if nested else core))


def _write_token(path: Path, expired: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": time.time() + 3600 if not expired else time.time() - 100,
            }
        )
    )


# ----------------------------------------------------------------- fake http


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


def _resource(mid: str, sender: str, subject: str, body: str, *, date: dt.datetime | None = None):
    """A format=full Gmail API resource (quoted-printable plain text)."""
    fields = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
    ]
    if date is not None:
        fields.append({"name": "Date", "value": _rfc2822(date)})
    return {
        "id": mid,
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": quopri.encodestring(body.encode("utf-8")).decode("ascii"), "dataEncoding": "q"},
            "headerFields": fields,
        },
    }


def _fake_gmail(monkeypatch, resources: dict[str, dict], posts: list | None = None):
    """Monkeypatch the httpx verbs GmailClient/get_access_token use.

    ``resources`` maps message id → API resource; ``posts`` is a list of
    side effects for POSTs to the token endpoint (dict = response payload,
    Exception = raise). Returns the call log."""
    calls: list[tuple] = []
    posts = list(posts or [])

    def fake_get(url, **kw):
        calls.append(("get", url, kw))
        if url == SEARCH_URL:
            return _FakeResponse({"messages": [{"id": m} for m in resources]})
        for mid in resources:
            if url == f"{SEARCH_URL}/{mid}":
                return _FakeResponse(resources[mid])
        return _FakeResponse({}, status=404)

    def fake_post(url, **kw):
        calls.append(("post", url, kw))
        if url != TOKEN_URL:
            raise AssertionError(f"unexpected POST {url}")
        if not posts:
            raise AssertionError("unexpected token POST")
        eff = posts.pop(0)
        if isinstance(eff, BaseException):
            raise eff
        return _FakeResponse(eff)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


class FakeLLM:
    """Duck-typed LLMClient: scripted chat responses, call log, no network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.max_tool_steps = 8

    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeLLM: more chats than scripted responses")
        return self.responses.pop(0)


def _seasoning_agent(tmp_path, responses, ctx) -> FakeLLM:
    """Register the calendar tools (as build_runtime would) and hand the
    context a FakeLLM-backed agent — then exactly the narrow mount the
    email scenario declares can be asserted on the scripted calls."""
    cal = calendar_plugin_mod.CalendarPlugin()
    cal.setup(ctx)
    for t in cal.tools():
        ctx.tools.register(f"calendar.{t.__name__}", t)
    fake = FakeLLM(responses)
    ctx.agent = Agent(fake, ctx.tools, ctx.bus)
    return fake


def _mounted_names(fake: FakeLLM) -> set:
    names = set()
    for call in fake.calls:
        for t in call["tools"] or []:
            names.add(t["function"]["name"])
    return names


# The Phase-6 fixture (PRD §9 golden set).
LUNCH = (
    "m-lunch",
    "mom@example.com",
    "Lunch with Mom? September 2 at noon.",
    "Let me know if that works for you.",
)
BIRTHDAY = (
    "m-bday",
    "sarah@example.com",
    "Party for Dana's birthday?",
    "Dana's birthday is June 2 - the party is on Saturday, June 2 at 6pm.",
)
NEWSLETTER = (
    "m-news",
    "newsletters@example.com",
    "[Newsletter] Your July AI roundup",
    "Top stories of the month, unsubscribe anytime.",
)
FIXTURE = [LUNCH, BIRTHDAY, NEWSLETTER]


def _triage(fx: tuple) -> "triage_mod.TriageItem":
    """Run a (mid, sender, subject, body) fixture through triage at NOW."""
    mid, sender, subject, body = fx
    return triage_mod.triage_message(mid, sender, subject, None, body, NOW)


def _fixture_resources() -> dict[str, dict]:
    return {
        mid: _resource(mid, sender, subject, body, date=NOW - dt.timedelta(hours=3))
        for mid, sender, subject, body in FIXTURE
    }


def _scan_email(ctx, delivery=None, **args):
    plugin = plugin_mod.EmailPlugin()
    plugin.setup(ctx)
    if delivery is not None:
        ctx.delivery = delivery
    base = {"days": 2, "now": NOW}
    base.update(args)
    return plugin, plugin._scan(ctx, base)


# ----------------------------------------------------------------- triage


class TestTriage:
    def test_event_iso(self):
        got = triage_mod.extract_event("Our meeting is 2026-09-02 14:00 in the hall.", NOW)
        assert got == dt.datetime(2026, 9, 2, 14, 0)

    def test_event_us_two_digit_year(self):
        got = triage_mod.extract_event("Flight lands 9/8/26 6:00 am at SFO", NOW)
        assert got == dt.datetime(2026, 9, 8, 6, 0)

    def test_event_month_day_future(self):
        got = triage_mod.extract_event("Lunch? September 2 at noon.", NOW)
        assert got == dt.datetime(2026, 9, 2, 12, 0)

    def test_event_month_day_past_rolls_to_next_occurrence(self):
        # Seen in August, June 2 already passed → next year (the birthday reading).
        got = triage_mod.extract_event("The party is on Saturday, June 2 at 6pm.", NOW)
        assert got == dt.datetime(2027, 6, 2, 18, 0)

    def test_event_relative_tomorrow(self):
        got = triage_mod.extract_event("Can we do tomorrow at 5pm?", NOW)
        assert got == dt.datetime(2026, 8, 30, 17, 0)

    @pytest.mark.parametrize(
        "ref_time, expected_day",
        [(dt.time(10, 0), 29), (dt.time(19, 30), 29), (dt.time(22, 0), 30)],
    )
    def test_event_tonight_reads_as_evening(self, ref_time, expected_day):
        # "tonight at 8" is 20:00 — never 08:00; after the hour has passed the
        # resolver's next-day rollover stands.
        ref = dt.datetime(2026, 8, 29, ref_time.hour, ref_time.minute)
        got = triage_mod.extract_event("tonight at 8 is perfect", ref)
        assert (got.day, got.hour) == (expected_day, 20)

    def test_event_weekday_and_month_name_priority(self):
        # a month-name date is more specific than a bare weekday: "September 4" wins
        # (2026-09-04 is indeed a Friday, so both agree)
        got = triage_mod.extract_event("Please confirm by Friday, September 4.", NOW)
        assert got == dt.datetime(2026, 9, 4, 9, 0)
        # bare weekday: Sat 08-29 → "Friday" already passed → 2026-09-04
        got = triage_mod.extract_event("Please confirm by Friday.", NOW)
        assert got == dt.datetime(2026, 9, 4, 9, 0)
        # a weekday named on itself means next week (Wed said on Wed)
        wed = dt.datetime(2026, 9, 2, 10, 0)
        assert triage_mod.extract_event("see you Wednesday", wed) == dt.datetime(2026, 9, 9, 9, 0)

    def test_event_weekday_next_week_explicit(self):
        got = triage_mod.extract_event("next week Thursday 11:00", NOW)
        assert got == dt.datetime(2026, 9, 10, 11, 0)

    def test_event_rejects_past_and_distant(self):
        assert triage_mod.extract_event("The party was on August 1 last year.", NOW) is None
        assert triage_mod.extract_event("Reunion on December 25, 2030.", NOW) is None
        assert triage_mod.extract_event("Nothing timey here.", NOW) is None

    def test_event_no_date_no_crash(self):
        # a bare number is not a date ("room 3") — no spurious event
        assert triage_mod.extract_event("I am in room 3, see you soon", NOW) is None

    def test_rsvp_deadline(self):
        got = triage_mod.extract_rsvp("Please confirm by Friday, September 5.", NOW)
        assert got == dt.datetime(2026, 9, 5, 23, 59)

    def test_rsvp_keyword_without_date_yields_none(self):
        assert triage_mod.extract_rsvp("Please let me know soon.", NOW) is None

    def test_occasion_birthday_with_date(self):
        got = triage_mod.extract_occasion("Dana's birthday is June 2 - party at 6pm.", NOW)
        assert got == ("birthday", dt.datetime(2027, 6, 2, 0, 0))

    def test_occasion_without_date_yields_none(self):
        assert triage_mod.extract_occasion("Happy birthday to you!", NOW) is None

    def test_needs_reply_question_ask_direct(self):
        assert triage_mod.needs_reply("a@b.c", "Can you make it?", "")
        assert triage_mod.needs_reply("a@b.c", "Update", "Please confirm the details.")
        assert triage_mod.needs_reply("a@b.c", "Your invoice is ready", "")
        assert not triage_mod.needs_reply("a@b.c", "FYI the road is closed", "just sharing, no action")

    def test_noise_from_and_subject(self):
        assert triage_mod.is_noise("noreply@shop.com", "Your order")
        assert triage_mod.is_noise("a@b.c", "[Newsletter] July edition")
        assert not triage_mod.is_noise("mom@example.com", "Dinner?")

    def test_life_terms_word_boundary(self):
        terms = triage_mod.life_terms("Lunch with Mom?", "we should call the dentist")
        assert "lunch" in terms and "mom" in terms and "dentist" in terms
        # "son" must not match inside "song"
        assert "son" not in triage_mod.life_terms("new song playlist", "")

    def test_extra_keywords_configured(self):
        # configured keywords are case-insensitive ("OZ" matches lowercase text)
        terms = triage_mod.life_terms("catching flights to OZ?", "", extra=("OZ",))
        assert "oz" in terms

    def test_propose_event_block(self):
        item = _triage(LUNCH)
        p = triage_mod.propose_from_item(item)
        assert p["start"] == "2026-09-02T12:00:00"
        assert p["end"] == "2026-09-02T13:00:00"
        assert p["category"] == "family"
        assert p["title"] == "Lunch with Mom? September 2 at noon."
        assert p["notes"].startswith("Email from mom@example.com:")
        assert p["source"] == "email:m-lunch"

    def test_propose_birthday_goes_to_gifts(self):
        item = _triage(BIRTHDAY)
        p = triage_mod.propose_from_item(item)
        assert p["category"] == "gifts"
        assert "birthday" in p["notes"]
        assert p["start"] == "2027-06-02T18:00:00"

    def test_rsvp_deadline_noted_alongside_event(self):
        # the "reply by" sentence comes first, so the deadline is Sept 4
        # while the (ISO) event stays Sept 10
        item = _triage(
            ("m-r", "a@b.c", "Dinner invitation", "Please reply by September 4. Dinner is 2026-09-10 19:00.")
        )
        assert item.rsvp_by == dt.datetime(2026, 9, 4, 23, 59)
        assert item.event == dt.datetime(2026, 9, 10, 19, 0)
        p = triage_mod.propose_from_item(item)
        assert p is not None
        assert p["start"] == "2026-09-10T19:00:00"
        assert p["end"] == "2026-09-10T20:00:00"
        assert "RSVP by 2026-09-04" in p["notes"]

    def test_propose_no_date_no_proposal(self):
        item = _triage(("m-x", "a@b.c", "Thinking of you", "hope you are well"))
        assert triage_mod.propose_from_item(item) is None

    def test_re_subject_stripped_in_title(self):
        item = _triage(("m-fwd", "a@b.c", "Fwd: Re: Dinner on Friday?", "we're on"))
        p = triage_mod.propose_from_item(item)
        assert p["title"] == "Dinner on Friday?"

    def test_interesting_property(self):
        assert not _triage(NEWSLETTER).interesting
        assert _triage(LUNCH).interesting


# ------------------------------------------------------------------- gmail


class TestGmail:
    def test_connect_prefilled_code_writes_token(self, tmp_path, monkeypatch):
        creds = tmp_path / "google_credentials.json"
        _write_creds(creds)
        token = tmp_path / "token.json"
        calls = _fake_gmail(
            monkeypatch,
            {},
            posts=[{"access_token": "at", "refresh_token": "rt", "expires_in": 3600}],
        )
        receipt = gmail_mod.connect(creds, token, open_browser=False, code="abc")
        assert receipt["status"] == "ok"
        assert receipt["scope"] == "https://www.googleapis.com/auth/gmail.readonly"
        doc = json.loads(token.read_text())
        assert doc["refresh_token"] == "rt" and doc["access_token"] == "at"
        post = [c for c in calls if c[0] == "post"][0]
        assert post[1] == TOKEN_URL
        assert post[2]["data"]["grant_type"] == "authorization_code"
        assert post[2]["data"]["code"] == "abc"

    def test_connect_prefilled_code_nested_credentials(self, tmp_path, monkeypatch):
        creds = tmp_path / "creds.json"
        _write_creds(creds, nested=True)
        token = tmp_path / "t.json"
        _fake_gmail(
            monkeypatch, {},
            posts=[{"access_token": "at", "refresh_token": "rt", "expires_in": 3600}],
        )
        receipt = gmail_mod.connect(creds, token, open_browser=False, code="abc")
        assert receipt["status"] == "ok"

    def test_connect_missing_refresh_token_is_actionable(self, tmp_path, monkeypatch):
        creds = tmp_path / "creds.json"
        _write_creds(creds)
        _fake_gmail(monkeypatch, {}, posts=[{"access_token": "at"}])
        receipt = gmail_mod.connect(creds, tmp_path / "t.json", open_browser=False, code="abc")
        assert receipt["status"] == "error"
        assert "revoke" in receipt["error"]

    def test_connect_bad_credentials_file(self, tmp_path, monkeypatch):
        creds = tmp_path / "bad.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({"client_id": "cid"}))  # no secret
        _fake_gmail(monkeypatch, {})
        receipt = gmail_mod.connect(creds, tmp_path / "t.json", open_browser=False, code="abc")
        assert receipt["status"] == "error" and "client_secret" in receipt["error"]

    def test_get_access_token_valid_skips_network(self, tmp_path, monkeypatch):
        creds = tmp_path / "c.json"
        _write_creds(creds)
        token = tmp_path / "t.json"
        _write_token(token, expired=False)
        calls = _fake_gmail(monkeypatch, {})
        assert gmail_mod.get_access_token(token, creds) == "at"
        assert calls == []  # no network at all

    def test_get_access_token_refresh_persists(self, tmp_path, monkeypatch):
        creds = tmp_path / "c.json"
        _write_creds(creds)
        token = tmp_path / "t.json"
        _write_token(token, expired=True)
        _fake_gmail(
            monkeypatch, {},
            posts=[{"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}],
        )
        assert gmail_mod.get_access_token(token, creds) == "at2"
        doc = json.loads(token.read_text())
        assert doc["access_token"] == "at2" and doc["refresh_token"] == "rt2"

    def test_get_access_token_no_token_file(self, tmp_path, monkeypatch):
        creds = tmp_path / "c.json"
        _write_creds(creds)
        _fake_gmail(monkeypatch, {})
        assert gmail_mod.get_access_token(tmp_path / "missing.json", creds) is None

    def test_message_decode_quoted_printable_and_rfc2047(self):
        subj = "=?utf-8?b?" + base64.b64encode("Réunion famille".encode()).decode() + "?="
        # Hand-built QP data: "=3D" is a literal "=", and the "=" at end of
        # line is a soft break (line wrap) that must be joined on decode.
        qdata = "line one=3D and=\nline two"
        res = {
            "id": "m1",
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": qdata, "dataEncoding": "q"},
                "headerFields": [
                    {"name": "From", "value": "Maman <m@x.fr>"},
                    {"name": "Subject", "value": subj},
                    {"name": "Date", "value": "Sat, 29 Aug 2026 06:00:00 +0000"},
                ],
            },
        }
        msg = gmail_mod.message_from_resource(res)
        assert msg.id == "m1"
        assert msg.sender == "Maman <m@x.fr>"
        assert msg.subject == "Réunion famille"
        assert msg.body == "line one= andline two"
        assert msg.date is not None and msg.date.tzinfo is None  # naive local

    def test_message_html_fallback_strips_tags(self):
        html = "<html><body><p>Hello&nbsp;<b>World</b></p></body></html>"
        res = {
            "id": "m2",
            "payload": {
                "mimeType": "text/html",
                "body": {"data": base64.b64encode(html.encode()).decode(), "dataEncoding": "b"},
                "headerFields": [{"name": "From", "value": "a@b.c"}],
            },
        }
        msg = gmail_mod.message_from_resource(res)
        assert msg.body.startswith("Hello")
        assert "<" not in msg.body and "World" in msg.body

    def test_client_search_and_fetch_use_bearer_token(self, monkeypatch):
        calls = _fake_gmail(monkeypatch, {"m1": _resource("m1", "a@b.c", "Hi", "hello")})
        client = gmail_mod.GmailClient(GMAIL_BASE, lambda: "tok-123")
        assert client.search("newer_than:2d", max_results=25) == ["m1"]
        msg = client.fetch("m1")
        assert msg.body == "hello"
        get_calls = [c for c in calls if c[0] == "get"]
        assert get_calls[0][2]["headers"]["Authorization"] == "Bearer tok-123"
        assert get_calls[0][2]["params"] == {"q": "newer_than:2d", "maxResults": 25}


# ----------------------------------------------------------------- plugin


class TestScan:
    def test_no_credentials_is_graceful_noop(self, tmp_path):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        events = []
        bus.subscribe("email.scan", lambda name, payload: events.append((name, payload)))
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["status"] == "no_credentials"
        assert result["proposed"] == [] and result["scanned"] == 0
        assert store.query_json("SELECT * FROM email_seen") == []
        assert approvals.pending() == []
        assert events[0][1]["no_credentials"] is True  # audited, not silent

    def test_credentials_but_no_connect_is_noop(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _fake_gmail(monkeypatch, {})  # no token file → get_access_token → None
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["status"] == "no_credentials"
        assert result["proposed"] == []

    def test_auth_error_degrades_not_crash(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json", expired=True)
        _fake_gmail(monkeypatch, {}, posts=[RuntimeError("oauth dead")])
        events = []
        bus.subscribe("email.scan", lambda name, payload: events.append((name, payload)))
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["status"] == "auth_error"
        assert result["proposed"] == []
        assert events[0][1]["error"] == "oauth dead"  # audited

    def test_exit_criterion_scan_flags_and_proposes(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        delivery = _FakeDelivery()
        events = []
        bus.subscribe("email.scan", lambda name, payload: events.append((name, payload)))

        plugin, result = _scan_email(ctx, delivery=delivery)

        # triage: 3 scanned, 2 worth surfacing, 1 noise
        assert result == {
            "status": "ok",
            "scanned": 3,
            "flagged": 2,
            "noise": 1,
            "proposed": result["proposed"],
            "seasoned": False,  # features.email_summarization off by default
        }
        assert len(result["proposed"]) == 2

        # proposals are pending calendar_blocks with the right payloads
        pending = {p["id"]: p for p in approvals.pending("calendar_block")}
        assert len(pending) == 2
        by_source = {p["payload"]["source"]: p["payload"] for p in pending.values()}
        lunch = by_source["email:m-lunch"]
        bday = by_source["email:m-bday"]
        assert lunch == {
            "title": "Lunch with Mom? September 2 at noon.",
            "start": "2026-09-02T12:00:00",
            "end": "2026-09-02T13:00:00",
            "category": "family",
            "notes": "Email from mom@example.com: Lunch with Mom? September 2 at noon.",
            "source": "email:m-lunch",
        }
        assert bday["category"] == "gifts"
        assert bday["start"] == "2027-06-02T18:00:00"
        assert "birthday" in bday["notes"]

        # idempotency bookkeeping: every message seen exactly once
        seen = {r["message_id"]: r for r in store.query_json("SELECT * FROM email_seen")}
        assert set(seen) == {"m-lunch", "m-bday", "m-news"}
        assert seen["m-lunch"]["proposed"] == 1 and seen["m-news"]["proposed"] == 0

        # the report was delivered and digested
        assert delivery.delivered and delivery.delivered[0][0] == "email"
        report = delivery.delivered[0][1]
        assert "Email triage — 2026-08-29" in report
        assert "Lunch with Mom?" in report and "Party for Dana's birthday?" in report
        assert "newsletter" not in report
        assert "proposal #" in report
        digests = store.query_json("SELECT content FROM digests WHERE kind='email'")
        assert digests and digests[0]["content"] == report

        # bus event carries the run's counts
        assert events[0][1] == {
            "scanned": 3,
            "flagged": 2,
            "noise": 1,
            "proposed": result["proposed"],
            "seasoned": False,
        }

    def test_rescan_never_reproposes(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        _, first = _scan_email(ctx)
        assert len(first["proposed"]) == 2
        _, second = _scan_email(ctx)
        assert second["proposed"] == []  # all already proposed
        assert len(approvals.pending("calendar_block")) == 2  # no duplicates
        assert {r["message_id"] for r in store.query_json("SELECT * FROM email_seen")} == {
            "m-lunch", "m-bday", "m-news"
        }  # no duplicate rows

    def test_approve_writes_ics_through_host(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        cal = calendar_plugin_mod.CalendarPlugin()
        cal.setup(ctx)  # subscribes the approvals → ICS wiring
        plugin, result = _scan_email(ctx)
        assert result["proposed"]

        aid = result["proposed"][0]
        payload = approvals.get(aid)["payload"]
        ics = IcsCalendarStore(resolve(cfg, cfg.calendar.ics_file))
        ics.create(
            CalendarEvent(
                title="seed",
                start=NOW + dt.timedelta(days=1, hours=9),
                end=NOW + dt.timedelta(days=1, hours=10),
            )
        )
        assert approvals.resolve(aid, True, "yes") is True
        start = dt.datetime.fromisoformat(payload["start"])
        end = dt.datetime.fromisoformat(payload["end"])
        got = ics.list(start.replace(minute=0), end)
        assert [e.title for e in got] == [payload["title"]]
        assert got[0].category == payload["category"]
        # human-edited event survived the merge write (vault=human tier)
        assert any(e.title == "seed" for e in ics.list(NOW, NOW + dt.timedelta(days=30)))
        # audited host write
        assert store.query_one("SELECT * FROM runs WHERE job='host.calendar_write'") is not None

    def test_llm_opt_in_seasons_report(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        cfg.features.email_summarization = True
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        valid = json.dumps(
            {
                "summary": "Mom wants lunch Sept 2; Dana's birthday is next June.",
                "action": "reply to Mom confirming Sept 2 noon",
            }
        )
        fake = _seasoning_agent(tmp_path, [valid], ctx)
        delivery = _FakeDelivery()
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        ctx.delivery = delivery
        result = plugin._scan(ctx, {"days": 2, "now": NOW})

        assert result["seasoned"] is True
        assert len(fake.calls) == 1  # accepted on the first answer
        # narrow mount: only the declared tool is ever offered
        assert _mounted_names(fake) == {"calendar.calendar_list"}
        report = delivery.delivered[0][1]
        assert "> Mom wants lunch Sept 2" in report
        assert "> suggested action: reply to Mom confirming Sept 2 noon" in report
        # the deterministic floor is still in the report
        assert "Lunch with Mom?" in report

    def test_llm_retry_then_accept(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        cfg.features.email_summarization = True
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        valid = json.dumps({"summary": "s", "action": "none"})
        fake = _seasoning_agent(tmp_path, ["not json at all", valid], ctx)
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["seasoned"] is True
        assert len(fake.calls) == 2
        assert "not accepted" in fake.calls[1]["messages"][-1]["content"]

    def test_llm_degrades_to_deterministic_floor(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        cfg.features.email_summarization = True
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        fake = _seasoning_agent(tmp_path, ["nope", "still nope"], ctx)
        delivery = _FakeDelivery()
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        ctx.delivery = delivery
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["seasoned"] is False
        assert len(fake.calls) == 2  # one retry, then the floor
        report = delivery.delivered[0][1]
        assert "Lunch with Mom?" in report  # deterministic lines still shipped
        assert "suggested action" not in report

    def test_llm_off_by_default_never_calls(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        assert cfg.features.email_summarization is False  # PRD §11 default
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())

        fake = _seasoning_agent(tmp_path, [], ctx)
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result["seasoned"] is False
        assert fake.calls == []  # off = local-only triage

    def test_scan_with_no_flagged_items_still_reports(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, {"m-news": _fixture_resources()["m-news"]})
        delivery = _FakeDelivery()
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        ctx.delivery = delivery
        result = plugin._scan(ctx, {"days": 2, "now": NOW})
        assert result == {
            "status": "ok",
            "scanned": 1,
            "flagged": 0,
            "noise": 1,
            "proposed": [],
            "seasoned": False,
        }
        assert "nothing worth surfacing" in delivery.delivered[0][1]

    def test_job_manifest(self, tmp_path):
        cfg, *_rest, ctx = _full_ctx(tmp_path)
        cfg.email.scan_cron = "30 7 * * *"
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)  # jobs() reads config captured in setup
        jobs = plugin.jobs()
        assert [j.name for j in jobs] == ["email.scan"]
        assert jobs[0].cron == "30 7 * * *"
        assert jobs[0].plugin == "email"

    def test_last_scan_tool_reads_current_items(self, tmp_path, monkeypatch):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        tools = plugin.tools()
        assert len(tools) == 1
        # before any scan: empty, not stale
        assert json.loads(tools[0]()) == []
        plugin._scan(ctx, {"days": 2, "now": NOW})
        items = json.loads(tools[0]())
        assert {i["subject"] for i in items} == {
            "Lunch with Mom? September 2 at noon.",
            "Party for Dana's birthday?",
        }
        assert all("body" not in i for i in items)  # small contexts, no bodies


# -------------------------------------------------------------------- CLI


class TestCLI:
    def _ns(self, **kw):
        base = dict(email="scan", days=0, llm=None, no_llm=False, now=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_scan_without_credentials_exit_zero(self, tmp_path, capsys):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        cmd = plugin.commands()[0]
        rc = cmd.handler(self._ns(), ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Gmail not connected" in out

    def test_scan_with_credentials_prints_report(self, tmp_path, monkeypatch, capsys):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _write_token(tmp_path / "data/email/token.json")
        _fake_gmail(monkeypatch, _fixture_resources())
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        cmd = plugin.commands()[0]
        rc = cmd.handler(self._ns(now=NOW.isoformat()), ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Email triage — 2026-08-29" in out
        assert "2 calendar proposal(s)" in out

    def test_connect_without_credentials_fails_cleanly(self, tmp_path, capsys):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        cmd = plugin.commands()[0]
        ns = types.SimpleNamespace(email="connect", credentials=None, no_browser=True, code=None)
        assert cmd.handler(ns, ctx) == 1
        assert "credentials" in capsys.readouterr().out

    def test_connect_with_code_prints_receipt(self, tmp_path, monkeypatch, capsys):
        cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
        cfg.email.credentials_file = "data/email/google_credentials.json"
        _write_creds(tmp_path / cfg.email.credentials_file)
        _fake_gmail(
            monkeypatch, {},
            posts=[{"access_token": "at", "refresh_token": "rt", "expires_in": 3600}],
        )
        plugin = plugin_mod.EmailPlugin()
        plugin.setup(ctx)
        cmd = plugin.commands()[0]
        ns = types.SimpleNamespace(email="connect", credentials=None, no_browser=True, code="abc")
        assert cmd.handler(ns, ctx) == 0
        out = capsys.readouterr().out
        assert "Gmail connected (read-only)" in out
        assert json.loads((tmp_path / "data/email/token.json").read_text())["refresh_token"] == "rt"
