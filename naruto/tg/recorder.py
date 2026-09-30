"""Records every group message in enabled chats, plus edits and group
upgrades. Runs before all other handlers (handler group -1)."""

import logging

from telegram import Message, MessageEntity, Update
from telegram.ext import ContextTypes

from naruto.db.chats import Chat
from naruto.services import Services
from naruto.tg.content import media_of, sender_of, to_new_message

logger = logging.getLogger(__name__)

GROUP_TYPES = ("group", "supergroup")


def is_command(message) -> bool:
    entities = getattr(message, "entities", None) or ()
    return any(e.type == MessageEntity.BOT_COMMAND and e.offset == 0 for e in entities)


def has_content(message) -> bool:
    """Text, caption or media. Service messages (joins, title changes,
    pins) have none and are not stored."""
    return bool(getattr(message, "text", None) or getattr(message, "caption", None)
                or media_of(message))


class Recorder:
    def __init__(self, services: Services):
        self.services = services

    @property
    def _bot_id(self) -> int | None:
        bot = self.services.status.bot
        return bot.id if bot else None

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or message.chat.type not in GROUP_TYPES:
            return
        chats = self.services.chats

        if message.migrate_to_chat_id:
            chats.migrate(message.chat_id, message.migrate_to_chat_id)
            return
        if message.migrate_from_chat_id:
            chats.migrate(message.migrate_from_chat_id, message.chat_id)
            return

        title = message.new_chat_title or message.chat.title
        chat, created = chats.upsert_seen(message.chat_id, title=title,
                                          chat_type=message.chat.type)
        bot_joined = any(u.id == self._bot_id for u in message.new_chat_members or ())
        if created and not bot_joined and self.services.access is not None:
            # A group the bot joined before it tracked membership. (When the
            # bot has just been added, the my_chat_member update notifies.)
            await self.services.access.notify_pending(chat)
        if not chat.enabled:
            return

        if message.new_chat_members:
            for user in message.new_chat_members:
                if user.id != self._bot_id:
                    self.services.members.upsert_live(
                        chat.chat_id, user.id, user.full_name or user.username or f"User {user.id}",
                        user.username, is_bot=user.is_bot)
        if is_command(message) or not has_content(message):
            return
        self.record(chat, message)

    def record(self, chat: Chat, message: Message) -> None:
        record = to_new_message(message, chat_id=chat.chat_id, bot_id=self._bot_id)
        stored = self.services.messages.insert_live(record)
        sender = sender_of(message)
        if sender.id is not None and not record.from_bot and message.sender_chat is None:
            self.services.members.upsert_live(chat.chat_id, sender.id, sender.name,
                                              sender.username, is_bot=sender.is_bot,
                                              seen_at=record.date)
        self.services.chats.touch_activity(chat.chat_id, record.date)
        logger.debug("Recorded message %s as row %s", message.message_id, stored.id)

    def record_sent(self, chat_id: int, sent: Message) -> None:
        """Store the bot's own message; Telegram never sends it back as an
        update."""
        chat = self.services.chats.get(chat_id)
        if chat is None or not chat.enabled:
            return
        self.services.messages.insert_live(
            to_new_message(sent, chat_id=chat.chat_id, bot_id=self._bot_id))

    async def on_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.edited_message
        if message is None or message.chat.type not in GROUP_TYPES:
            return
        chat = self.services.chats.get(message.chat_id)
        if chat is None or not chat.enabled:
            return
        text = message.text or message.caption or ""
        edit_date = int(message.edit_date.timestamp()) if message.edit_date else None
        self.services.messages.apply_edit(message.chat_id, message.message_id,
                                          text=text, edit_date=edit_date)
