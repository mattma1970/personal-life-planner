"""Plugin discovery (PRD.md §6.3): scan → import → setup → register.

Fault isolation is a core requirement: **one broken plugin must never kill
the daemon or another plugin.** A plugin whose ``plugin.py`` fails to import
or whose ``setup()`` raises is recorded in ``plugin_status`` and reported
(``plp plugins``, next digest) — the rest of the system boots normally.

Registration is in-memory and rebuilt every boot; the DB never stores code
(code on disk is the source of truth for capabilities).
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .capability import Capability
from .context import PluginContext
from .plugin import Job, Plugin

if TYPE_CHECKING:  # pragma: no cover
    from .bus import EventBus
    from .config import PlpConfig
    from .store import Store


class PluginLoadError(Exception):
    pass


@dataclass
class LoadedPlugin:
    name: str
    plugin: Plugin
    jobs_by_name: dict[str, Job]
    tools: list = field(default_factory=list)
    commands: list = field(default_factory=list)


def discover(
    plugins_dir: Path,
    store: "Store",
    bus: "EventBus",
    config: "PlpConfig",
    logger: logging.Logger | None = None,
    *,
    delivery=None,
    approvals=None,
    host=None,
) -> tuple[list[LoadedPlugin], list[dict]]:
    """Discover, set up, and collect all plugins under ``plugins_dir``.

    Returns ``(loaded, failures)`` where failures carry a human-readable error.
    Never raises: every per-plugin failure is contained.
    """
    log = logger or logging.getLogger("plp.kernel.discovery")
    loaded: list[LoadedPlugin] = []
    failures: list[dict] = []
    if not plugins_dir.is_dir():
        log.warning("plugins dir not found: %s (no plugins)", plugins_dir)
        return loaded, failures

    seen: set[str] = set()
    for d in sorted(plugins_dir.iterdir()):
        if not d.is_dir() or not (d / "plugin.py").is_file():
            continue
        label = d.name
        try:
            plugin = _import_plugin(d)
            if not plugin.name:
                plugin.name = d.name
            if plugin.name in seen:
                raise PluginLoadError(
                    f"plugin name {plugin.name!r} collides with an already-loaded plugin"
                )
            seen.add(plugin.name)

            # Full service handles: setup() is where plugins subscribe to bus
            # events and register host executors, which need approvals/host.
            ctx = PluginContext(
                store=store,
                bus=bus,
                config=config,
                delivery=delivery,
                capability=Capability.permissive(),
                approvals=approvals,
                host=host,
                job_name=None,
            )
            plugin.setup(ctx)  # may raise → isolated below

            jobs: dict[str, Job] = {}
            for j in plugin.jobs():
                if j.name in jobs:
                    raise PluginLoadError(f"duplicate job name {j.name!r}")
                j.plugin = plugin.name
                jobs[j.name] = j
            tools = list(plugin.tools())
            commands = list(plugin.commands())

            store.set_plugin_status(plugin.name, "ok", None)
            lp = LoadedPlugin(
                name=plugin.name,
                plugin=plugin,
                jobs_by_name=jobs,
                tools=tools,
                commands=commands,
            )
            loaded.append(lp)
            bus.publish("plugin.loaded", {"plugin": plugin.name})
            log.info(
                "plugin %s: %d job(s), %d tool(s), %d command(s)",
                plugin.name,
                len(jobs),
                len(tools),
                len(commands),
            )
        except Exception as exc:  # noqa: BLE001 - isolation by design
            err = f"{type(exc).__name__}: {exc}"
            log.error("plugin %s failed to load: %s", label, err)
            store.set_plugin_status(label, "failed", err)
            failures.append({"plugin": label, "error": err})
            bus.publish("plugin.load_failed", {"plugin": label, "error": err})

    return loaded, failures


def _import_plugin(d: Path) -> Plugin:
    """Import ``<d>/plugin.py`` and extract the plugin instance or class."""
    spec = importlib.util.spec_from_file_location(
        f"plp.plugins.{d.name}", d / "plugin.py"
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot import {d / 'plugin.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    candidate = getattr(module, "PLUGIN", None)
    if isinstance(candidate, Plugin):
        return candidate
    classes = [
        v
        for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Plugin) and v is not Plugin
    ]
    if not classes:
        raise PluginLoadError(
            f"{d.name}: no plugin found (define module.PLUGIN or a Plugin subclass)"
        )
    return classes[0]()
