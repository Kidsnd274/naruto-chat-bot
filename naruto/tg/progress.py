"""A short "working on it" line for slow skills (plan §13).

Summaries, plans and lists of open questions read a lot and think first, so
they can take half a minute, more when the model server has to read the
prompt from scratch. When such a run is still going after a while
(Settings → Behaviour → Progress message after), a placeholder is posted as
a reply to the request, and the answer then replaces it. Faster runs and
other skills only show "typing…".
"""

import asyncio
import contextlib
import logging

from telegram import Message
from telegram.error import TelegramError

from naruto.tg.sending import send_text

logger = logging.getLogger(__name__)

PLACEHOLDERS = {
    "summarize": "📖 Reading back through the chat…",
    "plan": "🗓 Pulling the plan together…",
    "questions": "❓ Collecting the open questions…",
}


class Progress:
    def __init__(self, telegram, chat_id: int, reply_to: int | None, *, skill: str,
                 after_seconds: float):
        self.telegram = telegram
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.skill = skill
        self.message: Message | None = None  # the placeholder, once posted
        self._slow = asyncio.Event()  # the current skill gets a placeholder
        self.skill_changed(skill)
        self._sending = False
        self._task = (asyncio.create_task(self._post_later(after_seconds))
                      if after_seconds > 0 else None)

    def skill_changed(self, skill: str) -> None:
        """The run's skill: the one it started with, then any hand-over
        (a mention that turns out to be a summary request)."""
        self.skill = skill
        if skill in PLACEHOLDERS:
            self._slow.set()
        else:
            self._slow.clear()

    async def _post_later(self, after_seconds: float) -> None:
        await asyncio.sleep(after_seconds)
        await self._slow.wait()  # a hand-over may still come
        self._sending = True
        try:
            sent = await send_text(self.telegram, self.chat_id, PLACEHOLDERS[self.skill],
                                   reply_to=self.reply_to)
            self.message = sent[0] if sent else None
        except TelegramError as exc:
            logger.info("Couldn't post the progress message: %s", exc)
        finally:
            self._sending = False

    async def stop(self) -> None:
        """The run is over: no placeholder from now on. One being sent right
        now is waited for, so it can be replaced rather than left behind."""
        if self._task is None:
            return
        if not self._sending:
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def discard(self) -> None:
        """Remove the placeholder (nothing to replace it with)."""
        if self.message is None:
            return
        try:
            await self.telegram.delete_message(self.message.chat_id, self.message.message_id)
        except TelegramError as exc:
            logger.info("Couldn't delete the progress message: %s", exc)
        self.message = None
