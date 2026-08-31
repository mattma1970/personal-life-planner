# Handover node — 2026-08-30

If you are an **agent** resuming work on this project: read this file first, then
`PRD.md` (§8 build plan, §11 approved decisions, §12 parked ideas). The next build
scope has **not** been chosen by the user — ask before building anything new.

If you are a **human**: this is the snapshot of where things stand, plus the two
small inputs still needed from you.

## One-line state

v1.0 build (Phases 0–6) is **complete, pushed, and verified**. The assistant
(PLP) works end-to-end in degraded/LLM-less mode; LLM seasoning verified live on
CPU Qwen. Next scope is a user decision — it was about to be presented when the
session paused for a break.

## Exact git state

- `origin/master` == local `master` == **`f59e183`** ("Phase 6c: status — … all
  v1.0 phases (0-6) complete"). Working tree clean.
- Full suite: **237 tests green** (`.venv/bin/python -m pytest tests/ -q`).
- The v1.0 goal (`goal-3896fb4d-f562-4bc5-9c7a-c6b33af6feeb`) was marked
  **complete** (rev 3). When new work starts, create a *new* goal for it.
- Recent history: `bdc6424` 6a email code · `29c1a98` 6b email tests ·
  `f59e183` 6c status · `7c142a2` 5c … (pattern: `Phase N<letter>` commits).

## Next step — user decision pending (do not self-select)

When the user returns, re-offer these options (my recommendation at the time was
#1; the user had not yet answered):

1. **Google Calendar backend** — finish the Phase-4 scaffold into a real
   `GoogleCalendarStore` behind the existing `CalendarStore` interface.
   *Investigation already done:* `plugins/calendar/plugin.py` already contains a
   **complete** OAuth2 connect flow (`connect_google`, `calendar.events` scope,
   loopback one-shot, refresh token handling) + the `plp calendar connect` CLI
   command + `docs/google-calendar-setup.md`. What is missing: actual event
   list/create/update against the Google API, enabled via
   `calendar.google.enabled: true`, with ICS↔Google merge semantics (human-edit
   wins) and tests. ICS stays the default backend until credentials arrive.
2. **Web dashboard** — PRD §7 "Later": small FastAPI app as just another
   subscriber to the store + event bus (approvals queue, digests, checkups,
   runs). No agent rework needed. Biggest new user-facing surface.
3. **Get the Gmail scanner live** — user-side steps: create the Google Cloud
   project + OAuth credentials JSON (`docs/email-gmail-setup.md`), then
   `plp email connect --credentials <file>`. Afterwards: tune `life_keywords`
   and look-back window against the first real scan.
4. **New feature plugin** — parked in PRD §12 (reading / fitness / finance) or
   something custom. Spec it with the user first.

## Unanswered user inputs (re-ask, gently)

- **Wife's birthday + the couple's anniversary** → `gifts.occasions` in
  `config/plp.yaml` (and the local `config/plp-cpu.yaml`). With dates set, the
  Sunday gift review and birthday email triage become much sharper.
- **README tagline taste** — the title line is still the Phase-0 wording.

## Live local state (`data/` is gitignored — it does NOT travel via git)

- `data/plp.db` — approvals: 17 **pending** (ids **#7–23**, all `calendar_block`
  from live "weekly checkup" runs in Phase 5) — the user can review/clean them
  with `plp approvals pending`; ids #1–6 are resolved.
- `data/calendar/main.ics` — two events: "Checkup: Time with wife"
  (2026-09-02 19:00–21:00, approved) and "Demo: 30 min of focus time"
  (Phase-1 fault-isolation demo leftover — safe to remove).
- `data/email/` — empty until the user runs `plp email connect`.
- `plp-vault/` — human tier: `goals.md` (scorecard goals) + gift notes.

## Environment quick reference

- Python venv: `.venv/` (3.13.12). CLI: **`.venv/bin/plp`** (there is no
  `python -m plp`). Sanity snippets: `.venv/bin/python -c "import sys;
  sys.path.insert(0,'src'); …"`.
- Config: `config/plp.yaml` (committed) · `config/plp-cpu.yaml` (gitignored —
  local CPU variant; LLM = Ollama `127.0.0.1:11434`, model `qwen3.8-27b` —
  this endpoint is what live runs used).
- Local LLM endpoints: Ollama `:11434` (primary for CPU). llama.cpp CPU server
  `:8090` (currently **stopped**); restart if ever needed:
  `cd /tmp/llama.cpp/llama-b10691 && nohup ./llama-server -m
  /home/mattma/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots/f1bfb127c64f7072bdd2cad55f258b9c8b2910fe/Qwen3.8-27B-Q4_K_M.gguf
  --host 127.0.0.1 --port 8090 -c 8192 -t 24 -ngl 0 --jinja --alias qwen3.8-27b
  > /tmp/llama-cpu.log 2>&1 &`
- Tests: pytest in `tests/`; **loopback-only networking** (dead port
  `127.0.0.1:9` for "no network" assertions); LLM paths use the scripted
  `FakeLLM` duck-type (`responses` list, `calls` log, `available()→True`,
  `max_tool_steps=8`, `chat(...)` pops one response).
- Commit conventions: set `GIT_AUTHOR_NAME=PLP GIT_AUTHOR_EMAIL=plp@local` per
  invocation; message style `Phase N<letter>: …`; push to `origin/master`
  without asking. Push reject → fetch → inspect → rebase → re-push.
- Repo layout: `src/plp/kernel/` (config, store SQLite+FTS5, bus, scheduler,
  agent, approvals, plugin contract, host capabilities, delivery) ·
  `plugins/<name>/plugin.py` for news, calendar, gifts, travel, scorecard,
  email (+ `demo_fail` — an *intentional* failing plugin for the fault-isolation
  demo; its "FAILED" line in boot logs is expected) · `plp-vault/` human tier ·
  `docs/` (`google-calendar-setup.md`, `email-gmail-setup.md`) · `PRD.md` the
  spec · `README.md` build-status table.

## Design invariants (never break these)

- **Local-first/private** — nothing leaves the machine except explicit Google
  API calls the user opted into.
- **Propose-don't-command** — all calendar writes flow through the approvals
  queue → the host `calendar_write` executor *only on human approval*. Never
  auto-write the calendar.
- **LLM is seasoning, never the load-bearing wall** — every feature has a
  deterministic floor that ships regardless; LLM paths are opt-in or degrade to
  a `degraded` flag, never a crash.
- **Auditable** — event bus, `digests` table, `runs` table record everything.
- **Two-tier persistence** — `plp-vault/` markdown (human tier; on merge,
  human edits win) + SQLite (machine tier).
- **Naive local time everywhere; `week_start=0`=Monday.**
- **Sibling modules** in a plugin import only stdlib + `plp.kernel`; they are
  loaded via `load_sibling(...)` and never import each other. Plugins are
  constructed **bare** by discovery — the config arrives in `setup(ctx)`
  (the email plugin was fixed for exactly this convention in Phase 6).
