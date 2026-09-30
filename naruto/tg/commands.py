"""Group commands that work in enabled chats: aliases and /group_info."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from naruto.db.chats import Chat
from naruto.db.messages import IMPORT, LIVE
from naruto.services import Services

logger = logging.getLogger(__name__)


def parse_alias_args(args: list[str] | None) -> tuple[str, str] | None:
    """Returns (target_username, alias) or None if the args are malformed."""
    if not args or len(args) < 2:
        return None
    target = args[0]
    if not target.startswith("@") or len(target) < 2:
        return None
    alias = " ".join(args[1:]).strip()
    if not alias:
        return None
    return target, alias


class GroupCommands:
    def __init__(self, services: Services):
        self.services = services

    def _enabled_chat(self, update: Update) -> Chat | None:
        chat = update.effective_chat
        if chat is None or chat.type not in ("group", "supergroup"):
            return None
        known = self.services.chats.get(chat.id)
        return known if known is not None and known.enabled else None

    async def _say(self, update: Update, context, text: str) -> None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._enabled_chat(update):
            await self._say(update, context, "I'm gonna be Hokage some day!")

    async def alias(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = self._enabled_chat(update)
        if chat is None:
            return
        parsed = parse_alias_args(context.args)
        if parsed is None:
            await self._say(update, context, "Usage: /alias @user <alias>")
            return
        username, alias = parsed
        user_id = self.services.members.find_user_id_by_username(chat.chat_id, username)
        if user_id is None:
            await self._say(update, context, f"I don't know {username} yet — they have to "
                                             "send a message in this chat first.")
            return
        self.services.members.add_alias(chat.chat_id, user_id, alias)
        await self._say(update, context, f"Got it — {username} is now also known as '{alias}'.")

    async def removealias(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = self._enabled_chat(update)
        if chat is None:
            return
        parsed = parse_alias_args(context.args)
        if parsed is None:
            await self._say(update, context, "Usage: /removealias @user <alias>")
            return
        username, alias = parsed
        user_id = self.services.members.find_user_id_by_username(chat.chat_id, username)
        if user_id is None:
            await self._say(update, context, f"I don't know {username} in this chat.")
            return
        if not self.services.members.remove_alias(chat.chat_id, user_id, alias):
            await self._say(update, context, f"{username} doesn't have the alias '{alias}'.")
            return
        await self._say(update, context, f"Removed alias '{alias}' from {username}.")

    async def clearaliases(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = self._enabled_chat(update)
        if chat is None:
            return
        self.services.members.clear_aliases(chat.chat_id)
        await self._say(update, context, "All aliases cleared in this chat.")

    async def group_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = self._enabled_chat(update)
        if chat is None:
            return
        messages = self.services.messages
        lines = [
            f"Chat: {chat.display_title} ({chat.type})",
            f"Stored messages: {messages.count(chat.chat_id, LIVE)} live, "
            f"{messages.count(chat.chat_id, IMPORT)} imported",
        ]
        bot = self.services.status.bot
        members = [m for m in self.services.members.list(chat.chat_id)
                   if bot is None or m.user_id != bot.id]
        lines.append("")
        if members:
            lines.append("Members:")
            for member in members:
                alias_part = f" [aliases: {', '.join(member.aliases)}]" if member.aliases else ""
                lines.append(f"- {member.display_name} ({member.handle}){alias_part}")
        else:
            lines.append("Members: (none seen yet)")
        await self._say(update, context, "\n".join(lines))
