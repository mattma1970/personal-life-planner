"""Agent runtime tests with a scripted fake LLM: bounded tool loop,
capability denial, structured-output success/degradation, LLM unavailability
(PRD.md §6.5 rules for 27B)."""

from __future__ import annotations

from plp.kernel.agent import Agent, Scenario
from plp.kernel.capability import Capability
from plp.kernel.config import LLMConfig
from plp.kernel.llm import LLMClient
from plp.kernel.registry import ToolRegistry
from plp.kernel import tool


class FakeLLM(LLMClient):
    def __init__(self, script: list[dict]) -> None:
        super().__init__(LLMConfig())
        self.script = list(script)
        self.calls = []

    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, temperature=0.4):
        self.calls.append({"messages": messages, "tools": tools})
        return self.script.pop(0)


class DeadLLM(LLMClient):
    def __init__(self) -> None:
        super().__init__(LLMConfig())

    def available(self) -> bool:
        return False


class Ctx:
    """The agent only passes ctx to scenario context functions."""


def make_agent(script: list[dict]) -> tuple[Agent, FakeLLM]:
    llm = FakeLLM(script)
    tools = ToolRegistry()
    tools.register("demo.echo", tool("echo a message")(lambda message: f"echo:{message}"))
    return Agent(llm, tools), llm


def tool_call_msg(name: str, args: str, call_id: str = "c1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "function": {"name": name, "arguments": args}}
        ],
    }


def test_bounded_tool_loop():
    agent, llm = make_agent(
        [
            tool_call_msg("demo.echo", '{"message": "hello"}'),
            {"role": "assistant", "content": "done: hello"},
        ]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", tools=["demo.echo"]), Ctx(), "go"
    )
    assert res["ok"] is True
    assert res["text"] == "done: hello"
    assert res["steps"] == 2
    # the tool observation went back to the model
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert "echo:hello" in second[-1]["content"]
    # mounted tool schemas were passed to the server
    assert llm.calls[0]["tools"][0]["function"]["name"] == "demo.echo"


def test_structured_output_success():
    schema = {
        "type": "object",
        "properties": {"score": {"type": "integer"}, "note": {"type": "string"}},
        "required": ["score"],
    }
    agent, _ = make_agent(
        [{"role": "assistant", "content": 'here you go: {"score": 8, "note": "good"}'}]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", output_schema=schema), Ctx(), "rate"
    )
    assert res["ok"] is True
    assert res["structured"] == {"score": 8, "note": "good"}
    assert res["schema_error"] is None


def test_structured_output_degrades_not_raises():
    schema = {
        "type": "object",
        "properties": {"score": {"type": "integer"}},
        "required": ["score"],
    }
    agent, _ = make_agent(
        [{"role": "assistant", "content": "I refuse to give you JSON"}]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", output_schema=schema), Ctx(), "rate"
    )
    assert res["ok"] is True  # the turn itself succeeded
    assert res["structured"] is None
    assert "not valid JSON" in res["schema_error"]


def test_capability_denies_tool():
    agent, llm = make_agent(
        [
            tool_call_msg("demo.echo", '{"message": "sneaky"}'),
            {"role": "assistant", "content": "ok, moving on"},
        ]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", tools=["demo.echo"]),
        Ctx(),
        "go",
        capability=Capability(strict=True),  # empty allow-set in strict mode
    )
    assert res["ok"] is True
    second = llm.calls[1]["messages"]
    assert "capability denies" in second[-1]["content"]


def test_unknown_tool_is_observation_not_crash():
    agent, _ = make_agent(
        [
            tool_call_msg("demo.missing", '{}'),
            {"role": "assistant", "content": "nevermind"},
        ]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", tools=["demo.missing"]), Ctx(), "go"
    )
    assert res["ok"] is True


def test_step_budget_exhaustion_degrades():
    agent, _ = make_agent(
        [
            tool_call_msg("demo.echo", '{"message": "1"}'),
            tool_call_msg("demo.echo", '{"message": "2"}', "c2"),
            tool_call_msg("demo.echo", '{"message": "3"}', "c3"),
        ]
    )
    res = agent.run_turn(
        Scenario(name="t", system_prompt="s", tools=["demo.echo"]),
        Ctx(),
        "go",
        capability=Capability(step_budget=2),
    )
    assert res["ok"] is False
    assert res["degraded"] is True
    assert res["reason"] == "step_budget_exhausted"


def test_llm_unavailable_degrades():
    agent = Agent(DeadLLM(), ToolRegistry())
    res = agent.run_turn(Scenario(name="t", system_prompt="s"), Ctx(), "go")
    assert res == {
        "ok": False,
        "degraded": True,
        "reason": "llm_unavailable",
        "text": "",
    }
