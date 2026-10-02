"""Turning a conversation into a scenario.

- From an attempt (a discovered failure kept as a regression case): the
  attempt is replayed up to the chosen turn with the model's recorded
  answers, which rebuilds the exact state before that turn (tools run again
  on the same data, so they do the same thing). That state becomes the new
  scenario's chat and state, and the chosen turn its turn.
- From an agent run in a real chat: the messages the run read are exact
  (unless the owner has deleted them since); notes, the digest, the board, reminders and
  history summaries are as they are now, not as they were then, and the
  scenario's provenance says so.
"""

import base64
from datetime import datetime
import json

from naruto import markers
from naruto.db.board import SECTION_KEYS
from naruto.db.plans import CONFIRMED, PROPOSED
from naruto.db.reminders import PENDING
from naruto.lab.scenario import MEDIA_KINDS, Scenario, parse_scenario
from naruto.lab.sandbox import CHAT_ID, Sandbox
from naruto.llm import ChatResult, LLMError, make_tool_call


class ReplayError(Exception):
    """The replay didn't follow the recorded attempt (the code changed, or
    the attempt didn't record what it needed)."""


class ReplayLLM:
    """Answers with an attempt's recorded model outputs, in order."""

    def __init__(self, steps: list[dict]):
        self.steps = list(steps)
        self.used = 0

    async def chat(self, messages, **kwargs) -> ChatResult:
        if self.used >= len(self.steps):
            raise ReplayError("The replay needed more model answers than the attempt recorded.")
        step = self.steps[self.used]
        self.used += 1
        if step.get("error"):
            raise LLMError(step["error"])
        calls = []
        for index, call in enumerate(step.get("tool_calls") or []):
            raw = (call.get("raw_arguments") if call.get("error")
                   else json.dumps(call.get("arguments") or {}))
            calls.append(make_tool_call(call.get("id"), call.get("name", ""), raw, index))
        return ChatResult(text=step.get("text") or "", reasoning=step.get("reasoning"),
                          model="replay", latency_ms=0, usage=None,
                          finish_reason=step.get("finish_reason") or "stop", tool_calls=calls)


def model_steps(turns: list[dict]) -> list[dict]:
    return [step for turn in turns for step in (turn.get("run") or {}).get("steps") or []
            if step.get("type") == "model"]


def _iso(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


_IMAGE_TYPES = ((b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                (b"GIF8", "image/gif"), (b"RIFF", "image/webp"))


def _data_uri(data: bytes) -> str:
    kind = next((mime for magic, mime in _IMAGE_TYPES if data.startswith(magic)), "image/png")
    return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"


def dump_chat(services, chat_id: int, messages, *, images: dict[int, bytes] | None = None,
              bot_id: int | None = None) -> dict:
    """A chat as a scenario's members, messages and state."""
    tz = services.timezone()
    images = images or {}
    ids = {m.message_id for m in messages}
    entries = []
    for message in messages:
        entry: dict = {"id": message.message_id, "date": message.date}
        if message.from_bot or (bot_id is not None and message.sender_id == bot_id):
            entry["bot"] = True
        else:
            entry["from"] = message.sender_name
            if message.sender_id is not None:
                entry["from_id"] = message.sender_id
            if message.sender_username:
                entry["username"] = message.sender_username
        text = message.text or ""
        kind = message.media_kind
        meta = message.media_meta or {}
        if kind in MEDIA_KINDS:
            entry["media"] = kind
            if kind == "sticker" and meta.get("emoji"):
                entry["emoji"] = meta["emoji"]
            if kind == "document" and meta.get("file_name"):
                entry["file_name"] = meta["file_name"]
            if kind == "poll":
                entry["poll"] = {"question": meta.get("question", ""),
                                 "options": meta.get("options") or ["?", "?"],
                                 "counts": meta.get("counts"),
                                 "anonymous": meta.get("anonymous", False)}
            if kind == "photo" and message.message_id in images:
                entry["image"] = _data_uri(images[message.message_id])
        elif kind:
            text = f"{markers.media_marker(kind, meta)} {text}".strip()
        if message.forwarded_from:
            text = f"[forwarded from {message.forwarded_from}] {text}".strip()
        entry["text"] = text
        if message.reply_to_message_id in ids:
            entry["reply_to"] = message.reply_to_message_id
        entries.append(entry)

    members = []
    for member in services.members.list(chat_id):
        if member.is_bot or member.user_id == bot_id:
            continue
        item = {"id": member.user_id, "name": member.display_name}
        if member.username:
            item["username"] = member.username
        if member.aliases:
            item["aliases"] = list(member.aliases)
        members.append(item)

    state: dict = {}
    digest = services.digests.get(chat_id)
    if digest and digest.text:
        state["digest"] = {"text": digest.text, "updated": _iso(digest.updated_at, tz)}
    notes = []
    for note in services.notes.for_chat(chat_id):
        item = {"text": note.content}
        if note.category:
            item["category"] = note.category
        person = services.people.get(note.person_id) if note.person_id else None
        if person is not None and person.accounts:
            item["about"] = person.accounts[0].user_id
        if note.locked:
            item["locked"] = True
        notes.append(item)
    if notes:
        state["notes"] = notes
    board = services.boards.get(chat_id)
    if not board.is_empty:
        state["board"] = {section: [item.as_dict() for item in board.items(section)]
                          for section in SECTION_KEYS if board.items(section)}
    reminders = [{"due": _iso(r.due_at, tz), "text": r.text}
                 for r in services.reminders.for_chat(chat_id, status=PENDING)]
    if reminders:
        state["reminders"] = reminders
    plans = [{"title": p.title, "items": p.items, "status": p.status}
             for p in reversed(services.plans.for_chat(chat_id, limit=20))
             if p.status in (PROPOSED, CONFIRMED)]
    if plans:
        state["plans"] = plans
    summaries = []
    for digest_row in services.history.for_chat(chat_id)[0]:
        summaries.append({
            "from": datetime.fromtimestamp(digest_row.period_start, tz).date().isoformat(),
            "to": datetime.fromtimestamp(digest_row.period_end - 1, tz).date().isoformat(),
            "text": digest_row.text, "grouping": digest_row.grouping,
            "messages": digest_row.message_count, "limitations": digest_row.limitations or []})
    if summaries:
        state["history_summaries"] = summaries
    return {"members": members, "messages": entries, "state": state}


def _turn_dict(turn, last_answer_id: int | None, expect: dict) -> dict:
    message = turn.message
    raw: dict = {"from": message.sender, "date": message.date}
    if message.sender_id is not None:
        raw["from_id"] = message.sender_id
    if message.username:
        raw["username"] = message.username
    if turn.command:
        raw["command"] = message.text
    else:
        raw["text"] = message.text
    if turn.reply_to_answer and last_answer_id is not None:
        raw["reply_to"] = last_answer_id
    elif message.reply_to is not None:
        raw["reply_to"] = message.reply_to
    if message.media:
        raw["media"] = message.media
    if message.has_image:
        raw["image"] = _data_uri(message.image_data())
    raw["expect"] = expect
    return raw


async def scenario_from_attempt(scenario: Scenario, turns: list[dict], settings: dict,
                                *, index: int, slug: str, expect: dict | None,
                                description: str, provenance: dict,
                                restore=None) -> dict:
    """The conversation of an attempt just before turn ``index`` (1-based),
    with that turn to answer."""
    if not 1 <= index <= len(scenario.turns):
        raise ReplayError(f"The scenario has turns 1 to {len(scenario.turns)}.")
    earlier = turns[:index - 1]
    replay = ReplayLLM(model_steps(earlier))
    sandbox = Sandbox(scenario, settings, llm_factory=lambda _: replay, restore=restore)
    try:
        await sandbox.setup()
        for turn, recorded in zip(scenario.turns[:index - 1], earlier):
            result = await sandbox.run_turn(turn)
            if result.error:
                raise ReplayError(f"Replaying turn {turn.index} failed: {result.error}")
            if (result.answer or "") != (recorded.get("answer") or ""):
                raise ReplayError(f"Replaying turn {turn.index} gave a different answer than "
                                  "the attempt recorded (has the code changed?).")
        target = scenario.turns[index - 1]
        for message in target.before:
            await sandbox._receive(message)
        await sandbox._deliver_reminders(until=target.date)
        services = sandbox.services
        stored = services.messages.latest(CHAT_ID, 5000)
        stored.sort(key=lambda m: (m.date, m.id))
        images = {m.id: m.image_data() for m in scenario.all_messages if m.has_image}
        dumped = dump_chat(services, CHAT_ID, stored, images=images, bot_id=services.status.bot.id)
        last_answer = sandbox._last_answer.message_id if sandbox._last_answer else None
        turn_expect = target.expect if expect is None else expect
        body = {
            "id": slug, "origin": scenario.origin, "category": scenario.category,
            "description": description or scenario.description,
            "timezone": scenario.timezone, "time": target.date,
            "chat": {"title": scenario.chat_title, "type": scenario.chat_type},
            "bot": {"name": scenario.bot_name, "username": scenario.bot_username},
            **dumped,
            "turns": [_turn_dict(target, last_answer, turn_expect)],
            "provenance": provenance,
        }
        for key, value in (("simulate", scenario.simulate), ("requires", scenario.requires),
                           ("rubric", scenario.rubric), ("settings", scenario.settings),
                           ("skill", scenario.skill)):
            if value:
                body[key] = value
        if scenario.generated_by:
            body["generated_by"] = scenario.generated_by
        parse_scenario(body, None)  # the result must itself be a valid scenario
        return body
    finally:
        sandbox.close()


def scenario_from_chat(services, run, *, slug: str, expect: dict | None,
                       description: str) -> dict:
    """A real chat around an agent run: the messages it read, and the chat's
    state as it is now."""
    if run.trigger_row_id is None:
        raise ReplayError("That run answered an ephemeral command (/catchup), which isn't "
                          "stored, so it can't become a scenario.")
    trigger = services.messages.get(run.trigger_row_id)
    if trigger is None:
        raise ReplayError("The run's messages are gone (deleted on the chat page).")
    chat = services.chats.get(run.chat_id)
    window = services.messages.before(run.chat_id, trigger, limit=max(run.window_size or 40, 1))
    bot = services.status.bot
    dumped = dump_chat(services, run.chat_id, window, bot_id=bot.id if bot else None)
    tz = services.timezone()
    raw_turn: dict = {"from": trigger.sender_name, "date": trigger.date}
    if trigger.sender_id is not None:
        raw_turn["from_id"] = trigger.sender_id
    if trigger.sender_username:
        raw_turn["username"] = trigger.sender_username
    text = trigger.text or ""
    if text.startswith("/") and text.split()[0].lstrip("/").split("@")[0] in (
            "summary", "plan", "questions", "remember", "remind"):
        raw_turn["command"] = text
    else:
        raw_turn["text"] = text
    ids = {m["id"] for m in dumped["messages"]}
    if trigger.reply_to_message_id in ids:
        raw_turn["reply_to"] = trigger.reply_to_message_id
    raw_turn["expect"] = expect or {}
    body = {
        "id": slug, "origin": "history", "category": "other",
        "description": description or f"From agent run {run.id} in a real chat.",
        "timezone": services.settings["general.timezone"] or "UTC",
        "time": trigger.date,
        "chat": {"title": chat.display_title if chat else "Group",
                 "type": chat.type if chat else "group"},
        "bot": {"name": bot.name if bot else "Naruto",
                "username": bot.username if bot else "naruto_bot"},
        **dumped,
        "turns": [raw_turn],
        "provenance": {
            "agent_run": run.id, "chat": run.chat_id,
            "exact": ["messages", "members"],
            "approximate": ["state: notes, digest, board, reminders, plans and history "
                            "summaries are as of when this scenario was made, "
                            f"{datetime.now(tz).isoformat(timespec='minutes')}, not as of "
                            "the original run"],
            "images": "real images aren't kept, so describe_image can't run",
        },
    }
    parse_scenario(body, None)
    return body
