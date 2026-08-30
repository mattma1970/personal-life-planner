"""News sources: the built-in list and the config override (PRD.md S1).

RSS-first; Anthropic publishes no RSS feed, so it is an HTML newsroom source
(the parser below reads the tag structure inside each ``/news/<slug>`` anchor,
which is stable even though their CSS class names are hashed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str = "rss"  # rss | html
    weight: float = 1.0


#: AI-focused defaults. Weights nudge ranking; see plugin.py::score_article.
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("OpenAI", "https://openai.com/news/rss.xml", weight=1.2),
    Source("Anthropic", "https://www.anthropic.com/news", kind="html", weight=1.2),
    Source("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    Source("Google Research", "https://research.google/blog/rss/"),
    Source("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    Source("arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", weight=0.8),
    Source("Hacker News", "https://news.ycombinator.com/rss", weight=0.8),
    Source("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss", weight=0.6),
)


def from_config(news_cfg) -> list[Source]:
    """Config-declared sources replace the built-ins wholesale."""
    if not news_cfg.sources:
        return list(DEFAULT_SOURCES)
    return [
        Source(s.name, s.url, s.kind or "rss", float(s.weight)) for s in news_cfg.sources
    ]


class _NewsroomParser(HTMLParser):
    """Collect (url, title, summary, date) from ``<a href="{prefix}...">`` blocks.

    Captures by tag, not by (hashed) class name: ``<time>`` → date, the first
    heading → title, the first ``<p>`` → summary, inside each target anchor.
    """

    def __init__(self, base_url: str, prefix: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.prefix = prefix
        self.items: list[dict] = []
        self._seen: set[str] = set()
        self._active: dict | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag, attrs):
        if self._active is not None:
            if tag == "time":
                self._capture = "date"
            elif tag in ("h1", "h2", "h3", "h4", "h5") and not self._active["title"]:
                self._capture = "title"
            elif tag == "p" and not self._active["summary"]:
                self._capture = "summary"
            return
        href = dict(attrs).get("href", "")
        if href.startswith(self.prefix):
            url = urljoin(self.base_url, href)
            if url in self._seen:
                return
            self._seen.add(url)
            self._active = {"url": url, "title": "", "summary": "", "date": ""}
            self._capture = None

    def handle_data(self, data):
        if self._active is None or self._capture is None:
            return
        text = data.strip()
        if not text:
            return
        field = {"date": "date", "title": "title", "summary": "summary"}[self._capture]
        if not self._active[field]:
            self._active[field] = text

    def handle_endtag(self, tag):
        if self._active is None:
            return
        if self._capture == "date" and tag == "time":
            self._capture = None
        elif self._capture == "title" and tag in ("h1", "h2", "h3", "h4", "h5"):
            self._capture = None
        elif self._capture == "summary" and tag == "p":
            self._capture = None
        if tag == "a":
            if self._active.get("title"):
                self.items.append(self._active)
            self._active = None
            self._capture = None


def parse_newsroom_html(html_text: str, base_url: str, prefix: str = "/news/") -> list[dict]:
    """Parse an HTML newsroom page into article dicts (no dates → None).

    Returns dicts ``{title, url, summary, published_at}`` — the same shape the
    plugin normalizes from RSS articles. Never raises on malformed HTML;
    returns whatever it could parse.
    """
    p = _NewsroomParser(base_url, prefix)
    p.feed(html_text)
    out = []
    for it in p.items:
        published = _newsroom_date(it["date"])
        out.append(
            {
                "title": it["title"],
                "url": it["url"],
                "summary": it["summary"][:500],
                "published_at": published,
            }
        )
    return out


def _newsroom_date(raw: str) -> str | None:
    """'Jul 24, 2026' → ISO date at 00:00 UTC (newsroom pages carry date only)."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip()[:16], "%b %d, %Y")
        return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None
