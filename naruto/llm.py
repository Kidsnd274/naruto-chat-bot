"""Client for the local OpenAI-compatible inference server.

Settings are read on every call, so changes in the web admin apply to the next
request. All requests are single-flight process-wide: a local server has one
useful execution slot, so concurrent triggers queue here instead of competing
for the GPU. Replies to people go first: background work (digests, memory)
waits while any reply is queued.
"""

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
import time
from typing import Any

import openai

from naruto.settings.service import SettingsService

logger = logging.getLogger(__name__)

# Sampling params the OpenAI SDK accepts as keyword arguments.
_STANDARD_PARAMS = ("temperature", "top_p")
# Params the SDK does not know; they reach llama.cpp-style servers through
# extra_body.
_EXTRA_BODY_PARAMS = ("top_k", "min_p", "repeat_penalty")

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
# Qwen's native tool-call format, for servers that don't parse it into
# structured tool_calls.
_INLINE_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)


class LLMError(Exception):
    """The model request failed; the message is safe to log."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    error: str | None = None  # the arguments were not a JSON object

    def as_request_part(self) -> dict:
        """The call as it goes back to the model in the assistant message."""
        return {"id": self.id, "type": "function",
                "function": {"name": self.name,
                             "arguments": self.raw_arguments or json.dumps(self.arguments)}}

    def summary(self) -> dict:
        data = {"id": self.id, "name": self.name, "arguments": self.arguments}
        if self.error:
            data["error"] = self.error
            data["raw_arguments"] = self.raw_arguments[:2000]
        return data


def parse_arguments(raw: Any) -> tuple[dict, str | None]:
    """Tool arguments arrive as a JSON string (sometimes already decoded)."""
    if isinstance(raw, dict):
        return raw, None
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"Arguments are not valid JSON ({exc.msg})."
    if not isinstance(value, dict):
        return {}, "Arguments must be a JSON object."
    return value, None


def make_tool_call(call_id: str | None, name: str, raw_arguments: Any, index: int) -> ToolCall:
    arguments, error = parse_arguments(raw_arguments)
    raw = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments or {})
    return ToolCall(id=call_id or f"call_{index}", name=(name or "").strip(),
                    arguments=arguments, raw_arguments=raw, error=error)


def split_inline_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Pull ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``
    blocks out of the answer text."""
    calls = []
    for index, match in enumerate(_INLINE_TOOL_CALL.finditer(text)):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("name"):
            calls.append(make_tool_call(None, str(payload["name"]),
                                        payload.get("arguments") or {}, index))
    if not calls:
        return text, []
    return _INLINE_TOOL_CALL.sub("", text).strip(), calls


@dataclass
class ChatResult:
    text: str
    reasoning: str | None
    model: str
    latency_ms: int
    usage: dict | None
    finish_reason: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    ttft_ms: int | None = None  # streamed requests only


def split_reasoning(content: str) -> tuple[str, str | None]:
    """Separate inline <think> blocks from the answer. An unclosed <think>
    (output cut off while reasoning) leaves no answer."""
    reasoning_parts = [match.strip() for match in _THINK_BLOCK.findall(content)]
    text = _THINK_BLOCK.sub("", content)
    lowered = text.lower()
    if "<think>" in lowered:
        start = lowered.index("<think>")
        reasoning_parts.append(text[start + len("<think>"):].strip())
        text = text[:start]
    reasoning = "\n\n".join(part for part in reasoning_parts if part) or None
    return text.strip(), reasoning


class LLMClient:
    def __init__(self, settings: SettingsService, api_key: str):
        self.settings = settings
        self.api_key = api_key
        self._client: openai.AsyncOpenAI | None = None
        self._client_key: tuple | None = None
        self._lock = asyncio.Lock()
        self._listed_model: tuple[str, str] | None = None  # (endpoint, model)
        self.in_flight = 0
        self.waiting = 0
        self._foreground_waiting = 0

    # -------------------------------------------------------------- client

    def _get_client(self) -> openai.AsyncOpenAI:
        key = (
            self.settings["model.endpoint_url"],
            self.settings["model.request_timeout_seconds"],
        )
        if self._client is None or self._client_key != key:
            endpoint, timeout = key
            self._client = openai.AsyncOpenAI(
                api_key=self.api_key,
                base_url=endpoint,
                timeout=float(timeout),
                max_retries=1,
            )
            self._client_key = key
        return self._client

    # ------------------------------------------------------------- request

    def build_request(
        self,
        messages: list[dict],
        *,
        model: str,
        reasoning: bool | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Keyword arguments for chat.completions.create(). Unset (None)
        params are omitted so the server's defaults apply."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        extra_body: dict[str, Any] = {}
        for name in _STANDARD_PARAMS:
            value = self.settings[f"model.{name}"]
            if value is not None:
                kwargs[name] = value
        for name in _EXTRA_BODY_PARAMS:
            value = self.settings[f"model.{name}"]
            if value is not None:
                extra_body[name] = value
        template_kwargs = dict(self.settings["model.chat_template_kwargs"] or {})
        if reasoning is not None:
            template_kwargs["enable_thinking"] = reasoning
        if template_kwargs:
            extra_body["chat_template_kwargs"] = template_kwargs
        if max_tokens is None:
            max_tokens = self.settings["model.max_output_tokens"]
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    async def resolve_model(self) -> str:
        """The configured model name, or the first model the server lists."""
        name = self.settings["model.name"]
        if name:
            return name
        endpoint = self.settings["model.endpoint_url"]
        if self._listed_model and self._listed_model[0] == endpoint:
            return self._listed_model[1]
        models = await self.list_models()
        if not models:
            raise LLMError("No model name is set and the server lists no models.")
        self._listed_model = (endpoint, models[0])
        return models[0]

    async def _acquire(self, background: bool) -> None:
        """Take the single inference slot. Background work only gets it when
        no reply is waiting; a running request is never interrupted."""
        self.waiting += 1
        try:
            if not background:
                self._foreground_waiting += 1
                try:
                    await self._lock.acquire()
                finally:
                    self._foreground_waiting -= 1
                return
            while True:
                await self._lock.acquire()
                if not self._foreground_waiting:
                    return
                self._lock.release()
                await asyncio.sleep(0.05)
        finally:
            self.waiting -= 1

    async def chat(
        self,
        messages: list[dict],
        *,
        reasoning: bool | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        background: bool = False,
    ) -> ChatResult:
        await self._acquire(background)
        self.in_flight += 1
        try:
            model = await self.resolve_model()
            kwargs = self.build_request(messages, model=model, reasoning=reasoning,
                                        max_tokens=max_tokens, tools=tools)
            return await request_completion(self._get_client(), kwargs, stream=stream,
                                            tools_offered=bool(tools))
        finally:
            self.in_flight -= 1
            self._lock.release()

    # -------------------------------------------------------------- health

    async def list_models(self, timeout: float = 10.0) -> list[str]:
        client = self._get_client().with_options(timeout=timeout, max_retries=0)
        models = []
        async for model in client.models.list():
            models.append(model.id)
        return models


# ------------------------------------------------------------------ requests

async def request_completion(client, kwargs: dict, *, stream: bool = False,
                             tools_offered: bool = False) -> ChatResult:
    """Send one chat completion and normalize the answer: reasoning split off,
    tool calls parsed. Streaming measures time to first token."""
    started = time.monotonic()
    try:
        if stream:
            parts = await _consume_stream(client, kwargs, started)
        else:
            parts = _from_response(await client.chat.completions.create(**kwargs))
    except LLMError:
        raise
    except openai.APIError as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    except openai.OpenAIError as exc:
        raise LLMError(type(exc).__name__) from exc
    except OSError as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}"[:300] if str(exc)
                       else type(exc).__name__) from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    text, inline_reasoning = split_reasoning(parts["content"])
    tool_calls = parts["tool_calls"]
    if tools_offered and not tool_calls:
        text, tool_calls = split_inline_tool_calls(text)
    return ChatResult(
        text=text,
        reasoning=parts["reasoning"] or inline_reasoning,
        model=parts["model"] or kwargs["model"],
        latency_ms=latency_ms,
        usage=parts["usage"],
        finish_reason=parts["finish_reason"],
        tool_calls=tool_calls,
        ttft_ms=parts.get("ttft_ms"),
    )


def _from_response(response) -> dict:
    if not getattr(response, "choices", None):
        raise LLMError("The server returned no choices.")
    choice = response.choices[0]
    message = choice.message
    calls = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        calls.append(make_tool_call(getattr(call, "id", None),
                                    getattr(function, "name", "") or "",
                                    getattr(function, "arguments", "") or "", index))
    return {
        "content": getattr(message, "content", None) or "",
        "reasoning": getattr(message, "reasoning_content", None),
        "tool_calls": calls,
        "model": getattr(response, "model", None),
        "usage": response.usage.model_dump() if getattr(response, "usage", None) else None,
        "finish_reason": getattr(choice, "finish_reason", None),
    }


async def _consume_stream(client, kwargs: dict, started: float) -> dict:
    content, reasoning = [], []
    calls: dict[int, dict] = {}
    ttft = usage = finish_reason = model = None
    events = await client.chat.completions.create(
        **kwargs, stream=True, stream_options={"include_usage": True})
    async for chunk in events:
        model = model or getattr(chunk, "model", None)
        if getattr(chunk, "usage", None):
            usage = (chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump")
                     else dict(chunk.usage))
        if not getattr(chunk, "choices", None):
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        piece = getattr(delta, "content", None)
        extra = getattr(delta, "model_extra", None) or {}
        thought = getattr(delta, "reasoning_content", None) or extra.get("reasoning_content")
        call_parts = getattr(delta, "tool_calls", None) or []
        if (piece or thought or call_parts) and ttft is None:
            ttft = int((time.monotonic() - started) * 1000)
        if piece:
            content.append(piece)
        if thought:
            reasoning.append(thought)
        for part in call_parts:
            slot = calls.setdefault(getattr(part, "index", None) or 0,
                                    {"id": None, "name": "", "arguments": ""})
            if getattr(part, "id", None):
                slot["id"] = part.id
            function = getattr(part, "function", None)
            if function is not None:
                if getattr(function, "name", None) and not slot["name"]:
                    slot["name"] = function.name
                if getattr(function, "arguments", None):
                    slot["arguments"] += function.arguments
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason
    tool_calls = [make_tool_call(slot["id"], slot["name"], slot["arguments"], index)
                  for index, slot in sorted(calls.items())]
    return {"content": "".join(content), "reasoning": "".join(reasoning) or None,
            "tool_calls": tool_calls, "model": model, "usage": usage,
            "finish_reason": finish_reason, "ttft_ms": ttft}
