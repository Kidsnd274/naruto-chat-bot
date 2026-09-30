"""Run evaluation cases against one or more models.

Each case is loaded into a throwaway in-memory database and the prompt is
built by the production ContextBuilder, so the evaluation measures exactly
what the bot would send. Answers go through the same clean-up as live
replies. Requests are streamed to measure time to first token.
"""

import base64
from dataclasses import dataclass, field
import json
import mimetypes
import sqlite3
import time
from typing import Any, Callable

import openai

from naruto.agent.context import ContextBuilder, ImageInput, Prompt
from naruto.agent.text import clean_model_output
from naruto.bootstrap import Bootstrap
from naruto.db import open_database
from naruto.db.messages import LIVE, NewMessage
from naruto.evaluation.cases import Case
from naruto.evaluation.checks import CheckResult, run_checks
from naruto.llm import LLMClient, split_reasoning
from naruto.services import BotIdentity, Services
from naruto.settings.registry import SettingError

CHAT_ID = -1_000_000
BOT_ID = 424242


@dataclass
class ModelTarget:
    name: str
    endpoint: str

    @property
    def label(self) -> str:
        return self.name


def parse_model(value: str, default_endpoint: str) -> ModelTarget:
    """``name`` or ``name@http://host:port/v1``."""
    if "@" in value:
        name, endpoint = value.split("@", 1)
        return ModelTarget(name.strip(), endpoint.strip())
    return ModelTarget(value.strip(), default_endpoint)


def load_settings_db(path: str) -> dict[str, Any]:
    """Settings stored in a bot database (opened read-only), so the
    evaluation uses the same prompts and sampling as production."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    finally:
        connection.close()
    return {key: json.loads(value) for key, value in rows}


@dataclass
class Attempt:
    case_id: str
    category: str
    model: str
    attempt: int
    status: str  # pass | fail | manual | error
    text: str = ""
    threaded: bool = False
    reasoning: str | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    prompt_tokens: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None
    prompt: list | None = None


def build_prompt(case: Case, base_settings: dict[str, Any]) -> tuple[Prompt, Services]:
    """Load the case into an in-memory database and build the request."""
    db = open_database(":memory:")
    bootstrap = Bootstrap(telegram_bot_token="0:eval", openai_api_key="eval", admin_password="",
                          owner_user_id=None, database_path=":memory:", web_host="127.0.0.1",
                          web_port=0)
    services = Services.create(bootstrap, db)
    for key, value in {**base_settings, **case.settings, "general.timezone": case.timezone}.items():
        if key in services.settings.registry:
            try:
                services.settings.set(key, value, actor="evaluation")
            except SettingError as exc:
                raise SettingError(f"case {case.id}: setting {key}: {exc}") from None
    bot = BotIdentity(id=BOT_ID, username=case.bot_username, name=case.bot_name)
    services.status.bot = bot

    services.chats.upsert_seen(CHAT_ID, title=case.chat_title, chat_type=case.chat_type)
    services.chats.set_status(CHAT_ID, "enabled")
    for member in case.members:
        user_id = member.get("id")
        if user_id is None:
            continue
        services.members.upsert_live(CHAT_ID, int(user_id), member.get("name", f"User {user_id}"),
                                     member.get("username"))
        for alias in member.get("aliases", []):
            services.members.add_alias(CHAT_ID, int(user_id), alias)

    names: dict[str, int] = {}

    def sender_id(message) -> int:
        if message.from_bot:
            return BOT_ID
        if message.sender_id is not None:
            return int(message.sender_id)
        return names.setdefault(message.sender, 10_000 + len(names))

    stored = None
    for message in case.messages + [case.trigger]:
        stored = services.messages.insert_live(NewMessage(
            chat_id=CHAT_ID, origin_chat_id=CHAT_ID, source=LIVE, message_id=message.id,
            sender_id=sender_id(message), sender_name=case.bot_name if message.from_bot else message.sender,
            sender_username=message.username, from_bot=message.from_bot, date=message.date,
            text=message.text, media_kind=message.media, reply_to_message_id=message.reply_to,
        ))
    images = []
    if case.trigger.image is not None:
        data = case.trigger.image.read_bytes()
        mime = mimetypes.guess_type(case.trigger.image.name)[0] or "image/jpeg"
        images.append(ImageInput(row_id=stored.id, mime_type=mime,
                                 base64=base64.b64encode(data).decode("ascii")))
    chat = services.chats.get(CHAT_ID)
    prompt = ContextBuilder(services).build(chat, stored, bot=bot, images=images, skill=case.skill)
    return prompt, services


ClientFactory = Callable[[ModelTarget], Any]


def default_client_factory(api_key: str, timeout: float) -> ClientFactory:
    def make(target: ModelTarget):
        return openai.AsyncOpenAI(api_key=api_key, base_url=target.endpoint, timeout=timeout,
                                  max_retries=0)
    return make


async def _call(client, kwargs: dict, stream: bool) -> dict:
    started = time.monotonic()
    if not stream:
        response = await client.chat.completions.create(**kwargs)
        total = int((time.monotonic() - started) * 1000)
        message = response.choices[0].message
        return {"content": message.content or "",
                "reasoning": getattr(message, "reasoning_content", None),
                "ttft_ms": None, "total_ms": total,
                "usage": response.usage.model_dump() if getattr(response, "usage", None) else None,
                "finish_reason": response.choices[0].finish_reason}
    content, reasoning = [], []
    ttft = None
    usage = None
    finish_reason = None
    events = await client.chat.completions.create(
        **kwargs, stream=True, stream_options={"include_usage": True})
    async for chunk in events:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else dict(chunk.usage)
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        piece = getattr(delta, "content", None)
        extra = getattr(delta, "model_extra", None) or {}
        thought = getattr(delta, "reasoning_content", None) or extra.get("reasoning_content")
        if (piece or thought) and ttft is None:
            ttft = int((time.monotonic() - started) * 1000)
        if piece:
            content.append(piece)
        if thought:
            reasoning.append(thought)
        if choice.finish_reason:
            finish_reason = choice.finish_reason
    return {"content": "".join(content), "reasoning": "".join(reasoning) or None,
            "ttft_ms": ttft, "total_ms": int((time.monotonic() - started) * 1000),
            "usage": usage, "finish_reason": finish_reason}


async def evaluate(
    cases: list[Case],
    targets: list[ModelTarget],
    *,
    client_factory: ClientFactory,
    base_settings: dict[str, Any] | None = None,
    repeat: int = 1,
    stream: bool = True,
    keep_prompts: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[Attempt]:
    base_settings = base_settings or {}
    clients = {target.label: client_factory(target) for target in targets}
    attempts = []
    for case in cases:
        prompt, services = build_prompt(case, base_settings)
        reasoning = services.settings[f"skills.{case.skill}.reasoning"]
        for target in targets:
            kwargs = LLMClient(services.settings, "eval").build_request(
                prompt.messages, model=target.name, reasoning=reasoning)
            for index in range(1, repeat + 1):
                attempt = Attempt(case.id, case.category, target.label, index, "error",
                                  prompt_tokens=prompt.estimated_tokens,
                                  prompt=prompt.messages if keep_prompts else None)
                if "tool_calls" in case.expect:
                    attempt.status = "skipped"
                    attempt.error = "Tool-calling cases need the agent loop (phase 3)."
                    attempts.append(attempt)
                    continue
                try:
                    output = await _call(clients[target.label], kwargs, stream)
                except Exception as exc:
                    attempt.error = f"{type(exc).__name__}: {exc}"[:500]
                else:
                    text, inline = split_reasoning(output["content"])
                    threaded, text = clean_model_output(text, case.bot_name)
                    attempt.text = text
                    attempt.threaded = threaded
                    attempt.reasoning = output["reasoning"] or inline
                    attempt.ttft_ms = output["ttft_ms"]
                    attempt.total_ms = output["total_ms"]
                    attempt.usage = output["usage"]
                    attempt.finish_reason = output["finish_reason"]
                    attempt.checks = run_checks(case.expect, text, threaded)
                    if attempt.checks:
                        attempt.status = "pass" if all(c.passed for c in attempt.checks) else "fail"
                    else:
                        attempt.status = "manual"
                attempts.append(attempt)
                if progress:
                    progress(f"{case.id} · {target.label} #{index}: {attempt.status}")
        services.db.close()
    return attempts
