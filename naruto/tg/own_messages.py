"""Deleting the bot's own messages: the only place that calls Telegram's
deleteMessage (plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md §5).

Every deletion targets a message this bot account sent in the chat at hand,
never a member's or another bot's, whatever admin rights the bot has.
Telegram lets a bot delete its own messages for 48 hours without any admin
right. The evidence comes from Telegram itself: the transcript row that
record_sent stored from a send response, or the Message a send just returned
(the progress placeholder); never from names, quoted text or imports. The
pinned board is never deleted.

A batch is all or nothing at the checks: if any target fails them, no delete
call is made. Then each message is deleted on its own and reported.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import warnings

from telegram import Message
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.warnings import PTBDeprecationWarning

from naruto.db.chats import Chat
from naruto.db.messages import StoredMessage
from naruto.services import Services

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 48 * 3600  # Telegram's limit for deleting a message
MAX_RETRY_WAIT_SECONDS = 30  # a longer flood wait fails instead

# What happened to each target.
DELETED = "deleted"
GONE = "already gone"
FAILED = "failed"
HELD = "not tried"  # another target in the batch was refused
# Refusals: no delete call is made for the batch.
TOO_OLD = "too old"
NOT_OURS = "not mine"
PROTECTED = "protected"
NOT_FOUND = "not found"
OUT_OF_REACH = "out of reach"
REFUSED = (TOO_OLD, NOT_OURS, PROTECTED, NOT_FOUND, OUT_OF_REACH)


@dataclass
class Result:
    row_id: int | None  # the transcript [id]
    status: str
    detail: str = ""  # why, for the model and the log


def _refusal(services: Services, chat: Chat, row: StoredMessage | None,
             now: float) -> tuple[str, str] | None:
    """Why a transcript row may not be deleted, or None if it may."""
    bot = services.status.bot
    if row is None or row.chat_id != chat.chat_id:
        return NOT_FOUND, "no such message in this chat"
    if not row.is_live:
        return OUT_OF_REACH, "it comes from an imported history"
    if bot is None or not row.from_bot or row.sender_id != bot.id:
        return NOT_OURS, f"{row.sender_name} sent it, and only your own messages can be deleted"
    if row.origin_chat_id != chat.chat_id:
        return OUT_OF_REACH, "it is from before the group was upgraded"
    if _is_board(services, chat.chat_id, row.origin_chat_id, row.message_id):
        return PROTECTED, "it is the pinned board"
    if now - row.date >= MAX_AGE_SECONDS:
        return TOO_OLD, "Telegram only allows deleting messages for 48 hours"
    return None


def _is_board(services: Services, chat_id: int, message_chat_id: int, message_id: int) -> bool:
    board = services.boards.get(chat_id)
    return (board.message_id == message_id
            and (board.message_chat_id or chat_id) == message_chat_id)


async def delete_rows(services: Services, telegram, chat: Chat,
                      row_ids: list[int]) -> list[Result]:
    """Delete the transcript messages ``row_ids`` (the bot's own, in
    ``chat``). Every one is checked first; if any is refused, none is
    deleted (the others are reported as not tried)."""
    rows = services.messages.get_many(list(row_ids))
    now = services.time()
    checked = []
    for row_id in dict.fromkeys(row_ids):
        row = rows.get(row_id)
        refused = _refusal(services, chat, row, now)
        checked.append((row_id, row, refused))
    if any(refused for _, _, refused in checked):
        logger.info("Not deleting messages %s: %s", list(row_ids),
                    "; ".join(f"{row_id} {refused[0]}" for row_id, _, refused in checked
                              if refused), extra={"chat_id": chat.chat_id})
        return [Result(row_id, *refused) if refused else
                Result(row_id, HELD, "another message in the request was refused")
                for row_id, _, refused in checked]
    results = []
    for row_id, row, _ in checked:
        if row.deleted_at is not None:
            results.append(Result(row_id, GONE))
            continue
        status, detail = await _delete(telegram, row.origin_chat_id, row.message_id)
        if status in (DELETED, GONE):
            services.messages.mark_deleted(row.id)
        logger.info("Deleting own message %s (row %s): %s %s", row.message_id, row.id, status,
                    detail, extra={"chat_id": chat.chat_id})
        results.append(Result(row_id, status, detail))
    return results


async def delete_sent(services: Services, telegram, message: Message, chat_id: int) -> bool:
    """Delete a message the bot just sent in ``chat_id`` (the progress
    placeholder), checked against the Message Telegram returned for it: the
    bot sent it, in that chat, and it isn't the board. True once it's gone."""
    bot = services.status.bot
    sender = message.from_user
    if bot is None or sender is None or sender.id != bot.id or message.chat_id != chat_id:
        logger.error("Refusing to delete message %s in chat %s: not the bot's own message.",
                     message.message_id, message.chat_id)
        return False
    if _is_board(services, services.chats.resolve(chat_id), message.chat_id,
                 message.message_id):
        logger.error("Refusing to delete message %s: it is the pinned board.", message.message_id)
        return False
    status, detail = await _delete(telegram, message.chat_id, message.message_id)
    if status == FAILED:
        logger.info("Couldn't delete message %s: %s", message.message_id, detail,
                    extra={"chat_id": chat_id})
    return status != FAILED


async def _delete(telegram, chat_id: int, message_id: int) -> tuple[str, str]:
    """One deleteMessage call, waiting once if Telegram asks to."""
    for attempt in range(2):
        try:
            await telegram.delete_message(chat_id=chat_id, message_id=message_id)
            return DELETED, ""
        except RetryAfter as exc:
            with warnings.catch_warnings():  # an int until PTB switches it to a timedelta
                warnings.simplefilter("ignore", PTBDeprecationWarning)
                wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
            if attempt or seconds > MAX_RETRY_WAIT_SECONDS:
                return FAILED, f"Telegram asked to wait {seconds:.0f} seconds"
            await asyncio.sleep(seconds)
        except BadRequest as exc:
            if "message to delete not found" in str(exc).lower():
                return GONE, ""
            return FAILED, f"Telegram refused: {exc}"
        except TelegramError as exc:
            return FAILED, f"Telegram refused: {exc}"
    return FAILED, ""
