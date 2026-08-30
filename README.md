# PersonalLifePlanner (PLP)

A local-first, plugin-based personal assistant: it scans the news and your email,
keeps the things that matter (your wife, gift-giving, holidays, important work)
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
.venv/bin/pytest                  # 67 tests
.venv/bin/plp plugins             # boot report: jobs, tools, plugin state
.venv/bin/plp daemon              # the daemon (30s tick; system cron only supervises the process)
```

Phase-1 demo (try it):

```bash
.venv/bin/plp plugins            # demo ok; demo_fail FAILED (fault isolation)
.venv/bin/plp run demo.hello '{"propose": true, "when": "tomorrow 09:00"}'
.venv/bin/plp runs               # audit trail
.venv/bin/plp approve 1          # resolve the proposal
.venv/bin/plp daemon --once      # fires the every-minute heartbeat cron
```

## Build status

| Phase | Component | Status |
|---|---|---|
| 0 | Scaffolding (repo, config, CLI stub) | ✅ done |
| 1 | Kernel core (scheduler, registries, daemon) | ✅ done — two plugins boot, cron fires and audits, failing plugin isolated |
| 2 | News + daily digest | pending |
| 3 | Vault + gifts + travel | pending |
| 4 | Calendar steward (ICS now, Google later) | pending |
| 5 | Scorecard + weekly checkup | pending |
| 6 | Email scanner (Gmail) | pending |
