"""The bounded tool-calling agent runtime (PRD.md §6.5).

A *scenario* declares everything an agent turn needs: system prompt, a
**narrow** mounted tool set, a context builder (from the store — never
"dump the life"), and an optional JSON schema the final answer must satisfy.

Guarantees:
- the loop is bounded by the context's ``Capability.step_budget``;
- tool results that error are returned to the model as observations, never
  raised out of the loop;
- load-bearing output is structured: parsed + validated → one retry hint →
  degrade to plain text;
- an unreachable LLM yields ``{"ok": False, "degraded": True, ...}`` so every
  feature keeps a non-LLM floor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .capability import Capability
from .context import PluginContext
from .llm import LLMClient, LLMError, LLMUnavailable
from .registry import ToolRegistry
from .util import extract_json_object, validate_against_schema

log = logging.getLogger("plp.kernel.agent")


@dataclass
class Scenario:
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)  # namespaced tool names
    context_fn: Callable[[PluginContext], str] | None = None
    output_schema: dict | None = None  # JSON Schema the final JSON must satisfy
    step_budget: int | None = None  # per-scenario guardrail (None = capability default)

    def context_text(self, ctx: PluginContext) -> str:
        return self.context_fn(ctx) if self.context_fn else ""


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        bus=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self._log = logger or log

    def run_turn(
        self,
        scenario: Scenario,
        ctx: PluginContext,
        user_message: str,
        capability: Capability | None = None,
    ) -> dict:
        """Run one bounded agent turn; returns a result dict (never raises)."""
        capability = capability or Capability.permissive(self.llm.max_tool_steps)
        if not self.llm.available():
            return {
                "ok": False,
                "degraded": True,
                "reason": "llm_unavailable",
                "text": "",
            }
        messages: list[dict] = [
            {"role": "system", "content": scenario.system_prompt}
        ]
        context_text = scenario.context_text(ctx)
        if context_text:
            messages.append({"role": "system", "content": context_text})
        messages.append({"role": "user", "content": user_message})
        tool_schemas = self.tools.openai_schemas(scenario.tools)
        budget = scenario.step_budget or capability.step_budget

        steps = 0
        while steps < budget:
            steps += 1
            last = steps >= budget  # final step: no tools, force the answer
            try:
                msg = self.llm.chat(
                    messages, tools=None if last else (tool_schemas or None)
                )
            except LLMUnavailable as exc:
                return {"ok": False, "degraded": True, "reason": str(exc), "text": ""}
            except LLMError as exc:
                return {"ok": False, "degraded": True, "reason": str(exc), "text": ""}

            if isinstance(msg, str):  # lenient: some providers return raw text
                msg = {"content": msg}
            if last:
                # A forced final that still emits tool calls: use its text if
                # any, otherwise the turn ends here (budget exhausted).
                if not (msg.get("content") or "").strip():
                    break
                msg = {"content": msg["content"]}
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final_text = msg.get("content") or ""
                result = {"ok": True, "text": final_text, "steps": steps}
                if scenario.output_schema is not None:
                    result.update(self._structure(final_text, scenario.output_schema))
                if self.bus:
                    self.bus.publish(f"agent.{scenario.name}.done", {"steps": steps})
                return result

            messages.append(msg)
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = None
                observation = self._call_tool(name, args, capability)
                if self.bus:
                    self.bus.publish(f"agent.{scenario.name}.tool", {"tool": name})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps(observation, default=str),
                    }
                )
        self._log.warning(
            "scenario %s: step budget (%d) exhausted without a final answer",
            scenario.name,
            budget,
        )
        if self.bus:
            self.bus.publish(
                f"agent.{scenario.name}.done",
                {"steps": steps, "exhausted": True},
            )
        return {
            "ok": False,
            "degraded": True,
            "reason": "step_budget_exhausted",
            "text": "",
        }

    def _call_tool(self, name: str, args: Any, capability: Capability) -> Any:
        if not capability.can_use_tool(name):
            return {"error": f"capability denies tool {name!r}"}
        t = self.tools.get(name)
        if t is None:
            return {"error": f"unknown tool {name!r}"}
        if args is None:
            return {"error": f"tool {name!r}: unparseable arguments"}
        if not isinstance(args, dict):
            return {"error": f"tool {name!r}: arguments must be a JSON object"}
        try:
            return t.call(**args)
        except Exception as exc:  # noqa: BLE001 - observations, not crashes
            self._log.warning("tool %s failed: %s", name, exc)
            return {"error": str(exc)}

    @staticmethod
    def _structure(text: str, schema: dict) -> dict:
        """Parse + validate the model's final answer against the required schema."""
        try:
            raw = extract_json_object(text)
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return {"structured": None, "schema_error": f"not valid JSON: {exc}"}
        errors = validate_against_schema(payload, schema)
        if errors:
            return {"structured": None, "schema_error": "; ".join(errors[:5])}
        return {"structured": payload, "schema_error": None}
