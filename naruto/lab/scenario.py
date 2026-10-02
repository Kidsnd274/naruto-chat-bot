"""Scenarios: a group chat, one or more turns that address the bot, and
what a good answer does.

A scenario is JSON. The old evaluation case format (``messages``,
``trigger``, ``expect``) is a scenario with one turn, so existing case files
still load. Everything else is optional::

    {
      "id": "banter-teasing-1",
      "origin": "synthetic",
      "category": "character",
      "description": "Wei teases Naruto about losing at bowling.",
      "time": "2026-10-04T18:05:00+08:00",
      "timezone": "Asia/Singapore",
      "chat": {"title": "BBQ crew", "type": "group"},
      "members": [{"id": 7, "name": "Alice", "username": "alice"},
                  {"id": 9, "name": "Wei", "aliases": ["Always Late"]}],
      "state": {"digest": "...", "notes": [{"text": "Wei is always late", "about": 9}],
                "board": {"plans": ["BBQ Sat 6pm"]},
                "reminders": [{"due": "2026-10-04 17:00", "text": "bring the grill"}]},
      "messages": [{"from": "Wei", "from_id": 9, "text": "lol you bowled a 40"}],
      "turns": [
        {"from": "Wei", "from_id": 9, "text": "@naruto_bot admit it, I'm better",
         "expect": {"max_chars": 300, "judge": {"good": "Cheeky comeback, stays friendly."}}},
        {"after": "2m", "from": "Alice", "from_id": 7, "reply_to_answer": true,
         "text": "ok but seriously, rematch saturday?",
         "expect": {"tool_calls": []}}
      ]
    }

Message fields: ``id``, ``from``, ``from_id``, ``username``, ``text``,
``date`` (ISO 8601 or Unix seconds), ``reply_to`` (an earlier ``id``),
``media`` (photo, sticker, animation, video, voice, document, poll), ``emoji``
(stickers), ``file_name`` (documents), ``poll`` (``{"question": ...,
"options": [...]}``), ``image`` (a file next to the scenario file, or a
data: URI: the photo's content, for vision), ``bot`` (the bot's own
message).

A turn is a message that addresses the bot (a mention, or a reply to one of
its messages: ``reply_to`` a bot message or ``reply_to_answer``), or a
``command`` such as ``"/summary today"``. It may have ``after`` (``"90s"``,
``"5m"``, ``"2h"``, ``"1d"`` after the previous turn, or a date),
``messages`` (chat before it) and its own ``expect``.

Times: without dates, messages are a minute apart. The first turn is at
``time`` (or a minute after the last message), and the bot answers at the
time of the turn it answers: that is "now" in the prompt.

``skill`` forces a skill instead of routing the request like the bot does:
that makes the scenario a focused experiment, not an end-to-end test.

``"continues": true`` marks turns written to follow on from an earlier
attempt's conversation (run with continue_from): its first turn may reply to
the bot's last answer, and it brings no chat, members or state of its own
beyond new members and messages.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

CATEGORIES = ("focus", "summarize", "tool", "search", "character", "vision", "memory",
              "planning", "reminder", "other")
ORIGINS = ("synthetic", "owner", "history")
MEDIA_KINDS = ("photo", "sticker", "animation", "video", "voice", "document", "poll")
COMMANDS = ("summary", "plan", "questions", "remember", "remind", "catchup")
STATE_KEYS = ("digest", "notes", "board", "reminders", "plans", "history_summaries")
SIMULATED_METHODS = ("send_message", "send_poll", "pin_chat_message", "unpin_chat_message",
                     "sendRichMessage", "editMessageText", "edit_message_text", "get_file",
                     "delete_message")
SIMULATED_FAILURES = ("error", "rights_error", "forbidden", "network", "timeout")
EXPECT_KEYS = {
    # what the answer says
    "contains_any", "contains_all", "not_contains", "regex", "min_chars", "max_chars",
    # how it is delivered
    "answers", "no_reply", "reply_threaded",
    # what the bot does
    "tool_calls", "forbidden_tools", "skill", "max_model_requests", "state",
    # for judges (AI or the owner)
    "judge", "manual",
}
STATE_CHECK_KEYS = ("reminders", "board", "notes", "polls", "plans", "pins")
TURN_KEYS = {"id", "from", "from_id", "username", "text", "date", "after", "reply_to",
             "reply_to_answer", "image", "media", "emoji", "file_name", "poll", "command",
             "messages", "expect"}
SCENARIO_KEYS = {"id", "origin", "generated_by", "category", "description", "time", "timezone",
                 "chat", "bot", "members", "state", "messages", "trigger", "expect", "turns",
                 "simulate", "requires", "rubric", "settings", "skill", "provenance",
                 "continues"}
DEFAULT_START = 1_780_000_000  # used when nothing has a date
MINUTE = 60
_DURATION = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|hours?|d|days?)\s*$",
                       re.IGNORECASE)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ScenarioError(ValueError):
    """A scenario that can't run; the message says where and why."""


@dataclass
class ScenarioMessage:
    id: int
    sender: str
    text: str
    date: int
    sender_id: int | None = None
    username: str | None = None
    reply_to: int | None = None
    media: str | None = None
    emoji: str | None = None
    file_name: str | None = None
    poll: dict | None = None
    from_bot: bool = False
    image: Path | None = None
    image_bytes: bytes | None = None  # an inline image (a data: URI in the JSON)

    @property
    def has_image(self) -> bool:
        return self.image is not None or self.image_bytes is not None

    def image_data(self) -> bytes:
        return self.image_bytes if self.image_bytes is not None else self.image.read_bytes()


@dataclass
class Turn:
    index: int  # 1-based
    message: ScenarioMessage  # what the person sent (for a command, the command text)
    expect: dict
    before: list[ScenarioMessage] = field(default_factory=list)  # chat since the last turn
    command: str | None = None  # "summary", "plan", ... (without the slash)
    args: list[str] = field(default_factory=list)
    reply_to_answer: bool = False

    @property
    def date(self) -> int:
        return self.message.date


@dataclass
class Scenario:
    id: str
    category: str
    description: str
    origin: str
    chat_title: str
    chat_type: str
    timezone: str
    members: list[dict]
    messages: list[ScenarioMessage]
    turns: list[Turn]
    state: dict = field(default_factory=dict)
    simulate: dict = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    rubric: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    skill: str | None = None  # forced: a focused experiment
    bot_name: str = "Naruto"
    bot_username: str = "naruto_bot"
    generated_by: str | None = None
    provenance: dict = field(default_factory=dict)
    base_dir: Path | None = None
    continues: bool = False  # follows on from an earlier attempt (continue_from)

    @property
    def focused(self) -> bool:
        return self.skill is not None

    @property
    def all_messages(self) -> list[ScenarioMessage]:
        result = list(self.messages)
        for turn in self.turns:
            result += turn.before + [turn.message]
        return result

    @property
    def has_auto_checks(self) -> bool:
        judged = {"judge", "manual"}
        return any(set(turn.expect) - judged for turn in self.turns)


# ------------------------------------------------------------------ parsing

def _date(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    raise ScenarioError(f"{where}: invalid date {value!r} (use ISO 8601 or Unix seconds).")


def parse_duration(value: str, where: str) -> int:
    match = _DURATION.match(str(value))
    if not match:
        raise ScenarioError(f"{where}: 'after' must look like 90s, 5m, 2h or 1d, or be a date.")
    return int(match.group(1)) * _UNIT[match.group(2)[0].lower()]


class _Builder:
    """Assigns message IDs and dates in conversation order."""

    def __init__(self, scenario_id: str, base_dir: Path | None, bot_name: str,
                 first_id: int = 1):
        self.where = f"scenario {scenario_id}"
        self.base_dir = base_dir
        self.bot_name = bot_name
        self.ids: dict[int, ScenarioMessage] = {}
        self.next_id = first_id

    def message(self, raw: dict, where: str, date: int | None) -> ScenarioMessage:
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: must be an object.")
        from_bot = bool(raw.get("bot", False))
        sender = raw.get("from") or (self.bot_name if from_bot else None)
        if not sender:
            raise ScenarioError(f"{where}: 'from' is required.")
        text = raw.get("text", "")
        if not isinstance(text, str):
            raise ScenarioError(f"{where}: 'text' must be a string.")
        image = raw.get("image")
        media = raw.get("media") or ("photo" if image else None)
        if media is not None and media not in MEDIA_KINDS:
            raise ScenarioError(f"{where}: unknown media {media!r} ({', '.join(MEDIA_KINDS)}).")
        if image and media != "photo":
            raise ScenarioError(f"{where}: 'image' goes with media 'photo'.")
        poll = raw.get("poll")
        if media == "poll" and not (isinstance(poll, dict) and poll.get("question")
                                    and isinstance(poll.get("options"), list)
                                    and len(poll["options"]) >= 2):
            raise ScenarioError(f"{where}: a poll needs \"poll\": {{\"question\": ..., "
                                "\"options\": [at least two]}.")
        image_path = image_bytes = None
        if isinstance(image, str) and image.startswith("data:"):
            try:
                image_bytes = base64.b64decode(image.split(",", 1)[1], validate=True)
            except (IndexError, ValueError):
                raise ScenarioError(f"{where}: 'image' isn't a valid base64 data: URI.") from None
        elif image:
            if self.base_dir is None:
                raise ScenarioError(f"{where}: send the image inline, as a data: URI "
                                    "(data:image/png;base64,...).")
            image_path = self.base_dir / image
            if not image_path.is_file():
                raise ScenarioError(f"{where}: image {image} not found next to the scenario.")
        message_id = raw.get("id")
        if message_id is None:
            while self.next_id in self.ids:
                self.next_id += 1
            message_id = self.next_id
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            raise ScenarioError(f"{where}: 'id' must be a number.") from None
        if message_id in self.ids:
            raise ScenarioError(f"{self.where}: message ids must be unique ({message_id}).")
        reply_to = raw.get("reply_to")
        if reply_to is not None and reply_to not in self.ids:
            raise ScenarioError(f"{where}: reply_to {reply_to} is not an earlier message in "
                                "the scenario.")
        message = ScenarioMessage(
            id=message_id, sender=str(sender), text=text,
            date=_date(raw.get("date"), where) or date or DEFAULT_START,
            sender_id=raw.get("from_id"), username=raw.get("username"), reply_to=reply_to,
            media=media, emoji=raw.get("emoji"), file_name=raw.get("file_name"),
            poll=poll if media == "poll" else None,
            from_bot=from_bot, image=image_path, image_bytes=image_bytes)
        self.ids[message_id] = message
        self.next_id = max(self.next_id, message_id + 1)
        return message

    def dated_run(self, raws: list, where: str, start: int | None, end: int | None
                  ) -> list[ScenarioMessage]:
        """Messages a minute apart: forwards from ``start``, or backwards so
        the last is a minute before ``end``. Explicit dates win."""
        result = []
        previous = None
        for i, raw in enumerate(raws):
            if end is not None and start is None:
                default = end - (len(raws) - i) * MINUTE
            else:
                default = (previous + MINUTE) if previous is not None else start
            message = self.message(raw, f"{where} {i + 1}", default)
            previous = message.date
            result.append(message)
        return result


def _check_expect(expect: Any, where: str) -> dict:
    if expect is None:
        return {}
    if not isinstance(expect, dict):
        raise ScenarioError(f"{where}: 'expect' must be an object.")
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        raise ScenarioError(f"{where}: unknown expectations {sorted(unknown)} "
                            f"(known: {', '.join(sorted(EXPECT_KEYS))}).")
    state = expect.get("state")
    if state is not None:
        if not isinstance(state, dict) or set(state) - set(STATE_CHECK_KEYS):
            raise ScenarioError(f"{where}: expect.state takes {', '.join(STATE_CHECK_KEYS)}.")
    judge = expect.get("judge")
    if judge is not None and not (isinstance(judge, str) or (
            isinstance(judge, dict) and set(judge) <= {"good", "bad"})):
        raise ScenarioError(f"{where}: expect.judge is text, or {{\"good\": ..., \"bad\": ...}}.")
    return dict(expect)


def _addresses_bot(message: ScenarioMessage, bot_username: str,
                   ids: dict[int, ScenarioMessage], reply_to_answer: bool) -> bool:
    """Mirrors tg.responder.is_trigger: a mention, or a reply to the bot."""
    if f"@{bot_username.lower()}" in message.text.lower() or reply_to_answer:
        return True
    target = ids.get(message.reply_to) if message.reply_to is not None else None
    return target is not None and target.from_bot


def parse_scenario(raw: dict, base_dir: Path | None = None,
                   defaults: dict | None = None, *, first_id: int = 1) -> Scenario:
    """``first_id``: where automatic message IDs start (a scenario that
    continues an earlier conversation starts after its messages)."""
    if not isinstance(raw, dict):
        raise ScenarioError("A scenario must be a JSON object.")
    data = {**(defaults or {}), **raw}
    scenario_id = data.get("id")
    if not scenario_id or not isinstance(scenario_id, str):
        raise ScenarioError("Every scenario needs an 'id' (text).")
    where = f"scenario {scenario_id}"
    unknown = set(data) - SCENARIO_KEYS
    if unknown:
        raise ScenarioError(f"{where}: unknown fields {sorted(unknown)}.")
    category = data.get("category", "other")
    if category not in CATEGORIES:
        raise ScenarioError(f"{where}: unknown category {category!r} ({', '.join(CATEGORIES)}).")
    origin = data.get("origin", "synthetic")
    if origin not in ORIGINS:
        raise ScenarioError(f"{where}: origin must be one of {', '.join(ORIGINS)}.")
    bot = data.get("bot") or {}
    bot_name = bot.get("name", "Naruto")
    bot_username = bot.get("username", "naruto_bot")

    raw_messages = data.get("messages") or []
    if not isinstance(raw_messages, list):
        raise ScenarioError(f"{where}: 'messages' must be a list.")
    raw_turns = data.get("turns")
    if raw_turns is None:
        if "trigger" not in data:
            raise ScenarioError(f"{where}: 'turns' (or 'trigger') is required.")
        raw_turns = [{**data["trigger"], "expect": data.get("expect")}]
    elif "trigger" in data or "expect" in data:
        raise ScenarioError(f"{where}: use either 'turns' or 'trigger' with 'expect', not both.")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ScenarioError(f"{where}: 'turns' must be a non-empty list.")

    builder = _Builder(scenario_id, base_dir, bot_name, first_id)
    first = raw_turns[0] if isinstance(raw_turns[0], dict) else {}
    start_time = _date(data.get("time"), f"{where}, time")
    first_date = _date(first.get("date"), f"{where}, turn 1")
    anchor = first_date or start_time
    if anchor is not None:
        messages = builder.dated_run(raw_messages, f"{where}, message", None, anchor)
    else:
        messages = builder.dated_run(raw_messages, f"{where}, message", DEFAULT_START, None)

    turns = []
    previous = messages[-1].date if messages else None
    for index, raw_turn in enumerate(raw_turns, start=1):
        turn_where = f"{where}, turn {index}"
        if not isinstance(raw_turn, dict):
            raise ScenarioError(f"{turn_where}: must be an object.")
        extra = set(raw_turn) - TURN_KEYS - {"bot"}
        if extra:
            raise ScenarioError(f"{turn_where}: unknown fields {sorted(extra)}.")
        if raw_turn.get("bot"):
            raise ScenarioError(f"{turn_where}: a turn is someone talking to the bot; put the "
                                "bot's own lines in 'messages'.")
        if index == 1 and raw_turn.get("messages"):
            raise ScenarioError(f"{turn_where}: put the chat before the first turn in the "
                                "scenario's 'messages'.")
        between_raw = raw_turn.get("messages") or []
        if not isinstance(between_raw, list):
            raise ScenarioError(f"{turn_where}: 'messages' must be a list.")
        explicit = _date(raw_turn.get("date"), turn_where)
        after = raw_turn.get("after")
        if index == 1:
            when = explicit or start_time or ((previous + MINUTE) if previous else DEFAULT_START)
            if after is not None:
                raise ScenarioError(f"{turn_where}: 'after' is for later turns.")
            before = []
        else:
            if explicit is None and after is not None:
                after_date = _date(after, turn_where) if not _DURATION.match(str(after)) else None
                explicit = after_date or (turns[-1].date + parse_duration(after, turn_where))
            if explicit is not None:
                before = builder.dated_run(between_raw, f"{turn_where}, message", None, explicit)
                when = explicit
            else:
                before = builder.dated_run(between_raw, f"{turn_where}, message",
                                           turns[-1].date + MINUTE, None)
                when = (before[-1].date if before else turns[-1].date) + MINUTE
        if turns and when < turns[-1].date:
            raise ScenarioError(f"{turn_where}: is earlier than the turn before it.")

        command = raw_turn.get("command")
        args: list[str] = []
        fields = dict(raw_turn)
        if command is not None:
            words = str(command).strip().lstrip("/").split()
            if not words or words[0].split("@")[0].lower() not in COMMANDS:
                raise ScenarioError(f"{turn_where}: command must be one of "
                                    f"{', '.join('/' + c for c in COMMANDS)}.")
            command = words[0].split("@")[0].lower()
            args = words[1:]
            if raw_turn.get("text"):
                raise ScenarioError(f"{turn_where}: a command turn has no 'text'.")
            fields["text"] = "/" + " ".join([command, *args])
        if raw_turn.get("reply_to_answer") and index == 1 and not data.get("continues"):
            raise ScenarioError(f"{turn_where}: there is no earlier answer to reply to (unless "
                                "the scenario \"continues\" an earlier conversation).")
        message = builder.message(fields, turn_where, when)
        expect = _check_expect(raw_turn.get("expect"), turn_where)
        turn = Turn(index=index, message=message, expect=expect, before=before, command=command,
                    args=args, reply_to_answer=bool(raw_turn.get("reply_to_answer")))
        if (command is None and data.get("skill") is None and expect.get("answers", True)
                and not _addresses_bot(message, bot_username, builder.ids,
                                       turn.reply_to_answer)):
            raise ScenarioError(
                f"{turn_where}: isn't addressed to the bot, so it wouldn't answer. Mention "
                f"@{bot_username}, reply to one of its messages (reply_to, reply_to_answer) or "
                "use a command; or add \"answers\": false to test that it stays quiet.")
        turns.append(turn)

    skill = data.get("skill")
    if skill is not None:
        from naruto.agent.skills import SKILLS  # imported late: keeps this module light
        if skill not in SKILLS:
            raise ScenarioError(f"{where}: unknown skill {skill!r} ({', '.join(SKILLS)}).")
        if any(turn.command for turn in turns):
            raise ScenarioError(f"{where}: a forced skill and commands don't mix.")
    state = data.get("state") or {}
    if data.get("continues") and state:
        raise ScenarioError(f"{where}: a scenario that continues a conversation has that "
                            "conversation's state; it can't bring its own.")
    if not isinstance(state, dict) or set(state) - set(STATE_KEYS):
        raise ScenarioError(f"{where}: 'state' takes {', '.join(STATE_KEYS)}.")
    simulate = data.get("simulate") or {}
    if not isinstance(simulate, dict):
        raise ScenarioError(f"{where}: 'simulate' must be an object.")
    for method, failure in simulate.items():
        if method not in SIMULATED_METHODS:
            raise ScenarioError(f"{where}: simulate takes Telegram methods "
                                f"({', '.join(SIMULATED_METHODS)}), not {method!r}.")
        if failure not in SIMULATED_FAILURES:
            raise ScenarioError(f"{where}: simulated failures are "
                                f"{', '.join(SIMULATED_FAILURES)}.")
    for name in ("requires", "rubric"):
        value = data.get(name) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ScenarioError(f"{where}: '{name}' must be a list of names.")
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise ScenarioError(f"{where}: 'settings' must be an object.")
    chat = data.get("chat") or {}
    members = data.get("members") or []
    if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
        raise ScenarioError(f"{where}: 'members' must be a list of objects.")
    return Scenario(
        id=scenario_id, category=category, description=data.get("description", ""),
        origin=origin, chat_title=chat.get("title", "Test group"),
        chat_type=chat.get("type", "group"), timezone=data.get("timezone", "UTC"),
        members=list(members), messages=messages, turns=turns, state=state,
        simulate=simulate, requires=list(data.get("requires") or []),
        rubric=list(data.get("rubric") or []), settings=settings, skill=skill,
        bot_name=bot_name, bot_username=bot_username, generated_by=data.get("generated_by"),
        provenance=dict(data.get("provenance") or {}), base_dir=base_dir,
        continues=bool(data.get("continues", False)))


def load_scenarios(path: Path) -> list[Scenario]:
    """A JSON file with a list of scenarios, or ``{"defaults": {...},
    "scenarios": [...]}`` (``"cases"`` also works, as in evaluation files)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"Could not read {path}: {exc}") from None
    defaults = {}
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        data = data.get("scenarios", data.get("cases"))
    if not isinstance(data, list) or not data:
        raise ScenarioError(f"{path}: expected a non-empty list of scenarios.")
    scenarios = [parse_scenario(raw, Path(path).parent, defaults) for raw in data]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ScenarioError(f"{path}: scenario ids must be unique.")
    return scenarios
