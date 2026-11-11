"""Chat providers (design §7, §13): model-agnostic JSON completion used only to *curate*.

``ChatProvider.complete_json`` returns one JSON object matching a JSON schema. Implementations:

- ``AnthropicChat``: Messages API over httpx; the schema is forced through a tool call, which is
  the reliable way to get structured output.
- ``OpenAICompatChat``: ``/chat/completions`` with a JSON-schema ``response_format``; also
  covers Ollama, vLLM and LiteLLM proxies.
- ``FakeChatProvider``: scripted responses for tests and offline demos.

Transient failures (HTTP 429/5xx, timeouts, connection errors) are retried with exponential
backoff, honouring ``Retry-After``. Everything else raises ``ChatError``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable

import httpx

from satchel.settings import Settings, get_settings

log = logging.getLogger("satchel.llm.chat")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class ChatError(RuntimeError):
    """The model call failed (after retries) or returned something unusable."""


class ChatMessage(TypedDict):
    role: str  # "user" | "assistant"
    content: str


class ToolSpec(TypedDict):
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class ModelTurn:
    """One assistant turn of a tool-use loop: text plus any tool calls."""

    text: str = ""
    tool_uses: list[ToolUse] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


# Provider-neutral transcript for the tool loop. Each item is one of:
#   {"role": "user", "content": str}
#   {"role": "assistant", "text": str, "tool_uses": [{"id","name","input"}]}
#   {"role": "tool", "results": [{"id","name","content","is_error"}]}
AgentMessage = dict[str, Any]


@runtime_checkable
class ToolChatProvider(Protocol):
    model_id: str

    def complete_with_tools(
        self, system: str, messages: list[AgentMessage], tools: list[ToolSpec],
        *, max_tokens: int = 4096,
    ) -> ModelTurn:
        """One model turn that may call tools (the caller runs them and loops)."""
        ...


@runtime_checkable
class ChatProvider(Protocol):
    model_id: str

    def complete_json(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any],
        *, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """One JSON object conforming to ``schema`` (a JSON Schema ``type: object``)."""
        ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a model reply that should be a JSON object (tolerates ``` fences and prose)."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ChatError(f"model reply is not JSON: {text[:200]!r}") from None
        try:
            out = json.loads(t[start : end + 1])
        except json.JSONDecodeError as e:
            raise ChatError(f"model reply is not valid JSON: {e}") from None
    if not isinstance(out, dict):
        raise ChatError("model reply is not a JSON object")
    return out


def _usage_of(body: dict[str, Any]) -> Usage:
    u = body.get("usage")
    if not isinstance(u, dict):
        return Usage()
    i = u.get("input_tokens", u.get("prompt_tokens", 0))
    o = u.get("output_tokens", u.get("completion_tokens", 0))
    return Usage(int(i) if isinstance(i, int) else 0, int(o) if isinstance(o, int) else 0)


class _HttpChat:
    """Shared retrying POST."""

    def __init__(
        self, *, timeout_s: float, max_retries: int, client: httpx.Client | None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout_s)
        self._max_retries = max_retries
        self._sleep = sleep
        self.usage = Usage()  # running totals across every call (judges included)

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        last = "no attempt made"
        for attempt in range(self._max_retries + 1):
            wait = min(2.0**attempt, 30.0) + random.uniform(0, 0.25)
            try:
                resp = self._client.post(url, headers=headers, json=payload)
            except httpx.TransportError as e:  # timeouts, connection resets, DNS
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    try:
                        body = resp.json()
                    except ValueError:
                        raise ChatError("chat endpoint returned a non-JSON body") from None
                    if not isinstance(body, dict):
                        raise ChatError("chat endpoint returned an unexpected body")
                    self.usage.add(_usage_of(body))
                    return body
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in _RETRY_STATUS:
                    raise ChatError(last)
                with contextlib.suppress(ValueError):
                    wait = max(wait, min(float(resp.headers.get("retry-after", "0")), 60.0))
            if attempt < self._max_retries:
                log.warning("chat call failed (%s); retry %d/%d", last, attempt + 1,
                            self._max_retries)
                self._sleep(wait)
        raise ChatError(f"chat call failed after {self._max_retries + 1} attempts: {last}")


class AnthropicChat(_HttpChat):
    def __init__(
        self, api_key: str, model: str = DEFAULT_ANTHROPIC_MODEL,
        base_url: str = DEFAULT_ANTHROPIC_URL, *, timeout_s: float = 120.0,
        max_retries: int = 4, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout_s=timeout_s, max_retries=max_retries, client=client, sleep=sleep)
        self.model = model
        self.model_id = f"anthropic:{model}"
        self._url = base_url.rstrip("/") + "/v1/messages"
        # Extra headers (gateways, proxies) first: the protocol headers always win.
        self._headers = {**(extra_headers or {}), "anthropic-version": ANTHROPIC_VERSION,
                         "content-type": "application/json"}
        if api_key:
            self._headers["x-api-key"] = api_key

    def complete_json(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any],
        *, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model, "max_tokens": max_tokens, "system": system,
            "messages": list(messages),
            "tools": [{
                "name": "respond",
                "description": "Return the answer as structured data.",
                "input_schema": schema,
            }],
            "tool_choice": {"type": "tool", "name": "respond"},
        }
        body = self._post(self._url, self._headers, payload)
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                data = block.get("input")
                if isinstance(data, dict):
                    return data
                if isinstance(data, str):
                    return parse_json_object(data)
        # Fall back to text blocks in case a proxy ignored tool_choice.
        text = "".join(
            b.get("text", "") for b in body.get("content") or []
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if text:
            return parse_json_object(text)
        raise ChatError(
            f"no structured output in the reply (stop_reason={body.get('stop_reason')})"
        )


    def complete_with_tools(
        self, system: str, messages: list[AgentMessage], tools: list[ToolSpec],
        *, max_tokens: int = 4096,
    ) -> ModelTurn:
        msgs: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                blocks += [
                    {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]}
                    for t in m.get("tool_uses", [])
                ]
                msgs.append({"role": "assistant", "content": blocks})
            else:
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     "is_error": bool(r.get("is_error"))}
                    for r in m["results"]
                ]})
        payload = {"model": self.model, "max_tokens": max_tokens, "system": system,
                   "messages": msgs, "tools": list(tools)}
        body = self._post(self._url, self._headers, payload)
        turn = ModelTurn(usage=_usage_of(body), stop_reason=body.get("stop_reason"))
        for b in body.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                turn.text += str(b.get("text", ""))
            elif b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                turn.tool_uses.append(ToolUse(str(b["id"]), str(b["name"]), b["input"]))
        return turn


class OpenAICompatChat(_HttpChat):
    def __init__(
        self, model: str, base_url: str = DEFAULT_OPENAI_URL, api_key: str | None = None,
        *, timeout_s: float = 120.0, max_retries: int = 4,
        client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout_s=timeout_s, max_retries=max_retries, client=client, sleep=sleep)
        self.model = model
        self.model_id = f"openai_compat:{model}"
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {**(extra_headers or {}), "content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"

    def complete_json(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any],
        *, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            # strict=False: local servers (Ollama, vLLM) reject or ignore strict schemas.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": False},
            },
        }
        body = self._post(self._url, self._headers, payload)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ChatError("chat endpoint reply has no choices[0].message.content") from None
        if not isinstance(content, str) or not content.strip():
            raise ChatError("chat endpoint returned an empty message")
        return parse_json_object(content)


    def complete_with_tools(
        self, system: str, messages: list[AgentMessage],
        tools: list[ToolSpec], *, max_tokens: int = 4096,
    ) -> ModelTurn:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_uses"):
                    entry["tool_calls"] = [
                        {"id": t["id"], "type": "function",
                         "function": {"name": t["name"], "arguments": json.dumps(t["input"])}}
                        for t in m["tool_uses"]
                    ]
                msgs.append(entry)
            else:
                msgs += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                         for r in m["results"]]
        payload = {
            "model": self.model, "max_tokens": max_tokens, "messages": msgs,
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}}
                for t in tools],
        }
        body = self._post(self._url, self._headers, payload)
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatError("chat endpoint reply has no choices[0].message") from None
        turn = ModelTurn(text=str(msg.get("content") or ""), usage=_usage_of(body),
                         stop_reason=body["choices"][0].get("finish_reason"))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            turn.tool_uses.append(ToolUse(str(call.get("id")), str(fn.get("name")),
                                          args if isinstance(args, dict) else {}))
        return turn


class FakeChatProvider:
    """Scripted provider for tests: replies come from a queue or a callable.

    ``replies`` is a list consumed in order (the last one repeats), or a callable
    ``(system, messages, schema) -> dict``. An ``Exception`` instance in the list is raised.
    Every call is recorded in ``calls``.
    """

    model_id = "fake:scripted"

    def __init__(
        self,
        replies: list[dict[str, Any] | Exception]
        | Callable[[str, list[ChatMessage], dict[str, Any]], dict[str, Any]]
        | None = None,
        turns: list[ModelTurn] | Callable[[str, list[AgentMessage], list[ToolSpec]], ModelTurn]
        | None = None,
    ) -> None:
        self._replies = replies if replies is not None else []
        self._turns = turns
        self.calls: list[dict[str, Any]] = []
        self.turn_calls: list[dict[str, Any]] = []
        self.usage = Usage()

    def complete_with_tools(
        self, system: str, messages: list[AgentMessage], tools: list[ToolSpec],
        *, max_tokens: int = 4096,
    ) -> ModelTurn:
        """Scripted tool-loop turns: a list consumed in order (the last one repeats) or a
        callable ``(system, messages, tools) -> ModelTurn``."""
        self.turn_calls.append({"system": system, "messages": list(messages)})
        if callable(self._turns):
            return self._turns(system, messages, tools)
        if not self._turns:
            raise ChatError("FakeChatProvider has no scripted turns")
        return self._turns[min(len(self.turn_calls) - 1, len(self._turns) - 1)]

    def complete_json(
        self, system: str, messages: list[ChatMessage], schema: dict[str, Any],
        *, max_tokens: int = 4096,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "messages": list(messages), "schema": schema})
        if callable(self._replies):
            return self._replies(system, messages, schema)
        if not self._replies:
            raise ChatError("FakeChatProvider has no scripted reply")
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        reply = self._replies[idx]
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_chat_provider(settings: Settings | None = None) -> ChatProvider | None:
    """The deployment provider (``SATCHEL_CHAT_*``), or ``None`` when ``SATCHEL_CHAT_PROVIDER``
    is unset or keyless. Per-workspace connections: ``satchel.llm.connections``."""
    from satchel.llm.connections import build_chat, deployment_target

    st = settings or get_settings()
    target = deployment_target(st)
    return build_chat(target, st) if target else None
