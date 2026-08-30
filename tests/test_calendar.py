"""Calendar plugin tests (Phase 4): ICS codec, ICS store with human-edit
survival, Google scaffold + fallback, host executor auth/audit/isolation,
the propose→approve→ICS end-to-end exit criterion, and the week CLI."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import httpx

from plp.kernel.approvals import Approvals
from plp.kernel.bus import EventBus
from plp.kernel.calendar import (
    GOOGLE_CALENDAR_URL,
    GOOGLE_TOKEN_URL,
    CalendarEvent,
    GoogleCalendarStore,
    GoogleNotConfigured,
    IcsCalendarStore,
    calendar_to_ics,
    open_calendar_store,
    parse_ics,
)
from plp.kernel.capability import Capability
from plp.kernel.config import PlpConfig
from plp.kernel.context import PluginContext
from plp.kernel.host import HostError, HostService
from plp.kernel.plugin import load_sibling
from plp.kernel.store import Store

PLUGINS = Path(__file__).resolve().parent.parent / "plugins" / "calendar"
plugin_mod = load_sibling("plp.plugins.calendar.plugin", PLUGINS / "plugin.py")

log = logging.getLogger("plp.test.calendar")

T1 = datetime(2026, 9, 3, 9, 0)
T2 = datetime(2026, 9, 3, 10, 0)


# ------------------------------------------------------------------- helpers


def _cfg(tmp_path, backend: str = "ics", google_enabled: bool = False) -> PlpConfig:
    cfg = PlpConfig()
    cfg.root = str(tmp_path)
    cfg.calendar.backend = backend
    cfg.calendar.ics_file = "cal/test.ics"
    cfg.calendar.google.enabled = google_enabled
    if google_enabled:
        cfg.calendar.google.credentials_file = str(tmp_path / "cal" / "creds.json")
    return cfg


def _store(tmp_path) -> IcsCalendarStore:
    return IcsCalendarStore(tmp_path / "cal" / "main.ics")


def _full_ctx(tmp_path):
    """Runtime-shaped context: approvals + host attached, as build_runtime
    does (Phase 4 kernel fix: setup() receives full service handles)."""
    cfg = _cfg(tmp_path)
    store = Store(tmp_path / "plp.db")
    store.migrate_core()
    bus = EventBus()
    approvals = Approvals(store, bus, log)
    host = HostService(store, bus, log)
    ctx = PluginContext(
        store=store,
        bus=bus,
        config=cfg,
        delivery=None,
        capability=Capability.permissive(),
        approvals=approvals,
        host=host,
        job_name=None,
    )
    return cfg, store, bus, approvals, host, ctx


def _event(title="Focus block", **kw) -> CalendarEvent:
    return CalendarEvent(title=title, start=T1, end=T2, **kw)


def _write_creds(path: Path, nested: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    core = {
        "client_id": "cid",
        "client_secret": "sec",
        "refresh_token": "rt",
        "calendar_id": "primary",
    }
    doc = {"installed": core} if nested else core
    path.write_text(json.dumps(doc))


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


EVENTS_URL = GOOGLE_CALENDAR_URL.format(cid="primary")


def _fake_google(monkeypatch, items: list[dict]):
    """Monkeypatch the module-level httpx verbs used by GoogleCalendarStore
    and connect_google. Returns the call log."""
    calls: list[tuple] = []

    def fake_get(url, **kw):
        calls.append(("get", url, kw))
        if url == "https://www.googleapis.com/calendar/v3/users/me/calendarList":
            return _FakeResponse({"items": [{"id": "primary", "primary": True}]})
        if url == EVENTS_URL:
            return _FakeResponse({"items": items})
        if url.startswith(EVENTS_URL + "/"):
            if items:
                return _FakeResponse(items[0])
            return _FakeResponse({}, status=404)
        return _FakeResponse({})

    def fake_post(url, **kw):
        calls.append(("post", url, kw))
        if url == GOOGLE_TOKEN_URL:
            return _FakeResponse({"access_token": "at", "refresh_token": "NEW-RT", "expires_in": 3600})
        body = kw.get("json") or {}
        return _FakeResponse({"id": "g-1", "summary": body.get("summary")})

    def fake_put(url, **kw):
        calls.append(("put", url, kw))
        body = kw.get("json") or {}
        return _FakeResponse(
            {
                "id": url.rsplit("/", 1)[-1],
                "summary": body.get("summary"),
                "start": {"dateTime": T1.isoformat()},
                "end": {"dateTime": T2.isoformat()},
            }
        )

    def fake_delete(url, **kw):
        calls.append(("delete", url, kw))
        return _FakeResponse({}, status=204)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "put", fake_put)
    monkeypatch.setattr(httpx, "delete", fake_delete)
    return calls


# -------------------------------------------------------------------- codec


def test_roundtrip_timed_with_escaping(tmp_path):
    ev = CalendarEvent(
        title='Backslash \\ and ; semicolon, comma',
        start=T1,
        end=T2,
        category="work",
        notes="line one\nline two",
    )
    text = calendar_to_ics([ev])
    back = parse_ics(text)[0]
    assert back.title == ev.title
    assert back.notes == ev.notes
    assert back.category == "work"
    assert back.start == T1 and back.end == T2


def test_roundtrip_allday_one_and_two_days(tmp_path):
    one = CalendarEvent(title="1d", start=datetime(2026, 9, 1), end=datetime(2026, 9, 2))
    two = CalendarEvent(title="2d", start=datetime(2026, 9, 1), end=datetime(2026, 9, 3))
    text = calendar_to_ics([one, two])
    # RFC 5545: all-day DTEND is exclusive — one-day event: DTSTART == DTEND
    assert "DTSTART;VALUE=DATE:20260901\r\nDTEND;VALUE=DATE:20260901" in text
    assert "DTEND;VALUE=DATE:20260902" in text
    back = parse_ics(text)
    by_title = {e.title: e for e in back}
    # parse normalizes DTEND (exclusive) back to morning-after
    assert by_title["1d"].start == datetime(2026, 9, 1)
    assert by_title["1d"].end == datetime(2026, 9, 2)
    assert by_title["2d"].end == datetime(2026, 9, 3)
    assert by_title["1d"].all_day and by_title["2d"].all_day


def test_fold_long_and_utf8_lines(tmp_path):
    ev = CalendarEvent(title="é" * 120, start=T1, end=T2)
    text = calendar_to_ics([ev])
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"line too long: {line[:80]!r}"
    back = parse_ics(text)[0]
    assert back.title == "é" * 120


def test_lf_tolerance_and_malformed_skipped(tmp_path):
    good = calendar_to_ics([_event("good")])
    raw = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\nUID:x@plp\r\nSUMMARY:bad no start\r\nEND:VEVENT\r\n"
        + good
    ).replace("\r\n", "\n")
    events = parse_ics(raw)
    assert [e.title for e in events] == ["good"]


# ----------------------------------------------------------------- ICS store


def test_crud_and_errors(tmp_path):
    s = _store(tmp_path)
    ev = s.create(_event())
    assert s.get(ev.uid).title == ev.title
    # duplicate UID rejected
    with pytest.raises(ValueError):
        s.create(CalendarEvent(title="dup", start=T1, end=T2, uid=ev.uid))
    # update
    s.update(ev.uid, title="renamed")
    assert s.get(ev.uid).title == "renamed"
    # unknown targets
    assert s.update("nope@plp", title="x") is None
    assert s.delete("nope@plp") is False
    assert s.delete(ev.uid) is True
    assert s.get(ev.uid) is None


def test_list_conflicts_categories(tmp_path):
    s = _store(tmp_path)
    s.create(_event("a", category="work"))
    # b touches a's end exactly → half-open [start,end) → NOT a conflict
    s.create(
        CalendarEvent(
            title="b",
            start=T2,
            end=T2 + timedelta(hours=1),
            category="personal",
        )
    )
    s.create(
        CalendarEvent(
            title="c",
            start=T1 + timedelta(minutes=30),
            end=T1 + timedelta(minutes=45),
        )
    )
    got = s.list(T1, T2, category="work")
    assert [e.title for e in got] == ["a"]
    # half-open windows: b starts exactly at the window end → excluded;
    # a and c (both inside) are included
    assert [e.title for e in s.conflicts(T1, T2)] == ["a", "c"]
    assert {e.uid for e in s.conflicts(T1, T2)} == {e.uid for e in s.list(T1, T2)}
    assert set(s.categories()) == {"personal", "work"}


def test_human_edits_survive_daemon_writes(tmp_path):
    s = _store(tmp_path)
    plp_ev = s.create(_event("plp-written"))
    # human hand-edits the file: renames our event AND adds an external one
    text = s.path.read_text()
    text = text.replace("SUMMARY:plp-written", "SUMMARY:human-renamed")
    external = (
        "\r\nBEGIN:VEVENT\r\nUID:ext-1@other\r\n"
        f"DTSTART:{T1.strftime('%Y%m%dT%H%M%S')}\r\n"
        f"DTEND:{T2.strftime('%Y%m%dT%H%M%S')}\r\n"
        "SUMMARY:hand-added\r\nCATEGORIES:family\r\nEND:VEVENT\r\n"
    )
    text = text.replace("END:VCALENDAR", external + "END:VCALENDAR")
    s.path.write_text(text)
    # daemon-style update on top of the human's version
    s.update(plp_ev.uid, notes="daemon edit")
    events = {e.uid: e for e in s.list(datetime(2026, 8, 1), datetime(2026, 10, 1))}
    assert set(events) == {plp_ev.uid, "ext-1@other"}
    assert events[plp_ev.uid].title == "human-renamed"
    assert events[plp_ev.uid].notes == "daemon edit"
    assert events["ext-1@other"].category == "family"


# -------------------------------------------------------- Google scaffold


def test_google_fallback_to_ics_when_unconfigured(tmp_path):
    cfg = _cfg(tmp_path, backend="google", google_enabled=True)  # enabled, no creds
    store = open_calendar_store(cfg, log)
    assert isinstance(store, IcsCalendarStore), "unconfigured google must fall back to ICS"


def test_google_enabled_and_configured_active(tmp_path):
    cfg = _cfg(tmp_path, backend="google", google_enabled=True)
    _write_creds(Path(cfg.calendar.google.credentials_file))
    store = open_calendar_store(cfg, log)
    assert isinstance(store, GoogleCalendarStore)


def test_google_not_configured_raising_path(tmp_path):
    g = GoogleCalendarStore(tmp_path / "nope" / "creds.json")
    with pytest.raises(GoogleNotConfigured):
        g._ensure_configured()
    # flat creds complete → passes; raw GCP export (nested) also passes
    p = tmp_path / "c1.json"
    _write_creds(p, nested=False)
    assert GoogleCalendarStore(p)._ensure_configured()["client_id"] == "cid"
    p2 = tmp_path / "c2.json"
    _write_creds(p2, nested=True)
    assert GoogleCalendarStore(p2)._ensure_configured()["client_id"] == "cid"


def test_google_store_crud_roundtrip(tmp_path, monkeypatch):
    _write_creds(tmp_path / "cal" / "creds.json")
    g = GoogleCalendarStore(tmp_path / "cal" / "creds.json")
    calls = _fake_google(monkeypatch, items=[])
    assert g.list(T1, T2) == []
    ev = g.create(_event("g"))
    assert ev.uid == "g-1"
    # with a known remote item: get / update / delete
    calls2 = _fake_google(
        monkeypatch,
        items=[
            {
                "id": "g-1",
                "summary": "g",
                "start": {"dateTime": T1.isoformat()},
                "end": {"dateTime": T2.isoformat()},
            }
        ],
    )
    got = g.get("g-1")
    assert got is not None and got.title == "g"
    up = g.update("g-1", title="g2")
    assert up.title == "g2"
    assert g.delete("g-1") is True
    kinds = [c[0] for c in calls + calls2]
    assert {"get", "post", "put", "delete"} <= set(kinds)
    # 404 on get → None (fresh fake with no items)
    _fake_google(monkeypatch, items=[])
    assert g.get("missing") is None


# ---------------------------------------------------------- host executor


def test_host_register_audit_deny_isolation(tmp_path):
    cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
    p = plugin_mod.CalendarPlugin()
    p.setup(ctx)
    # unknown action rejected; re-registering a known one is idempotent (last wins)
    with pytest.raises(HostError):
        host.register("not_a_real_action", lambda **k: {})
    host.register("calendar_write", lambda **k: {"status": "ok", "marker": "second"})
    receipt = host.call("calendar_write", Capability.permissive(), op="create", title="x")
    assert receipt == {"status": "ok", "marker": "second"}, "last registration wins"
    row = store.query_one("SELECT * FROM runs WHERE job='host.calendar_write'")
    assert row is not None
    # strict capability without host_actions → refused, not audited, not dispatched
    with pytest.raises(HostError):
        host.call("calendar_write", Capability(strict=True), op="create", title="x")
    assert (
        store.query_one("SELECT COUNT(*) AS n FROM runs WHERE job='host.calendar_write'")["n"] == 1
    )
    # restore the plugin's real executor; an exception in it is isolated
    # into the receipt (never a crash for the caller)
    host.register("calendar_write", p._host_write)

    def boom(**k):
        raise RuntimeError("boom")

    boom_store = IcsCalendarStore(tmp_path / "cal" / "boom.ics")
    boom_store.create = boom  # type: ignore[method-assign]
    p._store = boom_store
    receipt = host.call(
        "calendar_write",
        Capability.permissive(),
        op="create",
        title="x",
        when="2026-09-07 09:00",
    )
    assert receipt["status"] == "error" and "boom" in receipt["error"]


# --------------------------------------------- end-to-end (the exit criterion)


def test_propose_approve_writes_ics(tmp_path):
    """PRD exit criterion: proposal → approve → ICS entry, audited, idempotent."""
    cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
    p = plugin_mod.CalendarPlugin()
    p.setup(ctx)
    aid = approvals.propose(
        "calendar_block",
        {"title": "Ship phase 4", "when": "2026-09-04 10:00", "category": "work"},
        note="e2e",
    )
    assert approvals.get(aid)["status"] == "pending"
    assert (tmp_path / "cal" / "test.ics").exists() is False  # no write yet
    assert approvals.resolve(aid, True, "yes") is True
    events = p._store.list(datetime(2026, 9, 1), datetime(2026, 9, 5))
    assert [e.title for e in events] == ["Ship phase 4"]
    assert events[0].category == "work"
    assert events[0].start == datetime(2026, 9, 4, 10, 0)
    assert store.query_one("SELECT * FROM runs WHERE job='host.calendar_write'") is not None
    # re-approving is a no-op (Phase 4 kernel fix: no duplicate publish)
    assert approvals.resolve(aid, True) is False
    assert len(p._store.list(datetime(2026, 9, 1), datetime(2026, 9, 5))) == 1


def test_reject_and_non_calendar_approval_no_side_effect(tmp_path):
    cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
    plugin_mod.CalendarPlugin().setup(ctx)
    a1 = approvals.propose("calendar_block", {"title": "nope", "when": "2026-09-04 10:00"})
    assert approvals.resolve(a1, False) is True
    a2 = approvals.propose("llm_summary", {"what": "other"})
    approvals.resolve(a2, True)
    assert (tmp_path / "cal" / "test.ics").exists() is False
    assert (
        store.query_one("SELECT COUNT(*) AS n FROM runs WHERE job='host.calendar_write'")["n"] == 0
    )


def test_calendar_tools(tmp_path):
    cfg, store, bus, approvals, host, ctx = _full_ctx(tmp_path)
    p = plugin_mod.CalendarPlugin()
    p.setup(ctx)
    tools = {t.__name__: t for t in p.tools()}
    out = tools["calendar_propose"]("Standup", when="2026-09-05 09:00", category="work")
    assert out["proposal"] and "plp approve" in out["note"]
    assert tools["calendar_list"]("2026-09-01", "2026-09-08") == []
    assert tools["calendar_conflicts"]("2026-09-05", "2026-09-06") == []
    # propose with explicit start/end beats `when`
    out2 = tools["calendar_propose"]("Explicit", start="2026-09-06 08:00", end="2026-09-06 09:00")
    assert out2["proposal"] != out["proposal"]


# --------------------------------------------------------------------- CLI


def test_cli_week_view(tmp_path, capsys):
    from plp.cli import main

    repo_plugins = str(PLUGINS.parent)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "plp.yaml").write_text(
        f"plugins:\n  dir: {repo_plugins}\n"
        "calendar:\n  backend: ics\n  ics_file: cal/test.ics\n  week_start: 1\n"
        "  categories: []\n  google:\n    enabled: false\n"
    )
    s = IcsCalendarStore(tmp_path / "cal" / "test.ics")
    s.create(
        CalendarEvent(
            title="CLI week event",
            start=datetime(2026, 9, 3, 15, 0),
            end=datetime(2026, 9, 3, 16, 0),
        )
    )
    code = main(
        [
            "--config",
            str(tmp_path / "config" / "plp.yaml"),
            "calendar",
            "week",
            "--date",
            "2026-09-03",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "week of 2026-09-01" in out
    assert "CLI week event" in out
    assert "15:00-16:00" in out


# -------------------------------------------------------------- connect_google


def test_connect_google_flow(tmp_path, monkeypatch):
    """Full loopback OAuth flow: simulated browser hits the one-shot server,
    Google endpoints mocked, creds file gets refresh_token + calendar_id."""
    creds = tmp_path / "cal" / "creds.json"
    _write_creds(creds, nested=True)  # raw GCP export shape

    def fake_open(url, **kw):
        q = parse_qs(urlparse(url).query)
        assert q.get("state") == ["plp-connect"]
        assert q.get("response_type") == ["code"]
        redir = q["redirect_uri"][0]
        import urllib.request

        urllib.request.urlopen(f"{redir}?code=FAKE-CODE&state=plp-connect")
        return True

    calls = _fake_google(monkeypatch, [])
    monkeypatch.setattr("webbrowser.open", fake_open)

    res = plugin_mod.connect_google(creds, open_browser=True)
    assert res["status"] == "ok", res
    assert res["calendar_id"] == "primary"
    stored = json.loads(creds.read_text())
    assert stored["refresh_token"] == "NEW-RT"
    assert stored["calendar_id"] == "primary"
    # GCP-export top level preserved alongside the flattened client keys
    assert stored["client_id"] == "cid"
    kinds = [c[0] for c in calls]
    assert "post" in kinds and "get" in kinds  # token exchange + calendar list


def test_connect_google_manual_code_path(tmp_path, monkeypatch):
    """--code path: no loopback server, no browser."""
    creds = tmp_path / "cal" / "creds.json"
    _write_creds(creds, nested=False)
    _fake_google(monkeypatch, [])
    res = plugin_mod.connect_google(creds, code="PASTED-CODE", calendar_id="cal-x")
    assert res["status"] == "ok", res
    assert json.loads(creds.read_text())["calendar_id"] == "cal-x"
