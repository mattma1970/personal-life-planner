"""Feed parsing — RSS 2.0 and Atom, stdlib only (Phase 2, PRD.md S1).

Deliberately dependency-free: the handful of sources PLP tracks all speak
plain RSS/Atom. CDATA and inline HTML in descriptions are cleaned.
"""

from __future__ import annotations

import email.utils
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

_ATOM = "http://www.w3.org/2005/Atom"
_NS = {"atom": _ATOM}
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Article:
    title: str
    url: str
    summary: str = ""
    published_at: str | None = None  # ISO-8601 UTC, or None when the feed lacks dates


def clean(text: str | None) -> str:
    """Unescape entities, strip inline HTML, collapse whitespace."""
    if not text:
        return ""
    text = html.unescape(text)  # entities first: escaped tags become real tags
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_date(raw: str | None) -> str | None:
    """Normalize RSS (RFC 822) or Atom/W3C dates to ISO-8601 UTC. None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(raw)  # RSS: "Mon, 09 Jul 2026 12:34:56 GMT"
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # Atom: "2026-07-09T12:34:56Z"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def parse_feed(xml_text: str) -> list[Article]:
    """Parse an RSS 2.0 or Atom feed. Raises ValueError on unparseable input."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable feed XML: {exc}") from exc

    if root.tag == f"{{{_ATOM}}}feed":
        out: list[Article] = []
        for e in root.findall("atom:entry", _NS):
            title = clean(e.findtext("atom:title", "", _NS))
            link = e.find("atom:link[@rel='alternate']", _NS)
            if link is None:
                link = e.find("atom:link", _NS)
            url = (link.get("href") or "").strip() if link is not None else ""
            summary = clean(
                e.findtext("atom:summary", "", _NS)
                or e.findtext("atom:content", "", _NS)
            )
            published = parse_date(
                e.findtext("atom:published", "", _NS)
                or e.findtext("atom:updated", "", _NS)
            )
            if title and url:
                out.append(Article(title, url, summary[:500], published))
        return out  # a valid Atom feed may be empty right now

    if root.tag != "rss":
        raise ValueError(f"unrecognized feed (root <{root.tag}>, no items)")
    # A valid RSS feed can be legitimately empty — e.g. arXiv publishes
    # <skipDays> and serves an item-less channel on weekends.
    out = []
    for it in root.findall(".//item"):
        title = clean(it.findtext("title"))
        url = (it.findtext("link") or "").strip()
        summary = clean(it.findtext("description"))
        published = parse_date(it.findtext("pubDate") or it.findtext("updated"))
        if title and url:
            out.append(Article(title, url, summary[:500], published))
    return out
