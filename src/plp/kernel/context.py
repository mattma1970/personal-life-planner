"""``PluginContext`` — everything a plugin setup, job, or tool may touch.

One context instance per execution unit (plugin setup, job run, agent turn).
Holds references to kernel services plus the ``Capability`` that authorizes
them (PRD.md §6.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .approvals import Approvals
    from .capability import Capability
    from .config import PlpConfig
    from .delivery import Delivery
    from .host import HostService
    from .bus import EventBus
    from .store import Store


@dataclass
class PluginContext:
    store: "Store"
    bus: "EventBus"
    config: "PlpConfig"
    delivery: "Delivery | None"
    capability: "Capability"
    approvals: "Approvals | None" = None
    host: "HostService | None" = None
    job_name: str | None = None
    args: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        import logging

        if getattr(self, "_logger", None) is None:
            where = self.job_name or "plugin"
            self._logger = logging.getLogger(f"plp.ctx.{where}")

    def log(self, *a: object) -> None:  # thin sugar for plugin authors
        self._logger.info(*a)

    def vault_dir(self) -> Path:
        """The Obsidian-compatible vault directory (tier-1 persistence)."""
        from .config import resolve

        return resolve(self.config, self.config.vault.path)
