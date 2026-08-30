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

## Quickstart (scaffolding state)

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/plp --version
.venv/bin/pytest
```

## Build status

| Phase | Component | Status |
|---|---|---|
| 0 | Scaffolding (repo, config, CLI stub) | in progress |
| 1 | Kernel core (scheduler, registries, daemon) | pending |
| 2 | News + daily digest | pending |
| 3 | Vault + gifts + travel | pending |
| 4 | Calendar steward (ICS now, Google later) | pending |
| 5 | Scorecard + weekly checkup | pending |
| 6 | Email scanner (Gmail) | pending |
