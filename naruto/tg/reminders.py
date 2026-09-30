"""Sends reminders when they are due (checked every half minute)."""

from datetime import datetime
import logging
import time

from telegram.error import TelegramError

from naruto.services import Services
from naruto.tg.recorder import Recorder
from naruto.tg.sending import send_text

logger = logging.getLogger(__name__)

LATE_AFTER_SECONDS = 3600


class ReminderSender:
    def __init__(self, services: Services, recorder: Recorder):
        self.services = services
        self.recorder = recorder

    async def send_due(self, telegram=None) -> int:
        services = self.services
        telegram = telegram or services.telegram
        if telegram is None:
            return 0
        sent_count = 0
        now = int(time.time())
        for reminder in services.reminders.due(now):
            chat = services.chats.get(reminder.chat_id)
            if chat is None or not chat.enabled:
                services.reminders.mark_failed(reminder.id, "The chat isn't enabled.")
                continue
            text = f"⏰ Reminder: {reminder.text}"
            if now - reminder.due_at > LATE_AFTER_SECONDS:
                due = datetime.fromtimestamp(reminder.due_at, services.timezone())
                text += f"\n(This was due {due.strftime('%a %d %b, %H:%M')}; I was offline.)"
            if reminder.created_by_user_id:
                name = services.people.display_names([reminder.created_by_user_id]).get(
                    reminder.created_by_user_id)
                if name:
                    text += f"\n— set by {name}"
            try:
                sent = await send_text(telegram, chat.chat_id, text,
                                       on_migrated=services.chats.migrate)
            except TelegramError as exc:
                services.reminders.mark_failed(reminder.id, str(exc))
                logger.warning("Reminder %s failed: %s", reminder.id, exc,
                               extra={"chat_id": chat.chat_id})
                continue
            for message in sent:
                self.recorder.record_sent(message.chat_id, message)
            services.reminders.mark_sent(reminder.id, sent[0].message_id if sent else None)
            logger.info("Sent reminder %s", reminder.id, extra={"chat_id": chat.chat_id})
            sent_count += 1
        return sent_count
