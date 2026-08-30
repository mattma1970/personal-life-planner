"""News plugin tests (Phase 2): feed parsing, dedupe/scoring store,
per-source fetch isolation, digest skeleton — no network, no LLM."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plp.kernel.bus import EventBus
from plp.kernel.config import NewsConfig, NewsSourceCfg, PlpConfig
from plp.kernel.context import PluginContext
from plp.kernel.plugin import load_sibling
from plp.kernel.store import Store
from plp.kernel.util import utcnow_iso

PLUGINS = Path(__file__).resolve().parent.parent / "plugins" / "news"


def _load(fname: str) -> object:
    modname = f"plp.plugins.news.{Path(fname).stem}"
    return load_sibling(modname, PLUGINS / fname)  # sys.modules-cached


feedxml = _load("feedxml.py")
sources_mod = _load("sources.py")
newsstore_mod = _load("newsstore.py")
digest_mod = _load("digest.py")
plugin_mod = _load("plugin.py")

RSS2_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <link>http://example.com</link>
  <item>
    <title>Model release: GPT-6 arrives</title>
    <link>http://example.com/posts/1?utm_source=rss&amp;fbclid=zz</link>
    <description><![CDATA[<p>We release a new <b>model</b> today.</p>]]></description>
    <pubDate>Mon, 09 Jul 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Office cat video</title>
    <link>http://example.com/posts/2</link>
    <description>cats are nice</description>
    <pubDate>Mon, 09 Jul 2026 11:00:00 GMT</pubDate>
  </item>
  <item>
    <link>http://example.com/posts/notitle</link>
  </item>
</channel></rss>"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Test</title>
  <entry>
    <title>DeepMind announces new reasoning benchmark</title>
    <link rel="alternate" href="https://deepmind.example/blog/1"/>
    <summary>A new benchmark for reasoning models.</summary>
    <published>2026-07-09T09:30:00Z</published>
  </entry>
</feed>"""

NEWSROOM_HTML = """<html><body>
<a href="/news/some-other-page-2024" class="x"><h4>Old link</h4></a>
<div class="hashed-class">
  <a href="/news/claude-opus-5" class="card">
    <div><span>Product</span><time datetime="2026-07-24">Jul 24, 2026</time></div>
    <h4>Introducing Claude Opus 5</h4>
    <p>Opus 5 is a step change improvement in agentic tasks.</p>
  </a>
  <a href="/news/claude-opus-5" class="list-card">
    <div><span>Product</span><time datetime="2026-07-24">Jul 24, 2026</time></div>
    <h4>Introducing Claude Opus 5</h4>
    <p>Opus 5 is a step change improvement in agentic tasks.</p>
  </a>
  <a href="/news/research-note" class="list-card">
    <div><time>Jan 5, 2026</time></div>
    <h3>Research note on scaling</h3>
    <p>Findings from the lab.</p>
  </a>
</div></body></html>"""


# ------------------------------------------------------------- feed parsing


def test_rss2_parse():
    arts = feedxml.parse_feed(RSS2_FIXTURE)
    assert len(arts) == 2  # the title-less item is dropped
    a = arts[0]
    assert a.title == "Model release: GPT-6 arrives"
    assert a.url == "http://example.com/posts/1?utm_source=rss&fbclid=zz"
    assert a.published_at == "2026-07-09T12:00:00+00:00"
    assert "new model today" in a.summary  # HTML stripped
    assert "<b>" not in a.summary


def test_atom_parse():
    arts = feedxml.parse_feed(ATOM_FIXTURE)
    assert len(arts) == 1
    assert arts[0].title.startswith("DeepMind")
    assert arts[0].url == "https://deepmind.example/blog/1"
    assert arts[0].published_at == "2026-07-09T09:30:00+00:00"
    assert "benchmark" in arts[0].summary


def test_rss_date_none_when_missing():
    xml = '<rss><channel><item><title>t</title><link>http://x/1</link></item></channel></rss>'
    (a,) = feedxml.parse_feed(xml)
    assert a.published_at is None


def test_bad_xml_raises():
    with pytest.raises(ValueError):
        feedxml.parse_feed("this is not xml <rss>")


def test_unrecognized_feed_raises():
    with pytest.raises(ValueError):
        feedxml.parse_feed("<foo><bar/></foo>")


def test_empty_rss_is_valid_not_an_error():
    # arXiv serves an item-less <rss> on weekends (<skipDays>)
    xml = '<rss version="2.0"><channel><title>x</title></channel></rss>'
    assert feedxml.parse_feed(xml) == []


# ---------------------------------------------------------------- url/title


def test_normalize_url():
    n = newsstore_mod.normalize_url
    assert n("https://Ex.com/a/b/?utm_source=rss&fbclid=zz&ref=y") == "https://ex.com/a/b"
    assert n("https://ex.com/a/") == "https://ex.com/a"
    assert n("http://ex.com/a?keep=1") == "http://ex.com/a?keep=1"


def test_title_key():
    assert newsstore_mod.title_key("Hello,  World!! 42") == "helloworld42"


# ------------------------------------------------------------------- store


@pytest.fixture
def news_store(tmp_path):
    s = newsstore_mod.NewsStore(tmp_path / "n.db")
    yield s
    s.close()


def _item(url, title, summary="", published=None, score=1.0):
    return {
        "url": url,
        "title": title,
        "summary": summary,
        "published_at": published,
        "score": score,
    }


def test_upsert_new_then_refresh(news_store):
    items = [
        _item("http://s1/a", "Alpha one", "s1", utcnow_iso(), 1.0),
        _item("http://s1/b", "Beta two", "s2", utcnow_iso(), 2.0),
    ]
    assert news_store.upsert_articles("S", items) == (2, 0)
    assert news_store.upsert_articles("S", items) == (0, 2)
    assert news_store.query_one("SELECT COUNT(*) c FROM news")["c"] == 2


def test_cross_source_title_dedupe_keeps_best_score(news_store):
    news_store.upsert_articles(
        "A", [_item("http://a/story-1", "Claude Opus 5 released", "a", utcnow_iso(), 0.8)]
    )
    assert news_store.upsert_articles(
        "B", [_item("http://b/story-2", "claude OPUS 5 released!!", "b", utcnow_iso(), 1.2)]
    ) == (0, 1)
    row = news_store.query_one("SELECT * FROM news WHERE title_key = ?", ("claudeopus5released",))
    assert row is not None
    assert row["url"] == "http://a/story-1"  # first source's URL wins
    assert row["score"] == pytest.approx(1.2)  # best score wins


def test_title_dedupe_window_expires(news_store):
    news_store.upsert_articles("A", [_item("http://a/old", "Weekly roundup", "", "2020-01-01T00:00:00+00:00", 0.5)])
    # age the row beyond the 7-day dedupe window (SET param first, WHERE param second)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="milliseconds")
    news_store.execute("UPDATE news SET last_seen = ? WHERE title_key = ?", (old, "weeklyroundup"))
    # a fresh article with the same title must NOT be blocked by the stale duplicate
    assert news_store.upsert_articles("B", [_item("http://b/fresh", "weekly ROUNDUP", "", utcnow_iso(), 0.9)]) == (1, 0)
    assert news_store.query_one("SELECT COUNT(*) c FROM news WHERE title_key = 'weeklyroundup'")["c"] == 2


def test_stale_url_resurrection_updates_in_place(news_store):
    news_store.upsert_articles("A", [_item("http://a/x", "Same story", "", "2020-01-01T00:00:00+00:00", 0.1)])
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="milliseconds")
    news_store.execute("UPDATE news SET last_seen = ? WHERE url = ?", (old, "http://a/x"))
    assert news_store.upsert_articles("A", [_item("http://a/x?utm_source=x", "Same story", "new summary", utcnow_iso(), 0.7)]) == (0, 1)
    assert news_store.query_one("SELECT COUNT(*) c FROM news")["c"] == 1
    assert news_store.query_one("SELECT summary FROM news WHERE url = 'http://a/x'")["summary"] == "new summary"


def test_top_orders_by_score_within_window(news_store):
    now = utcnow_iso()
    news_store.upsert_articles(
        "A",
        [
            _item("http://a/low", "Low score item", "", now, 0.2),
            _item("http://a/high", "High score item", "", now, 0.9),
        ],
    )
    rows = news_store.top(5, window_hours=48)
    assert [r["title"] for r in rows] == ["High score item", "Low score item"]
    # age both rows beyond a 1h window → nothing
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="milliseconds")
    news_store.execute("UPDATE news SET last_seen = ?", (old,))
    assert news_store.top(5, window_hours=1) == []


def test_purge_removes_old_rows(news_store):
    now = utcnow_iso()
    news_store.upsert_articles("A", [_item("http://a/new", "New item", "", now, 1.0)])
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="milliseconds")
    news_store.execute("UPDATE news SET last_seen = ? WHERE url = ?", (old, "http://a/new"))
    news_store.upsert_articles("B", [_item("http://b/keep", "Keep item", "", now, 0.5)])
    purged = news_store.purge(days=14)
    assert purged == 1
    (row,) = news_store.query("SELECT url FROM news")
    assert row["url"] == "http://b/keep"


def test_search(news_store):
    news_store.upsert_articles(
        "A",
        [
            _item("http://a/1", "Quantum error correction breakthrough", "physics detail", utcnow_iso(), 1.0),
            _item("http://a/2", "Cat video goes viral", "", utcnow_iso(), 0.5),
        ],
    )
    rows = news_store.search("quantum", limit=5)
    assert len(rows) == 1
    assert rows[0]["title"].startswith("Quantum")
    # a query that matches nothing is fine
    assert news_store.search("zzz-no-match-zzz") == []


def test_source_status_roundtrip(news_store):
    news_store.set_source_status("OpenAI", "ok", None)
    news_store.set_source_status("OpenAI", "failed", "boom: 500")
    (row,) = news_store.source_statuses()
    assert row["name"] == "OpenAI"
    assert row["state"] == "failed"
    assert row["error"] == "boom: 500"


# ----------------------------------------------------------------- scoring


def test_score_recency_and_keywords():
    score = plugin_mod.score_article
    fresh_neutral = score(1.0, 1.0, "Office cat video", "", 72.0)
    fresh_ai = score(1.0, 1.0, "New open-source LLM benchmark released", "", 72.0)
    old_ai = score(71.0, 1.0, "New open-source LLM benchmark released", "", 72.0)
    assert fresh_ai > fresh_neutral  # keyword boost
    assert fresh_neutral > old_ai  # recency dominates
    assert score(999.0, 1.0, "x", "", 72.0) == 0.0  # past max age → 0
    # weight scales the score
    assert score(1.0, 2.0, "t", "", 72.0) > score(1.0, 1.0, "t", "", 72.0)


def test_keyword_boost_is_capped():
    score = plugin_mod.score_article
    a = score(0.0, 1.0, "model model model model", "", 72.0)
    b = score(0.0, 1.0, "model " * 40, "", 72.0)
    assert a == b  # min(4, hits) caps the boost


# ------------------------------------------------------------- from_config


def test_from_config_defaults():
    srcs = sources_mod.from_config(NewsConfig())
    names = [s.name for s in srcs]
    assert "OpenAI" in names and "Anthropic" in names
    assert any(s.kind == "html" and s.name == "Anthropic" for s in srcs)
    assert len(srcs) == 8


def test_from_config_override():
    cfg = NewsConfig(sources=[NewsSourceCfg(name="X", url="http://x/feed", weight=2.0)])
    srcs = sources_mod.from_config(cfg)
    assert len(srcs) == 1
    assert srcs[0].weight == 2.0
    assert srcs[0].kind == "rss"


# ---------------------------------------------------------- newsroom HTML


def test_newsroom_html_parse():
    arts = sources_mod.parse_newsroom_html(NEWSROOM_HTML, "https://www.anthropic.com")
    # 3 distinct /news/ slugs; the duplicated card collapses to one
    urls = [a["url"] for a in arts]
    assert len(arts) == 3
    assert "https://www.anthropic.com/news/claude-opus-5" in urls
    assert len(urls) == len(set(urls))
    opus = next(a for a in arts if a["url"].endswith("/news/claude-opus-5"))
    assert opus["title"] == "Introducing Claude Opus 5"
    assert "step change" in opus["summary"]
    assert opus["published_at"] == "2026-07-24T00:00:00+00:00"


def test_newsroom_html_malformed_never_raises():
    arts = sources_mod.parse_newsroom_html("<a href='/news/broken'><h4>??</a>", "https://x.com")
    assert isinstance(arts, list)


# ------------------------------------------------------------------ plugin


def _ctx(tmp_path, cfg: PlpConfig):
    from plp.kernel.capability import Capability

    return PluginContext(
        store=Store(cfg.root / "data" / "n.db"),
        bus=EventBus(),
        config=cfg,
        delivery=None,
        capability=Capability.permissive(8),
    )


def _cfg(tmp_path) -> PlpConfig:
    cfg = PlpConfig()
    cfg.root = tmp_path
    cfg.news = NewsConfig(
        sources=[
            NewsSourceCfg(name="good", url="http://x/good"),
            NewsSourceCfg(name="bad", url="http://x/bad"),
        ]
    )
    return cfg


def test_collect_per_source_isolation(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    ctx = _ctx(tmp_path, cfg)
    p.setup(ctx)

    def fake_fetch(client, s):
        if s.name == "good":
            return [
                {
                    "title": "Fresh LLM release",
                    "url": "http://x/good/1",
                    "summary": "a model",
                    "published_at": utcnow_iso(),
                }
            ]
        raise RuntimeError("boom")

    events: list = []
    ctx.bus.subscribe("news.", lambda e, p: events.append((e, p)))
    p._fetch_one = fake_fetch
    result = p._collect(ctx, {})
    assert result["ok"] == 1
    assert result["failed"] == 1
    assert result["new"] == 1
    assert result["failures"][0]["source"] == "bad"
    assert "boom" in result["failures"][0]["error"]
    statuses = {r["name"]: r for r in p._store.source_statuses()}
    assert statuses["good"]["state"] == "ok"
    assert statuses["bad"]["state"] == "failed"
    assert "boom" in statuses["bad"]["error"]
    assert any(e == "news.collected" and p["new"] == 1 for e, p in events)


def test_collect_skips_stale_items(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    ctx = _ctx(tmp_path, cfg)
    p.setup(ctx)
    stale = (datetime.now(timezone.utc) - timedelta(hours=cfg.news.max_age_hours * 3)).isoformat(
        timespec="seconds"
    )
    fresh = utcnow_iso()

    def fake_fetch(client, s):
        return [
            {"title": "Stale post", "url": "http://x/good/s", "summary": "", "published_at": stale},
            {"title": "Fresh post", "url": "http://x/good/f", "summary": "", "published_at": fresh},
        ]

    p._fetch_one = fake_fetch
    result = p._collect(ctx, {})
    assert result["new"] == 1
    urls = [r["url"] for r in p._store.query("SELECT url FROM news")]
    assert urls == ["http://x/good/f"]


def test_plugin_jobs_and_tools_registered(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    p.setup(_ctx(tmp_path, cfg))
    jobs = [j.name for j in p.jobs()]
    assert "news.collect" in jobs and "daily.digest" in jobs
    tools = p.tools()
    names = [t.__name__ for t in tools]
    assert "news_search" in names and "news_top" in names
    assert [c.name for c in p.commands()] == ["news"]


def test_tool_news_top_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    ctx = _ctx(tmp_path, cfg)
    p.setup(ctx)
    p._store.upsert_articles(
        "X",
        [
            {"url": "http://x/1", "title": "A new model release", "summary": "s",
             "published_at": utcnow_iso(), "score": 0.9},
        ],
    )
    tool = next(t for t in p.tools() if t.__name__ == "news_top")
    out = tool(limit=5)
    import json

    data = json.loads(out)
    assert data[0]["title"] == "A new model release"
    assert data[0]["source"] == "X"


# ------------------------------------------------------------------ digest


def test_digest_skeleton_with_items(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    p.setup(_ctx(tmp_path, cfg))
    p._store.upsert_articles(
        "X",
        [
            {"url": "http://x/1", "title": "Top model release", "summary": "the big one",
             "published_at": utcnow_iso(), "score": 0.9},
        ],
    )
    p._store.set_source_status("good", "failed", "timeout")
    p._store.set_source_status("bad", "failed", "boom")
    text = digest_mod.build_digest_text(p._store, max_items=5, window_hours=48, llm=None)
    assert text.startswith("News —")
    assert "Top model release" in text
    assert "0/2 ok" in text
    assert "down:" in text
    assert "If you read one thing today: Top model release" in text


def test_digest_empty_window(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    p.setup(_ctx(tmp_path, cfg))
    text = digest_mod.build_digest_text(p._store, max_items=5, window_hours=48, llm=None)
    assert "(nothing new in the last window)" in text


def test_digest_llm_seasoning_and_degradation(tmp_path):
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    p.setup(_ctx(tmp_path, cfg))
    p._store.upsert_articles(
        "X",
        [
            {"url": "http://x/1", "title": "Model A release", "summary": "",
             "published_at": utcnow_iso(), "score": 0.9},
            {"url": "http://x/2", "title": "Benchmark B", "summary": "",
             "published_at": utcnow_iso(), "score": 0.7},
        ],
    )

    class GoodLLM:
        def available(self):
            return True

        def chat(self, messages, tools=None, temperature=0.4):
            return {
                "role": "assistant",
                "content": 'Sure! {"headline": "Big day for models", "whys": ["why a", "why b"], "action": "read the release notes"}',
            }

    text = digest_mod.build_digest_text(p._store, max_items=5, window_hours=48, llm=GoodLLM())
    assert "> Big day for models" in text
    assert "why a" in text and "why b" in text
    assert "If you do one thing about this: read the release notes" in text

    class BadLLM:
        def available(self):
            return True

        def chat(self, messages, tools=None, temperature=0.4):
            return {"role": "assistant", "content": "not json at all"}

    text = digest_mod.build_digest_text(p._store, max_items=5, window_hours=48, llm=BadLLM())
    assert "> Big day for models" not in text  # degraded to plain skeleton
    assert "Model A release" in text

    class DeadLLM:
        def available(self):
            return False

        def chat(self, *a, **k):
            raise AssertionError("should not be called")

    text = digest_mod.build_digest_text(p._store, max_items=5, window_hours=48, llm=DeadLLM())
    assert "Model A release" in text


class _CapturingDelivery:
    def __init__(self):
        self.sent = []

    def deliver(self, kind, text):
        self.sent.append((kind, text))


class _UnavailableLLM:
    def available(self):
        return False

    def chat(self, *a, **k):
        raise AssertionError("chat must not be called when unavailable")


def test_digest_job_delivers_and_saves(tmp_path, monkeypatch):
    import plp.kernel.llm as llm_mod

    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: _UnavailableLLM())
    cfg = _cfg(tmp_path)
    p = plugin_mod.NewsPlugin()
    ctx = _ctx(tmp_path, cfg)
    p.setup(ctx)
    p._store.upsert_articles(
        "X",
        [{"url": "http://x/1", "title": "A new model release", "summary": "big",
          "published_at": utcnow_iso(), "score": 0.9}],
    )
    delivery = _CapturingDelivery()
    ctx.delivery = delivery
    result = p._digest(ctx, {})
    assert result["delivered"] is True
    assert result["seasoned"] is False  # LLM unavailable → deterministic skeleton
    assert len(delivery.sent) == 1
    assert delivery.sent[0][0] == "digest"
    assert delivery.sent[0][1].startswith("News —")
    (row,) = p._store.query("SELECT content FROM digests WHERE kind = 'news'")
    assert row["content"].startswith("News —")
