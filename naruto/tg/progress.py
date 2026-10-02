"""A short "working on it" line for slow skills (plan §13; its lifecycle:
plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md §6).

Summaries, plans and lists of open questions read a lot and think first, so
they can take half a minute, more when the model server has to read the
prompt from scratch. When such a run is still going after a while
(Settings → Behaviour → Progress message after), a placeholder is posted
silently as a reply to the request. It says when the run reaches a stage
the runner knows about (reading further back, writing the answer). Once the
answer is sent, the placeholder is deleted; when the run fails, it turns
into the error message and stays. Faster runs and other skills only show
"typing…". The responder decides all of this, never the model.
"""

import asyncio
import contextlib
import logging

from telegram import Message
from telegram.error import TelegramError

from naruto.services import Services
from naruto.tg.own_messages import delete_sent
from naruto.tg.sending import edit_text, send_text

logger = logging.getLogger(__name__)

PLACEHOLDERS = {
    "summarize": "📖 Reading back through the chat…",
    "plan": "🗓 Pulling the plan together…",
    "questions": "❓ Collecting the open questions…",
}
# Stages the runner reports (RunRequest.on_stage).
STAGES = {
    "reading": "📖 Reading further back…",
    "writing": "✍️ Writing it up…",
}
STAGE_EDIT_SECONDS = 5.0  # at most one edit this often


class Progress:
    def __init__(self, services: Services, telegram, chat_id: int, reply_to: int | None, *,
                 skill: str, after_seconds: float, thread_id: int | None = None):
        self.services = services
        self.telegram = telegram
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.thread_id = thread_id  # the request's forum topic
        self.skill = skill
        self.message: Message | None = None  # the placeholder, once posted
        self._slow = asyncio.Event()  # the current skill gets a placeholder
        self._stage: str | None = None  # the latest stage reported
        self._shown = ""  # the placeholder's text
        self._shown_at = 0.0  # loop time it was posted or last edited
        self._stopped = False
        self._editing = False
        self._edits: asyncio.Task | None = None
        self.skill_changed(skill)
        self._sending = False
        self._task = (asyncio.create_task(self._post_later(after_seconds))
                      if after_seconds > 0 else None)

    def skill_changed(self, skill: str) -> None:
        """The run's skill: the one it started with, then any hand-over
        (a mention that turns out to be a summary request)."""
        self.skill = skill
        self._stage = None
        if skill in PLACEHOLDERS:
            self._slow.set()
        else:
            self._slow.clear()

    def stage(self, name: str) -> None:
        """The run reached a stage. Shown on the placeholder, if there is
        one, with at most one edit every few seconds."""
        if name in STAGES:
            self._stage = name
            self._show_stage()

    def _show_stage(self) -> None:
        if (not self._stopped and self.message is not None and self._stage
                and self._edits is None):
            self._edits = asyncio.create_task(self._edit_stages())

    async def _edit_stages(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while (not self._stopped and self.message is not None
                   and STAGES.get(self._stage, self._shown) != self._shown):
                wait = self._shown_at + STAGE_EDIT_SECONDS - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue  # the stage may have changed meanwhile
                text = STAGES[self._stage]
                self._editing = True
                try:
                    await self.telegram.edit_message_text(
                        chat_id=self.message.chat_id, message_id=self.message.message_id,
                        text=text)
                except TelegramError as exc:
                    logger.info("Couldn't update the progress message: %s", exc)
                finally:
                    self._editing = False
                self._shown, self._shown_at = text, loop.time()
        finally:
            self._edits = None

    async def _post_later(self, after_seconds: float) -> None:
        await asyncio.sleep(after_seconds)
        await self._slow.wait()  # a hand-over may still come
        self._sending = True
        text = PLACEHOLDERS[self.skill]
        try:
            sent = await send_text(self.telegram, self.chat_id, text, reply_to=self.reply_to,
                                   thread_id=self.thread_id, silent=True)
            self.message = sent[0] if sent else None
            self._shown, self._shown_at = text, asyncio.get_running_loop().time()
        except TelegramError as exc:
            logger.info("Couldn't post the progress message: %s", exc)
        finally:
            self._sending = False
        self._show_stage()  # a stage reached before it was posted

    async def stop(self) -> None:
        """The run is over: no placeholder or stage edit from now on. One
        being sent right now is waited for, so it can be dealt with rather
        than left behind."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            if not self._sending:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        edits = self._edits
        if edits is not None:
            if not self._editing:
                edits.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await edits

    async def discard(self) -> None:
        """Delete the placeholder (the result is in the chat, or nothing
        more is said). If Telegram refuses, it's logged and left."""
        if self.message is None:
            return
        await delete_sent(self.services, self.telegram, self.message, self.chat_id)
        self.message = None

    async def show(self, text: str) -> Message | None:
        """Turn the placeholder into ``text`` (an error message), which then
        stays in the chat. None if Telegram refused the edit."""
        placeholder = self.message
        if placeholder is None:
            return None
        try:
            edited = await edit_text(self.telegram, placeholder.chat_id, placeholder.message_id,
                                     text)
        except TelegramError as exc:
            logger.info("Couldn't turn the progress message into the error: %s", exc)
            return None
        self.message = None  # not a placeholder any more
        return edited if isinstance(edited, Message) else placeholder
