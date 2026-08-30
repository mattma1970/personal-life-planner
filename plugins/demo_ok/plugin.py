"""Sample plugin: exercises the whole kernel pipeline (PRD.md §6.3 template).

Demonstrates:
- ``demo.heartbeat`` — a daily scheduled job (06:00): a minimal standing cron
  entry (catch-up proves the scheduler ticks; reschedule freely in config);
- ``demo.hello``     — a job with args that can create an *approval proposal*
  (``plp run demo.hello '{"propose": true}'`` → ``plp approve <id>``);
- ``demo.echo``      — a ``@tool`` function in the LLM-callable registry.

Real feature plugins (news, gifts, …) follow this exact shape.
"""

from __future__ import annotations

import logging
import time

from plp.kernel.plugin import Job, Plugin, tool

log = logging.getLogger("plp.plugins.demo")


class DemoPlugin(Plugin):
    name = "demo"

    def setup(self, ctx) -> None:
        self._ctx = ctx
        ctx.log("demo plugin ready (vault at %s)", ctx.vault_dir())

    # --------------------------------------------------------------- tools

    def tools(self):
        @tool("Echo a message back. Demonstrates schema-derived tool registration.")
        def echo(message: str, times: int = 1) -> str:
            """Repeat the message ``times``."""
            return (message + " ") * max(1, times)

        @tool("Get a wall-clock timestamp, for time-aware answers.")
        def now() -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S")

        return [echo, now]

    # --------------------------------------------------------------- jobs

    def jobs(self):
        def heartbeat(ctx, args) -> dict:
            return {"beat": True}

        def hello(ctx, args) -> dict:
            result = {"hello": True}
            if args.get("propose"):
                aid = ctx.approvals.propose(
                    "calendar_block",
                    {
                        "title": "Demo: 30 min of focus time",
                        "when": args.get("when", "tomorrow 09:00"),
                    },
                    note="created by demo.hello — resolve with 'plp approve <id>'",
                )
                result["proposal"] = aid
                ctx.log("created proposal #%d", aid)
            return result

        return [
            Job(name="demo.heartbeat", cron="0 6 * * *", handler=heartbeat,
                staleness_h=36, timeout_s=10),
            Job(name="demo.hello", cron="0 9 * * 1", handler=hello),
        ]
