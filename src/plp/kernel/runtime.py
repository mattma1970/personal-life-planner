"""Runtime assembly (PRD.md §6.2): config → store → bus → discovery → registries.

Every entry point (``daemon``, ``run``, ``runs``, ``approve``, later ``chat``)
goes through :func:`build_runtime`. Registration is rebuilt in memory on each
boot — the DB holds state, never code (PRD.md §6.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .agent import Agent
from .approvals import Approvals
from .bus import EventBus
from .capability import Capability
from .config import PlpConfig, resolve
from .context import PluginContext
from .delivery import Delivery
from .discovery import LoadedPlugin, discover
from .host import HostService
from .llm import LLMClient
from .registry import ToolRegistry
from .scheduler import Scheduler
from .store import Store


@dataclass
class Runtime:
    config: PlpConfig
    store: Store
    bus: EventBus
    delivery: Delivery
    llm: LLMClient
    agent: Agent
    approvals: Approvals
    host: HostService
    scheduler: Scheduler
    tools: ToolRegistry
    plugins: list[LoadedPlugin]
    plugin_failures: list[dict]
    logger: logging.Logger


def build_runtime(config: PlpConfig, *, load_plugins: bool = True) -> Runtime:
    logger = logging.getLogger("plp")

    store = Store(resolve(config, config.state_db.path))
    store.migrate_core()
    bus = EventBus()
    delivery = Delivery(config, bus, logger)
    approvals = Approvals(store, bus, logger)
    host = HostService(store, bus, logger)
    llm = LLMClient(config.llm, logger)
    tools = ToolRegistry(logger)
    agent = Agent(llm, tools, bus, logger)

    plugins: list[LoadedPlugin] = []
    failures: list[dict] = []
    if load_plugins:
        plugins, failures = discover(
            resolve(config, config.plugins.dir),
            store,
            bus,
            config,
            logger,
            delivery=delivery,
            approvals=approvals,
            host=host,
        )
        for lp in plugins:
            for t in lp.tools:
                tools.register(f"{lp.name}.{t.__name__}", t)

    def ctx_factory(row: dict, args: dict) -> "PluginContext":
        return PluginContext(
            store=store,
            bus=bus,
            config=config,
            delivery=delivery,
            capability=Capability.permissive(config.llm.max_tool_steps),
            approvals=approvals,
            host=host,
            job_name=row["name"],
            args=args,
        )

    scheduler = Scheduler(
        store=store,
        bus=bus,
        config=config,
        ctx_factory=ctx_factory,
        plugins_by_name={lp.name: lp for lp in plugins},
    )
    all_jobs = [j for lp in plugins for j in lp.jobs_by_name.values()]
    if all_jobs:
        scheduler.register_from_plugins(all_jobs)

    return Runtime(
        config=config,
        store=store,
        bus=bus,
        delivery=delivery,
        llm=llm,
        agent=agent,
        approvals=approvals,
        host=host,
        scheduler=scheduler,
        tools=tools,
        plugins=plugins,
        plugin_failures=failures,
        logger=logger,
    )
