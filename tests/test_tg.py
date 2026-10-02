"""Telegram layer: extraction, recording, chat approval, commands, replies."""

from types import SimpleNamespace

import pytest
from telegram import (
    Chat,
    ChatMemberMember,
    ChatMemberOwner,
    ChatPermissions,
    Dice,
    Document,
    MessageOriginUser,
    Sticker,
)
from telegram.error import BadRequest, ChatMigrated

import fakes
from fakes import ALICE, BOB, BOT_ID, GROUP_ID, OWNER, OWNER_ID, FakeBot, context, message, update
from naruto import markers
from naruto.llm import ChatResult, LLMError
from naruto.tg.access import ChatAccess, note_pin, rights_of
from naruto.tg.board import BoardPublisher
from naruto.tg.bot import sanitize_updates
from naruto.tg.commands import GroupCommands, parse_alias_args
from naruto.tg.content import ephemeral_message_id, forwarded_from, media_of, reply_snippet, to_new_message
from naruto.tg.recorder import Recorder
from naruto.tg.responder import FAILURE_TEXT, Responder, is_trigger
from naruto.tg.sending import send_ephemeral, send_text, split_message

SUPER_ID = -1004001


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def wired(services, bot):
    """Recorder, access and responder sharing the services, like TelegramBot."""
    recorder = Recorder(services)
    access = ChatAccess(services, bot)
    services.access = access
    responder = Responder(services, recorder)
    return SimpleNamespace(recorder=recorder, access=access, responder=responder)


def enable(services, chat_id=GROUP_ID, title="BBQ crew"):
    services.chats.upsert_seen(chat_id, title=title)
    services.chats.set_status(chat_id, "enabled")


# ----------------------------------------------------------------- content

def test_to_new_message_maps_fields():
    target = message(10, "when?", sender=BOB)
    msg = message(11, "saturday", reply_to=target, message_thread_id=None)
    record = to_new_message(msg, chat_id=GROUP_ID, bot_id=BOT_ID)
    assert (record.message_id, record.sender_id, record.sender_name, record.sender_username) == \
        (11, 7, "Alice", "alice")
    assert record.date == fakes.T0 and record.reply_to_message_id == 10
    assert record.reply_to_snippet == "Bob: when?"
    assert record.from_bot is False and record.origin_chat_id == GROUP_ID


def test_bot_message_is_marked_from_bot():
    record = to_new_message(message(1, "yo", sender=fakes.BOT_USER), chat_id=GROUP_ID, bot_id=BOT_ID)
    assert record.from_bot


def test_media_refs_keep_file_ids_not_bytes():
    photo = media_of(fakes.photo_message(1))
    assert (photo.kind, photo.file_id, photo.meta["width"]) == ("photo", "big-id", 1280)
    sticker = media_of(message(2, None, sticker=Sticker("s-id", "s-u", 512, 512, False, False,
                                                        "regular", emoji="😂")))
    assert markers.media_marker(sticker.kind, sticker.meta) == "[sticker 😂]"
    doc = media_of(message(3, None, document=Document("d-id", "d-u", file_name="plan.pdf")))
    assert markers.media_marker(doc.kind, doc.meta) == "[file: plan.pdf]"
    dice = media_of(message(4, None, dice=Dice(5, "🎲")))
    assert markers.media_marker(dice.kind, dice.meta) == "[dice 🎲 5]"
    assert media_of(message(5, "plain")) is None


def test_forwarded_from_and_snippets():
    msg = message(1, "fwd", forward_origin=MessageOriginUser(fakes.at(), BOB))
    assert forwarded_from(msg) == "Bob"
    assert reply_snippet(message(2, "hey", sender=fakes.BOT_USER), BOT_ID) == "you: hey"
    long = reply_snippet(message(3, "x" * 500), BOT_ID)
    assert len(long) <= len("Alice: ") + 200 and long.endswith("…")


def test_ephemeral_message_id_reads_api_kwargs():
    msg = message(1, "/enable", api_kwargs={"ephemeral_message_id": 77})
    assert ephemeral_message_id(msg) == 77
    assert ephemeral_message_id(message(2, "/enable")) is None


@pytest.mark.parametrize("kind,meta,expected", [
    ("photo", {}, "[photo]"),
    ("sticker", {}, "[sticker]"),
    ("animation", {}, "[GIF]"),
    ("video_note", {}, "[video message]"),
    ("voice", {}, "[voice message]"),
    ("audio", {"title": "Song"}, "[audio: Song]"),
    ("document", {}, "[file]"),
    ("poll", {"question": "When?"}, "[poll: When?]"),
    ("venue", {"title": "ECP"}, "[venue: ECP]"),
    ("contact", {"name": "Jo"}, "[contact: Jo]"),
    ("mystery", {}, "[media]"),
    (None, {}, ""),
])
def test_media_markers(kind, meta, expected):
    assert markers.media_marker(kind, meta) == expected


def test_message_body_joins_marker_and_caption():
    assert markers.message_body("look", "photo") == "[photo] look"
    assert markers.message_body("", "photo") == "[photo]"
    assert markers.message_body("hi", None) == "hi"


# ---------------------------------------------------------------- recorder

async def test_enabled_group_messages_are_recorded(services, wired, bot):
    enable(services)
    await wired.recorder.on_message(update(message(10, "hello")), context(bot))
    stored = services.messages.get_live(GROUP_ID, 10)
    assert stored.text == "hello" and stored.sender_name == "Alice"
    assert services.members.get(GROUP_ID, 7).username == "alice"
    assert services.chats.get(GROUP_ID).last_activity_at == fakes.T0


async def test_pending_group_records_nothing_and_notifies_owner_once(services, wired, bot):
    await wired.recorder.on_message(update(message(10, "hello")), context(bot))
    await wired.recorder.on_message(update(message(11, "again")), context(bot))
    assert services.chats.get(GROUP_ID).status == "pending"
    assert services.messages.count(GROUP_ID) == 0
    dms = [s for s in bot.sent if s["chat_id"] == OWNER_ID]
    assert len(dms) == 1 and "BBQ crew" in dms[0]["text"]
    buttons = dms[0]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == [f"access:approve:{GROUP_ID}",
                                                  f"access:leave:{GROUP_ID}"]


async def test_join_message_for_the_bot_does_not_double_notify(services, wired, bot):
    join = message(1, None, new_chat_members=[fakes.BOT_USER])
    await wired.recorder.on_message(update(join), context(bot))
    assert bot.sent == []  # my_chat_member handles the notification


async def test_commands_and_service_messages_are_not_recorded(services, wired, bot):
    enable(services)
    await wired.recorder.on_message(update(message(1, "/group_info", command=True)), context(bot))
    await wired.recorder.on_message(update(message(2, None, new_chat_title="New")), context(bot))
    renamed = fakes.group(title="New")  # later messages carry the new title
    await wired.recorder.on_message(update(message(3, None, chat=renamed, new_chat_members=[BOB])),
                                    context(bot))
    assert services.messages.count(GROUP_ID) == 0
    assert services.chats.get(GROUP_ID).title == "New"
    assert services.members.get(GROUP_ID, 8).display_name == "Bob"


async def test_media_without_caption_is_recorded_as_marker(services, wired, bot):
    enable(services)
    await wired.recorder.on_message(update(fakes.photo_message(5)), context(bot))
    stored = services.messages.get_live(GROUP_ID, 5)
    assert (stored.media_kind, stored.media_file_id, stored.text) == ("photo", "big-id", "")


async def test_edits_update_stored_text(services, wired, bot):
    enable(services)
    await wired.recorder.on_message(update(message(10, "at 6")), context(bot))
    edited = message(10, "at 7", edit_date=fakes.at(30))
    await wired.recorder.on_edit(update(edited=edited), context(bot))
    stored = services.messages.get_live(GROUP_ID, 10)
    assert stored.text == "at 7" and stored.edit_date == fakes.T0 + 30


async def test_group_upgrade_moves_everything(services, wired, bot):
    enable(services)
    await wired.recorder.on_message(update(message(10, "before")), context(bot))
    await wired.recorder.on_message(update(message(11, None, migrate_to_chat_id=SUPER_ID)),
                                    context(bot))
    supergroup = fakes.group(SUPER_ID, chat_type="supergroup")
    await wired.recorder.on_message(update(message(1, None, chat=supergroup,
                                                   migrate_from_chat_id=GROUP_ID)), context(bot))
    await wired.recorder.on_message(update(message(2, "after", chat=supergroup)), context(bot))
    chat = services.chats.get(SUPER_ID)
    assert chat.enabled and chat.type == "supergroup"
    assert services.messages.count(SUPER_ID) == 2
    assert services.chats.get(GROUP_ID).chat_id == SUPER_ID


async def test_record_sent_ignores_disabled_chats(services, wired, bot):
    services.chats.upsert_seen(GROUP_ID)
    sent = await bot.send_message(GROUP_ID, "hi")
    wired.recorder.record_sent(GROUP_ID, sent)
    assert services.messages.count(GROUP_ID) == 0


# ------------------------------------------------------------------ access

async def test_bot_added_to_group_becomes_pending_and_owner_gets_buttons(services, wired, bot):
    await wired.access.on_my_chat_member(fakes.member_update("member", "left"), context(bot))
    chat = services.chats.get(GROUP_ID)
    assert chat.status == "pending" and chat.membership == "member"
    assert chat.added_by_name == "Alice"
    assert "Added to <b>BBQ crew</b> by <b>Alice</b>" in bot.sent[-1]["text"]


async def test_promotion_updates_rights_without_notifying(services, wired, bot):
    await wired.access.on_my_chat_member(fakes.member_update("member", "left"), context(bot))
    bot.sent.clear()
    await wired.access.on_my_chat_member(
        fakes.member_update("administrator", "member", can_pin=True), context(bot))
    chat = services.chats.get(GROUP_ID)
    assert chat.can_pin is True and chat.can_delete is True and chat.membership == "administrator"
    assert bot.sent == []


def test_rights_of_each_kind_of_member():
    admin = fakes.member_update("administrator", can_pin=True).my_chat_member.new_chat_member
    no_pin = fakes.member_update("administrator", can_pin=False).my_chat_member.new_chat_member
    member = ChatMemberMember(fakes.BOT_USER)
    assert rights_of(admin) == (True, True)
    assert rights_of(no_pin) == (False, True)
    assert rights_of(ChatMemberOwner(fakes.BOT_USER, is_anonymous=False)) == (True, True)
    assert rights_of(member) == (False, False)  # the group's permissions unknown
    assert rights_of(member, ChatPermissions(can_pin_messages=True)) == (True, False)
    assert rights_of(member, ChatPermissions(can_pin_messages=False)) == (False, False)


async def test_a_member_can_pin_where_everyone_may(services, wired, bot):
    """Basic groups let every member pin unless the owner turned it off."""
    enable(services)
    bot.members_can_pin = True
    chat = await wired.access.check_rights(GROUP_ID)
    assert chat.can_pin is True and chat.can_delete is False and chat.membership == "member"
    assert chat.missing_rights() == []


async def test_rights_are_refreshed_where_never_checked(services, wired, bot):
    """Seen live: a group enabled before rights were tracked stayed "not
    checked yet" although the bot had been made an admin there."""
    enable(services)  # never checked
    enable(services, -4002, "Checked")
    services.chats.set_rights(-4002, can_pin=False, can_delete=False)
    enable(services, -4003, "Gone")
    services.chats.set_membership(-4003, "left")
    bot.member = fakes.member_update("administrator", can_pin=True).my_chat_member.new_chat_member

    assert await wired.access.refresh_rights(older_than=3600) == 1
    assert services.chats.get(GROUP_ID).can_pin is True
    assert services.chats.get(-4002).can_pin is False  # checked recently: left alone
    assert services.chats.get(-4003).rights_checked_at is None
    assert await wired.access.refresh_rights() == 2  # everything but the group it left


async def test_pins_teach_the_pin_right(services, wired, bot):
    enable(services)
    services.chats.set_rights(GROUP_ID, can_pin=False, can_delete=False)
    services.boards.set_section(GROUP_ID, "plans", ["BBQ"], actor="t")
    await BoardPublisher(services).publish(bot, services.chats.get(GROUP_ID))
    assert services.chats.get(GROUP_ID).can_pin is True  # the pin worked

    note_pin(services, GROUP_ID, BadRequest("Message to pin not found"))
    assert services.chats.get(GROUP_ID).can_pin is True  # says nothing about rights
    bot.fail_pin = True
    await BoardPublisher(services).publish(bot, services.chats.get(GROUP_ID), fresh=True)
    assert services.chats.get(GROUP_ID).can_pin is False


async def test_readded_enabled_group_stays_enabled(services, wired, bot):
    enable(services)
    await wired.access.on_my_chat_member(fakes.member_update("left", "member"), context(bot))
    assert services.chats.get(GROUP_ID).membership == "left"
    await wired.access.on_my_chat_member(fakes.member_update("member", "left"), context(bot))
    assert services.chats.get(GROUP_ID).enabled
    assert "still enabled" in bot.sent[-1]["text"]


async def test_enable_command_is_owner_only_and_answers_ephemerally(services, wired, bot):
    cmd = message(20, "/enable", sender=ALICE, command=True)
    await wired.access.on_enable_command(update(cmd), context(bot))
    assert services.chats.get(GROUP_ID) is None and bot.sent == []

    cmd = message(21, "/enable", sender=OWNER, command=True,
                  api_kwargs={"ephemeral_message_id": 5})
    await wired.access.on_enable_command(update(cmd), context(bot))
    assert services.chats.get(GROUP_ID).enabled
    reply = bot.sent[-1]
    assert reply["chat_id"] == GROUP_ID
    assert reply["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": OWNER_ID},
        "reply_parameters": {"ephemeral_message_id": 5},
    }
    assert "Missing admin rights: pin messages" in reply["text"]


async def test_disable_command_falls_back_to_dm_when_ephemeral_fails(services, wired, bot):
    enable(services)
    bot.fail_ephemeral = True
    await wired.access.on_disable_command(
        update(message(22, "/disable", sender=OWNER, command=True)), context(bot))
    assert services.chats.get(GROUP_ID).status == "disabled"
    assert bot.sent[-1]["chat_id"] == OWNER_ID
    assert "Disabled" in bot.sent[-1]["text"]


class FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.answers = []
        self.edited = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None):
        self.edited = text


async def test_approve_button(services, wired, bot):
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    bot.member = fakes.member_update("administrator", can_pin=True).my_chat_member.new_chat_member
    query = FakeQuery(f"access:approve:{GROUP_ID}", OWNER)
    await wired.access.on_callback(SimpleNamespace(callback_query=query), context(bot))
    chat = services.chats.get(GROUP_ID)
    assert chat.enabled and chat.can_pin
    assert query.edited == "✅ Enabled <b>BBQ crew</b>."


async def test_leave_button_and_non_owner(services, wired, bot):
    services.chats.upsert_seen(GROUP_ID)
    stranger = FakeQuery(f"access:leave:{GROUP_ID}", ALICE)
    await wired.access.on_callback(SimpleNamespace(callback_query=stranger), context(bot))
    assert bot.left == [] and stranger.answers == ["Only the bot's owner can do that."]

    query = FakeQuery(f"access:leave:{GROUP_ID}", OWNER)
    await wired.access.on_callback(SimpleNamespace(callback_query=query), context(bot))
    assert bot.left == [GROUP_ID]
    chat = services.chats.get(GROUP_ID)
    assert chat.status == "disabled" and chat.membership == "left"


async def test_private_messages_only_answer_the_owner(services, wired, bot):
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    dm = Chat(7, "private")
    await wired.access.on_private_message(update(message(1, "hi", chat=dm, bot=bot)), context(bot))
    assert bot.sent == []
    owner_dm = Chat(OWNER_ID, "private")
    await wired.access.on_private_message(
        update(message(2, "/start", sender=OWNER, chat=owner_dm, bot=bot)), context(bot))
    assert "Pending: 1" in bot.sent[-1]["text"]
    assert bot.sent[-1]["reply_markup"] is not None


# ---------------------------------------------------------------- commands

def test_parse_alias_args():
    assert parse_alias_args(["@bob", "Big", "B"]) == ("@bob", "Big B")
    assert parse_alias_args(["bob", "x"]) is None
    assert parse_alias_args(["@bob"]) is None
    assert parse_alias_args(None) is None


async def test_alias_commands_only_in_enabled_groups(services, bot):
    commands = GroupCommands(services)
    services.members.upsert_live(GROUP_ID, 8, "Bob", "bobby")
    alias_update = update(message(1, "/alias @bobby Big B", command=True))
    await commands.alias(alias_update, context(bot, ["@bobby", "Big", "B"]))
    assert bot.sent == []  # unknown chat: silent

    enable(services)
    await commands.alias(alias_update, context(bot, ["@bobby", "Big", "B"]))
    assert services.members.aliases(GROUP_ID, 8) == ["Big B"]
    await commands.alias(alias_update, context(bot, ["@nobody", "x"]))
    assert "I don't know @nobody yet" in bot.sent[-1]["text"]
    await commands.removealias(alias_update, context(bot, ["@bobby", "Big", "B"]))
    assert services.members.aliases(GROUP_ID, 8) == []
    await commands.group_info(alias_update, context(bot))
    assert "Stored messages: 0 live, 0 imported" in bot.sent[-1]["text"]
    assert "- Bob (@bobby)" in bot.sent[-1]["text"]


# --------------------------------------------------------------- responder

class FakeLLM:
    def __init__(self, text="[REPLY] Saturday, believe it!", error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.in_flight = self.waiting = 0

    async def chat(self, messages, *, reasoning=None, max_tokens=None, **kwargs):
        self.calls.append({"messages": messages, "reasoning": reasoning, **kwargs})
        if self.error:
            raise self.error
        return ChatResult(text=self.text, reasoning=None, model="m", latency_ms=5,
                          usage=None, finish_reason="stop")


def test_is_trigger(services):
    identity = services.status.bot
    assert is_trigger(message(1, "hey @Naruto_Bot"), identity)
    assert is_trigger(message(2, "sure", reply_to=message(1, "x", sender=fakes.BOT_USER)), identity)
    assert not is_trigger(message(3, "hey everyone"), identity)
    assert not is_trigger(message(4, "re", reply_to=message(1, "x", sender=BOB)), identity)
    # Seen live: pinning the board produced a service message pointing at
    # the bot's message, which looked like a reply to the bot.
    board = message(90, "📌 Board", sender=fakes.BOT_USER)
    pinned = message(91, None, sender=fakes.BOT_USER, reply_to=board, pinned_message=board)
    assert not is_trigger(pinned, identity)
    sticker = message(5, None, reply_to=board,
                      sticker=Sticker("s", "su", 512, 512, False, False, "regular", emoji="😂"))
    assert is_trigger(sticker, identity)  # a reply with media still counts


async def run_message(wired, bot, msg):
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.on_message(update(msg), context(bot))


async def test_mention_gets_threaded_reply_and_is_recorded(services, wired, bot):
    enable(services)
    llm = FakeLLM()
    services.llm = llm
    await run_message(wired, bot, message(10, "when's the bbq?", sender=BOB))
    await run_message(wired, bot, message(11, "@naruto_bot when is it?"))

    assert len(llm.calls) == 1
    assert llm.calls[0]["reasoning"] is True  # banter thinks briefly (Settings → reasoning)
    request = llm.calls[0]["messages"]
    assert request[-1]["content"].endswith("when is it?")
    assert "@naruto_bot" not in request[-1]["content"]
    reply = bot.sent[-1]
    assert reply["text"] == "Saturday, believe it!"
    assert reply["reply_parameters"].message_id == 11
    assert bot.actions and bot.actions[0][1] == "typing"
    stored = services.messages.get_live(GROUP_ID, 901)  # FakeBot numbers from 901
    assert stored.from_bot and stored.text == "Saturday, believe it!"


async def test_plain_group_chatter_and_disabled_chats_get_no_reply(services, wired, bot):
    llm = FakeLLM()
    services.llm = llm
    await run_message(wired, bot, message(10, "@naruto_bot hello?"))  # pending chat
    enable(services)
    await run_message(wired, bot, message(11, "just chatting"))
    await run_message(wired, bot, fakes.photo_message(12))
    assert llm.calls == []


async def test_model_failure_sends_apology(services, wired, bot):
    enable(services)
    services.llm = FakeLLM(error=LLMError("APIConnectionError"))
    await run_message(wired, bot, message(10, "@naruto_bot hi"))
    assert bot.sent[-1]["text"] == FAILURE_TEXT
    assert bot.sent[-1]["reply_parameters"] is None


async def test_empty_model_answer_sends_nothing(services, wired, bot):
    enable(services)
    services.llm = FakeLLM(text="   ")
    await run_message(wired, bot, message(10, "@naruto_bot hi"))
    assert bot.sent == []


async def test_markdown_failure_falls_back_to_plain_text(services, wired, bot):
    enable(services)
    services.llm = FakeLLM(text="*unbalanced")
    bot.fail_markdown = True
    await run_message(wired, bot, message(10, "@naruto_bot hi"))
    assert bot.sent[-1]["parse_mode"] is None and bot.sent[-1]["text"] == "*unbalanced"


async def test_images_are_downloaded_on_demand(services, wired, bot, monkeypatch):
    enable(services)
    llm = FakeLLM(text="A cat.")
    services.llm = llm
    calls = []

    async def fake_extract(msg, max_bytes):
        calls.append((msg.message_id, max_bytes))
        return [{"kind": "photo", "mime_type": "image/jpeg", "base64": "IMG", "width": 1,
                 "height": 1}], None

    monkeypatch.setattr("naruto.media.extract_attachments", fake_extract)
    photo = fakes.photo_message(10, sender=BOB)
    await run_message(wired, bot, photo)
    await run_message(wired, bot, message(11, "@naruto_bot what is this?", reply_to=photo))

    assert calls == [(10, 20 * 1024 * 1024)]
    parts = llm.calls[0]["messages"][-1]["content"]
    assert parts[-1]["image_url"]["url"] == "data:image/jpeg;base64,IMG"
    assert "which the current request replies to" in parts[-2]["text"]

    services.settings.set("media.enabled", False, actor="test")
    await run_message(wired, bot, message(12, "@naruto_bot and this?", reply_to=photo))
    assert isinstance(llm.calls[-1]["messages"][-1]["content"], str)


# ----------------------------------------------------------------- sending

def test_split_message_prefers_paragraphs():
    text = ("a" * 30 + "\n\n") * 5
    chunks = split_message(text, limit=70)
    assert all(len(c) <= 70 for c in chunks)
    assert "".join(chunks).replace("\n", "") == "a" * 150
    assert split_message("short") == ["short"]
    assert split_message("x" * 10, limit=4) == ["xxxx", "xxxx", "xx"]


async def test_send_text_threads_only_first_chunk():
    bot = FakeBot()
    sent = await send_text(bot, GROUP_ID, "one two three", reply_to=5)
    assert len(sent) == 1 and bot.sent[0]["reply_parameters"].message_id == 5
    assert bot.sent[0]["reply_parameters"].allow_sending_without_reply


async def test_send_text_follows_group_upgrade():
    bot = FakeBot()
    original = bot.send_message
    migrated = []

    async def send(chat_id, text, **kwargs):
        if chat_id == GROUP_ID:
            raise ChatMigrated(SUPER_ID)
        return await original(chat_id, text, **kwargs)

    bot.send_message = send
    await send_text(bot, GROUP_ID, "hi", reply_to=3,
                    on_migrated=lambda old, new: migrated.append((old, new)))
    assert migrated == [(GROUP_ID, SUPER_ID)]
    assert bot.sent[0]["chat_id"] == SUPER_ID and bot.sent[0]["reply_parameters"] is None


async def test_send_ephemeral_payload():
    bot = FakeBot()
    await send_ephemeral(bot, GROUP_ID, OWNER_ID, "psst", callback_query_id="q1")
    assert bot.sent[0]["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": OWNER_ID, "callback_query_id": "q1"},
    }


# -------------------------------------------------------- update sanitizing

def test_sanitize_updates_patches_missing_message_id_and_skips_garbage():
    raw = [
        {"update_id": 1, "message": {"date": fakes.T0, "chat": {"id": GROUP_ID, "type": "group"},
                                     "text": "/enable", "ephemeral_message_id": 9}},
        {"update_id": 2, "message": {"message_id": 3, "date": "not a date", "chat": None}},
        {"no_update_id": True},
    ]
    cleaned = sanitize_updates(raw)
    assert [u["update_id"] for u in cleaned] == [1, 2]
    assert cleaned[0]["message"]["message_id"] == 0
    assert cleaned[1] == {"update_id": 2}
    assert sanitize_updates(True) is True


# -------------------------------------------------------------- agent runs

async def test_answer_is_traced(services, wired, bot):
    enable(services)
    services.llm = FakeLLM()
    await run_message(wired, bot, message(10, "@naruto_bot hi"))
    runs, total = services.runs.recent()
    run = runs[0]
    assert total == 1 and run.status == "ok" and run.skill == "banter"
    assert run.response == "Saturday, believe it!" and run.reply_message_ids == [901]
    assert run.trigger_message_id == 10 and run.user_id == 7
    assert run.prompt[-1]["content"].endswith("hi") and run.prompt_tokens > 0
    assert run.finished_at is not None


async def test_failed_and_empty_answers_are_traced(services, wired, bot):
    enable(services)
    services.llm = FakeLLM(error=LLMError("APITimeoutError"))
    await run_message(wired, bot, message(10, "@naruto_bot hi"))
    services.llm = FakeLLM(text="")
    await run_message(wired, bot, message(11, "@naruto_bot hello"))
    runs, _ = services.runs.recent()
    assert [r.status for r in runs] == ["empty", "error"]
    assert runs[1].error == "APITimeoutError"


async def test_traced_prompt_never_contains_image_data(services, wired, bot, monkeypatch):
    enable(services)
    services.llm = FakeLLM(text="A cat.")

    async def fake_extract(msg, max_bytes):
        return [{"kind": "photo", "mime_type": "image/jpeg", "base64": "SECRETPIXELS", "width": 1,
                 "height": 1}], None

    monkeypatch.setattr("naruto.media.extract_attachments", fake_extract)
    await run_message(wired, bot, fakes.photo_message(10, caption="@naruto_bot what's this?"))
    run = services.runs.recent()[0][0]
    assert run.image_count == 1 and "SECRETPIXELS" not in str(run.prompt)


async def test_clearaliases_is_owner_only(services, bot):
    commands = GroupCommands(services)
    enable(services)
    services.members.upsert_live(GROUP_ID, 8, "Bob", "bobby")
    services.members.add_alias(GROUP_ID, 8, "Big B")
    await commands.clearaliases(update(message(1, "/clearaliases", command=True)), context(bot))
    assert "Only the bot's owner" in bot.sent[-1]["text"]
    assert services.members.aliases(GROUP_ID, 8) == ["Big B"]
    await commands.clearaliases(update(message(2, "/clearaliases", sender=OWNER, command=True)),
                                context(bot))
    assert "Cleared 1 aliases" in bot.sent[-1]["text"]
    assert services.members.aliases(GROUP_ID, 8) == []
