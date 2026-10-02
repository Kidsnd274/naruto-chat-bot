"""End to end: the real TelegramBot (startup, polling, handlers, sending)
against a fake Bot API. Catches wiring bugs that handler-level tests miss,
such as a startup step that never runs."""

import pytest
from telegram import Message

from naruto.llm import ChatResult
from naruto.tg.bot import TelegramBot
from naruto.tg.content import to_new_message
from telegram_fake import BOT_USER, FakeTelegram

OWNER = {"id": 1000, "is_bot": False, "first_name": "Owner", "username": "owner"}
ALICE = {"id": 7, "is_bot": False, "first_name": "Alice", "username": "alice"}
CHAT = -4001


class FakeLLM:
    def __init__(self, text="[REPLY] Oi! Saturday it is."):
        self.text = text
        self.calls = []
        self.in_flight = self.waiting = 0

    async def chat(self, messages, *, reasoning=None, max_tokens=None, **kwargs):
        self.calls.append(messages)
        return ChatResult(text=self.text, reasoning=None, model="fake", latency_ms=1,
                          usage=None, finish_reason="stop")

    async def list_models(self, timeout=10.0):
        return ["fake"]


@pytest.fixture
async def running(services):
    services.status.bot = None  # the real startup has to fill this in
    fake = FakeTelegram()
    services.llm = FakeLLM()
    bot = TelegramBot(services, request=fake, get_updates_request=fake)
    await bot.start()
    try:
        yield fake, services
    finally:
        await bot.stop()


async def test_startup_logs_in_and_registers_commands(running):
    fake, services = running
    assert services.status.bot.username == "naruto_bot"
    assert services.status.bot.id == BOT_USER["id"]
    assert services.status.can_read_all_group_messages is True
    group_commands = next(p["commands"] for name, p in fake.calls
                          if name == "setMyCommands" and p["scope"]["type"] == "all_group_chats")
    enable = next(c for c in group_commands if c["command"] == "enable")
    assert enable["is_ephemeral"] is True
    assert any(name == "setMyCommands" and p["scope"] == {"type": "chat", "chat_id": 1000}
               for name, p in fake.calls)


async def test_mention_in_enabled_group_gets_a_reply(running):
    fake, services = running
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    services.chats.set_status(CHAT, "enabled")
    fake.push_message("when's the bbq?", user=ALICE, message_id=10)
    fake.push_message("@naruto_bot remind me?", user=ALICE, message_id=11)

    reply = (await fake.wait_for("sendMessage"))[0]
    assert reply["chat_id"] == CHAT and reply["text"] == "Oi! Saturday it is."
    assert reply["reply_parameters"]["message_id"] == 11
    assert services.messages.count(CHAT) == 3  # two messages and the reply
    runs, total = services.runs.recent()
    assert total == 1 and runs[0].status == "ok"
    assert "when's the bbq?" in runs[0].prompt[1]["content"]


async def test_pending_group_is_silent_and_owner_is_asked(running):
    fake, services = running
    fake.push_message("@naruto_bot hello?", user=ALICE)
    dm = (await fake.wait_for("sendMessage"))[0]
    assert dm["chat_id"] == 1000 and "Approve" in str(dm["reply_markup"])
    await fake.settle()
    assert len(fake.sent()) == 1  # no reply in the group
    assert services.messages.count(CHAT) == 0


async def test_enable_command_reports_state(running):
    fake, services = running
    fake.push_message("/enable", user=OWNER, command=True,
                      ephemeral_message_id=3)
    first = (await fake.wait_for("sendMessage"))[0]
    assert first["ephemeral_message_parameters"] == {"receiver_user_id": 1000}
    assert first["reply_parameters"] == {"ephemeral_message_id": 3}
    assert first["text"].startswith("✅ Enabled.")
    assert services.chats.get(CHAT).enabled

    fake.push_message("/enable", user=OWNER, command=True)
    second = (await fake.wait_for("sendMessage", 2))[1]
    assert second["text"].startswith("✅ Already enabled here")

    fake.push_message("/disable", user=OWNER, command=True)
    fake.push_message("/disable", user=OWNER, command=True)
    replies = await fake.wait_for("sendMessage", 4)
    assert replies[2]["text"].startswith("⏸ Disabled.")
    assert replies[3]["text"].startswith("⏸ Already disabled")


async def test_enable_by_non_owner_is_ignored(running):
    fake, services = running
    fake.push_message("/enable", user=ALICE, command=True)
    await fake.settle()
    assert fake.sent() == [] or all(s["chat_id"] == 1000 for s in fake.sent())
    assert services.chats.get(CHAT) is None or not services.chats.get(CHAT).enabled


async def test_ephemeral_failure_falls_back_to_dm(running):
    fake, services = running
    fake.fail_ephemeral = True
    fake.push_message("/enable", user=OWNER, command=True)
    replies = await fake.wait_for("sendMessage", 2)  # failed ephemeral + DM
    assert replies[-1]["chat_id"] == 1000 and "Enabled" in replies[-1]["text"]


async def test_rights_are_checked_on_enable(running):
    fake, services = running
    fake.chat_member = {
        "status": "administrator", "user": BOT_USER, "can_be_edited": False,
        "is_anonymous": False, "can_manage_chat": True, "can_delete_messages": False,
        "can_manage_video_chats": False, "can_restrict_members": False,
        "can_promote_members": False, "can_change_info": False, "can_invite_users": False,
        "can_post_stories": False, "can_edit_stories": False, "can_delete_stories": False,
        "can_pin_messages": True,
    }
    fake.push_message("/enable", user=OWNER, command=True)
    await fake.wait_for("sendMessage")
    chat = services.chats.get(CHAT)
    assert chat.can_pin is True and chat.can_delete is False
    assert chat.membership == "administrator"
    assert any(name == "getChatMember" and p["user_id"] == 42 for name, p in fake.calls)


async def test_bot_added_to_group_asks_owner_and_approve_button_works(running):
    fake, services = running
    fake.push(my_chat_member={
        "chat": {"id": CHAT, "type": "group", "title": "BBQ crew"},
        "from": ALICE, "date": 1_780_000_000,
        "old_chat_member": {"status": "left", "user": BOT_USER},
        "new_chat_member": {"status": "member", "user": BOT_USER},
    })
    dm = (await fake.wait_for("sendMessage"))[0]
    assert "Added to <b>BBQ crew</b> by <b>Alice</b>" in dm["text"]

    for attempt in (1, 2):
        fake.push(callback_query={
            "id": f"q{attempt}", "from": OWNER, "chat_instance": "c",
            "data": f"access:approve:{CHAT}",
            "message": {"message_id": 1, "date": 1_780_000_000,
                        "chat": {"id": 1000, "type": "private", "first_name": "Owner"},
                        "from": BOT_USER, "text": dm["text"]},
        })
        edits = await fake.wait_for("editMessageText", attempt)
    assert edits[0]["text"].startswith("✅ Enabled <b>BBQ crew</b>")
    assert "was already enabled" in edits[1]["text"]
    assert services.chats.get(CHAT).enabled


async def test_owner_dm_start_lists_pending_groups(running):
    fake, services = running
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    fake.push_message("/start", chat_id=1000, user=OWNER, command=True)
    reply = (await fake.wait_for("sendMessage"))[0]
    assert reply["chat_id"] == 1000 and "Pending: 1" in reply["text"]


async def test_plan_buttons_and_poll_updates(running):
    fake, services = running
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    services.chats.set_status(CHAT, "enabled")
    plan = services.plans.create(CHAT, "BBQ", ["Sat 6pm"], run_id=None, proposed_for_user_id=7)
    services.plans.set_message(plan.id, 77, CHAT)
    fake.push(callback_query={
        "id": "q1", "from": ALICE, "chat_instance": "c", "data": f"plan:confirm:{plan.id}",
        "message": {"message_id": 77, "date": 1_780_000_000,
                    "chat": {"id": CHAT, "type": "group", "title": "BBQ crew"},
                    "from": BOT_USER, "text": "📋 Plan: BBQ"},
    })
    await fake.wait_for("sendRichMessage")
    assert services.plans.get(plan.id).status == "confirmed"
    assert any(name == "pinChatMessage" for name, _ in fake.calls)
    assert any(name == "answerCallbackQuery" for name, _ in fake.calls)

    poll_message = fake._sendPoll({"chat_id": CHAT, "question": "Day?",
                                   "options": ["Sat", "Sun"], "is_anonymous": False})
    services.messages.insert_live(to_new_message(Message.de_json(poll_message, None),
                                                 chat_id=CHAT, bot_id=42))
    poll = {**poll_message["poll"], "options": [
        {"text": "Sat", "voter_count": 1, "persistent_id": "o0"},
        {"text": "Sun", "voter_count": 0, "persistent_id": "o1"}], "total_voter_count": 1}
    fake.push(poll=poll)
    fake.push(poll_answer={"poll_id": poll["id"], "user": ALICE, "option_ids": [0],
                           "option_persistent_ids": ["o0"]})
    for _ in range(100):
        stored = services.messages.find_poll(poll["id"])
        if stored.media_meta.get("votes"):
            break
        await fake.settle(0.02)
    assert stored.media_meta["counts"] == [1, 0] and stored.media_meta["votes"] == {"7": [0]}


async def test_skill_commands_through_the_real_bot(running):
    fake, services = running
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    services.chats.set_status(CHAT, "enabled")
    group_commands = next(p["commands"] for name, p in fake.calls
                          if name == "setMyCommands" and p["scope"]["type"] == "all_group_chats")
    names = {c["command"]: c for c in group_commands}
    assert {"summary", "catchup", "plan", "questions", "board", "remember", "remind"} <= set(names)
    assert names["catchup"]["is_ephemeral"] is True

    fake.push_message("bbq on saturday?", user=ALICE, message_id=10)
    fake.push_message("/summary today", user=ALICE, message_id=11, command=True)
    reply = (await fake.wait_for("sendMessage"))[0]
    assert reply["reply_parameters"]["message_id"] == 11
    prompt = services.llm.calls[0]
    assert "Your task: summarize" in prompt[0]["content"]
    assert "bbq on saturday?" in prompt[1]["content"]

    fake.push_message("/catchup", user=ALICE, command=True, ephemeral_message_id=4)
    private = (await fake.wait_for("sendMessage", 2))[1]
    assert private["ephemeral_message_parameters"] == {"receiver_user_id": 7}
    assert "Your task: catch someone up" in services.llm.calls[1][0]["content"]
