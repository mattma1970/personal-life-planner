"""Life goals — human-editable vault file (PRD.md §11).

Format of ``plp-vault/goals.md`` (body after frontmatter): one section per
goal, each headed by ``## <title>`` and carrying exactly one machine-readable
line:

    ## Time with wife
    plp: category=wife target_hours_week=5
    Free text the checkup may quote — yours to write.

PLP only ever rewrites the ``plp:`` lines; every other line is the owner's.
The file is the single source of truth for targets — config only carries
cadence/format knobs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from plp.kernel.vault import dump_frontmatter

PLP_LINE_RE = re.compile(r"^plp:\s*(.+)$")
KV_RE = re.compile(r"(\w+)=(\S+)")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

#: Starter targets offered by `plp scorecard onboarding` (hours per week).
DEFAULT_GOALS: list[tuple[str, str, float]] = [
    ("Time with wife", "wife", 5.0),
    ("Deep work", "deep-work", 15.0),
    ("Gift work", "gifts", 1.0),
    ("Travel planning", "travel", 2.0),
]


@dataclass
class Goal:
    title: str
    category: str
    target_hours_week: float | None = None  # None = reported, not measured
    notes: str = ""


def _parse_plp_line(line: str) -> dict | None:
    m = PLP_LINE_RE.match(line.strip())
    if not m:
        return None
    kv = dict(KV_RE.findall(m.group(1)))
    if not kv.get("category"):
        return None
    out: dict = {"category": kv["category"].strip()}
    if "target_hours_week" in kv:
        try:
            out["target_hours_week"] = float(kv["target_hours_week"])
        except ValueError:
            pass  # malformed target → unmeasured, keep the rest
    return out


def parse_goals(text: str) -> list[Goal]:
    """Parse a goals.md body into Goal rows (order preserved, gaps tolerated)."""
    from plp.kernel.vault import parse_frontmatter

    _meta, body = parse_frontmatter(text)
    goals: list[Goal] = []
    sections = list(SECTION_RE.finditer(body))
    for i, m in enumerate(sections):
        end = sections[i + 1].start() if i + 1 < len(sections) else len(body)
        chunk = body[m.end():end].strip("\n")
        plp = None
        note_lines: list[str] = []
        for line in chunk.splitlines():
            if line.strip().startswith("plp:"):
                plp = plp or _parse_plp_line(line)
            else:
                note_lines.append(line)
        if plp is None:
            continue  # section without a plp: line → prose, not a goal
        goals.append(
            Goal(
                title=m.group(1).strip(),
                category=plp["category"],
                target_hours_week=plp.get("target_hours_week"),
                notes="\n".join(note_lines).strip(),
            )
        )
    return goals


def dump_goals(goals: list[Goal], header: str | None = None) -> str:
    """Round-trip: render goals back to goals.md body (idempotent per goal)."""
    head = header or (
        "One section per goal. The `plp:` line under each heading is what PLP "
        "reads — change targets there, or add new sections; everything else is yours."
    )
    parts = [head, ""]
    for g in goals:
        target = (
            f" target_hours_week={g.target_hours_week:g}"
            if g.target_hours_week is not None
            else ""
        )
        parts.append(f"## {g.title}\nplp: category={g.category}{target}")
        if g.notes:
            parts.append(g.notes)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def seed_goals() -> str:
    """Default goals.md content for a fresh install (onboarding edits it)."""
    from plp.kernel.vault import dump_frontmatter

    body = dump_goals(
        [
            Goal(title=t, category=c, target_hours_week=h, notes="")
            for t, c, h in DEFAULT_GOALS
        ]
    )
    return dump_frontmatter({"title": "Life goals"}, body)


def upsert_plp_lines(text: str, goals: list[Goal]) -> str:
    """Rewrite only the ``plp:`` lines of existing sections (human text wins:
    any section without a matching goal title is left byte-for-byte intact)."""
    from plp.kernel.vault import parse_frontmatter

    meta, body = parse_frontmatter(text)
    by_title = {g.title: g for g in goals}
    out = []
    pos = 0
    for m in SECTION_RE.finditer(body):
        end_m = list(SECTION_RE.finditer(body, m.end()))
        end = end_m[0].start() if end_m else len(body)
        out.append(body[pos : m.end() + 1])  # heading incl. newline
        chunk = body[m.end() + 1 : end]
        g = by_title.get(m.group(1).strip())
        if g is None:
            out.append(chunk)  # untouched
            continue
        target = (
            f" target_hours_week={g.target_hours_week:g}"
            if g.target_hours_week is not None
            else ""
        )
        kept = [
            ln
            for ln in chunk.splitlines()
            if not ln.strip().startswith("plp:")
        ]
        out.append(f"plp: category={g.category}{target}\n")
        if kept:
            out.append("\n".join(kept).strip("\n") + "\n")
        pos = end
    out.append(body[pos:])
    return dump_frontmatter(meta, "".join(out))
