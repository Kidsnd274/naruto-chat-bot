from types import SimpleNamespace
from unittest.mock import AsyncMock
import importlib
import sys

import pytest


@pytest.fixture
def bot_module(initialized_config):
    pytest.importorskip("telegram")
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


class FakeHistory:
    def __init__(self):
        self.records = []

    def add_user_message(self, chat_id, sender_name, sender_message, **kwargs):
        self.records.append({
            "role": "user",
            "content": f"{sender_name}: {sender_message}",
            "sender_name": sender_name,
            "telegram_message_id": kwargs["telegram_message_id"],
            "reply_to_message_id": kwargs["reply_to_message_id"],
            "attachments": kwargs["attachments"],
        })

    def add_assistant_message(self, chat_id, response):
        self.records.append({"role": "assistant", "content": response})

    def get_raw_chat_history(self, chat_id):
        return list(self.records)


class FakeMetadata:
    def update_user(self, *args):
        pass

    def set_chat_name(self, *args):
        pass

    def get_chat_info(self, chat_id):
        return {"chat_name": "Test chat", "users": []}


def make_message(message_id, *, text=None, caption=None, photo=False):
    return SimpleNamespace(
        id=message_id,
        message_id=message_id,
        chat_id=100,
        text=text,
        caption=caption,
        photo=[object()] if photo else [],
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        document=None,
        reply_to_message=None,
    )


def make_update(message, chat_type):
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(
            id=100,
            type=chat_type,
            title="Test chat" if chat_type != "private" else None,
            first_name="Alice",
        ),
        effective_user=SimpleNamespace(
            id=7,
            username="alice",
            full_name="Alice",
        ),
    )


@pytest.fixture
def media_bot(bot_module, monkeypatch):
    history = FakeHistory()
    telegram_bot = SimpleNamespace(
        id=42,
        username="naruto_bot",
        send_chat_action=AsyncMock(),
        send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=500),
        ),
    )
    context = SimpleNamespace(bot=telegram_bot)

    bot_module._last_seen_message_id.clear()
    bot_module.config.whitelist.enabled = False
    bot_module.config.media.enabled = True
    bot_module.config.media.max_bytes = 1_000_000
    monkeypatch.setattr(bot_module, "chat_history", history)
    monkeypatch.setattr(bot_module, "chat_metadata", FakeMetadata())

    attachment = {
        "kind": "photo",
        "mime_type": "image/jpeg",
        "base64": "PHOTO_SENTINEL",
        "width": 20,
        "height": 10,
    }
    extract = AsyncMock(return_value=([attachment], None))
    monkeypatch.setattr(bot_module.media, "extract_attachments", extract)

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="nice"))],
    )
    chat = AsyncMock(return_value=response)
    monkeypatch.setattr(bot_module.ai_client, "chat", chat)
    return bot_module, history, context, chat


async def test_bare_group_photo_is_stored_then_sent_on_later_trigger(media_bot):
    bot, history, context, chat = media_bot

    await bot.respond(make_update(make_message(10, photo=True), "group"), context)

    chat.assert_not_awaited()
    assert history.records[0]["content"] == "Alice: [sent a photo]"
    assert history.records[0]["attachments"][0]["base64"] == "PHOTO_SENTINEL"

    await bot.respond(
        make_update(make_message(11, text="@naruto_bot what is that?"), "group"),
        context,
    )

    chat.assert_awaited_once()
    model_messages = chat.await_args.args[0]
    user_parts = model_messages[1]["content"]
    image_index = next(i for i, part in enumerate(user_parts) if part["type"] == "image_url")
    assert "Telegram message 10 from Alice" in user_parts[image_index - 1]["text"]
    assert user_parts[image_index]["image_url"]["url"].endswith("PHOTO_SENTINEL")
    assert "Telegram message 11 from Alice" in user_parts[-1]["text"]


async def test_private_media_triggers_exactly_one_multimodal_call(media_bot):
    bot, history, context, chat = media_bot

    await bot.respond(make_update(make_message(20, photo=True), "private"), context)

    chat.assert_awaited_once()
    model_messages = chat.await_args.args[0]
    assert any(
        part.get("type") == "image_url"
        for part in model_messages[1]["content"]
    )
    assert history.records[0]["telegram_message_id"] == 20
