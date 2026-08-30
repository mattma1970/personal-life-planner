"""Calendar spine (PRD.md §1: "the calendar is the spine").

A small, dependency-free calendar abstraction with two backends:

- :class:`IcsCalendarStore` — the v1 backend: one human-readable ``.ics``
  file (RFC 5545 subset: VEVENT, floating local datetimes, ``VALUE=DATE``
  all-day events). The daemon is the writer; it merges on every write, so
  edits a human makes in any calendar tool are preserved (the human wins).
- :class:`GoogleCalendarStore` — the Phase-4 scaffold for the later Google
  backend: same interface, OAuth2 refresh-token flow and Calendar API calls
  implemented but inert until the owner completes
  ``docs/google-calendar-setup.md`` and sets ``calendar.google.enabled``.

Kernel (not plugin) because two consumers need it: the calendar plugin
(commands/tools) and other plugins that check or propose time (travel,
scorecard). Privileged writes must go through :class:`plp.kernel.host.
HostService` (``calendar_write``) — this module never grants a plugin
unsupervised write access.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import PlpConfig, resolve
from .util import parse_ts  # noqa: F401  (re-exported for callers)

log = logging.getLogger("plp.kernel.calendar")

PRODID = "-//PLP//PersonalLifePlanner//EN"
_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?$")


# --------------------------------------------------------------------------
# Event model
# --------------------------------------------------------------------------


@dataclass
class CalendarEvent:
    """One calendar entry. Datetimes are the owner's local wall-clock time
    (the daemon runs in one timezone; ICS uses ``VALUE=DATE`` when the event
    is all-day)."""

    title: str
    start: datetime | date
    end: datetime | date
    category: str = "personal"
    notes: str = ""
    uid: str = ""

    def __post_init__(self) -> None:
        # Normalize date → datetime at midnight first (comparisons below).
        if isinstance(self.start, date) and not isinstance(self.start, datetime):
            self.start = datetime.combine(self.start, time(0, 0))
        if isinstance(self.end, date) and not isinstance(self.end, datetime):
            self.end = datetime.combine(self.end, time(0, 0))
        # All-day event written with equal start/end dates → one-day span.
        if self.start.time() == time(0, 0) and self.end.time() == time(0, 0) and self.end <= self.start:
            self.end = self.end + timedelta(days=1)
        if not self.uid:
            self.uid = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}@plp"
        if self.end <= self.start:
            raise ValueError(f"event end {self.end} must be after start {self.start}")

    @property
    def all_day(self) -> bool:
        return (
            self.start.time() == time(0, 0)
            and self.end.time() == time(0, 0)
            and (self.end - self.start).total_seconds() % 86400 == 0
        )

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


# --------------------------------------------------------------------------
# ICS codec (RFC 5545 subset)
# --------------------------------------------------------------------------


def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _unesc(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "N": "\n", ";": ";", ",": ",", "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _fold(line: str) -> list[str]:
    """Fold to <= 75 octets without splitting UTF-8 characters (RFC 5545 §3.1)."""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    out, cur, cur_bytes, limit = [], "", 0, 75
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > limit and cur:
            out.append(cur)
            cur, cur_bytes, limit = " " + ch, 1 + b, 74
        else:
            cur += ch
            cur_bytes += b
    if cur:
        out.append(cur)
    return out


def _unfold(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_dt(value: str) -> tuple[datetime, bool]:
    """Parse ``YYYYMMDDTHHMMSS`` / ``YYYYMMDD`` → (datetime, all_day)."""
    m = _DT_RE.match(value)
    if not m:
        raise ValueError(f"unparseable ICS datetime: {value!r}")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4) is None:
        return datetime(y, mo, d), True
    return datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6))), False


def event_to_ics(event: CalendarEvent) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%S')}Z",
    ]
    if event.all_day:
        # In-memory end is the morning after the last day; ICS all-day DTEND
        # is exclusive → one day earlier. (A one-day event: DTSTART==DTEND.)
        last_day = (event.end - timedelta(days=1)).date()
        lines.append(f"DTSTART;VALUE=DATE:{event.start.date().strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{last_day.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{event.start.strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"DTEND:{event.end.strftime('%Y%m%dT%H%M%S')}")
    lines.append(f"SUMMARY:{_esc(event.title)}")
    if event.category:
        lines.append(f"CATEGORIES:{_esc(event.category)}")
    if event.notes:
        lines.append(f"DESCRIPTION:{_esc(event.notes)}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def parse_ics(text: str) -> list[CalendarEvent]:
    """Parse a VCALENDAR document → events (tolerant of CRLF/LF, folding,
    and property parameters like ``DTSTART;VALUE=DATE``)."""
    raw = text.replace("\r\n", "\n").split("\n")
    events: list[CalendarEvent] = []
    cur: dict[str, Any] | None = None
    for line in _unfold(raw):
        line = line.strip("\r")
        if not line:
            continue
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT"):
            if cur is not None:
                try:
                    events.append(_event_from_props(cur))
                except (ValueError, KeyError) as exc:
                    log.warning("skipping malformed VEVENT: %s", exc)
                cur = None
        elif cur is not None and ":" in line:
            key, _, val = line.partition(":")
            base = key.split(";")[0].upper()
            cur[base] = _unesc(val)
            cur[base + "_PARAMS"] = key[len(base):]  # e.g. ";VALUE=DATE"
    return events


def _event_from_props(p: dict[str, Any]) -> CalendarEvent:
    ds = p["DTSTART"]
    de = p.get("DTEND", ds)
    all_day = "VALUE=DATE" in (p.get("DTSTART_PARAMS") or "").upper() or "T" not in ds
    start, _ = _parse_dt(ds)
    if all_day:
        end = _parse_dt(de)[0] + timedelta(days=1)
    else:
        end = _parse_dt(de)[0]
    return CalendarEvent(
        title=p.get("SUMMARY", "").strip() or "(untitled)",
        start=start,
        end=end,
        category=(p.get("CATEGORIES") or "personal").split(",")[0].strip(),
        notes=p.get("DESCRIPTION", "").strip(),
        uid=p.get("UID", ""),
    )


def calendar_to_ics(events: list[CalendarEvent]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for e in sorted(events, key=lambda e: (e.start, e.uid)):
        lines.extend(event_to_ics(e).split("\r\n"))
    lines.append("END:VCALENDAR")
    out: list[str] = []
    for line in lines:
        out.extend(_fold(line))
    return "\r\n".join(out) + "\r\n"


# --------------------------------------------------------------------------
# Store interface
# --------------------------------------------------------------------------


class CalendarStore(ABC):
    """Backend-agnostic calendar. ``list``/``conflicts`` use overlap
    semantics against a half-open window ``[start, end)``."""

    @abstractmethod
    def list(
        self, start: datetime, end: datetime, category: str | None = None
    ) -> list[CalendarEvent]:
        """Events overlapping the window, optionally filtered by category."""

    @abstractmethod
    def get(self, uid: str) -> CalendarEvent | None: ...

    @abstractmethod
    def create(self, event: CalendarEvent) -> CalendarEvent: ...

    @abstractmethod
    def update(self, uid: str, **fields: Any) -> CalendarEvent | None:
        """Replace named fields of an event by UID."""

    @abstractmethod
    def delete(self, uid: str) -> bool: ...

    def conflicts(
        self, start: datetime, end: datetime, category: str | None = None
    ) -> list[CalendarEvent]:
        """Events that would clash with a proposed ``[start, end)``."""
        return self.list(start, end, category=category)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for e in self.list(datetime(2000, 1, 1), datetime(2100, 1, 1)):
            if e.category and e.category not in seen:
                seen.append(e.category)
        return seen


# --------------------------------------------------------------------------
# ICS backend
# --------------------------------------------------------------------------


class _SkipResult:
    """Sentinel: the mutation found nothing to change → skip the write."""

    __repr__ = lambda self: "<skip>"


_SKIP = _SkipResult()


class _IcsLock:
    """Exclusive flock on a sidecar lock file (same pattern as Vault)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> "_IcsLock":
        if self._fd is None:
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        return False


class IcsCalendarStore(CalendarStore):
    """One ``.ics`` file on disk. The daemon is the only writer; every
    mutation is *read current file → merge → atomic replace* under an flock,
    so a human editing the file (any calendar tool, Obsidian, a phone) in the
    meantime is preserved — the human always wins: we never clobber events we
    didn't create, and our own edit applies on top of the file's current
    contents, never a stale snapshot."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_name(path.name + ".plp.lock")

    # ------------------------------------------------------------- plumbing

    def _read(self) -> list[CalendarEvent]:
        if not self.path.exists():
            return []
        return parse_ics(self.path.read_text(encoding="utf-8"))

    def _write(self, events: list[CalendarEvent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ics-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(calendar_to_ics(events))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, fn) -> Any:
        """Run ``fn(events) -> events | _SKIP`` on the fresh file under lock."""
        with _IcsLock(self._lock_path):
            events = self._read()
            result = fn(events)
            if result is not _SKIP:
                self._write(result)
            return result

    # --------------------------------------------------------------- API

    def list(self, start, end, category=None):
        out = [e for e in self._read() if e.overlaps(start, end)]
        if category:
            out = [e for e in out if e.category == category]
        return sorted(out, key=lambda e: (e.start, e.uid))

    def get(self, uid):
        for e in self._read():
            if e.uid == uid:
                return e
        return None

    def create(self, event: CalendarEvent) -> CalendarEvent:
        if any(e.uid == event.uid for e in self._read()):
            raise ValueError(f"event uid already exists: {event.uid}")

        def apply(events):
            events.append(event)
            return events

        self._mutate(apply)
        return event

    def update(self, uid, **fields):
        editable = ("title", "start", "end", "category", "notes")
        if not fields:
            return self.get(uid)

        def apply(events):
            for i, e in enumerate(events):
                if e.uid != uid:
                    continue
                updated = {
                    "title": e.title,
                    "start": e.start,
                    "end": e.end,
                    "category": e.category,
                    "notes": e.notes,
                    "uid": e.uid,
                }
                for k, v in fields.items():
                    if k in editable:
                        updated[k] = v
                events[i] = CalendarEvent(**updated)  # re-validated
                return events
            return _SKIP

        out = self._mutate(apply)
        return None if out is _SKIP else self.get(uid)

    def delete(self, uid) -> bool:
        def apply(events):
            keep = [e for e in events if e.uid != uid]
            return keep if len(keep) != len(events) else _SKIP

        return self._mutate(apply) is not _SKIP


# --------------------------------------------------------------------------
# Google backend (scaffold — PRD.md §8 Phase 4)
# --------------------------------------------------------------------------

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/{cid}/events"


class GoogleNotConfigured(RuntimeError):
    """The Google backend is selected but the owner hasn't finished setup."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Google calendar is not configured ({detail}). Follow "
            "docs/google-calendar-setup.md, then set calendar.google.enabled: true."
        )


class GoogleCalendarStore(CalendarStore):
    """Same interface as the ICS store; talks to the Google Calendar REST API
    with an OAuth2 refresh-token grant. Inert — raises
    :class:`GoogleNotConfigured` — until the owner has created the GCP project
    and OAuth consent screen, generated credentials, run the one-time
    authorization, and saved the JSON to ``calendar.google.credentials_file``.
    ``docs/google-calendar-setup.md`` walks through every step."""

    def __init__(
        self,
        credentials_path: Path | None,
        token_path: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._log = logger or log
        self._creds: dict = {}
        self._access_token: str | None = None

    # ------------------------------------------------------------ config

    def _ensure_configured(self) -> dict:
        if self.credentials_path is None or not self.credentials_path.exists():
            raise GoogleNotConfigured(
                f"credentials file {self.credentials_path} not found (setup doc, step 4)"
            )
        self._creds = json.loads(self.credentials_path.read_text())
        for key in ("client_id", "client_secret", "refresh_token", "calendar_id"):
            if not self._creds.get(key):
                raise GoogleNotConfigured(f"missing {key!r} in credentials file (setup doc, step 4)")
        return self._creds

    def _refresh_token(self) -> str:
        import httpx

        if self._access_token:
            return self._access_token
        c = self._ensure_configured()
        r = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "refresh_token": c["refresh_token"],
            },
            timeout=20.0,
        )
        r.raise_for_status()
        self._access_token = r.json()["access_token"]
        return self._access_token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._refresh_token()}"}

    # --------------------------------------------------------------- CRUD

    def _parse_remote(self, item: dict) -> CalendarEvent:
        ds = item.get("start", {})
        de = item.get("end", {})
        d1 = datetime.fromisoformat(ds.get("date") or ds["dateTime"].replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(de.get("date") or de["dateTime"].replace("Z", "+00:00"))
        if "date" in ds:
            d2 += timedelta(days=1)
        cats = item.get("categories") or []
        return CalendarEvent(
            title=item.get("summary") or "(untitled)",
            start=d1,
            end=d2,
            category=(cats[0] if cats else "personal"),
            notes=item.get("description", "").strip(),
            uid=item.get("id", ""),
        )

    def _payload(self, event: CalendarEvent) -> dict:
        p: dict[str, Any] = {
            "summary": event.title,
            "start": (
                {"date": event.start.date().isoformat()}
                if event.all_day
                else {"dateTime": event.start.isoformat()}
            ),
            "end": (
                {"date": event.end.date().isoformat()}
                if event.all_day
                else {"dateTime": event.end.isoformat()}
            ),
        }
        if event.notes:
            p["description"] = event.notes
        if event.category:
            p["categories"] = [event.category]
        return p

    def list(self, start, end, category=None):
        import httpx

        c = self._ensure_configured()
        r = httpx.get(
            GOOGLE_CALENDAR_URL.format(cid=c["calendar_id"]),
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
            },
            headers=self._auth_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        events = [self._parse_remote(i) for i in r.json().get("items", [])]
        if category:
            events = [e for e in events if e.category == category]
        return events

    def get(self, uid):
        import httpx

        c = self._ensure_configured()
        r = httpx.get(
            f"{GOOGLE_CALENDAR_URL.format(cid=c['calendar_id'])}/{uid}",
            headers=self._auth_headers(),
            timeout=30.0,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._parse_remote(r.json())

    def create(self, event: CalendarEvent) -> CalendarEvent:
        import httpx

        c = self._ensure_configured()
        if not event.uid:
            event.uid = f"plp-{uuid.uuid4().hex}@google"
        r = httpx.post(
            GOOGLE_CALENDAR_URL.format(cid=c["calendar_id"]),
            json=self._payload(event),
            headers=self._auth_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        event.uid = r.json().get("id", event.uid)
        return event

    def update(self, uid, **fields):
        import httpx

        c = self._ensure_configured()
        current = self.get(uid)
        if current is None:
            return None
        for k in ("title", "start", "end", "category", "notes"):
            if k in fields:
                setattr(current, k, fields[k])
        r = httpx.put(
            f"{GOOGLE_CALENDAR_URL.format(cid=c['calendar_id'])}/{uid}",
            json=self._payload(current),
            headers=self._auth_headers(),
            timeout=30.0,
        )
        r.raise_for_status()
        return self._parse_remote(r.json())

    def delete(self, uid) -> bool:
        import httpx

        c = self._ensure_configured()
        r = httpx.delete(
            f"{GOOGLE_CALENDAR_URL.format(cid=c['calendar_id'])}/{uid}",
            headers=self._auth_headers(),
            timeout=30.0,
        )
        return r.status_code in (200, 204)


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def open_calendar_store(config: PlpConfig, logger: logging.Logger | None = None) -> CalendarStore:
    """Config-driven backend selection. Google is used only when explicitly
    enabled *and* fully configured; any gap falls back to the ICS backend with
    a warning — the assistant never loses its calendar over a config hole."""
    logger = logger or log
    cal = config.calendar
    if cal.backend == "google" and cal.google.enabled:
        try:
            store = GoogleCalendarStore(
                resolve(config, cal.google.credentials_file)
                if cal.google.credentials_file
                else None,
                resolve(config, cal.google.token_file),
                logger,
            )
            store._ensure_configured()
            logger.info("calendar: Google backend active (%s)", cal.google.credentials_file)
            return store
        except (GoogleNotConfigured, OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("calendar: Google backend not ready (%s); using ICS", exc)
    return IcsCalendarStore(resolve(config, cal.ics_file))
