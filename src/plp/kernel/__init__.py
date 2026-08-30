"""The PLP kernel — the stable, boring center of the system (PRD.md §6.2).

Components:

- ``config``      typed YAML configuration (validated, paths resolved)
- ``store``       SQLite (WAL + FTS5) machine-first tier, numbered migrations
- ``bus``         in-process event bus (isolated subscribers)
- ``scheduler``   in-daemon cron: ticks, due-logic, catch-up, coalesce,
                  supervised execution (timeout / retry / audit)
- ``discovery``   plugin discovery with per-plugin fault isolation
- ``registry``    ``@tool`` decorator + JSON-schema derivation + registry
- ``capability``  per-execution authority object (the sandboxing seam)
- ``context``     ``PluginContext`` — what setup/jobs/tools may touch
- ``llm``         thin OpenAI-compatible client for the self-hosted LLM
- ``agent``       bounded tool-calling runtime with structured output
- ``approvals``   propose/approve/reject/expire state machine
- ``host``        named, authorized host effects (audited)
- ``delivery``    terminal (later: email) sinks
- ``runtime``     ``build_runtime`` — assembles all of the above
"""

from .agent import Agent, Scenario
from .approvals import Approvals
from .bus import EventBus
from .capability import Capability
from .config import ConfigError, LLMConfig, PlpConfig, load_config, resolve
from .context import PluginContext
from .delivery import Delivery
from .discovery import LoadedPlugin, PluginLoadError, discover
from .host import HostError, HostService
from .llm import LLMClient, LLMError, LLMUnavailable
from .plugin import Command, Job, Plugin
from .registry import Tool, ToolError, ToolRegistry, derive_schema, tool
from .runtime import Runtime, build_runtime
from .scheduler import JobNotFoundError, Scheduler
from .store import Store

__all__ = [
    "Agent",
    "Scenario",
    "Approvals",
    "EventBus",
    "Capability",
    "ConfigError",
    "LLMConfig",
    "PlpConfig",
    "load_config",
    "resolve",
    "PluginContext",
    "Delivery",
    "LoadedPlugin",
    "PluginLoadError",
    "discover",
    "HostError",
    "HostService",
    "LLMClient",
    "LLMError",
    "LLMUnavailable",
    "Command",
    "Job",
    "Plugin",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "derive_schema",
    "tool",
    "Runtime",
    "build_runtime",
    "JobNotFoundError",
    "Scheduler",
    "Store",
]
