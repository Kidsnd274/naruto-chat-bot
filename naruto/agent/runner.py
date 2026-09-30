"""The bounded agent loop: model -> tool calls -> model ... -> final answer.

Limits come from the settings (agent.*): model requests per run, tool calls
per run and a deadline that includes waiting for the model server. The last
allowed request is reserved for the answer: its tool results say that no
more tools are available. Every model request and tool call is traced in
the run's ``steps``.
"""

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
from typing import Callable

from naruto.agent.claims import check_note, missing_actions
from naruto.agent.context import ContextBuilder, ImageInput
from naruto.agent.skills import Skill, get_skill
from naruto.agent.text import (
    clean_model_output,
    estimate_text_tokens,
    has_images,
    strip_bot_mention,
    strip_internal_json,
    without_image_data,
    without_images,
)
from naruto.agent.tools import RunState, ToolContext, ToolRegistry, default_registry
from naruto.agent.tools.base import timed
from naruto.db.chats import Chat
from naruto.db.messages import StoredMessage
from naruto.llm import ChatResult, LLMError
from naruto.services import BotIdentity, Services

logger = logging.getLogger(__name__)

FAILURE_TEXT = "Sorry, I couldn't get a response right now. Please try again later."
DEADLINE_TEXT = "Sorry, that took too long. Please try again."
STUCK_TEXT = "Sorry, I got stuck on that one. Could you ask again, a bit more specifically?"
LAST_CALL_NOTE = ("[No more tool calls are available in this response. Write your answer "
                  "now with what you have.]")
IMAGES_REFUSED_NOTE = "[The image can't be shown: the model server doesn't accept images.]"
TRACE_TEXT_CHARS = 20_000
# How OpenAI-compatible servers word a refused image (Halogen without a
# vision tower, llama.cpp without an mmproj, text-only models).
_IMAGES_REFUSED = re.compile(r"image|vision|multimodal|mmproj", re.IGNORECASE)


@dataclass
class RunRequest:
    chat: Chat
    trigger: StoredMessage
    bot: BotIdentity
    skill: str = "banter"
    images: list[ImageInput] = field(default_factory=list)
    trigger_message_id: int | None = None  # the Telegram message ID
    since: int | None = None  # read every message since then instead of the recent window
    note: str | None = None  # added to the current request, e.g. what a command asked for
    force_reply: bool = False  # always thread the answer to the trigger (commands)
    on_skill: Callable[[str], None] | None = None  # told the skill at the start and on hand-over


@dataclass
class RunOutcome:
    run_id: int
    status: str  # ok | empty | error
    text: str = ""  # what to send ("" sends nothing)
    threaded: bool = False
    fallback: bool = False  # text is a canned failure message, not the model's
    error: str | None = None
    actions: list[str] = field(default_factory=list)


class AgentRunner:
    def __init__(self, services: Services, telegram, *, record_sent=None,
                 registry: ToolRegistry | None = None, llm=None, stream: bool = False):
        """``llm`` defaults to services.llm; the evaluation passes its own
        client for the model under test."""
        self.services = services
        self.telegram = telegram
        self.record_sent = record_sent
        self.registry = registry or default_registry()
        self.llm = llm
        self.stream = stream

    async def run(self, request: RunRequest) -> RunOutcome:
        services = self.services
        settings = services.settings
        run_id = services.runs.start(
            chat_id=request.chat.chat_id, skill=request.skill,
            trigger_row_id=request.trigger.id or None,  # 0: an ephemeral command, not stored
            trigger_message_id=request.trigger_message_id, user_id=request.trigger.sender_id)
        state = RunState(run_id=run_id, max_model_requests=settings["agent.max_model_requests"])
        try:
            async with asyncio.timeout(settings["agent.deadline_seconds"]):
                return await self._loop(request, state)
        except TimeoutError:
            logger.warning("Run %s hit the %s s deadline", run_id,
                           settings["agent.deadline_seconds"])
            return self._finish(state, status="error", text=DEADLINE_TEXT, fallback=True,
                                error="Deadline reached")
        except Exception as exc:
            self._finish(state, status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            raise

    # ----------------------------------------------------------------- loop

    def _prepare(self, request: RunRequest, skill: Skill, since: int | None,
                 state: RunState, builder: ContextBuilder) -> tuple:
        """The first request for ``skill``: prompt, tools and tool context."""
        if request.on_skill is not None:
            request.on_skill(skill.name)
        services = self.services
        settings = services.settings.for_chat(request.chat.chat_id)
        allowed = list(skill.tools) if settings["agent.max_tool_calls"] else []
        tools = self.registry.schemas(allowed)
        reserved = estimate_text_tokens(json.dumps(tools)) if tools else 0
        prompt = builder.build(request.chat, request.trigger, bot=request.bot,
                               images=request.images, skill=skill.name, since=since,
                               note=request.note, reserved_tokens=reserved)
        services.runs.update(state.run_id, skill=skill.name,
                             prompt=without_image_data(prompt.messages),
                             prompt_tokens=prompt.estimated_tokens + reserved,
                             window_size=prompt.window_size, dropped=prompt.dropped,
                             image_count=prompt.image_count)
        if prompt.dropped:
            logger.warning("Dropped %s old messages to fit the input budget (%s tokens).",
                           prompt.dropped, settings["context.input_token_budget"])
        context = ToolContext(services=services, telegram=self.telegram, chat=request.chat,
                              trigger=request.trigger, bot=request.bot, builder=builder,
                              state=state, skill=skill.name, window_ids=prompt.window_ids,
                              record_sent=self.record_sent)
        return prompt, list(prompt.messages), allowed, tools, context

    async def _loop(self, request: RunRequest, state: RunState) -> RunOutcome:
        services = self.services
        settings = services.settings.for_chat(request.chat.chat_id)
        skill = get_skill(request.skill)
        since = request.since
        builder = ContextBuilder(services)
        prompt, messages, allowed, tools, context = self._prepare(request, skill, since, state,
                                                                  builder)
        max_tool_calls = settings["agent.max_tool_calls"]
        max_chars = settings["agent.tool_result_chars"]
        reasoning = settings[f"skills.{skill.name}.reasoning"]
        totals = {"latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0}
        result: ChatResult | None = None
        switched = False
        checked = False

        while True:
            state.model_requests += 1
            try:
                result = await self._request(messages, tools, reasoning, state)
            except LLMError as exc:
                logger.error("Model request failed: %s", exc)
                state.steps.append({"type": "model", "request": state.model_requests,
                                    "error": str(exc)})
                return self._finish(state, status="error", text=FAILURE_TEXT, fallback=True,
                                    error=str(exc), result=result, totals=totals)
            self._add_totals(totals, result)
            state.steps.append(self._model_step(state.model_requests, result))
            self._save_progress(state)

            calls = result.tool_calls
            last_request = state.model_requests >= state.max_model_requests
            if not calls and not checked and state.model_requests + 2 <= state.max_model_requests:
                # One more try when the answer skipped the tool the request
                # needs ("Reminder set!" without set_reminder). Two requests
                # must be left: one for the call, one for the answer.
                checked = True
                missing = missing_actions(self._request_text(request), result.text or "",
                                          allowed, self._done_tools(state))
                if missing:
                    note = check_note(missing)
                    logger.info("The answer skipped a needed tool; asking again (%s).",
                                ", ".join(check.tools[0] for check in missing))
                    state.steps.append({"type": "check", "note": note})
                    messages.append({"role": "assistant", "content": result.text or ""})
                    messages.append({"role": "user", "content": note})
                    continue
            if not calls or last_request:
                if calls:
                    logger.info("Ignoring %s tool calls on the last allowed request.", len(calls))
                break

            messages.append({"role": "assistant", "content": result.text or "",
                             "tool_calls": [call.as_request_part() for call in calls]})
            switching = any(call.name == "use_skill" for call in calls) and not switched
            for call in calls:
                elapsed = timed()
                if switching and call.name != "use_skill":
                    content, failed = "Not run: handing over to another mode.", True
                elif state.tool_calls >= max_tool_calls:
                    content, failed = ("Not run: the tool call limit for this response is "
                                       "reached. Answer with what you have."), True
                else:
                    state.tool_calls += 1
                    content, failed = await self.registry.execute(call, context, allowed,
                                                                  max_chars)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                state.steps.append({"type": "tool", "id": call.id, "name": call.name,
                                    "arguments": call.arguments, "result": content,
                                    "error": failed, "duration_ms": elapsed()})
                self._save_progress(state)
            if state.switch_to_skill and not switched:
                switched = True
                skill = get_skill(state.switch_to_skill)
                since = state.switch_since or since
                reasoning = settings[f"skills.{skill.name}.reasoning"]
                state.steps.append({"type": "switch", "skill": skill.name})
                logger.info("Handing over to the %s skill", skill.name)
                builder = ContextBuilder(services)
                prompt, messages, allowed, tools, context = self._prepare(
                    request, skill, since, state, builder)
                continue
            if (state.model_requests + 1 >= state.max_model_requests
                    or state.tool_calls >= max_tool_calls):
                messages[-1]["content"] += f"\n\n{LAST_CALL_NOTE}"

        answer, removed = strip_internal_json(result.text)
        if removed is not None and not answer and not result.tool_calls \
                and state.model_requests < state.max_model_requests:
            # The model answered in a background request's JSON format
            # instead of chatting. Ask again with the identical request.
            logger.warning("The answer was internal JSON instead of a reply; asking again.")
            state.model_requests += 1
            try:
                result = await self._request(messages, tools, reasoning, state)
            except LLMError as exc:
                logger.error("Model request failed: %s", exc)
                return self._finish(state, status="error", text=FAILURE_TEXT, fallback=True,
                                    error=str(exc), result=result, totals=totals)
            self._add_totals(totals, result)
            step = self._model_step(state.model_requests, result)
            step["purpose"] = "asked again: the previous answer was internal JSON"
            state.steps.append(step)
            self._save_progress(state)
            answer, removed = strip_internal_json(result.text)
        if removed is not None:
            logger.warning("Dropped internal JSON from the answer: %s", removed[:200])
            if not answer:
                return self._finish(state, status="error", text=STUCK_TEXT, fallback=True,
                                    error="The model answered with internal JSON instead of a "
                                          "reply; it was not posted.",
                                    result=result, totals=totals)
        should_reply, text = clean_model_output(answer, request.bot.name)
        if not text:
            if result.tool_calls or (state.tool_calls and not state.actions):
                # Still wanted tools when it had to answer.
                return self._finish(state, status="error", text=STUCK_TEXT, fallback=True,
                                    error="No answer after the last allowed request",
                                    result=result, totals=totals)
            logger.warning("The model returned an empty answer (finish reason %s).",
                           result.finish_reason)
            return self._finish(state, status="ok" if state.actions else "empty",
                                result=result, totals=totals)
        logger.info("Answered in %s ms (%s estimated prompt tokens, %s recent messages, "
                    "%s model requests, %s tool calls).", totals["latency_ms"],
                    prompt.estimated_tokens, prompt.window_size, state.model_requests,
                    state.tool_calls)
        if skill.name == "summarize" and services.keeper is not None:
            services.keeper.request_update(request.chat.chat_id)  # plan: digest on /summary
        return self._finish(state, status="ok", text=text,
                            threaded=should_reply or request.force_reply,
                            result=result, totals=totals)

    async def _request(self, messages: list[dict], tools: list[dict], reasoning,
                       state: RunState) -> ChatResult:
        llm = self.llm or self.services.llm
        try:
            return await llm.chat(messages, reasoning=reasoning, tools=tools or None,
                                  stream=self.stream)
        except LLMError as exc:
            if not (has_images(messages) and _IMAGES_REFUSED.search(str(exc))):
                raise
            # A text-only model: answer without the images rather than not at all.
            logger.warning("The model server refused images (%s); asking without them.", exc)
            state.steps.append({"type": "model", "request": state.model_requests,
                                "error": str(exc)[:500],
                                "purpose": "refused the images; asked again without them"})
            messages[:] = without_images(messages, IMAGES_REFUSED_NOTE)
            return await llm.chat(messages, reasoning=reasoning, tools=tools or None,
                                  stream=self.stream)

    @staticmethod
    def _request_text(request: RunRequest) -> str:
        """What the current request asks for, for the action check."""
        text = strip_bot_mention(request.trigger.text or "", request.bot.username)
        return f"{text}\n{request.note}" if request.note else text

    @staticmethod
    def _done_tools(state: RunState) -> set[str]:
        return {step["name"] for step in state.steps
                if step.get("type") == "tool" and not step.get("error")}

    # --------------------------------------------------------------- traces

    @staticmethod
    def _model_step(number: int, result: ChatResult) -> dict:
        step = {"type": "model", "request": number, "latency_ms": result.latency_ms,
                "finish_reason": result.finish_reason,
                "text": (result.text or "")[:TRACE_TEXT_CHARS],
                "tool_calls": [call.summary() for call in result.tool_calls]}
        if result.ttft_ms is not None:
            step["ttft_ms"] = result.ttft_ms
        if result.reasoning:
            step["reasoning"] = result.reasoning[:TRACE_TEXT_CHARS]
        if result.usage:
            step["usage"] = {k: v for k, v in result.usage.items()
                             if isinstance(v, (int, float))}
        return step

    @staticmethod
    def _add_totals(totals: dict, result: ChatResult) -> None:
        totals["latency_ms"] += result.latency_ms
        for key in ("prompt_tokens", "completion_tokens"):
            value = (result.usage or {}).get(key)
            if isinstance(value, int):
                totals[key] += value

    def _save_progress(self, state: RunState) -> None:
        self.services.runs.update(state.run_id, steps=state.steps,
                                  model_requests=state.model_requests,
                                  tool_calls=state.tool_calls)

    def _finish(self, state: RunState, *, status: str, text: str = "", threaded: bool = False,
                fallback: bool = False, error: str | None = None,
                result: ChatResult | None = None, totals: dict | None = None) -> RunOutcome:
        fields: dict = dict(status=status, steps=state.steps, model_requests=state.model_requests,
                            tool_calls=state.tool_calls, error=error)
        if result is not None:
            reasonings = [step["reasoning"] for step in state.steps
                          if step.get("type") == "model" and step.get("reasoning")]
            fields.update(model=result.model, finish_reason=result.finish_reason,
                          reasoning="\n\n---\n\n".join(reasonings) or None,
                          response=None if fallback else (text or None))
        if totals is not None:
            usage = {k: v for k, v in totals.items() if k != "latency_ms" and v}
            fields.update(latency_ms=totals["latency_ms"], usage=usage or None)
        self.services.runs.update(state.run_id, **fields)
        return RunOutcome(run_id=state.run_id, status=status, text=text, threaded=threaded,
                          fallback=fallback, error=error, actions=list(state.actions))
