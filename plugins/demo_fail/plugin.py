"""Sample plugin that fails on purpose (PRD.md §6.3 fault-isolation demo).

Phase-1 exit criterion: this plugin must be **isolated and reported** —
`plp plugins` shows it as FAILED with the error, while every other plugin
boots and the daemon keeps running. Delete this directory when the demo has
served its purpose.
"""

from __future__ import annotations

from plp.kernel.plugin import Plugin


class DemoFailPlugin(Plugin):
    name = "demo_fail"

    def setup(self, ctx) -> None:  # pragma: no cover - intentionally broken
        raise RuntimeError(
            "simulated setup failure (Phase 1 fault-isolation demo)"
        )
