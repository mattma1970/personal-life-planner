# PersonalLifePlanner (PLP)

A local-first, plugin-based personal assistant: it scans the news and your email,
keeps the things that matter (gift-giving, holidays, important work)
scheduled and measurable, and reports back in daily digests and a weekly checkup.

**Read [`PRD.md`](PRD.md) first** — it is the product and architecture spec.

## Shape

- `src/plp/kernel/` — the stable core: config, store, scheduler, tool registry,
  agent runtime (self-hosted Qwen 3.8 27B), delivery, approvals, capability/host boundary.
- `plugins/` — the features: `news`, `email`, `calendar`, `gifts`, `travel`, `scorecard`.
  New feature = new package; the kernel never changes.
- `plp-vault/` — human/LLM-first data: markdown + YAML frontmatter, Obsidian-compatible,
  git-versioned (gifts, travel, goals, notes).
- `data/plp.db` — machine-first state: SQLite (news, runs, approvals, scorecard, FTS index).

## Quickstart

```bash
uv venv
uv pip install -e ".[dev]"        # in this sandbox: UV_CACHE_DIR="$PWD/.uv-cache" uv pip install -e ".[dev]"
.venv/bin/pytest                  # 99 tests
.venv/bin/plp plugins             # boot report: jobs, tools, plugin state
.venv/bin/plp daemon              # the daemon (30s tick; system cron only supervises the process)
```

Phase-2 demo (the daily rhythm — try it):

```bash
.venv/bin/plp run news.collect    # fetch all 8 sources (per-source isolation; one dead feed never blocks)
.venv/bin/plp run daily.digest    # small digest: top items, source-health line, ONE suggested action
.venv/bin/plp news                # recent headlines + source health, on demand
```

Kernel demo (propose → approve):

```bash
.venv/bin/plp run demo.hello '{"propose": true, "when": "tomorrow 09:00"}'
.venv/bin/plp runs                # audit trail
.venv/bin/plp approve 1           # resolve the proposal
.venv/bin/plp daemon --once       # fires whatever is due (catch-up semantics)
```

## Build status

| Phase | Component | Status |
|---|---|---|
| 0 | Scaffolding (repo, config, CLI stub) | ✅ done |
| 1 | Kernel core (scheduler, registries, daemon) | ✅ done — two plugins boot, cron fires and audits, failing plugin isolated |
| 2 | News + daily digest | ✅ done — 8 sources (RSS + HTML newsroom), dedupe + scoring, LLM-seasoned digest with one suggested action |
| 3 | Vault + gifts + travel | ✅ done — vault (FTS, human-wins), gift lifecycle + Sunday review, travel brainstorm (LLM-seasoned or deterministic) |
| 4 | Calendar steward (ICS now, Google later) | ✅ done — ICS backend (human edits survive daemon writes), `plp calendar week/add/rm/connect`, propose→approve→ICS (audited), Google OAuth scaffold (fallback to ICS) |
| 5 | Scorecard + weekly checkup | ✅ done — weekly hours per goal category (calendar is the spine), 26-week trends, deterministic floor + LLM seasoning (narrow 6-tool mount, strict JSON, one retry, forced final), data-driven wins/drift, 2–3 approvable calendar proposals, human-editable `plp-vault/goals.md`; live-verified on CPU Qwen |
| 6 | Email scanner (Gmail) | pending |
