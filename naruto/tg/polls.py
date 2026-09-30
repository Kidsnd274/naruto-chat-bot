"""Vote counts for polls the bot sent. Telegram sends the bot poll updates
for its own polls, and answers for non-anonymous ones; the stored poll
message keeps the counts so the model can read the result."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from naruto.services import Services

logger = logging.getLogger(__name__)


class PollTracker:
    def __init__(self, services: Services):
        self.services = services

    async def on_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        poll = update.poll
        if poll is None:
            return
        stored = self.services.messages.find_poll(poll.id)
        if stored is None:
            return
        meta = dict(stored.media_meta)
        meta["options"] = [option.text for option in poll.options]
        meta["counts"] = [option.voter_count for option in poll.options]
        meta["total_voters"] = poll.total_voter_count
        meta["closed"] = bool(poll.is_closed)
        self.services.messages.update_media_meta(stored.id, meta)

    async def on_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        answer = update.poll_answer
        if answer is None or answer.user is None:
            return
        stored = self.services.messages.find_poll(answer.poll_id)
        if stored is None:
            return
        meta = dict(stored.media_meta)
        votes = dict(meta.get("votes") or {})
        if answer.option_ids:
            votes[str(answer.user.id)] = list(answer.option_ids)
        else:
            votes.pop(str(answer.user.id), None)  # vote retracted
        meta["votes"] = votes
        self.services.messages.update_media_meta(stored.id, meta)
        user = answer.user
        self.services.people.touch_live(user.id, user.full_name or user.username,
                                        user.username, is_bot=user.is_bot)
