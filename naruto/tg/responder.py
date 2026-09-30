"""Answers messages that mention the bot or reply to it, in enabled groups.

The trigger message has already been stored by the recorder (handler group
-1), so the prompt is built from the database.
"""

import logging

from telegram import Message, Update
from telegram.ext import ContextTypes

from naruto import media
from naruto.agent.context import ContextBuilder, ImageInput
from naruto.agent.text import clean_model_output, without_image_data
from naruto.db.chats import Chat
from naruto.db.messages import StoredMessage
from naruto.llm import LLMError
from naruto.services import BotIdentity, Services
from naruto.tg.recorder import GROUP_TYPES, Recorder
from naruto.tg.sending import send_text, typing

logger = logging.getLogger(__name__)

FAILURE_TEXT = "Sorry, I couldn't get a response right now. Please try again later."


def is_trigger(message, bot: BotIdentity) -> bool:
    """A mention of the bot in the text or caption, or a reply to one of its
    messages. Bare media without a mention is stored silently."""
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
        self.builder = ContextBuilder(services)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        bot = self.services.status.bot
        if message is None or bot is None or message.chat.type not in GROUP_TYPES:
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
                      trigger: StoredMessage, bot: BotIdentity) -> None:
        settings = self.services.settings
        runs = self.services.runs
        skill = "banter"
        run_id = runs.start(chat_id=chat.chat_id, skill=skill, trigger_row_id=trigger.id,
                            trigger_message_id=message.message_id, user_id=trigger.sender_id)
        try:
            async with typing(telegram_bot, message.chat_id):
                images = await self._images(message, trigger)
                prompt = self.builder.build(chat, trigger, bot=bot, images=images, skill=skill)
                runs.update(run_id, prompt=without_image_data(prompt.messages),
                            prompt_tokens=prompt.estimated_tokens, window_size=prompt.window_size,
                            dropped=prompt.dropped, image_count=prompt.image_count)
                if prompt.dropped:
                    logger.warning("Dropped %s old messages to fit the input budget (%s tokens).",
                                   prompt.dropped, settings["context.input_token_budget"])
                try:
                    result = await self.services.llm.chat(
                        prompt.messages, reasoning=settings[f"skills.{skill}.reasoning"])
                except LLMError as exc:
                    logger.error("Model request failed: %s", exc)
                    runs.update(run_id, status="error", error=str(exc))
                    await self._send(telegram_bot, chat, message, FAILURE_TEXT, reply=False)
                    return

            should_reply, text = clean_model_output(result.text, bot.name)
            outcome = dict(model=result.model, reasoning=result.reasoning, response=text,
                           usage=result.usage, latency_ms=result.latency_ms,
                           finish_reason=result.finish_reason)
            if not text:
                logger.warning("The model returned an empty answer (finish reason %s).",
                               result.finish_reason)
                runs.update(run_id, status="empty", **outcome)
                return
            logger.info("Answered in %s ms (%s estimated prompt tokens, %s recent messages).",
                        result.latency_ms, prompt.estimated_tokens, prompt.window_size)
            sent = await self._send(telegram_bot, chat, message, text, reply=should_reply)
            runs.update(run_id, status="ok", reply_message_ids=[m.message_id for m in sent],
                        **outcome)
        except Exception as exc:
            runs.update(run_id, status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            raise

    async def _send(self, telegram_bot, chat: Chat, message: Message, text: str,
                    *, reply: bool) -> list[Message]:
        sent = await send_text(
            telegram_bot, message.chat_id, text,
            reply_to=message.message_id if reply else None,
            on_migrated=self.services.chats.migrate,
        )
        for sent_message in sent:
            self.recorder.record_sent(sent_message.chat_id, sent_message)
        return sent

    async def _images(self, message: Message, trigger: StoredMessage) -> list[ImageInput]:
        """Download images on demand: the trigger's own, and the one it replies
        to. Only the Telegram file_id is stored; bytes are never kept."""
        settings = self.services.settings
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
