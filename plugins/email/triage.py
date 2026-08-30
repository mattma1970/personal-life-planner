"""Deterministic email triage (Phase 6) — stdlib only.

Turns a decoded message into flags and extracted facts:

- ``needs_reply``  direct address, a question, or an ask (confirm/reply/...)
- ``date``         an extractable calendar date (ISO / numeric / month name /
                   relative), resolved to naive-local time; a calendar
                   *proposal* is built only for concretely derivable dates —
                   never a guess
- ``rsvp``         an RSVP/confirm-by deadline → short reminder block
- ``birthday``     birthday/anniversary mention with a date → gifts block
- ``life``         life-relevant keyword hits (wife, family, dentist, ...)
- ``noise``        listserv / newsletter / no-reply / promo

Everything here is the non-LLM floor: the daily scan runs on this alone.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- vocabulary

#: Life-relevant terms checked against subject+body (word-boundary, case-insensitive).
LIFE_KEYWORDS: tuple[str, ...] = (
    "wife",
    "family",
    "mom",
    "mother",
    "dad",
    "father",
    "daughter",
    "son",
    "anniversary",
    "birthday",
    "lunch",
    "dinner",
    "brunch",
    "dentist",
    "doctor",
    "appointment",
    "gift",
    "wedding",
    "funeral",
    "flight",
    "school",
)

_NOISE_FROM = re.compile(
    r"(noreply|no[-_. ]?reply|donotreply|do[-_. ]?not[-_. ]?reply|newsletter|marketing"
    r"|press@|billing@|receipts?@|security@|newsletters?@|updates?@|alerts?@)",
    re.I,
)
_NOISE_SUBJECT = re.compile(
    r"(\[newsletter\]|\[no[- ]reply\]|unsubscribe|your (?:free |)trial|promo|discount code)",
    re.I,
)

_QUESTION = re.compile(r"\?")
_ASK = re.compile(
    r"\b(please|could you|can you|would you|let me know|confirm|reply|respond|when are you free)\b",
    re.I,
)
_DIRECT = re.compile(r"\b(you|your)\b", re.I)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_WEEKDAY_ALT = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    "|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun"
)
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

# (compiled regex, resolver(match, ref) -> date-or-None) — tried in order.
_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_US_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
_MON_DAY_RE = re.compile(rf"({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}}))?", re.I)
_DAY_MON_RE = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?(?:,?\s*(\d{{4}}))?", re.I)
_REL_RE = re.compile(r"\b(today|tomorrow|tonight)\b", re.I)
_WEEKDAY_RE_C = re.compile(rf"\b(next\s+week\s+)?({_WEEKDAY_ALT})\b", re.I)

_NOON = re.compile(r"\bnoon\b", re.I)
_MIDNIGHT = re.compile(r"\bmidnight\b", re.I)
_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", re.I)
_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_AT_N = re.compile(r"\bat\s+(\d{1,2})\b", re.I)

_RSVS_KW = re.compile(r"\b(rsvp|confirm(?:ed)?|reply|respond|let me know)\b", re.I)
_OCCASION_KW = re.compile(r"\b(birthday|anniversary)\b", re.I)
#: Explicit past markers — a year-less date next to these is a memory, not a
#: next-occurrence, so the forward roll to next year must not fire.
_PAST = re.compile(r"\b(last\s+year|last\s+month|last\s+week|yesterday)\b", re.I)


# ---------------------------------------------------------------- result

@dataclass
class TriageItem:
    message_id: str
    sender: str
    subject: str
    date: dt.datetime | None  # the message's own date (naive local)
    flags: list[str] = field(default_factory=list)
    event: dt.datetime | None = None      # extracted event time (naive local)
    rsvp_by: dt.datetime | None = None    # RSVP/confirm-by deadline
    occasion: str | None = None           # "birthday" | "anniversary"
    life_terms: list[str] = field(default_factory=list)
    body_excerpt: str = ""

    @property
    def interesting(self) -> bool:
        """Worth surfacing (anything but pure noise)."""
        return any(f in self.flags for f in ("needs_reply", "date", "rsvp", "birthday", "life"))


# ---------------------------------------------------------------- time

def _time_in(text: str, evening: bool = False) -> tuple[int, int] | None:
    """Find a time of day in ``text`` (already stripped of the date span).

    ``evening`` (the sentence said "tonight"): a bare "at 8" without an
    explicit meridiem is read as PM, since no one means 08:00 by tonight.
    """
    if _NOON.search(text):
        return (12, 0)
    if _MIDNIGHT.search(text):
        return (0, 0)
    m = _AMPM.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if not (1 <= hour <= 12 and minute <= 59):
            return None
        if m.group(3).lower().startswith("p") and hour != 12:
            hour += 12
        if m.group(3).lower().startswith("a") and hour == 12:
            hour = 0
        return (hour, minute)
    m = _HHMM.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour <= 23 and minute <= 59:
            return (hour, minute)
    m = _AT_N.search(text)
    if m:
        hour = int(m.group(1))
        if 0 <= hour <= 23:
            if evening and hour < 12:
                hour += 12
            return (hour, 0)
    return None


def _build(year: int, month: int, day: int, ref: dt.datetime, horizon: int) -> dt.datetime | None:
    """Validate a candidate date against the ref window (naive local)."""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (ref.year - 1 <= year <= ref.year + 2):
        return None
    try:
        d = dt.date(year, month, day)
    except ValueError:
        return None
    if d < ref.date() - dt.timedelta(days=1):
        return None  # the past is not a proposal
    if d > ref.date() + dt.timedelta(days=horizon):
        return None
    return dt.datetime(year, month, day, 9, 0)  # 09:00 default, refined by caller


def _year_resolve(year: str | None, ref: dt.datetime) -> int:
    if year:
        return int(year)
    return ref.year


# ---------------------------------------------------------------- date

def extract_event(text: str, ref: dt.datetime, horizon: int = 365) -> dt.datetime | None:
    """Extract one concrete event time from ``text`` (subject + body).

    Conservative by design: the first pattern that yields a *valid* date in
    the forward window wins; unresolvable text yields None (no proposal).
    """
    t = f" {text} "
    no_roll = bool(_PAST.search(t))  # "… last year" → no next-year roll
    for pattern, resolver in (
        (_ISO_RE, _r_iso),
        (_US_RE, _r_us),
        (_MON_DAY_RE, _r_monday),
        (_DAY_MON_RE, _r_daymon),
        (_REL_RE, _r_rel),
        (_WEEKDAY_RE_C, _r_weekday),
    ):
        m = pattern.search(t)
        if not m:
            continue
        rest = f"{t[: m.start()]} {t[m.end():]}"
        event = resolver(m, ref, horizon, no_roll=no_roll)
        if event is None:
            continue
        evening = m.group(0).lower().strip() in ("tonight",)
        tm = _time_in(rest, evening=evening)
        if tm:
            event = event.replace(hour=tm[0], minute=tm[1], second=0, microsecond=0)
            # "tonight at 8" received after 17:00: the stated time is still
            # later *today* — keep today instead of the resolver's rollover.
            if evening and ref.hour >= 17 and tm[0] > ref.hour:
                event = event.replace(year=ref.year, month=ref.month, day=ref.day)
        elif evening:
            event = event.replace(hour=19, minute=0, second=0, microsecond=0)
        return event
    return None


def _r_iso(m, ref, horizon, no_roll=False):
    d = _build(int(m.group(1)), int(m.group(2)), int(m.group(3)), ref, horizon)
    return d


def _r_us(m, ref, horizon, no_roll=False):
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    return _build(year, month, day, ref, horizon)


def _r_monday(m, ref, horizon, no_roll=False):
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    year = _year_resolve(m.group(3), ref)
    d = _build(year, month, day, ref, horizon)
    if d is None and m.group(3) is None and not no_roll:
        # Year-less "June 2" that already passed this year → next year's
        # occurrence (the birthday reading), never a past proposal. An
        # explicit past marker ("last year") forbids the roll.
        d = _build(year + 1, month, day, ref, horizon)
    return d


def _r_daymon(m, ref, horizon, no_roll=False):
    month = _MONTHS[m.group(2).lower()]
    day = int(m.group(1))
    year = _year_resolve(m.group(3), ref)
    d = _build(year, month, day, ref, horizon)
    if d is None and m.group(3) is None and not no_roll:
        d = _build(year + 1, month, day, ref, horizon)
    return d


def _r_rel(m, ref, horizon, no_roll=False):
    word = m.group(1).lower()
    if word == "today":
        d = ref.date()
    elif word == "tonight":
        d = ref.date() if ref.hour < 17 else ref.date() + dt.timedelta(days=1)
    else:  # tomorrow
        d = ref.date() + dt.timedelta(days=1)
    try:
        return dt.datetime(d.year, d.month, d.day, 9, 0)
    except ValueError:
        return None


def _r_weekday(m, ref, horizon, no_roll=False):
    name = m.group(2).lower()
    target = _WEEKDAYS.get(name)
    if target is None:
        return None
    delta = (target - ref.weekday()) % 7
    if delta == 0:
        delta = 7  # "Friday" said on a Friday means next week
    if m.group(1):  # explicit "next week ..."
        delta += 7
    d = ref.date() + dt.timedelta(days=delta)
    try:
        return dt.datetime(d.year, d.month, d.day, 9, 0)
    except ValueError:
        return None


# ---------------------------------------------------------------- rsvp / occasions

def _compact_date(window: str, ref: dt.datetime) -> dt.datetime | None:
    """Date finder for short windows (RSVP / occasion phrases)."""
    no_roll = bool(_PAST.search(window))
    for pattern, resolver in (
        (_ISO_RE, _r_iso),
        (_US_RE, _r_us),
        (_MON_DAY_RE, _r_monday),
        (_DAY_MON_RE, _r_daymon),
        (_WEEKDAY_RE_C, _r_weekday),
    ):
        m = pattern.search(window)
        if m:
            d = resolver(m, ref, 365, no_roll=no_roll)
            if d is not None:
                return d
    return None


def extract_rsvp(text: str, ref: dt.datetime) -> dt.datetime | None:
    """An RSVP / confirm-by / reply-by deadline (→ 23:59 that day)."""
    for m in _RSVS_KW.finditer(text):
        window = text[m.end() : m.end() + 90].split(".")[0]
        d = _compact_date(window, ref)
        if d is not None:
            return d.replace(hour=23, minute=59, second=0, microsecond=0)
    return None


def extract_occasion(text: str, ref: dt.datetime) -> tuple[str, dt.datetime] | None:
    """A birthday/anniversary with a date → (kind, date at midnight)."""
    for m in _OCCASION_KW.finditer(text):
        window = text[max(0, m.start() - 60) : m.end() + 60]
        d = _compact_date(window, ref)
        if d is not None:
            # the time is not meaningful for an occasion — the *day* is
            return (m.group(1).lower(), d.replace(hour=0, minute=0, second=0, microsecond=0))
    return None


# ---------------------------------------------------------------- flags

def is_noise(sender: str, subject: str) -> bool:
    return bool(_NOISE_FROM.search(sender) or _NOISE_SUBJECT.search(subject))


def needs_reply(sender: str, subject: str, body: str) -> bool:
    if is_noise(sender, subject):
        return False
    head = f"{subject} {body[:400]}"
    if _QUESTION.search(subject):
        return True
    if _ASK.search(head):
        return True
    if _DIRECT.search(subject):
        return True
    return False


def life_terms(subject: str, body: str, extra: tuple[str, ...] = ()) -> list[str]:
    hay = f"{subject} {body}".lower()
    hits: list[str] = []
    seen: set[str] = set()
    for term in LIFE_KEYWORDS + tuple(extra):
        t = term.lower()  # configured keywords ("Mom") match lowercase text too
        if t in seen:
            continue
        seen.add(t)
        if re.search(rf"\b{re.escape(t)}\b", hay):
            hits.append(t)
            if len(hits) == 3:
                break
    return hits


# ---------------------------------------------------------------- triage

def triage_message(
    message_id: str,
    sender: str,
    subject: str,
    msg_date: dt.datetime | None,
    body: str,
    ref: dt.datetime,
    extra_keywords: tuple[str, ...] = (),
) -> TriageItem:
    """Run all deterministic detectors over one message.

    ``ref`` is the naive-local "now" the dates are resolved against.
    """
    item = TriageItem(
        message_id=message_id,
        sender=sender or "unknown",
        subject=(subject or "").strip(),
        date=msg_date,
        body_excerpt=body[:300].replace("\n", " ").strip(),
    )
    full = f"{item.subject} {body}"
    if is_noise(sender, subject):
        item.flags.append("noise")
    if needs_reply(sender, subject, body):
        item.flags.append("needs_reply")
    item.life_terms = life_terms(subject, body, extra_keywords)
    if item.life_terms:
        item.flags.append("life")
    item.event = extract_event(full, ref)
    if item.event is not None:
        item.flags.append("date")
    item.rsvp_by = extract_rsvp(full, ref)
    if item.rsvp_by is not None:
        item.flags.append("rsvp")
    occ = extract_occasion(full, ref)
    if occ is not None:
        item.occasion = occ[0]
        item.flags.append("birthday")
        if item.event is None:
            item.event = occ[1]
    return item


# ---------------------------------------------------------------- proposals

_FAMILY_TERMS = {
    "wife", "family", "mom", "mother", "dad", "father",
    "daughter", "son", "wedding", "funeral", "school",
}


def _category(item: TriageItem) -> str:
    if "birthday" in item.flags:
        return "gifts"  # birthdays/anniversaries score against the gifts goal
    if set(item.life_terms) & _FAMILY_TERMS:
        return "family"
    return "personal"


def _clean_title(subject: str, limit: int = 60) -> str:
    s = subject.strip()
    while True:  # peel the whole "Re: Fwd: Re:" chain, not just the first
        s2 = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s, flags=re.I)
        if s2 == s:
            break
        s = s2
    s = s.strip()
    return (s[: limit - 3] + "...") if len(s) > limit else (s or "(no subject)")


def propose_from_item(item: TriageItem) -> dict | None:
    """Calendar-block proposal payload for a triaged item (or None).

    Priority: extracted event date; else an RSVP deadline (short 09:00
    reminder block) — an RSVP with no concrete event date still earns a
    proposal, per the Phase-6 spec.
    """
    if item.event is not None:
        start = item.event
        duration_h = 1
    elif item.rsvp_by is not None:
        start = item.rsvp_by.replace(hour=9, minute=0, second=0, microsecond=0)
        duration_h = 0.5
    else:
        return None

    notes = f"Email from {item.sender}: {item.subject[:60]}"
    if item.rsvp_by is not None and item.event != item.rsvp_by:
        notes += f" | RSVP by {item.rsvp_by:%Y-%m-%d}"
    if item.occasion:
        notes += f" | {item.occasion}"

    return {
        "title": _clean_title(item.subject),
        "start": start.isoformat(timespec="seconds"),
        "end": (start + dt.timedelta(hours=duration_h)).isoformat(timespec="seconds"),
        "category": _category(item),
        "notes": notes,
        "source": f"email:{item.message_id}",
    }
