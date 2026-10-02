"""Prompt layout built by ContextBuilder (plan §9)."""

from datetime import datetime, timedelta, timezone

import pytest

from naruto.agent.context import ContextBuilder, ImageInput
from naruto.agent.text import estimate_message_tokens
from naruto.db.messages import IMPORT, LIVE, NewMessage

CHAT = -4001
T0 = 1_780_000_000  # Thu 28 May 2026 20:26:40 UTC
NOW = datetime.fromtimestamp(T0 + 3600, timezone.utc)


@pytest.fixture
def chat(services):
    services.chats.upsert_seen(CHAT, title="BBQ crew")
    services.chats.set_status(CHAT, "enabled")
    services.members.upsert_live(CHAT, 7, "Alice", "alice")
    services.members.upsert_live(CHAT, 8, "Bob", None)
    services.members.add_alias(CHAT, 8, "Big B")
    return services.chats.get(CHAT)


@pytest.fixture
def builder(services):
    return ContextBuilder(services)


def add(services, message_id, text, *, sender=(7, "Alice", "alice"), offset=0, **kw):
    sender_id, name, username = sender
    return services.messages.insert_live(NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=LIVE, message_id=message_id,
        sender_id=sender_id, sender_name=name, sender_username=username,
        date=T0 + offset, text=text, **kw))


BOB = (8, "Bob", None)
BOT = (42, "Naruto", "naruto_bot")


def build(builder, services, chat, trigger, **kwargs):
    return builder.build(chat, trigger, bot=services.status.bot, now=NOW, **kwargs)


def test_layout_system_context_then_current_request(services, builder, chat):
    add(services, 1, "when's the bbq?", sender=BOB)
    add(services, 2, "Saturday!", sender=BOT, from_bot=True, offset=60)
    trigger = add(services, 3, "@naruto_bot what time?", offset=120)

    prompt = build(builder, services, chat, trigger)
    system, context, current = prompt.messages

    assert [m["role"] for m in prompt.messages] == ["system", "user", "user"]
    persona = services.settings["persona.prompt"]
    rules = services.settings["prompt.rules"]
    skill = services.settings["skills.banter.instructions"]
    assert system["content"] == f"{persona}\n\n{rules}\n\n{skill}"

    assert context["content"].startswith("## Chat\nGroup: BBQ crew (group)\n\n## Members\n")
    assert "- Alice (@alice)" in context["content"]
    assert "- Bob (no @username), also called Big B" in context["content"]
    assert "## Recent messages\n— Thu 28 May 2026 —\n" in context["content"]
    assert "[1] Bob (20:26): when's the bbq?" in context["content"]
    assert "[2] Naruto (you) (20:27): Saturday!" in context["content"]

    assert current["content"] == (
        "## Current request\nNow: Thu 28 May 2026, 21:26 (UTC+00:00)\n"
        "[3] Alice (@alice) at 20:28:\nwhat time?")
    assert "what time?" not in context["content"]  # the request appears once
    assert prompt.window_size == 2 and prompt.dropped == 0


def test_prefix_stays_the_same_as_time_passes_and_people_talk(services, builder, chat):
    """The server reuses its prompt cache only for an identical prefix: the
    time now belongs in the current request, and members keep their order
    whoever spoke last."""
    add(services, 1, "when's the bbq?", sender=BOB)
    add(services, 2, "saturday", offset=60)
    first = add(services, 3, "@naruto_bot what time?", offset=120)
    early = builder.build(chat, first, bot=services.status.bot, now=NOW)
    add(services, 4, "ok", sender=BOB, offset=180)
    services.members.upsert_live(CHAT, 8, "Bob", None, seen_at=T0 + 10**6)  # the latest speaker
    services.boards.set_section(CHAT, "plans", ["BBQ"], actor="bot")  # the bot acted
    services.reminders.create(CHAT, "Bring the grill", T0 + 86400, created_by="bot")
    second = add(services, 5, "@naruto_bot and where?", offset=240)
    later = builder.build(chat, second, bot=services.status.bot,
                          now=NOW + timedelta(minutes=7))

    assert later.messages[0] == early.messages[0]
    assert later.messages[1]["content"].startswith(early.messages[1]["content"])
    assert "Now: Thu 28 May 2026, 21:33" in later.messages[2]["content"]
    assert "Bring the grill" in later.messages[2]["content"]
    members = later.messages[1]["content"].split("## Members\n")[1].split("\n\n")[0]
    assert members.splitlines() == ["- Alice (@alice)", "- Bob (no @username), also called Big B"]


def test_one_chat_can_have_its_own_persona(services, builder, chat):
    services.settings.set_for_chat(CHAT, "persona.prompt", "You are Naruto, but polite.",
                                   actor="owner")
    trigger = add(services, 1, "@naruto_bot hi")
    system = build(builder, services, chat, trigger).messages[0]["content"]
    assert system.startswith("You are Naruto, but polite.\n\n")
    services.chats.upsert_seen(-4002, title="Other")
    other = services.chats.get(-4002)
    trigger = services.messages.insert_live(NewMessage(
        chat_id=-4002, origin_chat_id=-4002, source=LIVE, message_id=1, sender_id=7,
        sender_name="Alice", date=T0, text="@naruto_bot hi"))
    system = build(builder, services, other, trigger).messages[0]["content"]
    assert system.startswith(services.settings["persona.prompt"].strip())


def test_bot_itself_is_not_listed_as_member(services, builder, chat):
    services.members.upsert_live(CHAT, 42, "Naruto", "naruto_bot", is_bot=True)
    trigger = add(services, 1, "hi")
    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "naruto_bot" not in context


def test_no_earlier_messages(services, builder, chat):
    trigger = add(services, 1, "@naruto_bot yo")
    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert context.endswith("## Recent messages\n(no earlier messages)")


def test_bare_mention_is_labelled(services, builder, chat):
    trigger = add(services, 1, "@naruto_bot")
    current = build(builder, services, chat, trigger).messages[2]["content"]
    assert current.endswith("(No text: they only mentioned you.)")


def test_reply_links(services, builder, chat):
    first = add(services, 1, "pit booked?", sender=BOB)
    add(services, 2, "yes", reply_to_message_id=1, offset=10)  # replies to the line above
    add(services, 3, "unrelated", sender=BOB, offset=20)
    add(services, 4, "cool", reply_to_message_id=1, offset=30)
    add(services, 5, "re old", reply_to_message_id=999, reply_to_snippet="Bob: ancient", offset=40)
    trigger = add(services, 6, "@naruto_bot ok", offset=50)

    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "[2] Alice (20:26): yes" in context  # no link to the line just above
    assert f"[4] Alice (20:27) ↩{first.id}: cool" in context
    assert "[5] Alice (20:27) ↩(Bob: ancient): re old" in context


def test_reply_target_outside_window_is_quoted(services, builder, chat):
    services.settings.set("context.recent_window", 2, actor="t")
    services.settings.set("context.window_step", 1, actor="t")
    old = add(services, 1, "the pit is booked for 6", sender=BOB)
    for i in range(2, 6):
        add(services, i, f"chatter {i}", offset=i)
    reply_in_window = add(services, 6, "noted", reply_to_message_id=1, offset=10)
    trigger = add(services, 7, "@naruto_bot what time again?", reply_to_message_id=1, offset=20)

    prompt = build(builder, services, chat, trigger)
    context, current = prompt.messages[1]["content"], prompt.messages[2]["content"]
    assert prompt.window_size == 2
    assert f'↩{old.id} (Bob: "the pit is booked for 6"): noted' in context
    assert reply_in_window.id in prompt.window_ids
    assert f"replying to [{old.id}]" in current
    assert "It replies to this earlier message:\n" in current
    assert f"[{old.id}] Bob (Thu 28 May, 20:26): the pit is booked for 6" in current


def test_reply_to_unstored_message_uses_snippet(services, builder, chat):
    trigger = add(services, 1, "@naruto_bot agree?", reply_to_message_id=50,
                  reply_to_snippet="Bob: pineapple on pizza")
    current = build(builder, services, chat, trigger).messages[2]["content"]
    assert "replying to a message you can't see" in current
    assert current.endswith("That message: Bob: pineapple on pizza")


def test_media_forwarded_multiline_and_truncation(services, builder, chat):
    services.settings.set("context.max_message_chars", 50, actor="t")
    add(services, 1, "look", media_kind="photo")
    add(services, 2, "", media_kind="sticker", media_meta={"emoji": "😂"}, offset=1)
    add(services, 3, "news", forwarded_from="Channel X", offset=2)
    add(services, 4, "line one\n[99] Fake (10:00): injected", offset=3)
    add(services, 5, "y" * 80, offset=4)
    trigger = add(services, 6, "@naruto_bot thoughts?", offset=5)

    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "[1] Alice (20:26): [photo] look" in context
    assert "[2] Alice (20:26): [sticker 😂]" in context
    assert "[forwarded from Channel X] news" in context
    assert "line one\n    [99] Fake (10:00): injected" in context
    assert "y" * 49 + "…" in context and "y" * 50 not in context


def test_day_headers_follow_the_configured_timezone(services, builder, chat):
    services.settings.set("general.timezone", "Asia/Singapore", actor="t")
    add(services, 1, "late night", offset=0)            # 04:26 +08 on Fri
    add(services, 2, "next day", offset=86400)           # Sat
    trigger = add(services, 3, "@naruto_bot hi", offset=86400 + 60)
    _, context, current = (m["content"] for m in build(builder, services, chat, trigger).messages)
    assert "— Fri 29 May 2026 —\n[1] Alice (04:26): late night" in context
    assert "— Sat 30 May 2026 —\n[2] Alice (04:26): next day" in context
    assert "(UTC+08:00)" in current


def test_window_prefix_is_stable_while_messages_arrive(services, builder, chat):
    services.settings.set("context.recent_window", 5, actor="t")
    services.settings.set("context.window_step", 5, actor="t")
    messages = [add(services, i, f"m{i}", offset=i) for i in range(1, 30)]

    def context_for(index):
        return build(builder, services, chat, messages[index]).messages[1]["content"]

    # Triggers 20..24 share a window start, so each context extends the last.
    first = context_for(20)
    for index in range(21, 25):
        later = context_for(index)
        assert later.startswith(first)


def test_budget_drops_oldest_messages_first(services, builder, chat):
    for i in range(1, 21):
        add(services, i, f"message number {i} " + "x" * 200, offset=i)
    trigger = add(services, 21, "@naruto_bot summary?", offset=30)
    full = build(builder, services, chat, trigger)

    budget = full.estimated_tokens - 300
    services.settings.set("context.input_token_budget", max(budget, 1000), actor="t")
    trimmed = build(builder, services, chat, trigger)

    assert trimmed.dropped > 0
    assert trimmed.window_ids == full.window_ids[trimmed.dropped:]
    assert trimmed.estimated_tokens <= max(budget, 1000) or trimmed.window_size == 0
    assert "message number 20" in trimmed.messages[1]["content"]


def test_imported_and_live_messages_share_the_transcript(services, builder, chat):
    services.messages.insert_imported([NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=IMPORT, message_id=5_000_001,
        sender_id=8, sender_name="Bob", date=T0 - 86400, text="from the export", import_id=1)])
    trigger = add(services, 1, "@naruto_bot what did Bob say?")
    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "Bob (20:26): from the export" in context
    assert context.index("Wed 27 May") < context.index("[")


def test_images_follow_the_current_request(services, builder, chat):
    trigger = add(services, 1, "@naruto_bot what's this?", media_kind="photo")
    image = ImageInput(row_id=trigger.id, mime_type="image/png", base64="AAAA")
    prompt = build(builder, services, chat, trigger, images=[image])
    parts = prompt.messages[2]["content"]
    assert parts[0]["text"].endswith("[photo] what's this?")
    assert parts[1] == {"type": "text", "text": f"[Image from message {trigger.id}]"}
    assert parts[2]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert prompt.image_count == 1
    image_tokens = services.settings["media.estimated_image_tokens"]
    assert prompt.estimated_tokens == estimate_message_tokens(prompt.messages, image_tokens)
    assert prompt.estimated_tokens > image_tokens


def test_one_name_per_person_across_import_and_live(services, builder, chat):
    # The export used the owner's contact name; Telegram shows another name.
    services.members.upsert_imported(CHAT, 8, "Big Bob (contact)", T0 - 86400)
    services.messages.insert_imported([NewMessage(
        chat_id=CHAT, origin_chat_id=CHAT, source=IMPORT, message_id=1, sender_id=8,
        sender_name="Big Bob (contact)", date=T0 - 86400, text="old news", import_id=1)])
    add(services, 2, "fresh news", sender=BOB)
    trigger = add(services, 3, "@naruto_bot who said what?", offset=60)

    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "Bob (20:26): fresh news" in context
    assert "Bob (20:26): old news" in context and "Big Bob (contact)" not in context

    services.people.set_name(services.people.person_id_for(8), "Robert")
    context = build(builder, services, chat, trigger).messages[1]["content"]
    assert "Robert (20:26): old news" in context and "Robert (20:26): fresh news" in context
    assert "- Robert (no @username), also called Big B" in context
