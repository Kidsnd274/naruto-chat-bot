"""Sending messages: Markdown with a plain-text fallback, long-message
splitting, ephemeral replies and the typing indicator."""

import asyncio
from contextlib import asynccontextmanager
import logging

from telegram import Message, ReplyParameters
from telegram.constants import ChatAction
from telegram.error import BadRequest, ChatMigrated, TelegramError

logger = logging.getLogger(__name__)

# Telegram allows 4096 characters; leave room for Markdown entities that the
# plain-text fallback would otherwise count.
CHUNK_LIMIT = 4000
TYPING_INTERVAL_SECONDS = 4.5


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split on paragraph, then line, then word boundaries."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        cut = -1
        for separator in ("\n\n", "\n", " "):
            cut = text.rfind(separator, 0, limit)
            if cut > limit // 2:
                break
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def _send_one(bot, chat_id: int, text: str, reply_to: int | None) -> Message:
    reply = (ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
             if reply_to is not None else None)
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown",
                                      reply_parameters=reply)
    except BadRequest as exc:
        # Usually "can't parse entities": the model wrote unbalanced Markdown.
        logger.warning("Markdown send failed (%s); sending plain text.", exc.message)
        return await bot.send_message(chat_id=chat_id, text=text, reply_parameters=reply)


async def send_text(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    on_migrated=None,
) -> list[Message]:
    """Send ``text`` (split if long). Only the first chunk is a threaded
    reply. If the group was upgraded to a supergroup, ``on_migrated(old, new)``
    is called and the send is retried once on the new ID."""
    sent = []
    for index, chunk in enumerate(split_message(text)):
        target_reply = reply_to if index == 0 else None
        try:
            sent.append(await _send_one(bot, chat_id, chunk, target_reply))
        except ChatMigrated as exc:
            if on_migrated is not None:
                on_migrated(chat_id, exc.new_chat_id)
            chat_id = exc.new_chat_id
            sent.append(await _send_one(bot, chat_id, chunk, None))
    return sent


async def send_ephemeral(
    bot,
    chat_id: int,
    user_id: int,
    text: str,
    *,
    reply_to_ephemeral_id: int | None = None,
    callback_query_id: str | None = None,
) -> Message:
    """Send a message only ``user_id`` can see (Bot API 10.3). Replying to
    an ephemeral command or a button press works within 15 seconds for any
    bot; an admin bot can send ephemeral messages at any time."""
    parameters: dict = {"receiver_user_id": user_id}
    if callback_query_id:
        parameters["callback_query_id"] = callback_query_id
    api_kwargs: dict = {"ephemeral_message_parameters": parameters}
    if reply_to_ephemeral_id is not None:
        api_kwargs["reply_parameters"] = {"ephemeral_message_id": reply_to_ephemeral_id}
    return await bot.send_message(chat_id=chat_id, text=text, api_kwargs=api_kwargs)


@asynccontextmanager
async def typing(bot, chat_id: int):
    """Keep the typing indicator on while the block runs. The first action
    is sent right away; Telegram shows it for about five seconds, so a
    background task repeats it."""
    async def send():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError as exc:
            logger.debug("Typing indicator failed: %s", exc)

    async def repeat():
        while True:
            await asyncio.sleep(TYPING_INTERVAL_SECONDS)
            await send()

    await send()
    task = asyncio.create_task(repeat())
    try:
        yield
    finally:
        task.cancel()
