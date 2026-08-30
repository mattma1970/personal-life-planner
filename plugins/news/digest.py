"""Daily news digest — small by design, ends in ONE suggested life action.

The deterministic skeleton is load-bearing and always runs (PRD.md §6.5:
the LLM is seasoning, never the load-bearing wall). The LLM adds a one-line
headline and short "why it matters" annotations; unavailable model, malformed
reply, or failed validation all degrade to the plain ranked list.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("plp.news.digest")


def _age_label(published_at: str | None, first_seen: str) -> str:
    ref = published_at or first_seen
    try:
        dt = datetime.fromisoformat(ref)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "undated"
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if age_h < 1:
        return "today"
    if age_h < 24:
        return f"{int(age_h)}h ago"
    return f"{int(age_h // 24)}d ago"


def _health_line(statuses: list[dict]) -> str | None:
    if not statuses:
        return None
    ok = sum(1 for r in statuses if r["state"] == "ok")
    line = f"  Sources: {ok}/{len(statuses)} ok"
    dead = [f"{r['name']} ({r['error']})" for r in statuses if r["state"] != "ok"]
    if dead:
        line += " — down: " + ", ".join(dead)
    return line


def build_digest_text(
    store: Any,
    max_items: int = 8,
    window_hours: float = 48.0,
    llm: Any = None,
) -> str:
    """Assemble the digest. ``llm`` may be None (degraded mode)."""
    today = datetime.now(timezone.utc).strftime("%a %Y-%m-%d")
    lines: list[str] = [f"News — {today}"]

    items = store.top(max_items, window_hours=window_hours)
    health = _health_line(store.source_statuses())

    seasoning = _season_with_llm(llm, items) if llm is not None else None

    if seasoning:
        lines.append(f"> {seasoning['headline']}")
        for i, r in enumerate(items):
            line = f"  [{r['source']}] {r['title']}  ({_age_label(r['published_at'], r['first_seen'])})"
            if i < len(seasoning["whys"]):
                line += f" — {seasoning['whys'][i]}"
            lines.append(line)
    elif items:
        for r in items:
            lines.append(
                f"  [{r['source']}] {r['title']}  ({_age_label(r['published_at'], r['first_seen'])})"
            )
            if r["summary"]:
                lines.append(f"      {r['summary'][:200]}")
    else:
        lines.append("  (nothing new in the last window)")

    if health:
        lines.append(health)

    if seasoning:
        lines.append(f"  If you do one thing about this: {seasoning['action']}")
    elif items:
        lines.append(f"  If you read one thing today: {items[0]['title']}")
    lines.append("")
    return "\n".join(lines)


def _season_with_llm(llm: Any, items: list[dict]) -> dict | None:
    """Structured seasoning: {headline, whys[], action}. None on any failure.

    Small store-assembled context in, strict JSON out, validated before use —
    the 27B reliability rules from PRD.md §6.5.
    """
    if not items:
        return None
    try:
        if not llm.available():
            return None
        brief = "\n".join(
            f"- [{r['source']}] {r['title']} ({(r['published_at'] or 'undated')[:10]})"
            for r in items
        )
        msg = llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You season a daily news digest for an AI-research-oriented person. "
                        "Return STRICT JSON only, no prose, no markdown: "
                        '{"headline": "<one short sentence framing today>", '
                        '"whys": ["<max 12 words on why each item matters, same order as input>"], '
                        '"action": "<one concrete thing the reader can do today, max 15 words>"}. '
                        f"Number of items: {len(items)}."
                    ),
                },
                {"role": "user", "content": brief},
            ],
            temperature=0.4,
        )
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            return None
        from plp.kernel.util import extract_json_object, validate_against_schema

        data = json.loads(extract_json_object(content))
        schema = {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "whys": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "string"},
            },
        }
        if not data.get("headline") or not data.get("action"):
            return None
        if validate_against_schema(data, schema):
            return None
        whys = data.get("whys")
        if not isinstance(whys, list):
            whys = []
        return {
            "headline": str(data["headline"]).strip()[:160],
            "whys": [str(w).strip()[:120] for w in whys][: len(items)],
            "action": str(data["action"]).strip()[:160],
        }
    except Exception as exc:  # noqa: BLE001 - seasoning must never kill the digest
        log.debug("digest seasoning degraded: %s", exc)
        return None
