"""Commands that start a skill directly, so the model doesn't have to guess
the intent: /summary, /catchup, /plan, /questions, /remember, /remind, plus
/board, which just shows the board."""

from datetime import datetime, time as dtime, timedelta
import logging
import re
import time

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from naruto.agent.context import EPHEMERAL_TRIGGER_ID
from naruto.agent.runner import FAILURE_TEXT
from naruto.db.chats import Chat
from naruto.db.messages import LIVE, StoredMessage
from naruto.services import Services
from naruto.tg.board import BoardPublisher
from naruto.tg.content import ephemeral_message_id, sender_of
from naruto.tg.recorder import GROUP_TYPES
from naruto.tg.responder import Responder
from naruto.tg.sending import send_ephemeral, send_text, split_message

logger = logging.getLogger(__name__)

_DURATION = re.compile(r"^(\d+)\s*(h|hours?|d|days?|w|weeks?)$", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400}
CATCHUP_DEFAULT_HOURS = 24


def summary_scope(args: list[str], tz, now: float | None = None) -> tuple[int | None, str]:
    """(/summary arguments) -> (since, what to tell the model)."""
    text = " ".join(args or []).strip()
    now = now or time.time()
    if not text:
        return None, "summarize the recent discussion"
    lowered = text.lower()
    today = datetime.fromtimestamp(now, tz).date()
    if lowered == "today":
        return int(datetime.combine(today, dtime.min, tz).timestamp()), \
            "summarize today's messages"
    if lowered == "yesterday":
        start = datetime.combine(today - timedelta(days=1), dtime.min, tz)
        return int(start.timestamp()), "summarize the messages since yesterday morning"
    if lowered == "week":
        return int(now - 7 * 86400), "summarize the past week"
    match = _DURATION.match(lowered)
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)[0]]
        return int(now - seconds), f"summarize the messages of the last {text}"
    return None, f"summarize what was said about: {text} (search for it)"


class SkillCommands:
    def __init__(self, services: Services, responder: Responder, board: BoardPublisher):
        self.services = services
        self.responder = responder
        self.board = board

    # --------------------------------------------------------------- helpers

    def _chat(self, update: Update) -> Chat | None:
        chat = update.effective_chat
        if chat is None or chat.type not in GROUP_TYPES or update.effective_message is None:
            return None
        known = self.services.chats.get(chat.id)
        return known if known is not None and known.enabled else None

    async def _run_skill(self, update: Update, context, skill: str, *,
                         note: str, since: int | None = None) -> None:
        chat = self._chat(update)
        bot = self.services.status.bot
        if chat is None or bot is None:
            return
        message = update.effective_message
        trigger = self.services.messages.get_live(message.chat_id, message.message_id)
        if trigger is None:
            logger.warning("/%s message %s was not recorded; skipping.", skill, message.message_id)
            return
        logger.info("Running %s for message %s", skill, message.message_id)
        await self.responder.respond(context.bot, chat, message, trigger, bot, skill=skill,
                                     since=since, note=note, force_reply=True)

    async def _usage(self, update: Update, context, text: str) -> None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

    # -------------------------------------------------------------- commands

    async def summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        since, what = summary_scope(context.args, self.services.timezone())
        reply = getattr(message, "reply_to_message", None) if message else None
        if since is None and not context.args and reply is not None:
            stored = self.services.messages.get_live(reply.chat_id, reply.message_id)
            since = stored.date if stored else int(reply.date.timestamp())
            what = "summarize everything since the message they replied to"
        await self._run_skill(update, context, "summarize", since=since,
                              note=f"They used /summary: {what}.")

    async def plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        extra = " ".join(context.args or [])
        await self._run_skill(update, context, "plan", note=(
            "They used /plan: pull together the plan being discussed"
            + (f" ({extra})" if extra else "") + "."))

    async def questions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_skill(update, context, "questions",
                              note="They used /questions: find the open questions.")

    async def remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args or [])
        message = update.effective_message
        if not text and (message is None or message.reply_to_message is None):
            if self._chat(update):
                await self._usage(update, context, "Usage: /remember <fact>, or reply to a "
                                                   "message with /remember.")
            return
        note = (f"They used /remember: save this in the group's memory: {text}" if text else
                "They used /remember on the message they reply to: save what it says in the "
                "group's memory.")
        await self._run_skill(update, context, "remember", note=note)

    async def remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args or [])
        if not text:
            if self._chat(update):
                await self._usage(update, context, "Usage: /remind <when> <what>, e.g. "
                                                   "/remind Saturday 5pm bring the grill")
            return
        await self._run_skill(update, context, "remind",
                              note=f"They used /remind: set this reminder: {text}")

    async def show_board(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = self._chat(update)
        if chat is None:
            return
        if self.services.boards.get(chat.chat_id).is_empty:
            await self._usage(update, context, "The board is empty. Ask me to put plans, "
                                               "decisions or open questions on it.")
            return
        result = await self.board.publish(context.bot, chat, fresh=True)
        if "couldn't" in result.lower() or "failed" in result.lower():
            await self._usage(update, context, result)

    # --------------------------------------------------------------- catchup

    async def catchup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ephemeral: only the person asking sees the command and the answer."""
        chat = self._chat(update)
        bot = self.services.status.bot
        message = update.effective_message
        user = update.effective_user
        if chat is None or bot is None or user is None:
            return
        sender = sender_of(message)
        account = self.services.people.for_user(user.id)
        user_ids = [a.user_id for a in account.accounts] if account else [user.id]
        last = self.services.messages.search(chat.chat_id, None, sender_ids=user_ids, limit=1)
        now = int(time.time())
        since = last[0].date + 1 if last else now - CATCHUP_DEFAULT_HOURS * 3600
        when = datetime.fromtimestamp(since, self.services.timezone()).strftime("%a %d %b, %H:%M")
        note = (f"They used /catchup: tell them what they missed since they last spoke "
                f"({when})." if last else
                f"They used /catchup and haven't said anything here yet: catch them up on the "
                f"last {CATCHUP_DEFAULT_HOURS} hours.")
        trigger = StoredMessage(
            id=EPHEMERAL_TRIGGER_ID, chat_id=chat.chat_id, origin_chat_id=message.chat_id,
            source=LIVE, message_id=message.message_id or 0, import_id=None, thread_id=None,
            sender_id=sender.id, sender_name=sender.name, sender_username=sender.username,
            from_bot=False, date=now, edit_date=None, text="/catchup", media_kind=None,
            media_file_id=None, media_file_unique_id=None, media_meta={}, forwarded_from=None,
            reply_to_message_id=None, reply_to_row_id=None, reply_to_snippet=None, created_at=now)
        async with self.responder.chat_lock(chat.chat_id):
            outcome = await self.responder.run(context.bot, chat, message, trigger, bot,
                                               skill="catchup", since=since, note=note,
                                               show_typing=False)
        text = outcome.text or ("Nothing much happened since you last spoke."
                                if outcome.status in ("ok", "empty") else FAILURE_TEXT)
        await self._deliver_privately(context.bot, chat, message, user, text)

    async def _deliver_privately(self, telegram, chat: Chat, message, user, text: str) -> None:
        """Ephemeral in the group; else a DM; else a normal reply in the group."""
        reply_to = ephemeral_message_id(message)
        try:
            for index, chunk in enumerate(split_message(text)):
                await send_ephemeral(telegram, chat.chat_id, user.id, chunk,
                                     reply_to_ephemeral_id=reply_to if index == 0 else None)
            return
        except TelegramError as exc:
            logger.info("Ephemeral catch-up failed (%s); trying a DM.", exc)
        try:
            await send_text(telegram, user.id, f"Catch-up for {chat.display_title}:\n\n{text}")
            return
        except TelegramError as exc:
            logger.info("Catch-up DM failed (%s); answering in the group.", exc)
        await send_text(telegram, chat.chat_id, text, on_migrated=self.services.chats.migrate)
