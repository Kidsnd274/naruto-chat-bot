"""Client for the local OpenAI-compatible inference server.

Settings are read on every call, so changes in the web admin apply to the next
request. All requests are single-flight process-wide: a local server has one
useful execution slot, so concurrent triggers queue here instead of competing
for the GPU.
"""

import asyncio
from dataclasses import dataclass
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


class LLMError(Exception):
    """The model request failed; the message is safe to log."""


@dataclass
class ChatResult:
    text: str
    reasoning: str | None
    model: str
    latency_ms: int
    usage: dict | None
    finish_reason: str | None


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
    ) -> dict[str, Any]:
        """Keyword arguments for chat.completions.create(). Unset (None)
        params are omitted so the server's defaults apply."""
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
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

    async def chat(
        self,
        messages: list[dict],
        *,
        reasoning: bool | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self.waiting -= 1
        self.in_flight += 1
        try:
            return await self._chat(messages, reasoning, max_tokens)
        finally:
            self.in_flight -= 1
            self._lock.release()

    async def _chat(self, messages, reasoning, max_tokens) -> ChatResult:
        model = await self.resolve_model()
        kwargs = self.build_request(messages, model=model, reasoning=reasoning,
                                    max_tokens=max_tokens)
        started = time.monotonic()
        try:
            response = await self._get_client().chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        except (openai.OpenAIError, OSError) as exc:
            raise LLMError(type(exc).__name__) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if not getattr(response, "choices", None):
            raise LLMError("The server returned no choices.")
        choice = response.choices[0]
        message = choice.message
        text, inline_reasoning = split_reasoning(message.content or "")
        reasoning_text = getattr(message, "reasoning_content", None) or inline_reasoning
        usage = response.usage.model_dump() if getattr(response, "usage", None) else None
        return ChatResult(
            text=text,
            reasoning=reasoning_text,
            model=getattr(response, "model", None) or model,
            latency_ms=latency_ms,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    # -------------------------------------------------------------- health

    async def list_models(self, timeout: float = 10.0) -> list[str]:
        client = self._get_client().with_options(timeout=timeout, max_retries=0)
        models = []
        async for model in client.models.list():
            models.append(model.id)
        return models
