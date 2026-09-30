"""Which chats the bot works in, and how the owner approves them.

Three equivalent ways to enable or disable a group: owner-only /enable and
/disable in the group (ephemeral commands), the Approve / Leave buttons in the
owner's DMs, and the web admin. Pending and disabled chats get no replies and
nothing is recorded.
"""

from html import escape
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from naruto.db.chats import DISABLED, ENABLED, PENDING, Chat
from naruto.services import Services
from naruto.tg.content import ephemeral_message_id
from naruto.tg.sending import send_ephemeral

logger = logging.getLogger(__name__)

GROUP_TYPES = ("group", "supergroup")
PRESENT = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
           ChatMemberStatus.RESTRICTED, ChatMemberStatus.OWNER)
CALLBACK_PREFIX = "access"


def _rights_from_member(member) -> tuple[bool, bool]:
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return bool(getattr(member, "can_pin_messages", False)), \
            bool(getattr(member, "can_delete_messages", False))
    return False, False


def _chat_label(chat: Chat) -> str:
    return f"<b>{escape(chat.display_title)}</b>"


def _rights_note(chat: Chat) -> str:
    missing = chat.missing_rights()
    if not missing:
        return ""
    return ("\n⚠️ Missing admin rights: " + ", ".join(missing)
            + ". Make me a group admin with “Pin messages” for the pinned board.")


def approval_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"{CALLBACK_PREFIX}:approve:{chat.chat_id}"),
        InlineKeyboardButton("🚪 Leave", callback_data=f"{CALLBACK_PREFIX}:leave:{chat.chat_id}"),
    ]])


class ChatAccess:
    def __init__(self, services: Services, bot):
        self.services = services
        self.bot = bot

    # -------------------------------------------------------------- actions

    async def enable(self, chat_id: int, *, actor: str) -> Chat | None:
        chat = self.services.chats.set_status(chat_id, ENABLED)
        if chat is None:
            return None
        logger.info("Chat %s enabled by %s", chat.chat_id, actor, extra={"chat_id": chat.chat_id})
        return await self.check_rights(chat.chat_id)

    async def disable(self, chat_id: int, *, actor: str) -> Chat | None:
        chat = self.services.chats.set_status(chat_id, DISABLED)
        if chat is not None:
            logger.info("Chat %s disabled by %s", chat.chat_id, actor,
                        extra={"chat_id": chat.chat_id})
        return chat

    async def leave(self, chat_id: int, *, actor: str) -> Chat | None:
        chat = self.services.chats.get(chat_id)
        if chat is None:
            return None
        try:
            await self.bot.leave_chat(chat.chat_id)
        except TelegramError as exc:
            logger.warning("Could not leave chat %s: %s", chat.chat_id, exc)
        self.services.chats.set_membership(chat.chat_id, "left")
        logger.info("Left chat %s (%s)", chat.chat_id, actor, extra={"chat_id": chat.chat_id})
        return self.services.chats.set_status(chat.chat_id, DISABLED)

    async def check_rights(self, chat_id: int) -> Chat | None:
        """Ask Telegram what the bot may do in the chat (pin is needed for
        the board, delete is optional)."""
        chat = self.services.chats.get(chat_id)
        bot_identity = self.services.status.bot
        if chat is None or bot_identity is None:
            return chat
        try:
            member = await self.bot.get_chat_member(chat.chat_id, bot_identity.id)
        except TelegramError as exc:
            logger.warning("Could not check rights in chat %s: %s", chat.chat_id, exc)
            return chat
        can_pin, can_delete = _rights_from_member(member)
        self.services.chats.set_rights(chat.chat_id, can_pin=can_pin, can_delete=can_delete)
        self.services.chats.set_membership(chat.chat_id, str(member.status))
        return self.services.chats.get(chat.chat_id)

    # ------------------------------------------------------ owner messages

    async def notify_owner(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
        owner = self.services.bootstrap.owner_user_id
        if owner is None:
            return False
        try:
            await self.bot.send_message(owner, text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)
            return True
        except TelegramError as exc:
            # Usually: the owner never started a DM with the bot.
            logger.warning("Could not message the owner: %s", exc)
            return False

    async def notify_pending(self, chat: Chat, added_by: str | None = None, *,
                             force: bool = False) -> None:
        if chat.enabled or (chat.owner_notified_at and not force):
            return
        by = f" by <b>{escape(added_by)}</b>" if added_by else ""
        state = "" if chat.status == PENDING else " It is currently disabled."
        text = f"Added to {_chat_label(chat)}{by}.{state}"
        if await self.notify_owner(text, approval_keyboard(chat)):
            self.services.chats.mark_owner_notified(chat.chat_id)

    async def reply_privately(self, message, user_id: int, text: str) -> None:
        """Answer an in-group owner command without the group seeing it:
        ephemerally if possible, otherwise in the owner's DMs."""
        try:
            await send_ephemeral(self.bot, message.chat_id, user_id, text,
                                 reply_to_ephemeral_id=ephemeral_message_id(message))
            return
        except TelegramError as exc:
            logger.info("Ephemeral reply failed (%s); using DMs.", exc)
        chat_title = escape(message.chat.title or str(message.chat_id))
        await self.notify_owner(f"<b>{chat_title}</b>: {escape(text)}")

    # -------------------------------------------------------------- handlers

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        if change is None or change.chat.type not in GROUP_TYPES:
            return
        new_status = change.new_chat_member.status
        was_present = change.old_chat_member.status in PRESENT
        chat, _ = self.services.chats.upsert_seen(change.chat.id, title=change.chat.title,
                                                  chat_type=change.chat.type)
        by_user = change.from_user
        by_name = (by_user.full_name or by_user.username) if by_user else None

        if new_status not in PRESENT:
            self.services.chats.set_membership(chat.chat_id, str(new_status))
            logger.info("Bot removed from chat %s (%s)", chat.chat_id, new_status,
                        extra={"chat_id": chat.chat_id})
            return

        self.services.chats.set_membership(
            chat.chat_id, str(new_status),
            added_by_user_id=None if was_present else (by_user.id if by_user else None),
            added_by_name=None if was_present else by_name,
        )
        can_pin, can_delete = _rights_from_member(change.new_chat_member)
        self.services.chats.set_rights(chat.chat_id, can_pin=can_pin, can_delete=can_delete)
        chat = self.services.chats.get(chat.chat_id)
        if was_present:
            return  # a rights change, not a new join
        logger.info("Bot added to chat %s by %s", chat.chat_id, by_name,
                    extra={"chat_id": chat.chat_id})
        if chat.enabled:
            await self.notify_owner(
                f"Added back to {_chat_label(chat)}"
                + (f" by <b>{escape(by_name)}</b>" if by_name else "")
                + ". It is still enabled." + _rights_note(chat))
        else:
            await self.notify_pending(chat, by_name, force=True)

    async def on_enable_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle_command(update, enable=True)

    async def on_disable_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle_command(update, enable=False)

    async def _toggle_command(self, update: Update, *, enable: bool) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or message.chat.type not in GROUP_TYPES:
            return
        if not self.services.is_owner(user.id):
            logger.info("Ignoring /%s from non-owner %s", "enable" if enable else "disable",
                        user.id, extra={"chat_id": message.chat_id})
            return
        chat, _ = self.services.chats.upsert_seen(message.chat_id, title=message.chat.title,
                                                  chat_type=message.chat.type)
        if enable:
            chat = await self.enable(chat.chat_id, actor="owner (/enable)")
            text = "✅ Enabled. I'm reading along now and I'll answer when mentioned."
            missing = chat.missing_rights()
            if missing:
                text += ("\n⚠️ Missing admin rights: " + ", ".join(missing)
                         + ". Make me a group admin with “Pin messages”.")
        else:
            await self.disable(chat.chat_id, actor="owner (/disable)")
            text = "⏸ Disabled. I won't record or reply here until you /enable me."
        await self.reply_privately(message, user.id, text)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not (query.data or "").startswith(f"{CALLBACK_PREFIX}:"):
            return
        if not self.services.is_owner(query.from_user.id):
            await query.answer("Only the bot's owner can do that.", show_alert=True)
            return
        try:
            _, action, raw_id = query.data.split(":", 2)
            chat_id = int(raw_id)
        except ValueError:
            await query.answer("Unknown action.")
            return
        if action == "approve":
            chat = await self.enable(chat_id, actor="owner (DM button)")
            result = (f"✅ Enabled {_chat_label(chat)}." + _rights_note(chat)
                      if chat else "That chat is unknown.")
        elif action == "leave":
            chat = await self.leave(chat_id, actor="owner (DM button)")
            result = f"🚪 Left {_chat_label(chat)}." if chat else "That chat is unknown."
        else:
            await query.answer("Unknown action.")
            return
        await query.answer()
        try:
            await query.edit_message_text(result, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            logger.debug("Could not edit the approval message: %s", exc)

    async def on_private_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """DMs are for the owner's admin only; everyone else is ignored."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        if not self.services.is_owner(user.id):
            logger.info("Ignoring a private message from user %s", user.id)
            return
        pending = self.services.chats.list_by_status(PENDING)
        enabled = self.services.chats.list_by_status(ENABLED)
        web = self.services.bootstrap
        lines = [
            "Oi! I only chat in groups. This DM is for approving groups.",
            f"Enabled groups: {len(enabled)}. Pending: {len(pending)}.",
        ]
        if web.web_enabled:
            lines.append(f"Web admin: http://{escape(web.web_host)}:{web.web_port}/")
        keyboard = None
        if pending:
            lines.append("")
            lines.append("Waiting for approval:")
            rows = []
            for chat in pending[:20]:
                lines.append(f"• {_chat_label(chat)}")
                rows.append([
                    InlineKeyboardButton(f"✅ {chat.display_title[:24]}",
                                         callback_data=f"{CALLBACK_PREFIX}:approve:{chat.chat_id}"),
                    InlineKeyboardButton("🚪 Leave",
                                         callback_data=f"{CALLBACK_PREFIX}:leave:{chat.chat_id}"),
                ])
            keyboard = InlineKeyboardMarkup(rows)
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                 reply_markup=keyboard)
