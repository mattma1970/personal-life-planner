"""Named, authorized host effects (PRD.md §6.6).

No plugin or model turn touches the host directly: every privileged effect
goes through this service, which (1) checks the caller's ``Capability``,
(2) records the call in the audit stream, and (3) publishes an event.

v1 semantics: actions are **logged stubs** — the authorization and audit
machinery is real, the side effects land with the plugin that actually needs
them (calendar write in Phase 4, mail send in Phase 2/6).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .capability import Capability
from .util import utcnow_iso

if TYPE_CHECKING:  # pragma: no cover
    from .bus import EventBus
    from .store import Store

log = logging.getLogger("plp.kernel.host")


class HostError(PermissionError):
    pass


#: The closed list of privileged effects a capability may grant.
ACTIONS: dict[str, str] = {
    "calendar_write": "write/modify a real calendar entry",
    "mail_send": "send an outbound message",
    "fs_write_external": "write a file outside the project root",
}


class HostService:
    def __init__(
        self,
        store: "Store",
        bus: "EventBus",
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self._log = logger or log

    def call(self, action: str, capability: Capability, **kwargs) -> dict:
        """Authorize, audit, and (v1) record a host effect."""
        if action not in ACTIONS:
            raise HostError(f"unknown host action {action!r}; known: {sorted(ACTIONS)}")
        if not capability.can_host_action(action):
            raise HostError(f"capability denies host action {action!r}")
        detail = json.dumps(kwargs, default=str)
        self.store.execute(
            "INSERT INTO runs(job, plugin, status, started_at, ended_at, detail)"
            " VALUES (?, ?, 'ok', ?, ?, ?)",
            (f"host.{action}", None, utcnow_iso(), utcnow_iso(), detail),
        )
        self._log.warning("host action recorded: %s %s", action, detail)
        self.bus.publish(f"host.{action}", kwargs)
        return {"status": "recorded", "action": action}
