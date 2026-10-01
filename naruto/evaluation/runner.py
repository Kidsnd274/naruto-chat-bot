"""Run evaluation cases against one or more models.

Each attempt loads its case into a throwaway in-memory database and runs the
production agent loop (prompt builder, tools, limits), so the evaluation
measures what the bot would do. Telegram actions (polls, pins, the board)
go to a recording stand-in. Requests are streamed to measure time to first
token.
"""

import base64
from dataclasses import dataclass, field
import itertools
import json
import mimetypes
import sqlite3
import time
from types import SimpleNamespace
from typing import Any, Callable

import openai

from naruto.agent.context import ContextBuilder, ImageInput, Prompt
from naruto.agent.runner import AgentRunner, RunRequest
from naruto.bootstrap import Bootstrap
from naruto.db import open_database
from naruto.db.chats import Chat
from naruto.db.messages import LIVE, NewMessage, StoredMessage
from naruto.evaluation.cases import Case
from naruto.evaluation.checks import CheckResult, check_tool_calls, run_checks
from naruto.llm import ChatResult, LLMClient, request_completion
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
    tool_calls: list[dict] = field(default_factory=list)
    model_requests: int = 0
    steps: list | None = None


@dataclass
class LoadedCase:
    services: Services
    chat: Chat
    trigger: StoredMessage
    bot: BotIdentity
    images: list[ImageInput]


def load_case(case: Case, base_settings: dict[str, Any]) -> LoadedCase:
    """Load the case into a fresh in-memory database."""
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
    return LoadedCase(services, services.chats.get(CHAT_ID), stored, bot, images)


def build_prompt(case: Case, base_settings: dict[str, Any]) -> tuple[Prompt, Services]:
    """The first request the bot would send for this case."""
    loaded = load_case(case, base_settings)
    prompt = ContextBuilder(loaded.services).build(loaded.chat, loaded.trigger, bot=loaded.bot,
                                                   images=loaded.images, skill=case.skill)
    return prompt, loaded.services


class TargetLLM:
    """The model under test, with the bot's request parameters."""

    def __init__(self, services: Services, client, model: str):
        self.params = LLMClient(services.settings, "eval")
        self.client = client
        self.model = model

    async def chat(self, messages, *, reasoning=None, max_tokens=None, tools=None,
                   stream=False, background=False, info=None) -> ChatResult:
        kwargs = self.params.build_request(messages, model=self.model, reasoning=reasoning,
                                           max_tokens=max_tokens, tools=tools)
        return await request_completion(self.client, kwargs, stream=stream,
                                        tools_offered=bool(tools))


class RecordingTelegram:
    """Stands in for the Telegram bot: records actions and returns just
    enough for the tools to carry on."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(100_000)

    def _sent(self, chat_id) -> SimpleNamespace:
        return SimpleNamespace(message_id=next(self._ids), chat_id=chat_id)

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send_message", {"chat_id": chat_id, "text": text}))
        return self._sent(chat_id)

    async def send_poll(self, chat_id, question, options, **kwargs):
        self.calls.append(("send_poll", {"question": question, "options": options, **kwargs}))
        return self._sent(chat_id)

    async def do_api_request(self, endpoint, api_kwargs=None, **kwargs):
        self.calls.append((endpoint, api_kwargs or {}))
        return {"message_id": next(self._ids)}

    async def edit_message_text(self, **kwargs):
        self.calls.append(("edit_message_text", kwargs))
        return True

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.calls.append(("pin_chat_message", {"message_id": message_id}))
        return True

    async def unpin_chat_message(self, chat_id, **kwargs):
        self.calls.append(("unpin_chat_message", kwargs))
        return True


ClientFactory = Callable[[ModelTarget], Any]


def default_client_factory(api_key: str, timeout: float) -> ClientFactory:
    def make(target: ModelTarget):
        return openai.AsyncOpenAI(api_key=api_key, base_url=target.endpoint, timeout=timeout,
                                  max_retries=0)
    return make


async def run_attempt(case: Case, target: ModelTarget, client, *, base_settings: dict,
                      stream: bool, index: int, keep_prompts: bool) -> Attempt:
    loaded = load_case(case, base_settings)
    services = loaded.services
    attempt = Attempt(case.id, case.category, target.label, index, "error")
    try:
        runner = AgentRunner(services, RecordingTelegram(),
                             llm=TargetLLM(services, client, target.name), stream=stream)
        started = time.monotonic()
        outcome = await runner.run(RunRequest(chat=loaded.chat, trigger=loaded.trigger,
                                              bot=loaded.bot, skill=case.skill,
                                              images=loaded.images))
        attempt.total_ms = int((time.monotonic() - started) * 1000)
        run = services.runs.get(outcome.run_id)
        steps = run.steps or []
        model_steps = [step for step in steps if step.get("type") == "model"]
        attempt.prompt_tokens = run.prompt_tokens or 0
        attempt.prompt = run.prompt if keep_prompts else None
        attempt.steps = steps if keep_prompts else None
        attempt.model_requests = run.model_requests or 0
        attempt.tool_calls = [{"name": step["name"], "arguments": step.get("arguments") or {},
                               "error": bool(step.get("error"))}
                              for step in steps if step.get("type") == "tool"]
        attempt.reasoning = run.reasoning
        attempt.usage = run.usage
        attempt.finish_reason = run.finish_reason
        if model_steps:
            attempt.ttft_ms = model_steps[0].get("ttft_ms")
        if outcome.fallback or (run.status == "error" and not outcome.text):
            attempt.error = outcome.error or run.error or "No answer."
            return attempt
        attempt.text = outcome.text
        attempt.threaded = outcome.threaded
        attempt.checks = run_checks(case.expect, attempt.text, attempt.threaded)
        if "tool_calls" in case.expect:
            attempt.checks += check_tool_calls(case.expect["tool_calls"], attempt.tool_calls)
        if attempt.checks:
            attempt.status = "pass" if all(c.passed for c in attempt.checks) else "fail"
        else:
            attempt.status = "manual"
    except Exception as exc:
        attempt.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        services.db.close()
    return attempt


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
        load_case(case, base_settings).services.db.close()  # fail early on bad settings
        for target in targets:
            for index in range(1, repeat + 1):
                attempt = await run_attempt(case, target, clients[target.label],
                                            base_settings=base_settings, stream=stream,
                                            index=index, keep_prompts=keep_prompts)
                attempts.append(attempt)
                if progress:
                    progress(f"{case.id} · {target.label} #{index}: {attempt.status}")
    return attempts
