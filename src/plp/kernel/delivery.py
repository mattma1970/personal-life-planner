"""Delivery sinks: where digests and checkups land (PRD.md §6.2, §7).

v1: terminal sink. The email sink arrives with the Gmail work (Phases 2/6);
the interface is stable so adding sinks never touches producers.
"""

from __future__ import annotations

import logging
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .bus import EventBus
    from .config import PlpConfig

log = logging.getLogger("plp.kernel.delivery")


class DeliverySink(Protocol):
    def send(self, kind: str, text: str) -> None: ...


class TerminalSink:
    def send(self, kind: str, text: str) -> None:
        print(f"\n=== PLP {kind} ===\n{text}\n", flush=True)


class EmailSink:
    """Placeholder until Gmail send lands (Phase 2/6)."""

    def send(self, kind: str, text: str) -> None:
        log.warning("email delivery requested but not yet implemented (Phase 2/6)")


class Delivery:
    def __init__(
        self, config: "PlpConfig", bus: "EventBus", logger: logging.Logger | None = None
    ) -> None:
        self._log = logger or log
        self.sinks: list[DeliverySink] = []
        if config.delivery.terminal:
            self.sinks.append(TerminalSink())
        if config.delivery.email.enabled:
            self.sinks.append(EmailSink())
            self._log.warning(
                "email delivery enabled: sink is a placeholder until Gmail send ships"
            )

    def deliver(self, kind: str, text: str) -> None:
        for sink in self.sinks:
            try:
                sink.send(kind, text)
            except Exception:  # noqa: BLE001 - one bad sink never blocks the rest
                self._log.exception("delivery sink failed for %s", kind)
        self._log.info("delivered %s (%d chars) to %d sink(s)", kind, len(text), len(self.sinks))
