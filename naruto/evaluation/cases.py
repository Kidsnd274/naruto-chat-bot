"""Evaluation cases: a small chat, a triggering message and expectations.

Cases live in a JSON file outside the repository (they come from real
chats). The file is either a list of cases or ``{"defaults": {...},
"cases": [...]}``; defaults are merged into every case.

Minimal case::

    {
      "id": "direct-question-busy-chat",
      "category": "focus",
      "messages": [{"from": "Alice", "text": "when is the bbq?"}],
      "trigger": {"from": "Bob", "text": "@naruto_bot what should I bring?"},
      "expect": {"not_contains": ["ramen"], "manual": "Answers Bob, not the BBQ timing."}
    }

Message fields: ``from`` (name), ``from_id``, ``username``, ``text``,
``date`` (ISO 8601 or Unix seconds; defaults to one minute apart),
``id`` (defaults to the position), ``reply_to`` (an earlier ``id``),
``media`` (``photo``, ``sticker``, ...), ``bot`` (true for the bot's own
messages). The trigger also takes ``image`` (a file path, relative to the
cases file) for vision cases.

``expect.tool_calls`` lists tools the bot must call, e.g.
``[{"name": "create_poll", "arguments": {"options": "saturday"}}]`` (argument
values must appear in the call); ``[]`` means it must not call any tool.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

CATEGORIES = ("focus", "summarize", "tool", "search", "character", "vision", "other")
EXPECT_KEYS = {"contains_any", "contains_all", "not_contains", "regex", "min_chars",
               "max_chars", "reply_threaded", "manual", "tool_calls"}
DEFAULT_START = 1_780_000_000  # used when messages have no dates


class CaseError(ValueError):
    pass


@dataclass
class CaseMessage:
    id: int
    sender: str
    text: str
    date: int
    sender_id: int | None = None
    username: str | None = None
    reply_to: int | None = None
    media: str | None = None
    from_bot: bool = False
    image: Path | None = None


@dataclass
class Case:
    id: str
    category: str
    description: str
    chat_title: str
    chat_type: str
    timezone: str
    skill: str
    members: list[dict]
    messages: list[CaseMessage]
    trigger: CaseMessage
    expect: dict
    settings: dict = field(default_factory=dict)
    bot_name: str = "Naruto"
    bot_username: str = "naruto_bot"

    @property
    def has_auto_checks(self) -> bool:
        return any(key in self.expect for key in EXPECT_KEYS - {"manual"})


def _date(value: Any, fallback: int, where: str) -> int:
    if value is None:
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    raise CaseError(f"{where}: invalid date {value!r} (use ISO 8601 or Unix seconds).")


def _message(raw: dict, index: int, fallback_date: int, base_dir: Path, where: str) -> CaseMessage:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: must be an object.")
    sender = raw.get("from") or ("Naruto" if raw.get("bot") else None)
    if not sender:
        raise CaseError(f"{where}: 'from' is required.")
    text = raw.get("text", "")
    if not isinstance(text, str):
        raise CaseError(f"{where}: 'text' must be a string.")
    image = raw.get("image")
    return CaseMessage(
        id=int(raw.get("id", index)),
        sender=str(sender),
        text=text,
        date=_date(raw.get("date"), fallback_date, where),
        sender_id=raw.get("from_id"),
        username=raw.get("username"),
        reply_to=raw.get("reply_to"),
        media=raw.get("media") or ("photo" if image else None),
        from_bot=bool(raw.get("bot", False)),
        image=(base_dir / image) if image else None,
    )


def parse_case(raw: dict, base_dir: Path, defaults: dict | None = None) -> Case:
    data = {**(defaults or {}), **raw}
    case_id = data.get("id")
    if not case_id:
        raise CaseError("Every case needs an 'id'.")
    where = f"case {case_id}"
    category = data.get("category", "other")
    if category not in CATEGORIES:
        raise CaseError(f"{where}: unknown category {category!r} ({', '.join(CATEGORIES)}).")
    raw_messages = data.get("messages") or []
    if not isinstance(raw_messages, list):
        raise CaseError(f"{where}: 'messages' must be a list.")
    if "trigger" not in data:
        raise CaseError(f"{where}: 'trigger' is required.")
    expect = data.get("expect") or {}
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        raise CaseError(f"{where}: unknown expectations {sorted(unknown)}.")

    count = len(raw_messages)
    messages = [
        _message(item, i + 1, DEFAULT_START + i * 60, base_dir, f"{where}, message {i + 1}")
        for i, item in enumerate(raw_messages)
    ]
    trigger = _message(data["trigger"], count + 1, DEFAULT_START + count * 60, base_dir,
                       f"{where}, trigger")
    ids = [m.id for m in messages] + [trigger.id]
    if len(ids) != len(set(ids)):
        raise CaseError(f"{where}: message ids must be unique.")
    for message in messages + [trigger]:
        if message.reply_to is not None and message.reply_to not in ids:
            raise CaseError(f"{where}: reply_to {message.reply_to} is not a message in the case.")
    chat = data.get("chat") or {}
    bot = data.get("bot") or {}
    return Case(
        id=str(case_id),
        category=category,
        description=data.get("description", ""),
        chat_title=chat.get("title", "Test group"),
        chat_type=chat.get("type", "group"),
        timezone=data.get("timezone", "UTC"),
        skill=data.get("skill", "banter"),
        members=list(data.get("members") or []),
        messages=messages,
        trigger=trigger,
        expect=expect,
        settings=dict(data.get("settings") or {}),
        bot_name=bot.get("name", "Naruto"),
        bot_username=bot.get("username", "naruto_bot"),
    )


def load_cases(path: Path) -> list[Case]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"Could not read {path}: {exc}") from None
    defaults = {}
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        data = data.get("cases")
    if not isinstance(data, list) or not data:
        raise CaseError(f"{path}: expected a non-empty list of cases.")
    cases = [parse_case(raw, Path(path).parent, defaults) for raw in data]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise CaseError(f"{path}: case ids must be unique.")
    return cases
