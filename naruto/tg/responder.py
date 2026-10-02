"""Answers messages that mention the bot or reply to it, in enabled groups.

The trigger message has already been stored by the recorder (handler group
-1), so the prompt is built from the database. Runs in one chat happen one
after another, each answering its own trigger; runs in different chats share
the model server through the LLM client's queue.
"""

import asyncio
from collections import defaultdict
import logging

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from naruto import media
from naruto.agent.context import ImageInput
from naruto.agent.runner import (
    CHAT_DISABLED_ERROR,
    FAILURE_TEXT,
    AgentRunner,
    RunOutcome,
    RunRequest,
)
from naruto.db.chats import Chat
from naruto.db.messages import StoredMessage
from naruto.services import BotIdentity, Services
from naruto.tg.progress import Progress
from naruto.tg.recorder import GROUP_TYPES, Recorder, has_content
from naruto.tg.sending import send_text, topic_of, typing

logger = logging.getLogger(__name__)

__all__ = ["FAILURE_TEXT", "Responder", "is_trigger"]

NOT_SENT_TEXT = "Sorry, I couldn't send my answer. Please try again."


def is_trigger(message, bot: BotIdentity) -> bool:
    """A mention of the bot in the text or caption, or a reply to one of its
    messages. Bare media without a mention is stored silently; service
    messages (e.g. "Naruto pinned a message", which points at the pinned
    bot message) never trigger."""
    if not has_content(message):
        return False
    text = (message.text or message.caption or "").lower()
    if bot.username and f"@{bot.username.lower()}" in text:
        return True
    reply = message.reply_to_message
    return (reply is not None and reply.from_user is not None
            and reply.from_user.id == bot.id)


class Responder:
    def __init__(self, services: Services, recorder: Recorder):
        self.services = services
        self.recorder = recorder
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        bot = self.services.status.bot
        if message is None or message.chat.type not in GROUP_TYPES:
            return
        if bot is None:
            logger.warning("Not answering: the bot's own identity is unknown (not logged in yet).")
            return
        chat = self.services.chats.get(message.chat_id)
        if chat is None or not chat.enabled or not is_trigger(message, bot):
            return
        trigger = self.services.messages.get_live(message.chat_id, message.message_id)
        if trigger is None:
            logger.warning("Trigger message %s was not recorded; skipping.", message.message_id)
            return
        logger.info("Answering message %s from user %s", message.message_id, trigger.sender_id)
        await self.respond(context.bot, chat, message, trigger, bot)

    async def respond(self, telegram_bot, chat: Chat, message: Message,
                      trigger: StoredMessage, bot: BotIdentity, *, skill: str = "banter",
                      since: int | None = None, note: str | None = None,
                      force_reply: bool = False) -> None:
        async with self._chat_locks[chat.chat_id]:
            # The chat may have been disabled while this waited its turn.
            current = self.enabled_chat(chat.chat_id)
            if current is None:
                logger.info("Not answering message %s: the chat was disabled meanwhile.",
                            message.message_id)
                return
            progress = Progress(
                self.services, telegram_bot, message.chat_id, message.message_id or None,
                skill=skill, thread_id=topic_of(message),
                after_seconds=self.services.settings.for_chat(current.chat_id)[
                    "behaviour.progress_after_seconds"])
            try:
                outcome = await self.run(telegram_bot, current, message, trigger, bot,
                                         skill=skill, since=since, note=note,
                                         force_reply=force_reply,
                                         on_skill=progress.skill_changed,
                                         on_stage=progress.stage)
            except Exception:
                await progress.stop()
                await self._crashed(current, progress)
                raise
            finally:
                await progress.stop()
            if not self.still_enabled(current, outcome):
                await progress.discard()
                return
            await self.deliver(telegram_bot, current, message, outcome, progress=progress)

    async def _crashed(self, chat: Chat, progress: Progress) -> None:
        """The run raised: a placeholder shows the failure rather than
        "Pulling the plan together…" forever."""
        if progress.message is None:
            return
        if self.enabled_chat(chat.chat_id) is None:
            await progress.discard()
            return
        shown = await progress.show(FAILURE_TEXT)
        if shown is not None:
            self.recorder.record_sent(shown.chat_id, shown)

    def enabled_chat(self, chat_id: int) -> Chat | None:
        """The chat as stored now, if the bot may still work there."""
        chat = self.services.chats.get(chat_id)
        return chat if chat is not None and chat.enabled else None

    def still_enabled(self, chat: Chat, outcome: RunOutcome) -> bool:
        """Checked after a run and before its answer is sent: the chat may
        have been disabled while the model was busy."""
        if self.enabled_chat(chat.chat_id) is not None:
            return True
        logger.info("Dropping the answer of run %s: the chat was disabled while it ran.",
                    outcome.run_id)
        if outcome.error != CHAT_DISABLED_ERROR:  # the run already says it stopped
            self.services.runs.update(outcome.run_id,
                                      error="Not sent: the chat was disabled while it ran.")
        return False

    async def run(self, telegram_bot, chat: Chat, message: Message, trigger: StoredMessage,
                  bot: BotIdentity, *, skill: str = "banter", since: int | None = None,
                  note: str | None = None, force_reply: bool = False,
                  show_typing: bool = True, on_skill=None, on_stage=None) -> RunOutcome:
        """One agent run, without sending the answer. Callers that don't use
        respond() hold chat_lock() around it."""
        images = await self._images(message, trigger) if trigger.id else []
        runner = AgentRunner(self.services, telegram_bot, record_sent=self.recorder.record_sent)
        request = RunRequest(chat=chat, trigger=trigger, bot=bot, skill=skill, images=images,
                             trigger_message_id=message.message_id or None, since=since,
                             note=note, force_reply=force_reply, on_skill=on_skill,
                             on_stage=on_stage)
        if not show_typing:
            return await runner.run(request)
        async with typing(telegram_bot, message.chat_id):
            return await runner.run(request)

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        return self._chat_locks[chat_id]

    async def deliver(self, telegram_bot, chat: Chat, message: Message,
                      outcome: RunOutcome, *, progress: Progress | None = None) -> list[Message]:
        """Send the run's answer. A progress placeholder is deleted once the
        result is in the chat; a failed run turns it into the error message,
        which stays (plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md §6)."""
        placeholder = progress is not None and progress.message is not None
        if placeholder and outcome.fallback and outcome.text:
            shown = await progress.show(outcome.text)
            if shown is not None:
                self.recorder.record_sent(shown.chat_id, shown)
                return [shown]
            # Couldn't edit it: the error comes anew, and the placeholder goes.
        if not outcome.text:
            if placeholder:
                await progress.discard()
            return []
        try:
            sent = await self._send(telegram_bot, chat, message, outcome.text,
                                    reply=outcome.threaded and not outcome.fallback)
        except TelegramError as exc:
            if not placeholder:
                raise
            logger.warning("Couldn't send the answer of run %s: %s", outcome.run_id, exc)
            self.services.runs.update(outcome.run_id,
                                      error=f"Not sent: {type(exc).__name__}: {exc}"[:500])
            shown = await progress.show(NOT_SENT_TEXT)
            if shown is not None:
                self.recorder.record_sent(shown.chat_id, shown)
            return []
        if placeholder:
            await progress.discard()
        if sent and not outcome.fallback:
            self.services.runs.update(outcome.run_id,
                                      reply_message_ids=[m.message_id for m in sent])
        return sent

    async def _send(self, telegram_bot, chat: Chat, message: Message, text: str,
                    *, reply: bool) -> list[Message]:
        sent = await send_text(
            telegram_bot, message.chat_id, text,
            reply_to=message.message_id if reply else None,
            thread_id=topic_of(message),
            on_migrated=self.services.chats.migrate,
        )
        for sent_message in sent:
            self.recorder.record_sent(sent_message.chat_id, sent_message)
        return sent

    async def _images(self, message: Message, trigger: StoredMessage) -> list[ImageInput]:
        """Download images on demand: the trigger's own, and the one it replies
        to. Only the Telegram file_id is stored; bytes are never kept."""
        settings = self.services.settings.for_chat(trigger.chat_id)
        if not settings["media.enabled"]:
            return []
        targets = []
        if media.has_supported_media(message):
            targets.append((message, trigger.id, f"Image from message {trigger.id}"))
        reply = message.reply_to_message
        if reply is not None and media.has_supported_media(reply):
            row_id = trigger.reply_to_row_id or 0
            label = (f"Image from message {row_id}, which the current request replies to"
                     if row_id else "Image from the message the current request replies to")
            targets.append((reply, row_id, label))
        images = []
        max_bytes = settings["media.max_size_mb"] * 1024 * 1024
        for source, row_id, label in targets:
            attachments, failure = await media.extract_attachments(source, max_bytes=max_bytes)
            if failure:
                logger.info("Image for message %s unavailable: %s", row_id, failure)
            for attachment in attachments:
                images.append(ImageInput(row_id=row_id, mime_type=attachment["mime_type"],
                                         base64=attachment["base64"], label=label))
        return images
