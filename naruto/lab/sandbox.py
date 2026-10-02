"""Running a scenario the way the bot would, without touching anything real.

Each attempt gets a sandbox: a fresh in-memory database built from the
scenario, on the scenario's clock, with the configuration under test as its
settings. Messages go through the production recorder as real
python-telegram-bot messages; each turn is answered by the production
responder (images, the agent loop, tools, skill hand-overs, delivery), and
commands are routed by the same function as the Telegram handlers. Telegram
itself is a stand-in that records what the bot would do there and can be
told to fail. Model requests wait their turn in the bot's queue (see LabLLM).

What isn't simulated: digest upkeep and monthly history summaries (seed them
as state instead), progress messages, and how Telegram renders messages.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
import io
import itertools
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable

from telegram import (
    Animation,
    Chat as TgChat,
    Document,
    Message,
    MessageEntity,
    PhotoSize,
    Poll,
    PollOption,
    Sticker,
    Update,
    User,
    Video,
    Voice,
)
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

from naruto.agent.runner import BUSY_TEXT, DEADLINE_TEXT, FAILURE_TEXT, STUCK_TEXT
from naruto.agent.tools import default_registry
from naruto.bootstrap import Bootstrap
from naruto.db import open_database
from naruto.db.plans import CONFIRMED, PROPOSED
from naruto.db.reminders import PENDING
from naruto.lab.checks import (
    CheckResult,
    TurnVerdict,
    check_forbidden_tools,
    check_state,
    check_tool_calls,
    combine,
    run_checks,
    turn_outcome,
)
from naruto.lab.scenario import Scenario, ScenarioMessage, Turn, parse_duration
from naruto.llm import ChatResult, LLMClient
from naruto.model_queue import RequestInfo
from naruto.services import BotIdentity, Services
from naruto.settings.registry import SettingError
from naruto.tg.reminders import ReminderSender
from naruto.tg.recorder import Recorder
from naruto.tg.responder import Responder
from naruto.tg.skill_commands import catchup_trigger, command_request

logger = logging.getLogger(__name__)

CHAT_ID = -1_000_000
BOT_ID = 424242
ACTOR = "lab"
VISION_REFUSED = "refused the images"
STATE_META = "lab_sandbox"  # meta key of a saved sandbox's own state
FEATURES = ("vision",)  # besides tool names, what a scenario may require
# Canned answers that mean the model server, the queue or the deadline
# failed, not the model's judgment.
INFRA_TEXTS = (FAILURE_TEXT, BUSY_TEXT, DEADLINE_TEXT)

# What the sandbox doesn't do (capabilities and reports list them).
NOT_SIMULATED = [
    "Digest upkeep and monthly history summaries don't run in a sandbox: seed state.digest "
    "and state.history_summaries instead.",
    "Progress messages (\"Reading back through the chat…\") aren't shown.",
    "Pressing a plan's Confirm / Change buttons and voting in polls don't happen.",
    "How Telegram renders Markdown, HTML and rich messages isn't checked.",
    "/catchup's private (ephemeral) delivery is recorded, not sent.",
    "/board (show the board again) isn't a scenario command.",
    "Telegram rate limits and flaky networks only happen if a scenario simulates a failure.",
]
DEFERRED = [
    "Web search (planned in plans/WEB_SEARCH_PLAN.md) doesn't exist yet: scenarios that require "
    "\"web_search\" are skipped.",
    "Simulated streaming isn't built.",
    "Background tasks (digest updates, history summaries, import memory) aren't evaluated, though "
    "model settings a candidate changes apply to them too.",
]


# ------------------------------------------------------------------- model

class LabLLM:
    """The model client a sandbox uses: requests are built from the
    sandbox's settings (the configuration under test) and wait in the bot's
    queue at background priority, so replies to people go first."""

    def __init__(self, client: LLMClient, *, attempt_id: int | None = None,
                 still_wanted: Callable[[], bool] | None = None):
        self.client = client
        self.attempt_id = attempt_id
        self.still_wanted = still_wanted
        self.wait_ms = 0  # time spent waiting for the queue (and retries)

    async def chat(self, messages, *, reasoning=None, max_tokens=None, tools=None, stream=False,
                   background=False, info=None) -> ChatResult:
        started = time.monotonic()
        result = await self.client.chat(
            messages, reasoning=reasoning, max_tokens=max_tokens, tools=tools, stream=stream,
            background=True, info=RequestInfo(task="lab", lab_attempt_id=self.attempt_id,
                                              still_wanted=self.still_wanted))
        elapsed = int((time.monotonic() - started) * 1000)
        self.wait_ms += max(elapsed - result.latency_ms, 0)
        return result


# ---------------------------------------------------------------- telegram

def _failure(kind: str, method: str) -> TelegramError:
    match kind:
        case "rights_error":
            return BadRequest("Not enough rights to manage pinned messages in the chat"
                              if "pin" in method.lower() else
                              "Not enough rights to send messages to the chat")
        case "forbidden":
            return Forbidden("Forbidden: bot was kicked from the group chat")
        case "network":
            return NetworkError("Simulated network error")
        case "timeout":
            return TimedOut("Simulated time-out")
    return BadRequest(f"Simulated failure of {method}")


class SimulatedFile:
    def __init__(self, data: bytes):
        self.data = data
        self.file_size = len(data)

    async def download_to_memory(self, out: io.BufferedIOBase, **kwargs) -> None:
        out.write(self.data)


class SandboxTelegram:
    """Stands in for telegram.Bot. Sends come back as real Message objects
    (so the recorder stores them exactly as live ones), and every call is
    recorded as a simulated action. ``simulate`` makes methods fail."""

    def __init__(self, sandbox: "Sandbox", simulate: dict[str, str]):
        self.sandbox = sandbox
        self.simulate = dict(simulate)
        self.calls: list[dict] = []
        self.unsupported: list[str] = []
        self.pins: list[int] = []
        self._ids = itertools.count(sandbox.first_bot_message_id)

    @property
    def id(self) -> int:
        return BOT_ID

    def _record(self, method: str, **details) -> None:
        self.calls.append({"method": method, **details})

    def _maybe_fail(self, method: str, **details) -> None:
        kind = self.simulate.get(method)
        if kind:
            self._record(method, failed=kind, **details)
            raise _failure(kind, method)

    def _message(self, chat_id: int, **kwargs) -> Message:
        message = Message(message_id=next(self._ids), date=self.sandbox.now_datetime(),
                          chat=self.sandbox.tg_chat, from_user=self.sandbox.bot_user, **kwargs)
        self.sandbox.tg_messages[message.message_id] = message
        return message

    async def send_message(self, chat_id, text, parse_mode=None, reply_parameters=None,
                           reply_markup=None, api_kwargs=None, **kwargs):
        reply_to = getattr(reply_parameters, "message_id", None)
        details = {"text": text}
        if reply_to is not None:
            details["reply_to"] = reply_to
        if reply_markup is not None:
            details["buttons"] = [button.text for row in reply_markup.inline_keyboard
                                  for button in row]
        if api_kwargs and "ephemeral_message_parameters" in api_kwargs:
            details["ephemeral"] = True
        self._maybe_fail("send_message", **details)
        target = self.sandbox.tg_message(reply_to) if reply_to is not None else None
        # The transcript stores what the group sees: HTML (plans, the board)
        # without its tags.
        shown = unescape(re.sub(r"</?[a-z][^>]*>", "", text)) if parse_mode == "HTML" else text
        message = self._message(chat_id, text=shown, reply_to_message=target)
        self._record("send_message", message_id=message.message_id, **details)
        return message

    async def send_poll(self, chat_id, question, options, is_anonymous=True,
                        allows_multiple_answers=False, **kwargs):
        details = {"question": question, "options": list(options),
                   "anonymous": is_anonymous, "multiple": allows_multiple_answers}
        self._maybe_fail("send_poll", **details)
        message_id = next(self._ids)
        poll = Poll(id=f"lab-poll-{message_id}", question=question,
                    options=[PollOption(text, 0, persistent_id=f"o{i}")
                             for i, text in enumerate(options)], total_voter_count=0,
                    is_closed=False, is_anonymous=is_anonymous, type=Poll.REGULAR,
                    allows_multiple_answers=allows_multiple_answers, allows_revoting=True,
                    members_only=False)
        message = Message(message_id=message_id, date=self.sandbox.now_datetime(),
                          chat=self.sandbox.tg_chat, from_user=self.sandbox.bot_user, poll=poll)
        self.sandbox.tg_messages[message_id] = message
        self._record("send_poll", message_id=message_id, **details)
        return message

    async def do_api_request(self, endpoint, api_kwargs=None, **kwargs):
        api_kwargs = dict(api_kwargs or {})
        details = {k: v for k, v in api_kwargs.items() if k != "chat_id"}
        self._maybe_fail(endpoint, **details)
        if endpoint == "sendRichMessage":
            message_id = next(self._ids)
            self._record(endpoint, message_id=message_id, **details)
            return {"message_id": message_id, "date": int(self.sandbox.clock()),
                    "chat": {"id": api_kwargs.get("chat_id"), "type": "group"}}
        self._record(endpoint, **details)
        return True

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kwargs):
        self._maybe_fail("edit_message_text", message_id=message_id, text=text)
        self._record("edit_message_text", message_id=message_id, text=text)
        return Message(message_id=message_id or 0, date=self.sandbox.now_datetime(),
                       chat=self.sandbox.tg_chat, from_user=self.sandbox.bot_user, text=text)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=None, **kwargs):
        self._maybe_fail("pin_chat_message", message_id=message_id)
        self.pins.append(message_id)
        self._record("pin_chat_message", message_id=message_id)
        return True

    async def unpin_chat_message(self, chat_id, message_id=None, **kwargs):
        self._maybe_fail("unpin_chat_message", message_id=message_id)
        if message_id in self.pins:
            self.pins.remove(message_id)
        self._record("unpin_chat_message", message_id=message_id)
        return True

    async def get_file(self, file_id, **kwargs):
        self._maybe_fail("get_file", file_id=file_id)
        source = self.sandbox.images.get(file_id)
        if source is None:
            self.unsupported.append(
                "an image was needed that the scenario doesn't provide (add 'image' to the "
                "message)")
            raise BadRequest("Wrong file_id or the file is temporarily unavailable")
        self._record("get_file", file_id=file_id)
        return SimulatedFile(source.image_data())

    async def send_chat_action(self, chat_id, action, **kwargs):
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self._record("delete_message", message_id=message_id)
        return True


# ------------------------------------------------------------------ results

@dataclass
class TurnResult:
    index: int
    kind: str  # mention | command | focused | not_addressed
    trigger: dict
    skill: str | None = None  # the skill asked for (command or forced)
    final_skill: str | None = None  # after a hand-over
    note: str | None = None
    since: int | None = None
    run_id: int | None = None
    run: dict | None = None
    answer: str = ""
    threaded: bool = False
    fallback: bool = False
    sent: bool = False
    delivery: str = "none"  # group | ephemeral | usage | none
    actions: list[str] = field(default_factory=list)
    telegram: list[dict] = field(default_factory=list)
    state_changes: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    outcome: str = "unjudged"
    reason: str | None = None
    error: str | None = None
    duration_ms: int = 0
    model_requests: int = 0
    model_ms: int = 0
    usage: dict = field(default_factory=dict)
    models: list[str] = field(default_factory=list)

    @property
    def tool_calls(self) -> list[dict]:
        return [{"name": step["name"], "arguments": step.get("arguments") or {},
                 "error": bool(step.get("error")), "result": step.get("result", "")}
                for step in (self.run or {}).get("steps") or [] if step.get("type") == "tool"]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AttemptResult:
    scenario_id: str
    focused: bool
    turns: list[TurnResult]
    outcome: str
    reason: str | None = None
    final_state: dict = field(default_factory=dict)
    model_requests: int = 0
    model_ms: int = 0
    wait_ms: int = 0
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    final_clock: float = 0  # the conversation's time after the last turn
    max_message_id: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _run_dict(run) -> dict:
    return {"id": run.id, "skill": run.skill, "status": run.status, "model": run.model,
            "prompt": run.prompt, "prompt_tokens": run.prompt_tokens,
            "window_size": run.window_size, "dropped": run.dropped,
            "image_count": run.image_count, "steps": run.steps or [],
            "reasoning": run.reasoning, "response": run.response, "usage": run.usage,
            "latency_ms": run.latency_ms, "finish_reason": run.finish_reason,
            "error": run.error, "model_requests": run.model_requests,
            "tool_calls": run.tool_calls}


def _diff(before: list, after: list) -> dict:
    """Items added and removed, compared as JSON."""
    def key(item):
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    old = {key(i): i for i in before}
    new = {key(i): i for i in after}
    changes = {}
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    if added:
        changes["added"] = added
    if removed:
        changes["removed"] = removed
    return changes


# ------------------------------------------------------------------ sandbox

class Sandbox:
    def __init__(self, scenario: Scenario, settings: dict[str, Any], *,
                 llm_factory: Callable[[Any], Any], api_key: str = "lab",
                 restore: Path | None = None):
        """``settings``: every setting value of the configuration under test
        (a baseline snapshot plus a candidate's changes). ``llm_factory``
        gets the sandbox's SettingsService and returns the model client.
        ``restore``: continue from an earlier attempt's saved state (save());
        the scenario's turns then follow on from that conversation."""
        self.scenario = scenario
        first = scenario.turns[0].date if scenario.turns else None
        earliest = min((m.date for m in scenario.messages), default=first)
        self._now = float(earliest or first or 0)
        self.db = open_database(":memory:", clock=self.clock)
        saved: dict = {}
        self.restored = restore is not None
        if restore is not None:
            self.db.restore_from(restore)
            saved = json.loads(self.db.get_meta(STATE_META) or "{}")
            self._now = float(saved.get("clock", self._now))
        bootstrap = Bootstrap(telegram_bot_token="0:lab", openai_api_key=api_key,
                              admin_password="", owner_user_id=None,
                              database_path=":memory:", web_host="127.0.0.1", web_port=0)
        self.services = Services.create(bootstrap, self.db)
        self._apply_settings(settings)
        self.services.llm = llm_factory(self.services.settings)
        self.bot = BotIdentity(id=BOT_ID, username=scenario.bot_username, name=scenario.bot_name)
        self.services.status.bot = self.bot
        self.bot_user = User(id=BOT_ID, first_name=scenario.bot_name, is_bot=True,
                             username=scenario.bot_username)
        self.tg_chat = TgChat(id=CHAT_ID, type=scenario.chat_type, title=scenario.chat_title)
        self.tg_messages: dict[int, Message] = {}
        self.images = {f"lab-photo-{m.id}": m for m in scenario.all_messages if m.has_image}
        top = max([m.id for m in scenario.all_messages] + [saved.get("max_message_id", 0)])
        self.first_bot_message_id = max(100_000, top + 10_000,
                                        saved.get("next_bot_message_id", 0))
        self.telegram = SandboxTelegram(self, scenario.simulate)
        self.telegram.pins = list(saved.get("pins", []))
        self.services.telegram = self.telegram
        self.recorder = Recorder(self.services)
        self.responder = Responder(self.services, self.recorder)
        self._user_ids: dict[str, int] = dict(saved.get("user_ids", {}))
        self._usernames = {int(k): v for k, v in saved.get("usernames", {}).items()}
        self._usernames.update({int(m["id"]): m["username"] for m in scenario.members
                                if m.get("id") is not None and m.get("username")})
        self._updates = itertools.count(1)
        self._last_answer: Message | None = None
        if saved.get("last_answer"):
            self._last_answer = self.tg_message(int(saved["last_answer"]))

    # ------------------------------------------------------------- clock

    def clock(self) -> float:
        return self._now

    def now_datetime(self) -> datetime:
        return datetime.fromtimestamp(self._now, timezone.utc)

    def _set_time(self, when: float) -> None:
        self._now = max(self._now, float(when))

    # ------------------------------------------------------------- setup

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        values = {**settings, **self.scenario.settings,
                  "general.timezone": self.scenario.timezone}
        registry = self.services.settings.registry
        for key, value in values.items():
            if key not in registry:
                continue
            try:
                self.services.settings.set(key, value, actor=ACTOR)
            except SettingError as exc:
                raise SettingError(f"setting {key}: {exc}") from None

    async def setup(self) -> None:
        """The chat as it is before the first turn. A restored sandbox
        already has its chat and state: only new members and messages."""
        services = self.services
        if not self.restored:
            services.chats.upsert_seen(CHAT_ID, title=self.scenario.chat_title,
                                       chat_type=self.scenario.chat_type)
            services.chats.set_status(CHAT_ID, "enabled")
        for member in self.scenario.members:
            user_id = member.get("id")
            if user_id is None:
                continue
            self._user_ids.setdefault(member.get("name", ""), int(user_id))
            services.members.upsert_live(CHAT_ID, int(user_id),
                                         member.get("name", f"User {user_id}"),
                                         member.get("username"))
            for alias in member.get("aliases", []):
                services.members.add_alias(CHAT_ID, int(user_id), alias)
        for message in self.scenario.messages:
            await self._receive(message)
        if not self.restored:
            self._seed_state()

    @property
    def chat(self):
        return self.services.chats.get(CHAT_ID)

    def _sender_id(self, message: ScenarioMessage) -> int:
        if message.from_bot:
            return BOT_ID
        if message.sender_id is not None:
            return int(message.sender_id)
        return self._user_ids.setdefault(message.sender, 10_000 + len(self._user_ids))

    def tg_message(self, message_id: int) -> Message | None:
        """A message the bot could reply to, rebuilt from storage if needed."""
        if message_id in self.tg_messages:
            return self.tg_messages[message_id]
        stored = self.services.messages.get_live(CHAT_ID, message_id)
        if stored is None:
            return None
        user = (self.bot_user if stored.from_bot else
                User(id=stored.sender_id or 0, first_name=stored.sender_name, is_bot=False,
                     username=stored.sender_username))
        message = Message(message_id=message_id,
                          date=datetime.fromtimestamp(stored.date, timezone.utc),
                          chat=self.tg_chat, from_user=user, text=stored.text)
        self.tg_messages[message_id] = message
        return message

    def _media(self, message: ScenarioMessage) -> dict:
        file_id = f"lab-{message.media}-{message.id}"
        unique = f"u-{file_id}"
        match message.media:
            case "photo":
                sizes = [PhotoSize(f"lab-photo-{message.id}", unique, 1280, 960)]
                for size in sizes:
                    size.set_bot(self.telegram)
                return {"photo": sizes}
            case "sticker":
                return {"sticker": Sticker(file_id, unique, 512, 512, False, False,
                                           Sticker.REGULAR, emoji=message.emoji)}
            case "animation":
                return {"animation": Animation(file_id, unique, 320, 240, 3)}
            case "video":
                return {"video": Video(file_id, unique, 640, 480, 10)}
            case "voice":
                return {"voice": Voice(file_id, unique, 5)}
            case "document":
                return {"document": Document(file_id, unique,
                                             file_name=message.file_name or "file")}
            case "poll":
                poll = message.poll or {}
                return {"poll": Poll(
                    id=f"lab-poll-{message.id}", question=poll.get("question", ""),
                    options=[PollOption(str(text), int(count), persistent_id=f"o{i}")
                             for i, (text, count) in enumerate(zip(
                                 poll.get("options", []),
                                 poll.get("counts") or [0] * len(poll.get("options", []))))],
                    total_voter_count=sum(poll.get("counts") or []), is_closed=False,
                    is_anonymous=bool(poll.get("anonymous", False)), type=Poll.REGULAR,
                    allows_multiple_answers=bool(poll.get("multiple", False)),
                    allows_revoting=True, members_only=False)}
        return {}

    def _tg(self, message: ScenarioMessage, *, reply_to: Message | None = None,
            command: bool = False) -> Message:
        user_id = self._sender_id(message)
        # Telegram sends the sender's @username with every message; scenarios
        # give it once, in the member list.
        username = message.username or self._usernames.get(user_id)
        user = (self.bot_user if message.from_bot else
                User(id=user_id, first_name=message.sender, is_bot=False, username=username))
        if reply_to is None and message.reply_to is not None:
            reply_to = self.tg_message(message.reply_to)
        entities = None
        if command and message.text:
            entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0,
                                      len(message.text.split()[0]))]
        media = self._media(message) if message.media else {}
        text_field = "caption" if media and message.text else "text"
        text_value = {text_field: message.text} if message.text else {}
        tg = Message(message_id=message.id,
                     date=datetime.fromtimestamp(message.date, timezone.utc),
                     chat=self.tg_chat, from_user=user, reply_to_message=reply_to,
                     entities=entities if text_field == "text" else None,
                     **text_value, **media)
        tg.set_bot(self.telegram)
        self.tg_messages[message.id] = tg
        return tg

    async def _receive(self, message: ScenarioMessage, **kwargs) -> Message:
        """A message arrives in the group (or the bot's own earlier line)."""
        await self._deliver_reminders(until=message.date)
        self._set_time(message.date)
        tg = self._tg(message, **kwargs)
        if message.from_bot:
            self.recorder.record_sent(CHAT_ID, tg)
        else:
            await self.recorder.on_message(Update(update_id=next(self._updates), message=tg),
                                           None)
        return tg

    def _seed_state(self) -> None:
        services = self.services
        state = self.scenario.state
        tz = services.timezone()
        digest = state.get("digest")
        if digest:
            text = digest if isinstance(digest, str) else digest.get("text", "")
            services.digests.save(CHAT_ID, text, actor="scenario")
            updated = None if isinstance(digest, str) else digest.get("updated")
            if updated:
                self.db.execute("UPDATE digests SET updated_at = ? WHERE chat_id = ?",
                                (_when(updated, tz), CHAT_ID))
        for note in state.get("notes") or []:
            if isinstance(note, str):
                note = {"text": note}
            about = note.get("about")
            person_id = None
            if about is not None:
                user_id = about if isinstance(about, int) else self._user_ids.get(str(about))
                person_id = services.people.person_id_for(user_id) if user_id else None
            created = services.notes.add(CHAT_ID, note["text"], category=note.get("category"),
                                         person_id=person_id, created_by="scenario",
                                         actor="scenario")
            if note.get("locked"):
                services.notes.set_locked(created.id, True, actor="scenario")
        board = state.get("board") or {}
        if board:
            sections = {section: [item if isinstance(item, dict) else {"text": item}
                                  for item in items] for section, items in board.items()}
            services.boards.set_sections(CHAT_ID, sections, actor="scenario")
        for reminder in state.get("reminders") or []:
            by = reminder.get("by")
            services.reminders.create(CHAT_ID, reminder["text"], _when(reminder["due"], tz),
                                      created_by="scenario",
                                      created_by_user_id=by if isinstance(by, int) else None)
        for plan in state.get("plans") or []:
            created = services.plans.create(CHAT_ID, plan["title"], list(plan.get("items") or []),
                                            run_id=None, proposed_for_user_id=None)
            if plan.get("status") == CONFIRMED:
                services.plans.decide(created.id, CONFIRMED, user_id=None, name="scenario")
        for summary in state.get("history_summaries") or []:
            start = _when(summary["from"], tz)
            end = _when(summary["to"], tz) + 86400  # the end day is included
            services.history.add(
                chat_id=CHAT_ID, status="active", source="export",
                grouping=summary.get("grouping", "month"), timezone=self.scenario.timezone,
                period_start=start, period_end=end, first_message_at=start,
                last_message_at=end - 1, message_count=int(summary.get("messages", 0)),
                import_id=None, period_id=None, fingerprint="scenario", text=summary["text"],
                limitations=list(summary.get("limitations") or []), actor="scenario")

    # ----------------------------------------------------------- reminders

    async def _deliver_reminders(self, until: float) -> None:
        """Send reminders that come due before ``until``, each at its due
        time, as the reminder job would."""
        sender = ReminderSender(self.services, self.recorder)
        while True:
            due = [r for r in self.services.reminders.for_chat(CHAT_ID, status=PENDING)
                   if r.due_at <= until]
            if not due:
                return
            self._set_time(min(r.due_at for r in due))
            await sender.send_due(self.telegram)

    # -------------------------------------------------------------- state

    def state(self) -> dict:
        services = self.services
        board = services.boards.get(CHAT_ID)
        return {
            "board": {section: [{"text": item.text, "done": item.done}
                                for item in board.items(section)]
                      for section in ("plans", "decided", "questions")},
            "reminders": [{"id": r.id, "text": r.text, "due_at": r.due_at}
                          for r in services.reminders.for_chat(CHAT_ID, status=PENDING)],
            "notes": [{"id": n.id, "text": n.content, "category": n.category,
                       "person_id": n.person_id}
                      for n in services.notes.for_chat(CHAT_ID)],
            "plans": [{"id": p.id, "title": p.title, "items": p.items, "status": p.status}
                      for p in services.plans.for_chat(CHAT_ID, limit=50)],
            "polls": [{"question": m.media_meta.get("question", ""),
                       "options": m.media_meta.get("options", [])}
                      for m in services.messages.latest(CHAT_ID, 500)
                      if m.media_kind == "poll" and m.from_bot],
            "pins": list(self.telegram.pins),
            "digest": (services.digests.get(CHAT_ID).text
                       if services.digests.get(CHAT_ID) else None),
        }

    # -------------------------------------------------------------- turns

    async def run_turn(self, turn: Turn) -> TurnResult:
        scenario = self.scenario
        for message in turn.before:
            await self._receive(message)
        await self._deliver_reminders(until=turn.date)
        self._set_time(turn.date)
        before_state = self.state()
        calls_before = len(self.telegram.calls)
        unsupported_before = len(self.telegram.unsupported)
        started = time.monotonic()
        reply_to = self._last_answer if turn.reply_to_answer else None
        message = turn.message
        result = TurnResult(index=turn.index, kind="mention",
                            trigger={"id": message.id, "from": message.sender,
                                     "text": message.text, "date": message.date})
        chat = self.chat
        if turn.command == "catchup":
            tg = self._tg(message, reply_to=reply_to, command=True)  # ephemeral: not stored
        else:
            tg = await self._receive(message, reply_to=reply_to,
                                     command=turn.command is not None)
        try:
            if turn.command is not None:
                outcome = await self._command(turn, tg, result)
            elif scenario.skill is not None:
                result.kind, result.skill = "focused", scenario.skill
                outcome = await self._answer(tg, result, skill=scenario.skill)
            elif not turn.expect.get("answers", True) and not _addressed(tg, self.bot):
                result.kind = "not_addressed"
                outcome = None
            else:
                result.skill = "banter"
                outcome = await self._answer(tg, result, skill="banter")
        except Exception as exc:
            logger.exception("Lab turn %s failed", turn.index)
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            outcome = None
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.telegram = self.telegram.calls[calls_before:]
        after_state = self.state()
        for key in ("board", "reminders", "notes", "plans", "polls", "pins"):
            before, after = before_state[key], after_state[key]
            if key == "board":
                changes = {section: _diff(before[section], after[section])
                           for section in after if _diff(before[section], after[section])}
            else:
                changes = _diff(before, after)
            if changes:
                result.state_changes[key] = changes
        unsupported = self.telegram.unsupported[unsupported_before:]
        verdict = self._verdict(turn, result, after_state, unsupported)
        result.checks = [check.as_dict() for check in verdict.checks]
        result.outcome, result.reason = verdict.outcome, verdict.reason
        return result

    async def _command(self, turn: Turn, tg: Message, result: TurnResult):
        services = self.services
        result.kind = "command"
        reply = tg.reply_to_message
        replied_to_date = None
        if turn.command == "summary" and reply is not None:
            stored = services.messages.get_live(CHAT_ID, reply.message_id)
            replied_to_date = stored.date if stored else int(reply.date.timestamp())
        last_spoke = None
        if turn.command == "catchup":
            person = services.people.for_user(tg.from_user.id)
            user_ids = [a.user_id for a in person.accounts] if person else [tg.from_user.id]
            last = services.messages.search(CHAT_ID, None, sender_ids=user_ids, limit=1)
            last_spoke = last[0].date if last else None
        request = command_request(turn.command, turn.args, tz=services.timezone(),
                                  now=services.time(), replied_to_date=replied_to_date,
                                  has_reply=reply is not None, last_spoke_at=last_spoke)
        if request.usage:
            await self.telegram.send_message(CHAT_ID, request.usage)
            result.answer, result.sent, result.delivery = request.usage, True, "usage"
            return None
        result.skill, result.note, result.since = request.skill, request.note, request.since
        if turn.command == "catchup":
            trigger = catchup_trigger(chat_id=CHAT_ID, origin_chat_id=CHAT_ID,
                                      message_id=tg.message_id, sender_id=tg.from_user.id,
                                      sender_name=tg.from_user.full_name,
                                      sender_username=tg.from_user.username,
                                      now=int(services.time()))
            return await self._answer(tg, result, skill=request.skill, trigger=trigger,
                                      since=request.since, note=request.note, ephemeral=True)
        return await self._answer(tg, result, skill=request.skill, since=request.since,
                                  note=request.note, force_reply=True)

    async def _answer(self, tg: Message, result: TurnResult, *, skill: str, trigger=None,
                      since=None, note=None, force_reply=False, ephemeral=False):
        services = self.services
        chat = self.chat
        if trigger is None:
            trigger = services.messages.get_live(CHAT_ID, tg.message_id)
            if trigger is None:
                raise RuntimeError("The turn's message wasn't recorded.")
        outcome = await self.responder.run(self.telegram, chat, tg, trigger, self.bot,
                                           skill=skill, since=since, note=note,
                                           force_reply=force_reply, show_typing=False)
        result.run_id = outcome.run_id
        result.answer, result.threaded = outcome.text, outcome.threaded
        result.fallback, result.actions = outcome.fallback, list(outcome.actions)
        if outcome.text:
            if ephemeral:
                result.sent, result.delivery = True, "ephemeral"
            else:
                sent = await self.responder.deliver(self.telegram, chat, tg, outcome)
                result.sent = bool(sent)
                result.delivery = "group" if sent else "none"
                if sent:
                    self._last_answer = sent[0]
        run = services.runs.get(outcome.run_id)
        if run is not None:
            result.run = _run_dict(run)
            result.final_skill = run.skill
            result.model_requests = run.model_requests or 0
            result.usage = run.usage or {}
            model_steps = [s for s in run.steps or [] if s.get("type") == "model"]
            result.model_ms = sum(s.get("latency_ms") or 0 for s in model_steps)
            result.models = sorted({run.model} - {None}) if run.model else []
        return outcome

    def _verdict(self, turn: Turn, result: TurnResult, state: dict,
                 unsupported: list[str]) -> TurnVerdict:
        expect = turn.expect
        if result.error:
            return turn_outcome([], error=result.error)
        if result.fallback and result.answer in INFRA_TEXTS:
            run_error = (result.run or {}).get("error") or result.answer
            return turn_outcome([], error=run_error)
        steps = (result.run or {}).get("steps") or []
        vision_refused = any(VISION_REFUSED in (s.get("purpose") or "") for s in steps)
        if vision_refused and ("vision" in self.scenario.requires or turn.message.image):
            return turn_outcome([], skipped="the model server doesn't accept images")
        if result.run and (result.run.get("error") or "").startswith("Not sent to the model"):
            return turn_outcome([], error=result.run["error"])
        text = result.answer if not result.fallback else ""
        checks: list[CheckResult] = []
        if result.fallback and result.answer == STUCK_TEXT:
            checks.append(CheckResult("answered", False, (result.run or {}).get("error")
                                      or "the model gave no usable answer"))
        if "answers" in expect:
            checks.append(CheckResult("answers", result.sent == bool(expect["answers"]),
                                      "sent an answer" if result.sent else "sent nothing"))
        if "no_reply" in expect:
            nothing = not result.sent
            checks.append(CheckResult("no_reply", nothing == bool(expect["no_reply"]),
                                      "sent nothing" if nothing else "sent an answer"))
        checks += run_checks(expect, text, result.threaded)
        calls = result.tool_calls
        if "tool_calls" in expect:
            checks += check_tool_calls(expect["tool_calls"], calls)
        if "forbidden_tools" in expect:
            checks.append(check_forbidden_tools(expect["forbidden_tools"], calls))
        if "skill" in expect:
            checks.append(CheckResult("skill", result.final_skill == expect["skill"],
                                      f"used {result.final_skill}"))
        if "max_model_requests" in expect:
            checks.append(CheckResult("max_model_requests",
                                      result.model_requests <= int(expect["max_model_requests"]),
                                      f"{result.model_requests} requests"))
        if "state" in expect:
            checks += check_state(expect["state"], state, self.services.timezone())
        if unsupported and any(not c.passed for c in checks):
            return turn_outcome(checks, skipped="; ".join(dict.fromkeys(unsupported)))
        return turn_outcome(checks)

    # ------------------------------------------------------------ attempt

    def missing_features(self) -> list[str]:
        """What the scenario requires that doesn't exist in this version."""
        tools = set(default_registry().tools)
        return [name for name in self.scenario.requires
                if name not in tools and name not in FEATURES]

    async def run(self) -> AttemptResult:
        started = time.monotonic()
        missing = self.missing_features()
        if missing:
            return AttemptResult(scenario_id=self.scenario.id, focused=self.scenario.focused,
                                 turns=[], outcome="skipped",
                                 reason=f"needs {', '.join(missing)}, which this version of "
                                        "the bot doesn't have")
        await self.setup()
        turns = []
        for turn in self.scenario.turns:
            result = await self.run_turn(turn)
            turns.append(result)
            if result.outcome == "error":
                break  # the rest of the conversation depends on this answer
        usage: dict = {}
        for turn in turns:
            for key, value in (turn.usage or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
        outcomes = [turn.outcome for turn in turns]
        if len(turns) < len(self.scenario.turns):
            outcomes.append("error")
        reasons = [t.reason or t.error for t in turns if t.reason or t.error]
        return AttemptResult(
            scenario_id=self.scenario.id, focused=self.scenario.focused, turns=turns,
            outcome=combine(outcomes), reason="; ".join(dict.fromkeys(reasons)) or None,
            final_state=self.state(), model_requests=sum(t.model_requests for t in turns),
            model_ms=sum(t.model_ms for t in turns),
            wait_ms=getattr(self.services.llm, "wait_ms", 0),
            duration_ms=int((time.monotonic() - started) * 1000), usage=usage,
            models=sorted({m for t in turns for m in t.models}), final_clock=self._now,
            max_message_id=self.max_message_id())

    def max_message_id(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(message_id), 0) FROM messages "
                                  "WHERE chat_id = ?", (CHAT_ID,)) or 0)

    def save(self, path: Path) -> None:
        """Keep the state after the last turn, so another attempt can
        continue the conversation (``restore``)."""
        self.db.set_meta(STATE_META, json.dumps({
            "clock": self._now, "pins": self.telegram.pins, "user_ids": self._user_ids,
            "usernames": {str(k): v for k, v in self._usernames.items()},
            "last_answer": self._last_answer.message_id if self._last_answer else None,
            "max_message_id": self.max_message_id(),
            "next_bot_message_id": next(self.telegram._ids),
        }))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db.backup_to(path)

    def close(self) -> None:
        self.db.close()


def _addressed(tg: Message, bot: BotIdentity) -> bool:
    from naruto.tg.responder import is_trigger
    return is_trigger(tg, bot)


def _when(value, tz) -> int:
    """A date for seeded state: ISO 8601, 'YYYY-MM-DD HH:MM' or a day
    (local time), or Unix seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid date {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return int(parsed.timestamp())


async def run_scenario(scenario: Scenario, settings: dict[str, Any], *,
                       llm_factory: Callable[[Any], Any], api_key: str = "lab") -> AttemptResult:
    """One attempt at a scenario under one configuration."""
    sandbox = Sandbox(scenario, settings, llm_factory=llm_factory, api_key=api_key)
    try:
        return await sandbox.run()
    finally:
        sandbox.close()


__all__ = ["AttemptResult", "LabLLM", "Sandbox", "SandboxTelegram", "TurnResult",
           "run_scenario", "parse_duration"]
