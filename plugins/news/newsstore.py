"""News persistence: plugin-owned tables on the shared state DB (PRD.md §5).

Tables (migration namespace ``plugin.news``):

- ``news`` — one row per distinct article. Dedupe is two-keyed: normalized
  URL (same story, same source) and title key (same story, different
  source). Re-fetches refresh ``last_seen`` and may raise the score; rows
  untouched for 7 days stop matching the title-key dedupe so genuinely new
  coverage is never blocked by a stale duplicate.
- ``news_fts`` — FTS5 external-content index over title+summary (triggers
  keep it in sync); search degrades to LIKE when FTS5 is unavailable.
- ``news_source_status`` — per-source health for the digest's source line.
"""

from __future__ import annotations

import re
import sqlite3
import logging
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from plp.kernel.store import Store
from plp.kernel.util import parse_ts, utcnow_iso

log = logging.getLogger("plp.news.store")

_NEWS_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title_key TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            published_at TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_news_score ON news(score DESC);
        CREATE INDEX IF NOT EXISTS idx_news_seen ON news(last_seen);
        CREATE TABLE IF NOT EXISTS news_source_status (
            name TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            error TEXT,
            last_checked TEXT NOT NULL
        );
        """,
    ),
]

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "ref", "source", "cmpid"}


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop tracking params and fragments, strip trailing slash."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path or "/", urlencode(query), ""))


def title_key(title: str) -> str:
    """Lowercase alnum-only key used for cross-source story dedupe."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


class NewsStore(Store):
    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        self.migrate_core()  # self-contained on a fresh DB; no-op after the kernel ran it
        self.migrate_for("news", _NEWS_MIGRATIONS)
        self.news_fts = self._setup_news_fts()

    def _setup_news_fts(self) -> bool:
        if not self.fts5:
            return False
        try:
            self.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5("
                "title, summary, content='news', content_rowid='id')"
            )
            self.execute(
                "CREATE TRIGGER IF NOT EXISTS news_fts_ai AFTER INSERT ON news BEGIN"
                " INSERT INTO news_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary); END"
            )
            self.execute(
                "CREATE TRIGGER IF NOT EXISTS news_fts_ad AFTER DELETE ON news BEGIN"
                " INSERT INTO news_fts(news_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary); END"
            )
            self.execute(
                "CREATE TRIGGER IF NOT EXISTS news_fts_au AFTER UPDATE ON news BEGIN"
                " INSERT INTO news_fts(news_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);"
                " INSERT INTO news_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary); END"
            )
            return True
        except sqlite3.OperationalError as exc:
            log.warning("news FTS5 unavailable (%s); search degrades to LIKE", exc)
            return False

    # ----------------------------------------------------------------- items

    def upsert_articles(self, source: str, items: list[dict]) -> tuple[int, int]:
        """Insert/refresh articles. Items: ``{url, title, summary, published_at, score}``.

        Returns ``(new, refreshed)``.
        """
        now = utcnow_iso()
        now_dt = parse_ts(now)
        dedupe_cutoff = (now_dt - timedelta(days=7)).isoformat(timespec="milliseconds")
        new = refreshed = 0
        for it in items:
            url = normalize_url(it.get("url", ""))
            key = title_key(it.get("title", ""))
            if not url or not key:
                continue
            row = self.query_one(
                "SELECT id, url FROM news WHERE (url = ? OR title_key = ?) AND last_seen >= ?",
                (url, key, dedupe_cutoff),
            )
            if row is None:
                stale = self.query_one("SELECT id FROM news WHERE url = ?", (url,))
                if stale is not None:
                    self.execute(
                        "UPDATE news SET title=?, title_key=?, summary=?, source=?,"
                        " published_at=?, first_seen=?, last_seen=?, score=? WHERE id=?",
                        (
                            it["title"],
                            key,
                            it.get("summary", ""),
                            source,
                            it.get("published_at"),
                            it.get("first_seen", now),
                            now,
                            float(it.get("score", 0.0)),
                            stale["id"],
                        ),
                    )
                    refreshed += 1
                else:
                    self.execute(
                        "INSERT INTO news(url, title_key, title, summary, source,"
                        " published_at, first_seen, last_seen, score)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            url,
                            key,
                            it["title"],
                            it.get("summary", ""),
                            source,
                            it.get("published_at"),
                            it.get("first_seen", now),
                            now,
                            float(it.get("score", 0.0)),
                        ),
                    )
                    new += 1
            else:
                if row["url"] != url:
                    # same story from another source: keep the row, best score wins
                    self.execute(
                        "UPDATE news SET score = max(score, ?), last_seen = ? WHERE id = ?",
                        (float(it.get("score", 0.0)), now, row["id"]),
                    )
                else:
                    self.execute(
                        "UPDATE news SET last_seen = ?,"
                        " score = max(score, ?),"
                        " summary = CASE WHEN summary = '' THEN ? ELSE summary END,"
                        " published_at = COALESCE(?, published_at)"
                        " WHERE id = ?",
                        (now, float(it.get("score", 0.0)), it.get("summary", ""), it.get("published_at"), row["id"]),
                    )
                refreshed += 1
        return new, refreshed

    def top(self, limit: int, window_hours: float) -> list[dict]:
        cutoff = (
            parse_ts(utcnow_iso()) - timedelta(hours=window_hours)
        ).isoformat(timespec="milliseconds")
        rows = self.query(
            "SELECT * FROM news WHERE last_seen >= ? ORDER BY score DESC, last_seen DESC LIMIT ?",
            (cutoff, limit),
        )
        return [dict(r) for r in rows]

    def search(self, text: str, limit: int = 20) -> list[dict]:
        if self.news_fts:
            safe = text.replace('"', '""')
            rows = self.query(
                "SELECT n.* FROM news_fts f JOIN news n ON n.id = f.rowid"
                " WHERE news_fts MATCH ? ORDER BY rank LIMIT ?",
                (f'"{safe}"', limit),
            )
        else:
            like = f"%{text}%"
            rows = self.query(
                "SELECT * FROM news WHERE title LIKE ? OR summary LIKE ? ORDER BY score DESC LIMIT ?",
                (like, like, limit),
            )
        return [dict(r) for r in rows]

    def purge(self, days: float = 14) -> int:
        cutoff = (
            parse_ts(utcnow_iso()) - timedelta(days=days)
        ).isoformat(timespec="milliseconds")
        cur = self._conn().execute("DELETE FROM news WHERE last_seen < ?", (cutoff,))
        self._conn().commit()
        return cur.rowcount

    def stats(self) -> dict:
        row = self.query_one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN published_at IS NOT NULL THEN 1 ELSE 0 END) AS dated FROM news"
        )
        return {"total": row["total"] or 0, "dated": row["dated"] or 0}

    # ------------------------------------------------------------ source health

    def set_source_status(self, name: str, state: str, error: str | None) -> None:
        self.execute(
            "INSERT INTO news_source_status(name, state, error, last_checked) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET state=excluded.state,"
            " error=excluded.error, last_checked=excluded.last_checked",
            (name, state, error, utcnow_iso()),
        )

    def source_statuses(self) -> list[dict]:
        rows = self.query("SELECT * FROM news_source_status ORDER BY name")
        return [dict(r) for r in rows]
