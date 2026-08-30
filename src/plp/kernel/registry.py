"""The tool registry: ``@tool`` functions → namespaced, schema-derived tools.

One registry, two consumers (PRD.md §6.2): deterministic job handlers call
tools directly, and the agent runtime exposes them to the model via OpenAI
function-calling. JSON schemas are *derived from type hints* — nothing is
hand-written, so there is nothing to drift.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import types
import typing
from typing import Any

from .plugin import TOOL_ATTR, tool  # noqa: F401 (re-exported for convenience)
from .util import validate_against_schema

__all__ = ["tool", "TOOL_ATTR", "ToolError", "Tool", "ToolRegistry", "derive_schema"]


class ToolError(ValueError):
    """Argument validation failed for a tool call."""


# ----------------------------------------------------------------- schema

def _json_schema_for(tp: Any) -> dict:
    origin = getattr(tp, "__origin__", None)
    if tp is type(None):
        return {"type": "null"}
    if origin is typing.Union or isinstance(tp, types.UnionType):
        # covers typing.Union[X, None] and PEP 604 (X | None)
        args = [a for a in tp.__args__ if a is not type(None)]
        if len(args) == 1:
            schema = _json_schema_for(args[0])
            schema["nullable"] = True
            return schema
        return {}  # complex union: unconstrained
    if origin is list:
        inner = _json_schema_for(tp.__args__[0]) if tp.__args__ else {"type": "string"}
        return {"type": "array", "items": inner}
    if origin is typing.Literal:
        first = tp.__args__[0]
        base = {"string", "integer", "number", "boolean"}
        tname = (
            "string"
            if any(isinstance(v, str) for v in tp.__args__)
            else "integer"
            if any(isinstance(v, int) and not isinstance(v, bool) for v in tp.__args__)
            else "boolean"
            if any(isinstance(v, bool) for v in tp.__args__)
            else "number"
        )
        return {"type": tname, "enum": list(tp.__args__)}
    if tp in (str, int, float, bool):
        return {"type": {str: "string", int: "integer", float: "number", bool: "boolean"}[tp]}
    if tp is dict:
        return {"type": "object"}
    if tp in (_dt.datetime, _dt.date):
        return {"type": "string", "format": "date-time" if tp is _dt.datetime else "date"}
    return {}  # unknown type: unconstrained


def _resolved_hints(fn) -> dict:
    """Type hints with string annotations (PEP 563) resolved against the module.

    Falls back to raw annotations when resolution fails.
    """
    try:
        return typing.get_type_hints(fn)
    except (NameError, TypeError, AttributeError):
        hints: dict = {}
        for name, p in inspect.signature(fn).parameters.items():
            if p.annotation is not inspect.Parameter.empty:
                hints[name] = p.annotation
        return hints


def derive_schema(fn) -> dict:
    """Build the JSON Schema for a tool function from its type hints."""
    sig = inspect.signature(fn)
    hints = _resolved_hints(fn)
    props: dict[str, dict] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        ann = hints.get(name, p.annotation)
        schema = _json_schema_for(ann) if ann is not inspect.Parameter.empty else {"type": "string"}
        if p.default is not inspect.Parameter.empty:
            if schema.get("type") in ("string", "integer", "number", "boolean"):
                schema["default"] = p.default
            if schema.get("type") == "array":
                schema["default"] = []
        else:
            required.append(name)
        props[name] = schema
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# ----------------------------------------------------------------- tools

class Tool:
    def __init__(self, name: str, fn, description: str, schema: dict) -> None:
        self.name = name
        self.fn = fn
        self.description = description
        self.schema = schema

    def validate(self, kwargs: dict) -> None:
        unknown = set(kwargs) - set(self.schema.get("properties", {}))
        if unknown:
            raise ToolError(f"{self.name}: unknown argument(s): {sorted(unknown)}")
        for req in self.schema.get("required", []):
            if req not in kwargs:
                raise ToolError(f"{self.name}: missing required argument {req!r}")
        for key, value in kwargs.items():
            sub = self.schema.get("properties", {}).get(key, {})
            for err in validate_against_schema(value, sub):
                raise ToolError(f"{self.name}: {key}: {err}")

    def call(self, **kwargs: Any) -> Any:
        self.validate(kwargs)
        return self.fn(**kwargs)

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


class ToolRegistry:
    def __init__(self, logger=None) -> None:
        self._tools: dict[str, Tool] = {}
        self._log = logger

    def register(self, name: str, fn) -> Tool:
        meta = fn.__dict__.get(TOOL_ATTR)
        if meta is None:
            raise ValueError(f"{fn.__name__} is not decorated with @tool")
        if name in self._tools:
            raise ValueError(f"duplicate tool name {name!r}")
        t = Tool(name, fn, meta["description"], derive_schema(fn))
        self._tools[name] = t
        if self._log:
            self._log.debug("tool registered: %s", name)
        return t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_schemas(self, names: list[str] | None = None) -> list[dict]:
        wanted = names if names is not None else self.names()
        return [self._tools[n].openai_schema() for n in wanted if n in self._tools]
