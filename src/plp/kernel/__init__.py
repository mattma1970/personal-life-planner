"""The PLP kernel — the stable platform plugins run on.

Components (built out across build Phases 1+; see PRD.md §6.2):

- ``config``     — load/validate ``config/plp.yaml`` (pydantic models)
- ``store``      — SQLite (WAL + FTS5): runs, approvals, digests, plugin tables
- ``bus``        — in-process event bus (job finished/failed, digest sections…)
- ``scheduler``  — in-daemon cron: per-job lock, timeout, retry, catch-up-on-wake
- ``plugins``    — discovery: scan ``plugins/``, import, setup(), register
- ``registry``   — tool registry with JSON schemas derived from type hints
- ``capability`` — the per-context authority object (sandboxing seam)
- ``host``       — named, authorized privileged effects (calendar, mail, files)
- ``agent``      — bounded tool-calling loop over the LLM (ToolExecutor seam)
- ``llm``        — thin OpenAI-compatible client (self-hosted Qwen 3.8 27B)
- ``digest``     — digest assembly from plugin-contributed sections
- ``delivery``   — pluggable sinks (terminal, email)
- ``approvals``  — proposal state machine: pending → approved/rejected/expired
"""
