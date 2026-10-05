"""Plan cards the bot used to post with Confirm / Change buttons. Plans are
kept on the board now (plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md
§7): a press on an old card's button says so and removes that card's
buttons. Its text stays as it is."""

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "plan"
OLD_CARD_ANSWER = "Plans are kept on the board now."


async def on_old_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{CALLBACK_PREFIX}:"):
        return
    await query.answer(OLD_CARD_ANSWER)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except (TelegramError, TypeError) as exc:  # TypeError: an inaccessible message
        logger.debug("Couldn't remove an old plan card's buttons: %s", exc)
