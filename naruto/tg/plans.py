"""Plan proposals: the bot posts a plan with Confirm / Change buttons, and a
confirmed plan goes on the board."""

from html import escape
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import ContextTypes

from naruto.db.board import BoardFull
from naruto.db.chats import Chat
from naruto.db.plans import CONFIRMED, Plan
from naruto.services import Services
from naruto.tg.board import BoardPublisher

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "plan"


def plan_keyboard(plan: Plan) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"{CALLBACK_PREFIX}:confirm:{plan.id}"),
        InlineKeyboardButton("✏️ Change", callback_data=f"{CALLBACK_PREFIX}:change:{plan.id}"),
    ]])


def render_plan(plan: Plan, status: str | None = None) -> str:
    lines = [f"📋 <b>Plan: {escape(plan.title)}</b>"]
    lines += [f"• {escape(item)}" for item in plan.items]
    if status:
        lines += ["", status]
    return "\n".join(lines)


def plain_plan(plan: Plan, status: str | None = None) -> str:
    """The same text without HTML, as stored in the transcript."""
    lines = [f"📋 Plan: {plan.title}", *(f"• {item}" for item in plan.items)]
    if status:
        lines += ["", status]
    return "\n".join(lines)


async def send_plan(telegram, services: Services, chat: Chat, plan: Plan):
    """Post the proposal; returns the sent message."""
    chat_id = chat.chat_id
    kwargs = dict(text=render_plan(plan), parse_mode="HTML", reply_markup=plan_keyboard(plan))
    try:
        sent = await telegram.send_message(chat_id=chat_id, **kwargs)
    except ChatMigrated as exc:
        services.chats.migrate(chat_id, exc.new_chat_id)
        sent = await telegram.send_message(chat_id=exc.new_chat_id, **kwargs)
    services.plans.set_message(plan.id, sent.message_id, sent.chat_id)
    return sent


class PlanButtons:
    def __init__(self, services: Services, board: BoardPublisher):
        self.services = services
        self.board = board

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not (query.data or "").startswith(f"{CALLBACK_PREFIX}:"):
            return
        try:
            _, action, raw_id = query.data.split(":", 2)
            plan = self.services.plans.get(int(raw_id))
        except ValueError:
            plan = None
        chat = self.services.chats.get(plan.chat_id) if plan else None
        if plan is None or chat is None or not chat.enabled:
            await query.answer("That plan is no longer available.")
            return
        user = query.from_user
        name = self.services.people.display_names([user.id]).get(
            user.id, user.full_name or user.username or "someone")
        if action == "change":
            await query.answer("Mention me with what should change, and I'll post an updated plan.",
                               show_alert=True)
            return
        if action != "confirm":
            await query.answer("Unknown action.")
            return
        if not self.services.plans.decide(plan.id, CONFIRMED, user_id=user.id, name=name):
            await query.answer("This plan was already confirmed or replaced.")
            return
        try:
            self.services.boards.add_item(chat.chat_id, "plans", plan.one_line(), done=True,
                                          actor=f"plan {plan.id} confirmed by {name}")
            on_board = True
        except BoardFull as exc:
            logger.warning("Plan %s confirmed but not added to the board: %s", plan.id, exc,
                           extra={"chat_id": chat.chat_id})
            on_board = False
        logger.info("Plan %s confirmed by user %s", plan.id, user.id,
                    extra={"chat_id": chat.chat_id})
        if on_board:
            await query.answer("Confirmed! It's on the board.")
        else:
            await query.answer("Confirmed! But the board is full, so it isn't on it. Ask me to "
                               "clear finished plans, then I can add it.", show_alert=True)
        status = f"✅ Confirmed by {name}"
        try:
            await query.edit_message_text(render_plan(plan, escape(status)), parse_mode="HTML")
        except TelegramError as exc:
            logger.debug("Couldn't edit the plan message: %s", exc)
        if plan.message_id and plan.message_chat_id:
            self.services.messages.apply_edit(plan.message_chat_id, plan.message_id,
                                              text=plain_plan(plan, status), edit_date=None)
        if on_board:
            await self.board.publish(context.bot, chat)
