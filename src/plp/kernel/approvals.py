"""The proposal/approval state machine — "propose, don't command" (PRD.md §2, §6.2).

The assistant never writes to the real world directly: it *proposes*
(calendar blocks, purchases, plans). Proposals persist in the store, are
delivered with digests, and are resolved by the human (``plp approve``,
later: email reply / web UI). Stale proposals expire.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .bus import EventBus
    from .store import Store

log = logging.getLogger("plp.kernel.approvals")

#: Proposals older than this (days) expire if the human never resolves them.
DEFAULT_EXPIRE_DAYS = 30.0


class Approvals:
    def __init__(self, store: "Store", bus: "EventBus", logger: logging.Logger | None = None) -> None:
        self.store = store
        self.bus = bus
        self._log = logger or log

    def propose(self, kind: str, payload: dict, note: str | None = None) -> int:
        """Create a pending proposal. ``kind`` e.g. calendar_block, purchase, plan."""
        aid = self.store.propose(kind, payload, note)
        self._log.info("proposed #%d %s: %s", aid, kind, payload)
        self.bus.publish(f"approval.{aid}.proposed", {"kind": kind, "payload": payload})
        return aid

    def pending(self, kind: str | None = None) -> list[dict]:
        return self.store.pending_approvals(kind)

    def get(self, aid: int) -> dict | None:
        return self.store.get_approval(aid)

    def resolve(self, aid: int, approved: bool, note: str | None = None) -> bool:
        """Approve or reject a pending proposal. False if not found/not pending.
        The bus event is published only on a real state change — re-resolving
        an already-resolved id must not re-trigger side effects (Phase 4:
        an approval event drives the calendar write)."""
        ok = self.store.resolve_approval(aid, approved, note)
        if not ok:
            return False
        event = "approved" if approved else "rejected"
        self.bus.publish(f"approval.{aid}.{event}", {"id": aid})
        self._log.info("approval #%d %s", aid, event)
        return ok

    def expire_stale(self, max_age_days: float = DEFAULT_EXPIRE_DAYS) -> int:
        """Expire proposals the human ignored past the window."""
        n = self.store.expire_stale_approvals(max_age_days * 24)
        if n:
            self.bus.publish("approvals.expired", {"count": n})
        return n
