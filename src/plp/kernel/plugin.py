"""The plugin contract (PRD.md §6.3).

A plugin is a directory under ``plugins/`` with a single ``plugin.py`` that
exposes either a module-level ``PLUGIN`` instance or a ``Plugin`` subclass.

Lifecycle at daemon boot (kernel/discovery.py):

    import → setup(ctx) → tools()/jobs()/commands() → register

Notes for authors:
- **Tools are plain functions** (the LLM cannot pass objects). When a tool
  needs kernel services, capture the ``PluginContext`` the plugin stored in
  ``setup()`` via closure — never take a ``ctx`` parameter.
- **Jobs** may be pure pipelines, ``ctx``-driven logic, or hybrid (deterministic
  work plus one bounded model step via ``ctx``).
- **Digest sections** (Phase 2+) receive a digest-builder object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .context import PluginContext

#: Attribute stamp used by @tool (imported by the registry).
TOOL_ATTR = "__plp_tool__"


def tool(description: str):
    """Mark a function as an LLM-callable tool with a model-facing description.

    Parameters must be JSON-representable (str/int/float/bool/list/dict).
    Do NOT take a ``ctx`` parameter — capture the ``PluginContext`` via
    closure from the plugin's ``setup()`` instead (see module docstring).
    The JSON schema is derived from the type hints by the kernel registry.
    """

    def deco(fn):
        import inspect

        sig = inspect.signature(fn)
        if "ctx" in sig.parameters:
            raise ValueError(
                f"@tool {fn.__name__}: tools must not take a 'ctx' parameter — "
                "capture the PluginContext from setup() via closure instead"
            )
        fn.__dict__[TOOL_ATTR] = {"description": description}
        return fn

    return deco

#: Job handler signature: ``(PluginContext, args dict) -> result (JSON-able)``
Handler = Callable[["PluginContext", dict], Any]

#: Command handler signature: ``(argparse.Namespace, PluginContext) -> exit code``
CommandHandler = Callable[[Any, "PluginContext"], int]


@dataclass
class Job:
    """A schedulable unit of work.

    Exactly one of ``cron`` (recurring, 5-field expression) or ``fire_at``
    (ISO timestamp, one-shot) may be set.

    ``staleness_h`` bounds catch-up-on-wake: a job only fires as a catch-up
    when the most recent missed slot is no older than this window — see
    PRD.md §6.4.
    """

    name: str
    handler: Handler
    cron: str | None = None
    fire_at: str | None = None
    staleness_h: float = 36.0
    retries: int = 2
    timeout_s: float = 300.0
    plugin: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.cron is None) == (self.fire_at is None):
            raise ValueError(f"job {self.name!r}: exactly one of cron/fire_at is required")
        if self.retries < 0:
            raise ValueError(f"job {self.name!r}: retries must be >= 0")
        if self.timeout_s <= 0:
            raise ValueError(f"job {self.name!r}: timeout_s must be > 0")

    @property
    def kind(self) -> str:
        return "cron" if self.cron is not None else "oneshot"

    @property
    def spec(self) -> str:
        return self.cron if self.cron is not None else self.fire_at  # type: ignore[return-value]


@dataclass
class Command:
    """A CLI intent exposed by a plugin (e.g. ``plp gift add ...``).

    Wire-up of the parser lives in the CLI (Phase 1+ plugins register their
    ``add_arguments(parser)`` hook).
    """

    name: str
    help: str = ""
    handler: CommandHandler | None = None
    add_arguments: Callable[[Any], None] | None = None


class Plugin:
    """Base class for all plugins. Override the methods you need.

    ``setup`` runs at daemon boot before ``tools``/``jobs`` are collected —
    store the ``PluginContext`` there (``self._ctx = ctx``) and use it from
    tool closures.
    """

    #: Must match a unique name; defaults to the directory name if empty.
    name: str = ""

    def setup(self, ctx: "PluginContext") -> None:
        """One-time boot hook: migrations, config validation, service handles."""

    def tools(self) -> list:
        """Functions decorated with ``@tool`` — callable by the LLM and by jobs."""
        return []

    def jobs(self) -> list[Job]:
        return []

    def commands(self) -> list[Command]:
        return []

    def digest_sections(self, digest: Any) -> None:
        """Contribute to a digest/checkup builder (used from Phase 2 onward)."""
