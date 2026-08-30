"""Thin OpenAI-compatible client for the self-hosted LLM (PRD.md §6.5).

Works with Ollama, vLLM, llama.cpp server, or any ``/chat/completions``
endpoint serving Qwen 3.8 27B. No SDK dependency — plain httpx. A failed
connect raises ``LLMUnavailable`` so callers can degrade gracefully
(PRD.md: the model is seasoning, never the load-bearing wall).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import LLMConfig

log = logging.getLogger("plp.kernel.llm")


class LLMError(Exception):
    """The LLM request failed (bad status, malformed response)."""


class LLMUnavailable(LLMError):
    """The LLM endpoint could not be reached — callers should degrade."""


class LLMClient:
    def __init__(self, cfg: LLMConfig, logger: logging.Logger | None = None) -> None:
        self.base_url = cfg.base_url.rstrip("/")
        self.model = cfg.model
        self.api_key = cfg.api_key
        self.timeout_s = cfg.timeout_seconds
        self.max_tool_steps = cfg.max_tool_steps
        self._log = logger or log

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def available(self) -> bool:
        """Cheap reachability probe (GET /models). Never raises."""
        try:
            r = httpx.get(f"{self.base_url}/models", timeout=5.0)
            return r.status_code < 500
        except httpx.HTTPError as exc:
            self._log.debug("LLM unavailable: %s", exc)
            return False

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.4,
    ) -> dict:
        """One chat completion; returns the assistant message dict.

        ``tools`` is a list of OpenAI function schemas (see ToolRegistry).
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
                timeout=self.timeout_s,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"cannot reach LLM at {self.base_url}: {exc}") from exc
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed LLM response: {exc}") from exc
