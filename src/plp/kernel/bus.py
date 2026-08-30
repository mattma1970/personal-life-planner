"""In-process event bus.

Events are dotted names, e.g. ``job.news.collect.finished``,
``approval.3.approved``, ``plugin.loaded``. Subscribers match by exact name or
prefix. A misbehaving subscriber is logged and isolated — it can never break
the publisher or other subscribers (the daemon must keep running).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("plp.kernel.bus")

EventCallback = Callable[[str, dict], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, EventCallback]] = []
        self._lock = threading.Lock()

    def subscribe(self, prefix: str, cb: EventCallback) -> None:
        with self._lock:
            self._subs.append((prefix, cb))

    def publish(self, event: str, payload: dict | None = None) -> None:
        payload = payload or {}
        log.debug("event %s %s", event, payload)
        with self._lock:
            subs = list(self._subs)
        for prefix, cb in subs:
            if event != prefix and not event.startswith(prefix):
                continue
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001 - isolation by design
                log.exception("subscriber for %r failed on event %s", prefix, event)

    def drain_test_events(self) -> list[Any]:
        """Test helper: no-op hook so tests can attach collectors uniformly."""
        return []
