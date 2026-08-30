"""News plugin — Phase 2 (PRD.md S1, phase 2).

Jobs
- ``news.collect`` — fetch every source (RSS-first, per-source isolation:
  one dead feed never blocks the rest), dedupe (URL + title key), score
  (recency × source weight × AI-relevance), store to the state DB.
- ``daily.digest`` — small, readable digest of the last window: top items
  by score, a source-health line, and ONE suggested action. The LLM
  (Qwen 27B) is seasoning: it adds a headline and per-item "why it matters"
  lines; unavailable → plain ranked list (PRD.md §6.5).

Sources are config-driven (``news.sources``); empty = the built-in AI-focused
list in ``sources.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from plp.kernel.config import resolve
from plp.kernel.plugin import Command, Job, Plugin, load_sibling, tool
from plp.kernel.util import parse_ts, utcnow_iso

_HERE = Path(__file__).resolve().parent
feedxml = load_sibling("plp.plugins.news.feedxml", _HERE / "feedxml.py")
sources_mod = load_sibling("plp.plugins.news.sources", _HERE / "sources.py")
newsstore_mod = load_sibling("plp.plugins.news.newsstore", _HERE / "newsstore.py")

USER_AGENT = "PLP/0.1 (+local personal-life-planner; news collector)"

#: Terms that nudge an article toward the front of the digest.
AI_TERMS = (
    "llm", "model", "release", "agent", "benchmark", "paper", "arxiv",
    "open source", "open-source", "open weights", "open-weights", "training",
    "inference", "quantiz", "gpt", "claude", "gemini", "deepseek", "qwen",
    "mistral", "llama", "transformer", "fine-tun", "eval", "reasoning",
    "multimodal", "diffusion", "world model",
)


def score_article(age_hours: float, weight: float, title: str, summary: str, max_age_hours: float) -> float:
    """recency × source weight × relevance — deterministic, no LLM involved."""
    recency = max(0.0, 1.0 - (age_hours / max_age_hours)) if age_hours is not None else 0.0
    text = f"{title} {summary}".lower()
    hits = sum(1 for t in AI_TERMS if t in text)
    relevance = 1.0 + 0.25 * min(4, hits)
    return round(weight * recency * relevance, 4)


class NewsPlugin(Plugin):
    name = "news"

    def setup(self, ctx) -> None:
        self._store = newsstore_mod.NewsStore(resolve(ctx.config, ctx.config.state_db.path))
        self._news_cfg = ctx.config.news
        self._sources = sources_mod.from_config(ctx.config.news)
        ctx.log("news: %d source(s) (%s)", len(self._sources), ", ".join(s.name for s in self._sources))

    # ------------------------------------------------------------------ fetch

    def _fetch_one(self, client: httpx.Client, s: "sources_mod.Source") -> list[dict]:
        r = client.get(s.url)
        r.raise_for_status()
        if s.kind == "rss":
            return [
                {"title": a.title, "url": a.url, "summary": a.summary, "published_at": a.published_at}
                for a in feedxml.parse_feed(r.text)
            ]
        return sources_mod.parse_newsroom_html(r.text, s.url, "/news/")

    def _collect(self, ctx, args: dict) -> dict:
        cfg = self._news_cfg
        now = parse_ts(utcnow_iso())
        new_total = refreshed_total = 0
        failures: list[dict] = []
        with httpx.Client(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            follow_redirects=True,
        ) as client:
            for s in self._sources:
                try:
                    raw = self._fetch_one(client, s)
                    items: list[dict] = []
                    for a in raw[: cfg.per_source_limit]:
                        pub = a.get("published_at")
                        age_h = (now - parse_ts(pub)).total_seconds() / 3600 if pub else 0.0
                        if pub and age_h > cfg.max_age_hours * 2:
                            continue  # stale item from a slow feed: skip entirely
                        items.append(
                            {
                                **a,
                                "score": score_article(age_h, s.weight, a["title"], a.get("summary", ""), cfg.max_age_hours),
                            }
                        )
                    new_n, refreshed_n = self._store.upsert_articles(s.name, items)
                    self._store.set_source_status(s.name, "ok", None)
                    new_total += new_n
                    refreshed_total += refreshed_n
                    ctx.log("  %s: %d item(s) (%d new)", s.name, len(items), new_n)
                except Exception as exc:  # noqa: BLE001 - per-source isolation (PRD.md S1)
                    err = f"{type(exc).__name__}: {exc}"
                    self._store.set_source_status(s.name, "failed", err)
                    failures.append({"source": s.name, "error": err})
                    ctx.log("  %s FAILED: %s", s.name, err)
        purged = self._store.purge(days=14)
        ctx.bus.publish("news.collected", {"new": new_total, "failed": len(failures)})
        return {
            "sources": len(self._sources),
            "ok": len(self._sources) - len(failures),
            "failed": len(failures),
            "new": new_total,
            "refreshed": refreshed_total,
            "purged": purged,
            "failures": failures,
        }

    # ------------------------------------------------------------------- jobs

    def jobs(self) -> list[Job]:
        return [
            Job(name="news.collect", handler=self._collect, cron="0 7 * * *", timeout_s=300, staleness_h=36),
            Job(name="daily.digest", handler=self._digest, cron="5 7 * * *", timeout_s=180, staleness_h=36),
        ]

    def _digest(self, ctx, args: dict) -> dict:
        digest_mod = load_sibling(
            "plp.plugins.news.digest", _HERE / "digest.py"
        )  # deferred: keeps boot light
        cfg = self._news_cfg
        text = digest_mod.build_digest_text(
            self._store,
            max_items=cfg.digest_max_items,
            window_hours=cfg.digest_window_hours,
            llm=None,
        )
        if ctx.delivery is not None:
            ctx.delivery.deliver("digest", text)
        self._store.save_digest("news", text)
        return {"delivered": text.strip() != "", "chars": len(text)}

    # ------------------------------------------------------------------ tools

    def tools(self) -> list:
        store = self._store
        window = self._news_cfg.digest_window_hours

        @tool("Search collected news headlines and summaries (full-text). Returns a JSON array of {title, source, published_at, score}.")
        def news_search(query: str, limit: int = 5) -> str:
            rows = store.search(query, limit)
            return json.dumps(
                [{k: r[k] for k in ("title", "source", "published_at", "score")} for r in rows],
                ensure_ascii=False,
            )

        @tool("The top-scored recent news items (most relevant, freshest first). Returns a JSON array.")
        def news_top(limit: int = 5) -> str:
            rows = store.top(limit, window_hours=window)
            return json.dumps(
                [{k: r[k] for k in ("title", "source", "published_at", "score")} for r in rows],
                ensure_ascii=False,
            )

        return [news_search, news_top]

    # --------------------------------------------------------------- commands

    def commands(self) -> list[Command]:
        store = self._store

        def news_cmd(a, ctx) -> int:
            limit = getattr(a, "limit", 15)
            rows = store.top(limit, window_hours=168)
            statuses = store.source_statuses()
            if not rows and not statuses:
                print("No news collected yet. Run: plp run news.collect")
                return 0
            if statuses:
                ok = sum(1 for r in statuses if r["state"] == "ok")
                print(f"Sources: {ok}/{len(statuses)} ok")
                for r in statuses:
                    if r["state"] != "ok":
                        print(f"  x {r['name']}: {r['error']}")
                print()
            for r in rows:
                pub = (r["published_at"] or r["first_seen"])[:10]
                print(f"[{r['source']:<16}] {r['title']}  ({pub}, {r['score']:.2f})")
                if r["summary"]:
                    print(f"    {r['summary'][:160]}")
            return 0

        cmd = Command(name="news", help="recent collected news + source health", handler=news_cmd)
        cmd.add_arguments = lambda p: p.add_argument("--limit", type=int, default=15)
        return [cmd]
