"""Tool registry tests: schema derivation from type hints, namespacing,
argument validation (PRD.md §6.2/§6.5)."""

from __future__ import annotations

from typing import Literal

import pytest

from plp.kernel.registry import ToolError, ToolRegistry, derive_schema, tool


def test_schema_derivation():
    @tool("demo tool")
    def t(a: str, b: int = 3, c: list[int] | None = None, d: bool = True) -> str:
        return a

    s = derive_schema(t)
    assert s["type"] == "object"
    assert s["properties"]["a"] == {"type": "string"}
    assert s["properties"]["b"] == {"type": "integer", "default": 3}
    assert s["properties"]["c"] == {
        "type": "array",
        "items": {"type": "integer"},
        "nullable": True,
        "default": [],
    }
    assert s["properties"]["d"] == {"type": "boolean", "default": True}
    assert s["required"] == ["a"]
    assert s["additionalProperties"] is False


def test_literal_enum():
    @tool("pick a mode")
    def t(mode: Literal["quick", "full"] = "quick") -> str:
        return mode

    s = derive_schema(t)
    assert s["properties"]["mode"]["enum"] == ["quick", "full"]
    assert s["required"] == []


def test_ctx_parameter_rejected():
    with pytest.raises(ValueError, match="ctx"):
        tool("bad")(lambda ctx: None)


def test_registry_call_and_validation():
    reg = ToolRegistry()

    @tool("echo a message")
    def echo(message: str, times: int = 1) -> str:
        return (message + " ") * times

    t = reg.register("demo.echo", echo)
    assert t.call(message="hi", times=2) == "hi hi "
    assert t.call(message="hi") == "hi "  # default applied

    with pytest.raises(ToolError, match="missing required"):
        t.call()
    with pytest.raises(ToolError, match="unknown argument"):
        t.call(message="hi", nope=1)
    with pytest.raises(ToolError, match="expected string"):
        t.call(message=42)
    with pytest.raises(ToolError, match="expected integer"):
        t.call(message="hi", times="2")
    with pytest.raises(ToolError, match="expected integer, got bool"):
        t.call(message="hi", times=True)  # bool is not a valid JSON number


def test_duplicate_registration_rejected():
    reg = ToolRegistry()

    @tool("a")
    def a(x: str) -> str:
        return x

    @tool("b")
    def b(x: str) -> str:
        return x

    reg.register("n.a", a)
    with pytest.raises(ValueError, match="duplicate"):
        reg.register("n.a", b)


def test_openai_schema_shape():
    reg = ToolRegistry()

    @tool("echo")
    def echo(message: str) -> str:
        return message

    reg.register("x.echo", echo)
    s = reg.openai_schemas(["x.echo"])[0]
    assert s["type"] == "function"
    assert s["function"]["name"] == "x.echo"
    assert s["function"]["parameters"]["properties"]["message"]["type"] == "string"
