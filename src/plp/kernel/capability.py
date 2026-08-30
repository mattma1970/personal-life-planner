"""The per-execution authority object — the sandboxing seam (PRD.md §6.6).

Every job, tool call, and agent turn runs under a ``Capability``. v1 ships
permissive defaults (empty sets mean "unrestricted"); tightening later is a
config change, not a refactor — and the future process/sidecar boundary
reuses this same object as its authorization contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    #: Namespaced tool names this context may call. Empty = all registered.
    tools: frozenset[str] = field(default_factory=frozenset)
    #: Path prefixes readable. Empty = unrestricted (v1).
    fs_read: frozenset[str] = field(default_factory=frozenset)
    #: Path prefixes writable. Empty = unrestricted (v1).
    fs_write: frozenset[str] = field(default_factory=frozenset)
    #: Network endpoints (host:port / URL prefixes) allowed for egress.
    network: frozenset[str] = field(default_factory=frozenset)
    #: Named host actions allowed (see host.ACTIONS). Empty = none in strict
    #: mode; under v1 defaults host actions are permitted but logged.
    host_actions: frozenset[str] = field(default_factory=frozenset)
    #: Max tool calls per agent turn.
    step_budget: int = 8
    #: v1 semantics: empty sets above mean "unrestricted".
    strict: bool = False

    def can_use_tool(self, name: str) -> bool:
        if self.strict and not self.tools:
            return False
        return not self.tools or name in self.tools

    def can_host_action(self, action: str) -> bool:
        if self.strict and not self.host_actions:
            return False
        return not self.host_actions or action in self.host_actions

    @classmethod
    def permissive(cls, step_budget: int = 8) -> "Capability":
        return cls(step_budget=step_budget, strict=False)
