"""Which chats the bot works in, and how the owner approves them.

Three equivalent ways to enable or disable a group: owner-only /enable and
/disable in the group (ephemeral commands), the Approve / Leave buttons in the
owner's DMs, and the web admin. Pending and disabled chats get no replies and
nothing is recorded.
"""

from html import escape
import logging
import time

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
# Telegram keeps undelivered updates for a day and hands them all over when
# the bot starts polling again, so commands sent during an outage arrive
# together, too late for an ephemeral answer (15 seconds).
LATE_AFTER_SECONDS = 60
BACKLOG_SECONDS = 2 * 86400


def arrived_late(message, now: float | None = None) -> bool:
    """The command was sent while the bot was offline. A date older than
    Telegram's backlog can't be one (an ephemeral command's date is
    undocumented), so it counts as on time."""
    sent = getattr(message, "date", None)
    if sent is None:
        return False
    age = (time.time() if now is None else now) - sent.timestamp()
    return LATE_AFTER_SECONDS < age <= BACKLOG_SECONDS


def rights_of(member, permissions=None) -> tuple[bool, bool]:
    """(can pin, can delete) for the bot's ChatMember. A plain member can
    pin when the group lets every member pin: ``permissions`` are the
    chat's default ChatPermissions (basic groups allow it unless changed)."""
    status = member.status
    if status == ChatMemberStatus.OWNER:
        return True, True
    if status == ChatMemberStatus.ADMINISTRATOR:
        can_pin = getattr(member, "can_pin_messages", None)  # absent: every admin can pin
        return can_pin is None or bool(can_pin), bool(getattr(member, "can_delete_messages", False))
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "can_pin_messages", False)), False
    if status == ChatMemberStatus.MEMBER and permissions is not None:
        return bool(getattr(permissions, "can_pin_messages", False)), False
    return False, False


def is_rights_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "right" in text or "admin" in text


def note_pin(services: Services, chat_id: int, error: Exception | None = None) -> None:
    """What an actual pin says about the pin right, between checks: it
    worked, or Telegram refused it for lack of rights."""
    chat = services.chats.get(chat_id)
    if chat is None:
        return
    if error is None:
        can_pin = True
    elif is_rights_error(error):
        can_pin = False
    else:
        return  # e.g. the message is gone: says nothing about rights
    if chat.can_pin is not can_pin:
        logger.info("Pinning %s in chat %s: pin right is %s", "worked" if can_pin else "was refused",
                    chat.chat_id, "on" if can_pin else "off", extra={"chat_id": chat.chat_id})
        services.chats.set_rights(chat.chat_id, can_pin=can_pin, can_delete=chat.can_delete)


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
        """Enable a chat (no-op if it already is) and re-check admin rights."""
        before = self.services.chats.get(chat_id)
        chat = self.services.chats.set_status(chat_id, ENABLED)
        if chat is None:
            return None
        if not (before and before.enabled):
            logger.info("Chat %s enabled by %s", chat.chat_id, actor,
                        extra={"chat_id": chat.chat_id})
        return await self.check_rights(chat.chat_id)

    async def disable(self, chat_id: int, *, actor: str) -> Chat | None:
        before = self.services.chats.get(chat_id)
        chat = self.services.chats.set_status(chat_id, DISABLED)
        if chat is not None and not (before and before.status == DISABLED):
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
        if chat is None:
            return chat
        identity = self.services.status.bot
        bot_id = identity.id if identity else self.bot.id
        try:
            member = await self.bot.get_chat_member(chat.chat_id, bot_id)
        except TelegramError as exc:
            logger.warning("Could not check rights in chat %s: %s", chat.chat_id, exc)
            return chat
        can_pin, can_delete = await self._rights(chat.chat_id, member)
        self.services.chats.set_rights(chat.chat_id, can_pin=can_pin, can_delete=can_delete)
        self.services.chats.set_membership(chat.chat_id, str(member.status))
        return self.services.chats.get(chat.chat_id)

    async def _rights(self, chat_id: int, member) -> tuple[bool, bool]:
        permissions = None
        if member.status == ChatMemberStatus.MEMBER:
            try:
                permissions = (await self.bot.get_chat(chat_id)).permissions
            except TelegramError as exc:
                logger.info("Could not read the permissions of chat %s: %s", chat_id, exc)
        return rights_of(member, permissions)

    async def refresh_rights(self, *, older_than: float = 0) -> int:
        """Check rights again in every enabled chat the bot is in that
        wasn't checked within ``older_than`` seconds (or never): a promotion
        can arrive while the bot is offline, or before it tracked rights."""
        cutoff = time.time() - older_than
        checked = 0
        for chat in self.services.chats.list_by_status(ENABLED):
            if chat.membership in ("left", "kicked"):
                continue
            if chat.rights_checked_at and chat.rights_checked_at > cutoff:
                continue
            await self.check_rights(chat.chat_id)
            checked += 1
        return checked

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

    async def reply_privately(self, message, user_id: int, text: str, *,
                              dm_fallback: bool = True) -> None:
        """Answer an in-group owner command without the group seeing it:
        ephemerally if possible, otherwise in the owner's DMs."""
        try:
            await send_ephemeral(self.bot, message.chat_id, user_id, text,
                                 reply_to_ephemeral_id=ephemeral_message_id(message))
            return
        except TelegramError as exc:
            if not dm_fallback:
                logger.info("Ephemeral reply failed (%s); not sending it as a DM.", exc)
                return
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
        can_pin, can_delete = await self._rights(chat.chat_id, change.new_chat_member)
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
            already = chat.enabled
            chat = await self.enable(chat.chat_id, actor="owner (/enable)")
            text = ("✅ Already enabled here: I'm reading along and I answer when mentioned."
                    if already else
                    "✅ Enabled. I'm reading along now and I'll answer when mentioned.")
            missing = chat.missing_rights()
            if missing:
                text += ("\n⚠️ Missing admin rights: " + ", ".join(missing)
                         + ". Make me a group admin with “Pin messages”.")
        elif chat.status == DISABLED:
            already = True
            text = "⏸ Already disabled here. Use /enable to turn me back on."
        else:
            already = False
            await self.disable(chat.chat_id, actor="owner (/disable)")
            text = "⏸ Disabled. I won't record or reply here until you /enable me."
        if arrived_late(message):
            if already:
                logger.info("Not answering a late /%s: nothing changed",
                            "enable" if enable else "disable", extra={"chat_id": chat.chat_id})
                return
            text += "\n(You sent this while I was offline.)"
        # "Already ..." is only worth an answer where the owner typed it.
        await self.reply_privately(message, user.id, text, dm_fallback=not already)

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
            before = self.services.chats.get(chat_id)
            chat = await self.enable(chat_id, actor="owner (DM button)")
            if chat is None:
                result = "That chat is unknown."
            elif before is not None and before.enabled:
                result = f"✅ {_chat_label(chat)} was already enabled." + _rights_note(chat)
            else:
                result = f"✅ Enabled {_chat_label(chat)}." + _rights_note(chat)
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
            "I only chat in groups. This DM is for approving groups.",
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
