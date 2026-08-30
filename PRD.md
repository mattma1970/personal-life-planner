# PersonalLifePlanner (PLP) — Product Requirements & Architecture

**Status:** v1.0 — workshop complete, direction approved · build: Phase 1 done (2026-08-30)
**Location:** `/home/mattma/PersonalLifePlanner`
**Stack:** Python 3.12 · local-first · self-hosted LLM (Qwen 3.8 27B, OpenAI-compatible endpoint)

---

## 1. Vision

A highly personalized assistant whose job is to help its owner **be more present in his life**: keeping the things that matter — his wife, gift-giving, holidays, important work — visible, scheduled, and hard to miss, without costing him attention. The assistant does the remembering, scanning, proposing, and measuring. The human approves and executes.

One line: **make time for what matters visible, scheduled, and measurable — without spending your own attention on the bookkeeping.**

The **calendar is the spine**: category-tagged events are both the delivery mechanism (protected time) and the measurement instrument (where the week actually went).

## 2. Design principles

1. **Local-first, private.** All personal data (gifts, goals, metrics, notes) stays on this machine: SQLite + a markdown vault. The LLM is self-hosted (Qwen 3.8 27B), so nothing personal leaves the LAN. No cloud stores, no cloud LLM.
2. **Propose, don't command.** The assistant writes *proposals* (calendar blocks, purchases, plans); the human approves, rejects, or lets them expire. The model never writes to the real calendar directly.
3. **The LLM is seasoning, never the load-bearing wall.** Every feature has a degraded, LLM-less mode. The model adds judgment (summaries, brainstorming, checkup writing) on top of a working core.
4. **Everything is auditable.** Every run, tool call, and approval is logged. Vault changes are git diffs. `plp runs` shows what the assistant did.
5. **Config-driven.** Sources, goals, schedules, and preferences live in editable files, not code.
6. **New feature = new plugin package.** The kernel is a stable platform; features never touch kernel code.

## 3. User loops

### Daily (default: 07:00 collect / 07:05 digest)
1. **News scan** → AI labs, model releases, notable posts; deduped and ranked.
2. **Email scan** → needs-reply flags; dates/RSVPs/birthdays extracted to the calendar; life-relevant mail surfaced.
3. **Today check** → calendar, gift-vault deadlines (anniversary in 3 weeks?), conflicts.
4. **Deliver one digest** — deliberately small, ending in **one** suggested life action.

### Weekly checkup (default: Sunday 20:00)
- **Scorecard**: actual hours vs. stated goals per category (time with wife, gift work, travel planning, important work).
- **Wins & drift**: "You gave her the gift — nice. But no dedicated time together for 10 days."
- **Proposals**: 2–3 concrete blocks for next week (personal *and* work), one-tap approval.
- **Vault updates**: approaching occasions → purchase/deliver or travel-planning blocks.

## 4. Services

Each service is a plugin implementing the contract in §6.3.

| # | Service | What it does | Trigger |
|---|---------|--------------|---------|
| S1 | **News collector** | AI lab blogs (OpenAI, Anthropic, DeepMind), Hugging Face, arXiv, HN/AI-Reddit; config-driven source list; dedupe, rank, digest | Daily |
| S2 | **Email scanner** | Gmail triage (read-only); needs-reply flags; date/RSVP/birthday extraction → calendar proposals; life-relevant surfacing; LLM summarization opt-in (off by default) | Daily |
| S3 | **Calendar steward** | Work + personal blocks; category tags (wife/family, gifts, travel, deep work); conflict-aware; proposals/approvals | Continuous + checkup |
| S4 | **Gift vault** | Low-friction capture of present ideas for the owner's wife; lifecycle *idea → shortlist → bought → given*; tied to her birthday/anniversary; feeds the checkup | On capture + weekly |
| S5 | **Holiday planner** | Brainstorms trips/holidays from stated preferences (budget, weather, her interests); calendar feasibility; planning + booking deadlines; trip on the calendar | On demand + nudge |
| S6 | **Life scorecard** | Quantitative goals ("5h/week with wife"); measured from calendar hours + vault activity; trended over months; reported in the checkup | Weekly |

## 5. Data & persistence

Two tiers, chosen per data character, swappable per domain:

- **The Vault** (human-first, LLM-first) — `plp-vault/`: Obsidian-compatible markdown with YAML frontmatter, git-versioned, indexed by the kernel (FTS5). Owns: gifts, travel plans, goal definitions, reference notes. The owner can open any of it in Obsidian; the assistant's writes are reviewable git diffs. Markdown is also the model's native read/write format — no SQL-to-prose translation.
- **The state DB** (machine-first) — `data/plp.db`: SQLite (WAL + FTS5). Owns: news items, email scan results, scorecard time series, run/audit log, scheduler state, proposals, and the vault's search index.

```
plp-vault/
├── gifts/2026-anniversary.md     # --- occasion: anniversary / status: shortlist / budget: 150 ---
├── travel/hawaii-march-2026.md
├── goals.md                      # --- category: wife / target_hours_week: 5 ---
└── notes/
```

**The seam:** each domain owns a store interface (`GiftStore`, `TravelStore`, `NewsStore`, `CalendarStore`, `ScorecardStore`) with a chosen backend — markdown vault for human-facing domains, SQLite for machine-facing ones — selected in config. Migrating a domain's backend later is a config change plus a mechanical field mapping. **Cloud stores are deliberately out** (local-first); future remote access = sync the vault (git/Syncthing).

**Vault write rules:** single writer (the daemon); read-modify-write with file locking; atomic replace (temp + rename); index rebuilt from mtime scan; on conflict **the human's file edit wins**.

## 6. Architecture

### 6.1 Shape — a "kernel + plugins" modular monolith

One daemon process; one codebase; a hard seam. No microservices, no heavy agent framework.

```
PersonalLifePlanner/
├── PRD.md · README.md · pyproject.toml
├── config/plp.yaml               # LLM endpoint, schedules, delivery, per-plugin config
├── plp-vault/                    # tier 1: markdown + frontmatter (git-tracked)
├── data/plp.db                   # tier 2: SQLite (gitignored)
├── src/plp/
│   ├── kernel/                   # config · store · scheduler · bus · registry
│   │                             # agent · llm · digest · delivery/ · approvals/
│   │                             # host/ · capability.py · plugins.py
│   ├── cli.py                    # plp daemon | run | runs | plugins | chat |
│   │                             #       approve | calendar
│   └── server.py                 # (deferred) FastAPI — another store/bus subscriber
├── plugins/                      # news/ email/ calendar/ gifts/ travel/ scorecard/
└── tests/
```

### 6.2 Kernel components

| Component | Role |
|---|---|
| **Config** | One YAML: LLM endpoint (base URL + model → Qwen server), schedules with per-job overrides, delivery targets, per-plugin config validated against each plugin's schema |
| **Store** | SQLite (WAL + FTS5). Core owns `runs`, `approvals`, `digests`; plugins own their tables via migrations. Code on disk is the source of truth for *capabilities*; the DB is the source of truth for *state* — registries are rebuilt in memory at every boot, never persisted |
| **Scheduler** | In-daemon cron (system cron only supervises the process). Jobs from plugin manifests, overridable in config, plus one-shot jobs the agent creates at runtime. 30–60s tick; per-job lock (overlaps coalesce); timeout; small retry/backoff; **catch-up-on-wake** (fires when last run is older than a per-job staleness window); every run logged; failures surface in the next digest |
| **Tool registry** | Type-hinted functions + `@tool(description=…)` → JSON Schema derived from signatures, namespaced per plugin (`gifts.search`). **One registry, two consumers:** deterministic jobs call tools directly; agent turns let the model call them |
| **Agent runtime** | Thin OpenAI-compatible client + bounded tool-calling loop: per-turn step budget, per-turn capability, structured output (validated schema → one retry → degrade to plain text). Tool execution sits behind a `ToolExecutor` interface — same-process now, sandboxed sidecar later, zero feature rework |
| **Delivery** | Pluggable sinks: terminal now, email-to-self when configured. Digest = builder object plugins contribute sections to; missing sections are flagged, never fatal |
| **Approvals** | Proposal state machine (pending → approved / rejected / expired) — the mechanics of "propose, don't command" |
| **Host + Capability** | All privileged effects (calendar write, email send, file writes outside the project) go through named kernel services authorized against a `Capability` object (allowed tools, fs paths, network, host actions, step budget) threaded through every context. Secrets live only in the kernel; the model never sees raw credentials |

### 6.3 Plugin contract

A plugin is a directory in `plugins/` with one `plugin.py` exposing a class:

```python
class GiftsPlugin(Plugin):
    name = "gifts"
    def setup(self, ctx) -> None: ...        # migrations, config validation
    def tools(self) -> list[Tool]: ...       # LLM-callable, schemas derived
    def jobs(self) -> list[Job]: ...         # name + cron + handler
    def digest_sections(self, d) -> None: ...# contribute to daily digest / checkup
    def commands(self) -> list[Command]: ... # CLI intents: plp gift add …
```

**Discovery:** the kernel scans `plugins/`, loads each `plugin.py` directly (no packaging gymnastics), calls `setup()`, registers everything — **isolated per plugin**: one broken plugin skips and reports in the next digest; it never kills the boot. Job handlers come in three flavors the scheduler treats identically: **pipeline** (pure code), **agent turn** (`run_turn(scenario=…)` — the scenario declares its mounted tools, context sources, and output schema), and **hybrid** (deterministic work + one bounded model step).

### 6.4 Scheduling semantics

- The daemon *is* the cron; a systemd unit (deployment-only) restarts the daemon if it dies.
- A trigger firing while a job is running is **dropped, not queued** (per-job lock).
- **Catch-up-on-wake:** at boot/tick, any job whose last run is older than its staleness window fires immediately (asleep through 07:00 → digest at 09:15); jobs that ran recently never re-fire.
- Recurring jobs come from manifests; **one-shot jobs are rows in the DB** the agent can insert at runtime ("nudge me on the 12th").
- Every run → `runs` table (status, duration, error) → visible in `plp runs` and the digest.

### 6.5 Agent runtime — rules for a self-hosted 27B model

1. **Narrow per-scenario tool sets** (≤ ~8 mounted) — the checkup mounts scorecard/calendar/gift tools; chat mounts a curated subset; never the whole registry.
2. **Strict structured output** for anything load-bearing (checkup JSON, proposals); validate → one retry → plain-text fallback. Never let free text *be* the data path.
3. **Small contexts:** scenario context assembled from the store (last week's scorecard, vault state, upcoming events) — never "dump the life into the prompt."
4. **Graceful degradation:** news digest → raw ranked list; checkup → numbers-only scorecard; vault fully usable via CLI — all with the LLM server down.
5. **Reliability is measured, not assumed:** a recorded-replay harness (canned responses for CI, live smoke tests against the Qwen endpoint) tracks function-calling quality across model updates; if the serving layer's function calling proves weak, the fallback is JSON-in-message parsing behind the same `ToolExecutor` seam.

### 6.6 Security & the future sandbox

The sandbox is not a later subsystem; it is the **default-deny policy of an authority boundary built now**:

- **Capability objects** (above) are permissive in v1; tightening them later is a config change, not a refactor.
- **Host actions are mediated:** no plugin or model turn touches the host directly; every privileged effect is a named kernel service, authorized, and logged.
- **`ToolExecutor` interface:** same-process today; the future "execute a pipeline of code / interact with the computer" feature is a stricter executor in a child process/sidecar speaking the same validated tool messages, under rlimits/seccomp/container at the deploy layer.
- **The approval state machine is the human side of the sandbox:** anything beyond the baseline capability becomes a proposal requiring consent.
- **Secrets:** kernel-only (local keyring/file); plugins receive scoped token handles; model prompts never contain credentials — so even prompt injection from a hostile web page or email cannot exfiltrate anything unscoped.

## 7. Delivery & interface

- **v1:** CLI — `plp daemon` (the assistant), `plp run <job>`, `plp runs`, `plp plugins`, `plp chat`, `plp approve`, `plp calendar`, plus per-plugin commands (`plp gift add …`). Digests/checkups to the terminal, and by email when SMTP/Gmail-send is configured.
- **Later:** a small web dashboard as a FastAPI app — just another subscriber to the store and event bus; no agent rework.

## 8. Build plan — each phase demoable before the next starts

| Phase | Delivers | Exit criteria (demo) |
|---|---|---|
| **0. Scaffolding** | Repo layout, `pyproject.toml`, deps, config loader + `plp.yaml`, SQLite init, git | `plp --version`; config validates; empty DB migrates |
| **1. Kernel core** | Bus, scheduler (tick/catch-up/lock/retry/runs), plugin discovery + registries, `@tool` schema derivation, Capability/context, terminal delivery, audit, full CLI, test-hook plugin | Two sample plugins boot; a cron fires and logs; a failing plugin isolates and reports |
| **2. News + daily digest** | News plugin (RSS-first per source, per-source isolation, dedupe, scoring), `news.collect`, digest builder, `daily.digest` | `plp run news.collect && plp run daily.digest` prints a real, readable digest |
| **3. Vault + gifts + travel** | Vault store (markdown + frontmatter, atomic writes, FTS index, human-wins), gifts plugin (CLI + tools + weekly review job), travel plugin (preferences, brainstorm via Qwen w/ mock fallback, feasibility, plan-doc lifecycle) | `plp gift add "…"` → file in vault + FTS-searchable; travel brainstorm writes a plan doc |
| **4. Calendar steward** | `CalendarStore` interface; ICS backend (CRUD, categories, conflicts); Google OAuth backend scaffold + step-by-step instructions; proposals/approvals end-to-end | Proposal → approve → ICS entry; `plp calendar week` view |
| **5. Scorecard + weekly checkup** | Goals in vault; aggregation (calendar hours by category + vault activity); checkup scenario (narrow tools, structured output, replay + live Qwen), `checkup.weekly`, trend history | Sunday checkup: scorecard, wins/drift, 2–3 approvable proposals |
| **6. Email scanner** | Gmail read-only OAuth; daily triage; date/RSVP/birthday → calendar proposals; life-relevant surfacing; LLM summarization opt-in | `plp run email.scan` flags items and proposes calendar entries (graceful no-op without credentials) |

**Explicitly deferred:** web dashboard, vault sync, code-execution sidecar with hard sandbox, auto-drafted replies, features beyond the six services.

## 9. Testing

- Unit tests per plugin against an in-memory `PluginContext` — no daemon needed.
- Golden pipeline tests with fixture payloads (RSS, Gmail).
- Scheduler tests: catch-up-on-wake, coalescing, overlap, retry, staleness windows.
- Vault atomicity/conflict tests (daemon write vs. human edit).
- **Recorded-replay agent harness** for CI + live smoke script against the Qwen endpoint.

## 10. Risks

| Risk | Mitigation |
|---|---|
| 27B function-calling reliability | Narrow tool sets, strict schemas, one retry, JSON-in-prompt fallback, non-LLM floor; measured by the replay harness |
| Google OAuth needs owner action (Cloud project, credentials) | Full instructions in Phase 4; ICS works until then |
| News source fragility (HTML changes) | RSS-first, per-source failure isolation, source-health line in the digest |
| Scope creep | Phase gates: no phase N+1 until phase N's demo passes |
| Self-hosted LLM availability | Every feature degrades; digest/checkup still delivered without intelligence |

## 11. Decisions & defaults (approved in workshop)

- **Language:** Python 3.12.
- **LLM:** self-hosted Qwen 3.8 27B via an OpenAI-compatible endpoint (`config/plp.yaml`: `llm.base_url`, `llm.model`).
- **Cadences:** daily 07:00 collect / 07:05 digest; checkup Sunday 20:00 — all overridable in config.
- **Email scope:** triage + date/RSVP/birthday extraction; LLM thread summarization **off** by default (opt-in flag).
- **Goals:** seeded via an interactive onboarding interview, then edited in `plp-vault/goals.md`.
- **Gift vault depth:** idea / occasion / budget / status + freeform notes (including "what to say when I give it").
- **Calendar:** local ICS backend now; Google OAuth behind the same `CalendarStore` interface (scaffold + instructions; connected when credentials arrive).

## 12. Future directions (parked, not planned)

Web dashboard · vault sync (git/Syncthing) · sandboxed code-execution sidecar · auto-drafted email replies · additional feature plugins (reading, fitness, finance) · multi-device.
