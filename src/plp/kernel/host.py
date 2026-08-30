"""Named, authorized host effects (PRD.md §6.6).

No plugin or model turn touches the host directly: every privileged effect
goes through this service, which (1) checks the caller's ``Capability``,
(2) records the call in the audit stream, (3) publishes an event, and (4)
dispatches to the registered executor for that action — or records the call
without an effect if none is registered yet.

Executors are attached by the plugin that owns the side effect
(calendar write lands in Phase 4; mail send later). A misbehaving executor
is isolated: its error becomes a receipt, never a crash for the caller.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

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
        self._executors: dict[str, Callable[..., dict[str, Any]]] = {}

    def register(self, action: str, executor: Callable[..., dict[str, Any]]) -> None:
        """Attach the executor for a known privileged action (idempotent;
        a later registration replaces the previous one — last plugin wins)."""
        if action not in ACTIONS:
            raise HostError(f"cannot register executor for unknown host action {action!r}")
        self._executors[action] = executor
        self._log.info("host: executor registered for %r", action)

    def call(self, action: str, capability: Capability, **kwargs) -> dict:
        """Authorize, audit, and dispatch a host effect.

        Raises :class:`HostError` when the capability denies the action;
        otherwise returns a receipt dict (``status`` is ``ok``/``error``
        when an executor ran, ``recorded`` when none was attached).
        """
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
        self.bus.publish(f"host.{action}", kwargs)
        executor = self._executors.get(action)
        if executor is None:
            self._log.warning("host action recorded (no executor yet): %s %s", action, detail)
            return {"status": "recorded", "action": action, "note": "no executor attached yet"}
        try:
            receipt = executor(**kwargs) or {"status": "ok", "action": action}
        except Exception as exc:  # isolation: executor errors never crash callers
            self._log.error("host action %s executor failed: %s", action, exc)
            receipt = {"status": "error", "action": action, "error": str(exc)}
        self._log.info("host action done: %s -> %s", action, receipt.get("status"))
        return receipt
