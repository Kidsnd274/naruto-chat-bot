"""Phase 4: group memory, the digest, skills and commands, reminders,
image descriptions, retention and import distillation."""

import asyncio
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

import fakes
from fakes import ALICE, BOB, GROUP_ID, OWNER, FakeBot, ScriptedLLM, context, message, tool_call, update
from naruto import jobs
from naruto.agent.runner import AgentRunner, RunRequest
from naruto.agent.tools.reminders import parse_when
from naruto.db.memory import NoteLocked
from naruto.db.messages import IMPORT, LIVE, NewMessage
from naruto.importer.service import ImportService
from naruto.memory.keeper import MemoryKeeper
from naruto.memory.notes import MemoryOutputError, apply_note_actions, parse_json_object
from naruto.tg.access import ChatAccess
from naruto.tg.board import BoardPublisher
from naruto.tg.recorder import Recorder
from naruto.tg.reminders import ReminderSender
from naruto.tg.responder import Responder
from naruto.tg.skill_commands import SkillCommands, summary_scope

FIXTURE = Path(__file__).parent / "fixtures" / "export_basic_group.json"


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def chat(services):
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    services.chats.set_status(GROUP_ID, "enabled")
    services.members.upsert_live(GROUP_ID, 7, "Alice", "alice")
    services.members.upsert_live(GROUP_ID, 8, "Bob", None)
    return services.chats.get(GROUP_ID)


@pytest.fixture
def wired(services, bot, chat):
    recorder = Recorder(services)
    services.access = ChatAccess(services, bot)
    services.telegram = bot
    responder = Responder(services, recorder)
    return SimpleNamespace(recorder=recorder, responder=responder,
                           skills=SkillCommands(services, responder, BoardPublisher(services)))


def store(services, message_id, text, *, sender=(7, "Alice"), offset=0, source=LIVE, **kw):
    user_id, name = sender
    record = NewMessage(chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source=source,
                        message_id=message_id, sender_id=user_id, sender_name=name,
                        date=fakes.T0 + offset, text=text,
                        import_id=1 if source == IMPORT else None, **kw)
    if source == IMPORT:
        services.messages.insert_imported([record])
        return services.messages.get(services.db.scalar(
            "SELECT id FROM messages WHERE source = 'import' AND message_id = ?", (message_id,)))
    return services.messages.insert_live(record)


def person(services, user_id):
    return services.people.person_id_for(user_id)


def results_of(llm, request=1):
    return [m["content"] for m in llm.calls[request]["messages"] if m["role"] == "tool"]


# ------------------------------------------------------------------ notes

def test_notes_keep_history_and_respect_locks(services, chat):
    notes = services.notes
    note = notes.add(GROUP_ID, "  Sam is   vegetarian ", category="preference",
                     person_id=person(services, 7), created_by="member", actor="t1")
    assert note.content == "Sam is vegetarian" and note.category == "preference"
    notes.update(note.id, content="Sam is vegan", actor="t2")
    notes.set_locked(note.id, True, actor="owner")
    with pytest.raises(NoteLocked):
        notes.update(note.id, content="x", actor="bot")
    with pytest.raises(NoteLocked):
        notes.delete(note.id, actor="bot")
    notes.update(note.id, content="Sam is vegan (since 2025)", actor="owner", force=True)
    assert [c.action for c in notes.history(note.id)] == ["updated", "locked", "updated",
                                                          "created"]
    assert notes.add(GROUP_ID, "x", category="nonsense", created_by="bot",
                     actor="t").category == "group_fact"
    assert notes.for_chat(GROUP_ID, query="vegan")[0].id == note.id
    assert notes.delete_for_chat(GROUP_ID) == 2 and notes.history(note.id) == []


@pytest.mark.parametrize("text", [
    '{"digest": "x", "notes": []}',
    'Sure!\n```json\n{"digest": "x"}\n```',
    'Here you go: {"digest": "x", "notes": [{"a": 1}]} hope it helps',
])
def test_parse_json_object(text):
    assert parse_json_object(text)["digest"] == "x"


def test_parse_json_object_rejects_other_answers():
    with pytest.raises(MemoryOutputError):
        parse_json_object("no json here")
    with pytest.raises(MemoryOutputError):
        parse_json_object("[1, 2]")


def test_apply_note_actions(services, chat):
    locked = services.notes.add(GROUP_ID, "Wei is always late", created_by="owner", actor="o")
    services.notes.set_locked(locked.id, True, actor="o")
    other_chat = services.notes.add(-999, "elsewhere", created_by="owner", actor="o")
    counts = apply_note_actions(services, GROUP_ID, [
        {"action": "add", "content": "Alice is vegetarian", "category": "preference",
         "about": "alice", "sources": [5, "6", 999]},
        {"action": "add", "content": "alice is VEGETARIAN"},  # duplicate
        {"action": "update", "id": locked.id, "content": "Wei is on time now"},
        {"action": "update", "id": f"n{other_chat.id}", "content": "nope"},
        {"action": "delete", "id": locked.id},
        "garbage",
    ], created_by="bot", actor="digest", bot_id=42, known_row_ids={5, 6})
    assert counts == {"added": 1, "updated": 0, "skipped": 5}
    added = services.notes.for_chat(GROUP_ID, category="preference")[0]
    assert added.person_id == person(services, 7) and added.source_row_ids == [5, 6]
    assert services.notes.get(locked.id).content == "Wei is always late"

    services.settings.set("memory.max_notes_per_chat", 10, actor="t")
    for i in range(10):
        services.notes.add(GROUP_ID, f"fact {i}", created_by="bot", actor="t")
    assert apply_note_actions(services, GROUP_ID, [{"content": "one more"}], created_by="bot",
                              actor="t", bot_id=42)["skipped"] == 1


# ----------------------------------------------------------------- keeper

def digest_answer(text="- BBQ Sat 6pm at East Coast", notes=()):
    return json.dumps({"digest": text, "notes": list(notes)})


async def test_digest_update_reads_new_messages_and_adds_notes(services, chat):
    rows = [store(services, i, f"message {i}", offset=i) for i in range(1, 4)]
    rows.append(store(services, 4, "I'm vegetarian btw", sender=(8, "Bob"), offset=4))
    services.llm = ScriptedLLM(digest_answer(notes=[
        {"action": "add", "content": "Bob is vegetarian", "category": "preference",
         "about": "Bob", "sources": [rows[3].id]}]))
    keeper = MemoryKeeper(services)
    text = await keeper.update(chat)
    assert text == "- BBQ Sat 6pm at East Coast"
    call = services.llm.calls[0]
    assert call["background"] is True and call["tools"] is None
    assert "Naruto" in call["messages"][0]["content"]
    assert "{bot_name}" not in call["messages"][0]["content"]
    assert "I'm vegetarian btw" in call["messages"][1]["content"]
    digest = services.digests.get(GROUP_ID)
    assert digest.last_row_id == rows[-1].id
    note = services.notes.for_chat(GROUP_ID)[0]
    assert note.person_id == person(services, 8) and note.created_by == "bot"
    run = services.runs.recent()[0][0]
    assert run.skill == "digest" and run.status == "ok" and run.window_size == 4

    assert services.digests.unread_count(GROUP_ID, digest) == (0, None)
    store(services, 5, "new", offset=5)
    services.llm = ScriptedLLM(digest_answer("- updated"))
    await keeper.update(chat)
    prompt = services.llm.calls[0]["messages"][1]["content"]
    assert "## Current digest\n- BBQ Sat 6pm at East Coast" in prompt
    assert "[n1] Bob: Bob is vegetarian (preference)" in prompt
    assert "message 1" not in prompt and "new" in prompt


async def test_digest_failures_are_recorded_and_backed_off(services, chat):
    store(services, 1, "hello")
    services.llm = ScriptedLLM("I can't do JSON today")
    keeper = MemoryKeeper(services)
    assert await keeper.update(chat) is None
    digest = services.digests.get(GROUP_ID)
    assert digest.error == "The answer was not a JSON object." and digest.failed_at
    assert services.runs.recent()[0][0].status == "error"
    services.settings.set("memory.digest_every_messages", 5, actor="t")
    for i in range(2, 8):
        store(services, i, f"m{i}", offset=i)
    assert keeper.due_chats() == []  # backing off after the failure
    keeper.request_update(GROUP_ID)
    assert [c.chat_id for c in keeper.due_chats()] == [GROUP_ID]


async def test_digest_schedule(services, chat):
    keeper = MemoryKeeper(services)
    services.settings.set("memory.digest_every_messages", 20, actor="t")
    services.settings.set("memory.digest_quiet_minutes", 30, actor="t")
    for i in range(12):
        store(services, i + 1, f"m{i}", offset=i)
    newest = fakes.T0 + 11
    assert keeper.due_chats(now=newest + 60) == []  # 12 messages, not quiet yet
    assert [c.chat_id for c in keeper.due_chats(now=newest + 31 * 60)] == [GROUP_ID]
    for i in range(12, 20):
        store(services, i + 1, f"m{i}", offset=i)
    assert [c.chat_id for c in keeper.due_chats(now=fakes.T0 + 20)] == [GROUP_ID]


async def test_a_busy_chat_can_update_its_digest_less_often(services, chat):
    keeper = MemoryKeeper(services)
    services.settings.set("memory.digest_quiet_minutes", 0, actor="t")
    services.settings.set_for_chat(GROUP_ID, "memory.digest_every_messages", 200, actor="t")
    for i in range(60):
        store(services, i + 1, f"m{i}", offset=i)
    assert keeper.due_chats(now=fakes.T0 + 100) == []  # 60 is the global threshold
    services.settings.reset_for_chat(GROUP_ID, "memory.digest_every_messages", actor="t")
    assert [c.chat_id for c in keeper.due_chats(now=fakes.T0 + 100)] == [GROUP_ID]


async def test_automatic_notes_can_be_turned_off(services, chat):
    store(services, 1, "I love hiking")
    kept = services.notes.add(GROUP_ID, "Alice likes swimming", created_by="owner", actor="o")
    services.settings.set("memory.auto_notes", False, actor="t")
    services.llm = ScriptedLLM(digest_answer(notes=[
        {"content": "Alice loves hiking"},
        {"action": "update", "id": f"n{kept.id}", "content": "Alice likes hiking"}]))
    await MemoryKeeper(services).update(chat)
    assert "Automatic notes are turned off" in services.llm.calls[0]["messages"][0]["content"]
    # Neither added nor corrected.
    assert [n.content for n in services.notes.for_chat(GROUP_ID)] == ["Alice likes swimming"]


async def test_an_owner_edit_during_a_digest_update_wins(services, chat):
    store(services, 1, "BBQ on Saturday?")
    services.digests.save(GROUP_ID, "- Owner's version", actor="owner")
    edited = asyncio.Event()

    class EditingLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            # The owner corrects the digest while the model is busy.
            services.digests.save(GROUP_ID, "- Owner's correction", actor="owner")
            edited.set()
            return await super().chat(messages, **kwargs)

    services.llm = EditingLLM(digest_answer("- Stale model version"))
    keeper = MemoryKeeper(services)
    assert await keeper.update(chat) is None and edited.is_set()
    digest = services.digests.get(GROUP_ID)
    assert digest.text == "- Owner's correction"
    assert digest.last_row_id is None  # the batch is still unread...
    assert [c.chat_id for c in keeper.due_chats()] == [GROUP_ID]  # ...and read again soon
    run = services.runs.recent()[0][0]
    assert run.status == "error" and "edited or deleted" in run.error

    services.llm = ScriptedLLM(digest_answer("- Saturday BBQ (owner's correction kept)"))
    assert await keeper.update(chat) == "- Saturday BBQ (owner's correction kept)"
    assert "## Current digest\n- Owner's correction" in services.llm.calls[0]["messages"][1]["content"]


async def test_a_digest_deleted_during_an_update_stays_deleted(services, chat):
    store(services, 1, "hello")

    class DeletingLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            services.digests.save(GROUP_ID, "- Owner wrote one meanwhile", actor="owner")
            return await super().chat(messages, **kwargs)

    services.llm = DeletingLLM(digest_answer("- From the model"))
    assert await MemoryKeeper(services).update(chat) is None  # no digest when it started
    assert services.digests.get(GROUP_ID).text == "- Owner wrote one meanwhile"

    services.digests.save(GROUP_ID, "- Base", actor="owner")
    store(services, 2, "more", offset=2)

    class ClearingLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            services.digests.clear(GROUP_ID)
            return await super().chat(messages, **kwargs)

    services.llm = ClearingLLM(digest_answer("- From the model"))
    assert await MemoryKeeper(services).update(chat) is None
    assert services.digests.get(GROUP_ID) is None


def test_deleted_note_ids_are_never_reused(services, chat):
    first = services.notes.add(GROUP_ID, "Alice is vegetarian", created_by="owner", actor="o")
    second = services.notes.add(GROUP_ID, "Bob is late", created_by="owner", actor="o")
    services.notes.delete(second.id, actor="o")
    third = services.notes.add(-999, "Another chat's fact", created_by="owner", actor="o")
    assert third.id > second.id > first.id
    assert [change.action for change in services.notes.history(third.id)] == ["created"]


async def test_memory_and_digest_are_background(services, wired, bot, chat):
    services.notes.add(GROUP_ID, "The group does a BBQ every National Day",
                       category="recurring_plan", created_by="owner", actor="o")
    services.notes.add(GROUP_ID, "Bob is vegetarian", person_id=person(services, 8),
                       created_by="bot", actor="o")
    services.digests.save(GROUP_ID, "- Planning a BBQ for Sat", actor="t")
    services.reminders.create(GROUP_ID, "Bring the grill", int(time.time()) + 3600,
                              created_by="t")
    services.llm = ScriptedLLM("Oi!")
    msg = message(6, "@naruto_bot what's up?")
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.on_message(update(msg), context(bot))
    background = services.llm.calls[0]["messages"][1]["content"]
    current = services.llm.calls[0]["messages"][2]["content"]
    assert ("Group memory (notes you keep):\n"
            "- [n1] The group does a BBQ every National Day (recurring plan)\n"
            "- [n2] Bob: Bob is vegetarian (group fact)") in background
    assert "What's been going on (digest, updated" in background
    assert "- Planning a BBQ for Sat" in background
    assert "Pending reminders:\n- reminder 1:" in current and "Bring the grill" in current


# ------------------------------------------------------------ memory tools

async def run_tools(services, bot, chat, *calls, skill="banter", text="@naruto_bot go",
                    message_id=999):
    trigger = store(services, message_id, text, offset=10_000)
    llm = ScriptedLLM(list(calls), "Done.")
    services.llm = llm
    outcome = await AgentRunner(services, bot, llm=llm).run(
        RunRequest(chat=chat, trigger=trigger, bot=services.status.bot, skill=skill))
    return outcome, results_of(llm)


async def test_remember_forget_and_search(services, bot, chat):
    _, results = await run_tools(
        services, bot, chat,
        tool_call("remember", {"content": "Alice is vegetarian", "category": "preference",
                               "about": "me"}, "a"),
        tool_call("remember", {"content": "Alice is vegan", "replaces_note_id": 1}, "b"),
        tool_call("search_memory", {"about": "alice"}, "c"),
        tool_call("forget", {"note_id": "n1"}, "d"),
        tool_call("forget", {"note_id": 7}, "e"), text="@naruto_bot remember that I'm vegetarian")
    assert results[0].startswith("Saved as [n1]: [n1] Alice: Alice is vegetarian (preference)")
    assert results[1].startswith("Updated note [n1]") and "vegan" in results[1]
    assert results[2].startswith("1 notes about Alice:")
    assert results[3] == "Forgot [n1]: Alice is vegan"
    assert results[4] == "Error: There is no note 7 in this group's memory."
    history = services.notes.history(1)
    assert [c.action for c in history] == ["deleted", "updated", "created"]
    assert "asked by user 7" in history[-1].changed_by


async def test_the_bot_saves_notes_on_its_own_too(services, bot, chat):
    """Asked ("remember that…") the note is the member's; noticed in passing
    ("btw my birthday is 3 March") it is the bot's own."""
    await run_tools(services, bot, chat,
                    tool_call("remember", {"content": "Alice is vegetarian", "about": "me"}),
                    text="@naruto_bot remember that I'm vegetarian")
    await run_tools(services, bot, chat,
                    tool_call("remember", {"content": "Alice's birthday is 3 March",
                                           "category": "date", "about": "me"}),
                    text="@naruto_bot btw my birthday is 3 March, plan something",
                    message_id=1000)
    asked, noticed = services.notes.for_chat(GROUP_ID)
    assert (asked.created_by, asked.created_by_user_id) == ("member", 7)
    assert (noticed.created_by, noticed.created_by_user_id) == ("bot", None)
    assert "on its own, talking with user 7" in services.notes.history(noticed.id)[0].changed_by


async def test_locked_notes_cannot_be_forgotten(services, bot, chat):
    note = services.notes.add(GROUP_ID, "Wei is always late", created_by="owner", actor="o")
    services.notes.set_locked(note.id, True, actor="o")
    _, results = await run_tools(services, bot, chat, tool_call("forget", {"note_id": note.id}),
                                 skill="remember")
    assert "locked by the owner" in results[0]
    assert services.notes.get(note.id) is not None


# -------------------------------------------------------------- reminders

def test_parse_when(services):
    tz = services.timezone()
    now = 1_790_000_000
    assert parse_when("in 2 hours", tz, now) == now + 7200
    assert parse_when("in 3 days", tz, now) == now + 3 * 86400
    assert parse_when("2026-10-02 09:00", tz) == 1_790_931_600  # UTC in the tests
    # What models and people actually write.
    assert parse_when("in 1 minute", tz, now) == now + 60
    assert parse_when("in a minute", tz, now) == now + 60
    assert parse_when("90 mins", tz, now) == now + 5400
    assert parse_when("in 1h 30m", tz, now) == now + 5400
    assert parse_when("in 1 hour and 15 minutes", tz, now) == now + 4500
    assert parse_when("2 hours from now", tz, now) == now + 7200
    # now is Mon 21 Sep 2026, 14:13:20 UTC
    assert parse_when("17:30", tz, now) == 1_790_011_800  # later today
    assert parse_when("9:00", tz, now) == 1_790_067_600  # already past: tomorrow
    assert parse_when("tomorrow 9am", tz, now) == 1_790_067_600
    assert parse_when("today at 5pm", tz, now) == 1_790_010_000
    assert parse_when("tmr 12pm", tz, now) == 1_790_078_400
    with pytest.raises(Exception, match="Give a time"):
        parse_when("2026-10-02", tz)
    with pytest.raises(Exception, match="must look like"):
        parse_when("next tuesday-ish", tz)
    for vague in ("5", "25:00", "13pm", "tomorrow"):
        with pytest.raises(Exception, match="must look like"):
            parse_when(vague, tz, now)


async def test_set_and_cancel_reminders(services, bot, chat):
    _, results = await run_tools(
        services, bot, chat,
        tool_call("set_reminder", {"when": "in 2 hours", "text": "Bring  the grill"}, "a"),
        tool_call("set_reminder", {"when": "2020-01-01 10:00", "text": "too late"}, "b"),
        tool_call("cancel_reminder", {"reminder_id": 1}, "c"),
        tool_call("cancel_reminder", {"reminder_id": 1}, "d"), skill="remind")
    assert results[0].startswith("Reminder 1 set for ") and results[0].endswith("Bring the grill")
    assert "already past" in results[1]
    assert results[2] == "Cancelled reminder 1: Bring the grill"
    assert results[3] == "Error: Reminder 1 is already cancelled."


async def test_due_reminders_are_sent_and_recorded(services, chat, bot):
    services.members.upsert_live(GROUP_ID, 7, "Alice", "alice")
    now = int(time.time())
    services.reminders.create(GROUP_ID, "Bring the grill", now - 10, created_by="t",
                              created_by_user_id=7)
    services.reminders.create(GROUP_ID, "Old one", now - 7200, created_by="t")
    services.reminders.create(-777, "Nowhere", now - 10, created_by="t")
    services.reminders.create(GROUP_ID, "Later", now + 3600, created_by="t")
    sender = ReminderSender(services, Recorder(services))
    assert await sender.send_due(bot) == 2
    texts = [s["text"] for s in bot.sent]
    assert "⏰ Reminder: Old one\n(This was due" in texts[0]
    assert texts[1] == "⏰ Reminder: Bring the grill\n— set by Alice"
    statuses = [r.status for r in sorted(services.reminders.for_chat(GROUP_ID), key=lambda r: r.id)]
    assert statuses == ["sent", "sent", "pending"]
    assert services.reminders.get(3).status == "failed"
    assert services.messages.get_live(GROUP_ID, 901).text.startswith("⏰ Reminder")


class FlakyBot(FakeBot):
    """Fails the next sends with the given errors, then works."""

    def __init__(self, *errors):
        super().__init__()
        self.errors = list(errors)

    async def send_message(self, chat_id, text, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        return await super().send_message(chat_id, text, **kwargs)


async def test_a_reminder_survives_a_telegram_outage(services, chat, monkeypatch):
    now = 1_790_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    reminder = services.reminders.create(GROUP_ID, "Bring the grill", now - 5, created_by="t")
    bot = FlakyBot(NetworkError("Connection reset"), TimedOut())
    sender = ReminderSender(services, Recorder(services))

    assert await sender.send_due(bot) == 0
    stored = services.reminders.get(reminder.id)
    assert (stored.status, stored.attempts, stored.next_attempt_at) == ("pending", 1, now + 30)
    assert await sender.send_due(bot) == 0  # waits for its next attempt
    now += 30
    assert await sender.send_due(bot) == 0  # timed out: backs off further
    stored = services.reminders.get(reminder.id)
    assert (stored.status, stored.attempts, stored.next_attempt_at) == ("pending", 2, now + 60)

    now += 60  # Telegram is back
    assert await sender.send_due(bot) == 1
    stored = services.reminders.get(reminder.id)
    assert stored.status == "sent" and stored.error is None and stored.next_attempt_at is None
    assert [s["text"] for s in bot.sent] == ["⏰ Reminder: Bring the grill"]
    now += 3600
    assert await sender.send_due(bot) == 0 and len(bot.sent) == 1  # never sent twice


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # RetryAfter(42)
async def test_flood_limits_wait_as_long_as_telegram_asks(services, chat, monkeypatch):
    now = 1_790_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    reminder = services.reminders.create(GROUP_ID, "Vote!", now - 5, created_by="t")
    bot = FlakyBot(RetryAfter(42))
    assert await ReminderSender(services, Recorder(services)).send_due(bot) == 0
    stored = services.reminders.get(reminder.id)
    assert stored.status == "pending" and stored.next_attempt_at == now + 43


async def test_permanent_failures_stay_failed(services, chat, monkeypatch):
    now = 1_790_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    kicked = services.reminders.create(GROUP_ID, "A", now - 5, created_by="t")
    bot = FlakyBot(Forbidden("bot was kicked from the group chat"),
                   BadRequest("Chat not found"), BadRequest("Chat not found"))  # + plain text
    sender = ReminderSender(services, Recorder(services))
    services.reminders.create(GROUP_ID, "B", now - 4, created_by="t")
    assert await sender.send_due(bot) == 0
    assert [r.status for r in services.reminders.for_chat(GROUP_ID)] == ["failed", "failed"]
    assert services.reminders.get(kicked.id).error == "bot was kicked from the group chat"
    now += 86400
    assert await sender.send_due(bot) == 0 and bot.sent == []  # never tried again


# ---------------------------------------------------------- describe_image

async def test_describe_image_on_demand_and_cached(services, bot, chat, monkeypatch):
    photo = store(services, 50, "", offset=5, media_kind="photo", media_file_id="file-50")
    imported = store(services, 60, "", offset=-100, source=IMPORT, media_kind="photo")
    fetched = []

    async def fake_extract(telegram, kind, file_id, meta, max_bytes):
        fetched.append(file_id)
        return {"kind": kind, "mime_type": "image/jpeg", "base64": "PIXELS", "width": 1,
                "height": 1}, None

    monkeypatch.setattr("naruto.media.extract_stored", fake_extract)
    trigger = store(services, 999, "@naruto_bot what was in that photo?", offset=10_000)
    llm = ScriptedLLM([tool_call("describe_image", {"message_id": photo.id}, "a"),
                       tool_call("describe_image", {"message_id": imported.id}, "b")],
                      "A dog on a beach.", "It shows a dog.")
    services.llm = llm
    outcome = await AgentRunner(services, bot, llm=llm).run(
        RunRequest(chat=chat, trigger=trigger, bot=services.status.bot))
    results = results_of(llm, 2)
    assert results[0] == f"Image in message {photo.id}: A dog on a beach."
    assert "imported history" in results[1]
    vision = llm.calls[1]["messages"]
    assert vision[1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,PIXELS"
    assert outcome.text == "It shows a dog." and fetched == ["file-50"]
    run = services.runs.get(outcome.run_id)
    assert run.model_requests == 3 and "PIXELS" not in json.dumps(run.steps)

    services.llm = ScriptedLLM("Cute dog!")
    trigger2 = store(services, 1000, "@naruto_bot nice", offset=10_001)
    await AgentRunner(services, bot).run(RunRequest(chat=chat, trigger=trigger2,
                                                    bot=services.status.bot))
    transcript = services.llm.calls[0]["messages"][1]["content"]
    assert f"[{photo.id}] Alice" in transcript and "[photo: A dog on a beach.]" in transcript


async def test_describe_image_needs_a_request_to_spare(services, bot, chat):
    services.settings.set("agent.max_model_requests", 2, actor="t")
    photo = store(services, 50, "", media_kind="photo", media_file_id="f")
    _, results = await run_tools(services, bot, chat,
                                 tool_call("describe_image", {"message_id": photo.id}))
    assert "No time left" in results[0]


# ------------------------------------------------------------------ skills

async def test_banter_hands_over_to_summarize(services, bot, chat):
    for i in range(1, 4):
        store(services, i, f"old topic {i}", offset=i)
    since = "2026-05-28"  # the fixture messages' day
    trigger = store(services, 999, "@naruto_bot what did we talk about today?", offset=10_000)
    llm = ScriptedLLM([tool_call("use_skill", {"skill": "summarize", "since": since}, "a"),
                       tool_call("search_chat", {"query": "x"}, "b")], "Here's the rundown.")
    keeper = services.keeper = MemoryKeeper(services)
    outcome = await AgentRunner(services, bot, llm=llm).run(
        RunRequest(chat=chat, trigger=trigger, bot=services.status.bot))
    assert outcome.text == "Here's the rundown."
    assert "use_skill" in [t["function"]["name"] for t in llm.calls[0]["tools"]]
    summarize = llm.calls[1]
    assert "Your task: summarize" in summarize["messages"][0]["content"]
    assert summarize["reasoning"] is True
    assert "get_earlier_messages" in [t["function"]["name"] for t in summarize["tools"]]
    assert "old topic 1" in summarize["messages"][1]["content"]
    run = services.runs.get(outcome.run_id)
    assert run.tool_steps[1]["result"] == "Not run: handing over to another mode."
    assert run.skill == "summarize" and {"type": "switch", "skill": "summarize"} in run.steps
    assert GROUP_ID in keeper._requested  # a summary refreshes the digest


async def test_use_skill_since_accepts_days(services, bot, chat):
    from naruto.agent.tools.skills import parse_since
    ctx = SimpleNamespace(services=services)
    assert abs(parse_since(ctx, "2 days") - (time.time() - 2 * 86400)) < 5
    assert abs(parse_since(ctx, "6 hours") - (time.time() - 6 * 3600)) < 5


def test_summary_scope(services):
    tz = services.timezone()
    now = 1_790_000_000  # Wed 23 Sep 2026, 13:33 UTC
    assert summary_scope([], tz, now) == (None, "summarize the recent discussion")
    assert summary_scope(["today"], tz, now)[0] == 1_789_948_800
    assert summary_scope(["3h"], tz, now)[0] == now - 3 * 3600
    assert summary_scope(["2", "days"], tz, now)[0] == now - 2 * 86400
    assert summary_scope(["the", "bbq"], tz, now) == (
        None, "summarize what was said about: the bbq (search for it)")


# ---------------------------------------------------------------- commands

async def command(wired, bot, text, *, sender=ALICE, **kwargs):
    msg = message(kwargs.pop("message_id", 20), text, sender=sender, command=True,
                  offset=kwargs.pop("offset", 100), bot=bot, **kwargs)
    await wired.recorder.on_message(update(msg), context(bot))
    return msg


async def test_summary_command_is_recorded_and_answered_in_thread(services, wired, bot):
    services.llm = ScriptedLLM("Rundown: BBQ Saturday.")
    store(services, 1, "bbq saturday?", offset=50)
    msg = await command(wired, bot, "/summary today")
    await wired.skills.summary(update(msg), context(bot, ["today"]))
    stored = services.messages.get_live(GROUP_ID, 20)
    assert stored.text == "/summary today"
    call = services.llm.calls[0]
    assert "Your task: summarize" in call["messages"][0]["content"]
    assert "(They used /summary: summarize today's messages.)" in call["messages"][-1]["content"]
    assert bot.sent[-1]["text"] == "Rundown: BBQ Saturday."
    assert bot.sent[-1]["reply_parameters"].message_id == 20


async def test_other_commands_pick_their_skill(services, wired, bot):
    for name, handler, args, skill in [
            ("/plan", wired.skills.plan, [], "consolidate the plan"),
            ("/questions", wired.skills.questions, [], "open questions"),
            ("/remember Sam is vegan", wired.skills.remember, ["Sam", "is", "vegan"],
             "group memory"),
            ("/remind sat 5pm grill", wired.skills.remind, ["sat", "5pm", "grill"], "reminders")]:
        services.llm = ScriptedLLM("ok")
        msg = await command(wired, bot, name, message_id=len(bot.sent) + 30)
        await handler(update(msg), context(bot, args))
        assert f"Your task: {skill}" in services.llm.calls[0]["messages"][0]["content"], name


async def test_remember_and_remind_need_arguments(services, wired, bot):
    services.llm = ScriptedLLM("ok")
    msg = await command(wired, bot, "/remember")
    await wired.skills.remember(update(msg), context(bot, []))
    assert bot.sent[-1]["text"].startswith("Usage: /remember")
    msg = await command(wired, bot, "/remind", message_id=21)
    await wired.skills.remind(update(msg), context(bot, []))
    assert bot.sent[-1]["text"].startswith("Usage: /remind")
    assert services.llm.calls == []


async def test_board_command(services, wired, bot):
    msg = await command(wired, bot, "/board")
    await wired.skills.show_board(update(msg), context(bot))
    assert bot.sent[-1]["text"].startswith("The board is empty")
    services.boards.set_section(GROUP_ID, "plans", ["BBQ"], actor="t")
    await wired.skills.show_board(update(msg), context(bot))
    assert bot.api_calls[-1][0] == "sendRichMessage" and bot.pins
    assert services.messages.get_live(GROUP_ID, 20) is None  # /board isn't stored


async def test_catchup_is_private(services, wired, bot):
    store(services, 1, "I'll be late", sender=(7, "Alice"), offset=10)
    store(services, 2, "Alice, can you bring chips?", sender=(8, "Bob"), offset=20)
    services.llm = ScriptedLLM("Bob asked you to bring chips.")
    msg = await command(wired, bot, "/catchup", api_kwargs={"ephemeral_message_id": 9},
                        offset=30)
    await wired.skills.catchup(update(msg), context(bot))
    assert services.messages.get_live(GROUP_ID, 20) is None
    call = services.llm.calls[0]["messages"]
    assert "Your task: catch someone up" in call[0]["content"]
    assert "can you bring chips" in call[1]["content"] and "I'll be late" not in call[1]["content"]
    assert "## Current request\nNow: " in call[-1]["content"]
    assert "\nAlice (@alice) at" in call[-1]["content"]
    sent = bot.sent[-1]
    assert sent["api_kwargs"]["ephemeral_message_parameters"] == {"receiver_user_id": 7}
    assert sent["api_kwargs"]["reply_parameters"] == {"ephemeral_message_id": 9}
    assert sent["text"] == "Bob asked you to bring chips."
    run = services.runs.recent()[0][0]
    assert run.skill == "catchup" and run.trigger_row_id is None


async def test_catchup_falls_back_to_a_dm(services, wired, bot):
    bot.fail_ephemeral = True
    services.llm = ScriptedLLM("Nothing much.")
    msg = await command(wired, bot, "/catchup")
    await wired.skills.catchup(update(msg), context(bot))
    assert bot.sent[-1]["chat_id"] == 7
    assert bot.sent[-1]["text"].startswith("Catch-up for BBQ crew:")


def test_recorder_stores_only_visible_skill_commands(services, chat):
    recorder = Recorder(services)
    for i, text in enumerate(["/summary", "/group_info", "/plan@naruto_bot now"], 1):
        asyncio.run(recorder.on_message(update(message(i, text, command=True)), None))
    assert [m.text for m in services.messages.latest(GROUP_ID, 10)] == [
        "/summary", "/plan@naruto_bot now"]


# --------------------------------------------------------------- retention

def test_retention_deletes_old_messages_descriptions_and_reminders(services, chat):
    now = time.time()
    old = store(services, 1, "old", offset=int(now - 40 * 86400 - fakes.T0),
                media_kind="photo", media_file_id="f")
    new = store(services, 2, "new", offset=int(now - 86400 - fakes.T0))
    reply = store(services, 3, "re", offset=int(now - 3600 - fakes.T0), reply_to_message_id=1)
    assert reply.reply_to_row_id == old.id
    services.messages.save_description(old, "a cat", "m")
    imported = store(services, 4, "old import", source=IMPORT,
                     offset=int(now - 40 * 86400 - fakes.T0))
    unread = store(services, 5, "not read yet", offset=int(now - 33 * 86400 - fakes.T0))
    services.digests.save(GROUP_ID, "digest", actor="t", last=old)  # read up to message 1
    services.reminders.create(GROUP_ID, "done", int(now - 40 * 86400), created_by="t")
    services.reminders.mark_sent(1, 5)
    assert jobs.cleanup_live_messages(services) == "1 live messages"
    assert jobs.cleanup_imported_messages(services) == "1 imported messages"
    assert jobs.cleanup_reminders(services) == "1 old reminders"
    assert services.messages.get(old.id) is None and services.messages.get(new.id)
    assert services.messages.get(unread.id) is not None  # unread: 7 more days
    assert services.messages.get(imported.id) is None
    assert services.messages.get(reply.id).reply_to_row_id is None
    assert services.messages.descriptions([old.id]) == {}
    services.settings.set("retention.live_messages_days", 0, actor="t")
    assert jobs.cleanup_live_messages(services) is None


def test_memory_moves_with_a_group_upgrade(services, chat):
    services.notes.add(GROUP_ID, "fact", created_by="owner", actor="o")
    services.digests.save(GROUP_ID, "digest", actor="o")
    services.reminders.create(GROUP_ID, "r", int(time.time()) + 60, created_by="o")
    services.chats.migrate(GROUP_ID, -1004001)
    assert services.notes.for_chat(-1004001)[0].content == "fact"
    assert services.notes.history(1)[0].note_id == 1
    assert services.digests.get(-1004001).text == "digest"
    assert services.reminders.for_chat(-1004001)[0].text == "r"


# ------------------------------------------------------------ distillation

async def import_with_notes(services, tmp_path, **changes):
    """Import the fixture's messages and distill notes (no summaries)."""
    importer = ImportService(services, tmp_path / "imports")
    record = await importer.create_from_upload(io.BytesIO(FIXTURE.read_bytes()), "result.json")
    options = importer.default_options(record)
    options.archive, options.distill = False, True
    for name, value in changes.items():
        setattr(options, name, value)
    await importer.start(record.id, GROUP_ID, options=options)
    await asyncio.gather(*importer._tasks.values())
    return importer, importer.repo.get(record.id)


async def test_import_is_distilled_into_notes_and_a_first_digest(services, tmp_path):
    services.settings.set("retention.imported_messages_days", 0, actor="t")
    services.settings.set("import.digest_window_days", 365, actor="t")  # the fixture is old
    services.keeper = MemoryKeeper(services)
    services.llm = ScriptedLLM(
        json.dumps({"notes": [{"action": "add", "content": "Wei always brings the grill",
                               "category": "running_joke", "about": "Wei"}]}),
        digest_answer("- BBQ planning"))
    importer, record = await import_with_notes(services, tmp_path)
    assert record.status == "done" and record.distill_status == "done"
    assert (record.distill_total, record.distill_done, record.notes_added) == (1, 1, 1)
    note = services.notes.for_chat(GROUP_ID)[0]
    assert note.created_by == "import" and note.person_id == person(services, 9)
    first = services.llm.calls[0]
    assert first["background"] is True and "## Messages (part 1 of 1" in \
        first["messages"][1]["content"]
    assert first["info"].task == "distill" and first["info"].import_id == record.id
    assert services.digests.get(GROUP_ID).text == "- BBQ planning"
    assert services.digests.get(GROUP_ID).updated_by == f"import {record.id}"
    assert not Path(record.file_path or tmp_path / "gone").exists()


async def test_an_old_import_does_not_become_the_current_digest(services, tmp_path):
    services.settings.set("retention.imported_messages_days", 0, actor="t")
    services.keeper = MemoryKeeper(services)  # digest start: imports of the last 14 days
    services.llm = ScriptedLLM(json.dumps({"notes": []}))
    _, record = await import_with_notes(services, tmp_path)
    assert record.status == "done" and record.imported == 12
    assert services.digests.get(GROUP_ID) is None  # a month-old export isn't "now"
    assert services.keeper.due_chats() == []
    store(services, 1, "new live message", offset=10_000_000)  # live counts whatever its age
    keeper = services.keeper
    assert keeper.services.digests.unread_count(GROUP_ID, None, keeper.imported_since())[0] == 1


async def test_distillation_failure_pauses_and_resumes(services, tmp_path):
    services.settings.set("retention.imported_messages_days", 0, actor="t")
    services.llm = ScriptedLLM("not json")
    importer, record = await import_with_notes(services, tmp_path)
    assert record.status == "paused" and record.raw_status == "done" and record.imported > 0
    assert record.distill_status == "paused" and "kept failing" in record.distill_error
    assert Path(record.file_path).exists()  # kept to resume

    services.llm = ScriptedLLM(json.dumps({"notes": [{"content": "Wei grills"}]}))
    importer.resume(record.id)
    await asyncio.gather(*importer._tasks.values())
    record = importer.repo.get(record.id)
    assert record.status == "done" and record.distill_status == "done"
    assert record.notes_added == 1 and record.file_path is None
    assert services.messages.count(GROUP_ID, IMPORT) == 12  # not imported twice


async def test_reading_an_export_for_distillation_keeps_the_bot_responsive(services, tmp_path,
                                                                          monkeypatch):
    from naruto.memory import distill

    services.settings.set("retention.imported_messages_days", 0, actor="t")
    services.llm = ScriptedLLM(json.dumps({"notes": []}))
    real = distill.export_chunks

    def slow_export_chunks(*args, **kwargs):
        time.sleep(0.3)  # a large export being parsed
        return real(*args, **kwargs)

    monkeypatch.setattr(distill, "export_chunks", slow_export_chunks)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    beating = asyncio.create_task(heartbeat())
    _, record = await import_with_notes(services, tmp_path)
    beating.cancel()
    assert record.distill_status == "done"
    assert ticks >= 10  # the loop kept running while the export was read


def test_reading_an_export_stops_on_shutdown(services):
    from naruto.memory.distill import DistillStopped, export_chunks

    with pytest.raises(DistillStopped):
        export_chunks(services, str(FIXTURE), chunk_tokens=1000, max_chars=1500,
                      stopping=lambda: True)
