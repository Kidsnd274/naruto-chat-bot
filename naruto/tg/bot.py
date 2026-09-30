"""Builds the python-telegram-bot Application and registers the handlers."""

import logging
import time
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from naruto.logs import current_chat_id
from naruto.services import BotIdentity, Services
from naruto.tg.access import CALLBACK_PREFIX, ChatAccess
from naruto.tg.commands import GroupCommands
from naruto.tg.recorder import Recorder
from naruto.tg.responder import Responder

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member", "callback_query"]
MESSAGE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post",
                "business_message", "edited_business_message")


def sanitize_updates(result: Any) -> Any:
    """Keep polling alive when Telegram sends something python-telegram-bot
    22.8 cannot parse (it predates Bot API 10.2+).

    One unparseable update makes get_updates() raise before the offset
    advances, so polling would retry the same update forever. A message
    without message_id (possible for ephemeral messages) gets 0; any update
    that still fails is replaced by an empty one so it is skipped.
    """
    if not isinstance(result, list):
        return result
    cleaned = []
    for raw in result:
        if not isinstance(raw, dict) or "update_id" not in raw:
            continue
        for key in MESSAGE_KEYS:
            message = raw.get(key)
            if isinstance(message, dict) and "message_id" not in message:
                message["message_id"] = 0
        query = raw.get("callback_query")
        if isinstance(query, dict) and isinstance(query.get("message"), dict):
            query["message"].setdefault("message_id", 0)
        try:
            Update.de_json(raw, None)
        except Exception as exc:
            logger.warning("Skipping update %s that could not be parsed (%s; keys: %s)",
                           raw["update_id"], type(exc).__name__, sorted(raw))
            raw = {"update_id": raw["update_id"]}
        cleaned.append(raw)
    return cleaned


class ResilientBot(ExtBot):
    async def _do_post(self, endpoint: str, data, *args, **kwargs):
        result = await super()._do_post(endpoint, data, *args, **kwargs)
        if endpoint == "getUpdates":
            return sanitize_updates(result)
        return result


GROUP_COMMANDS = [
    BotCommand("group_info", "Show who I know in this chat"),
    BotCommand("alias", "Give someone a nickname: /alias @user name"),
    BotCommand("removealias", "Remove a nickname: /removealias @user name"),
    BotCommand("clearaliases", "Remove all nicknames in this chat"),
    BotCommand("enable", "Owner only: let me work in this group",
               api_kwargs={"is_ephemeral": True}),
    BotCommand("disable", "Owner only: stop me in this group",
               api_kwargs={"is_ephemeral": True}),
]
OWNER_COMMANDS = [BotCommand("start", "Show status and groups waiting for approval")]


class TelegramBot:
    def __init__(self, services: Services, *, request=None, get_updates_request=None):
        """``request`` / ``get_updates_request`` replace the HTTP layer (tests)."""
        self.services = services
        token = services.bootstrap.telegram_bot_token
        bot = ResilientBot(
            token=token,
            request=request or HTTPXRequest(connection_pool_size=16),
            get_updates_request=get_updates_request or HTTPXRequest(),
        )
        self.application: Application = (
            ApplicationBuilder()
            .bot(bot)
            .concurrent_updates(16)
            .build()
        )
        self.recorder = Recorder(services)
        self.access = ChatAccess(services, self.application.bot)
        services.access = self.access
        self.commands = GroupCommands(services)
        self.responder = Responder(services, self.recorder)
        self._register()

    def _register(self) -> None:
        app = self.application
        groups = filters.ChatType.GROUPS
        private = filters.ChatType.PRIVATE

        app.add_handler(TypeHandler(Update, self._track_update), group=-2)
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & groups,
                                       self.recorder.on_message), group=-1)
        app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & groups,
                                       self.recorder.on_edit), group=-1)

        app.add_handler(ChatMemberHandler(self.access.on_my_chat_member,
                                          ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(CallbackQueryHandler(self.access.on_callback,
                                             pattern=rf"^{CALLBACK_PREFIX}:"))
        app.add_handler(CommandHandler("enable", self.access.on_enable_command, filters=groups))
        app.add_handler(CommandHandler("disable", self.access.on_disable_command, filters=groups))
        app.add_handler(CommandHandler("start", self.commands.start, filters=groups))
        app.add_handler(CommandHandler("alias", self.commands.alias, filters=groups))
        app.add_handler(CommandHandler("removealias", self.commands.removealias, filters=groups))
        app.add_handler(CommandHandler("clearaliases", self.commands.clearaliases, filters=groups))
        app.add_handler(CommandHandler("group_info", self.commands.group_info, filters=groups))
        app.add_handler(MessageHandler(
            filters.UpdateType.MESSAGE & groups & ~filters.COMMAND, self.responder.on_message))
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & private,
                                       self.access.on_private_message))
        app.add_error_handler(self._on_error)

    async def _track_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = self.services.status
        status.last_update_at = time.time()
        status.telegram_connected = True
        status.telegram_error = None
        chat = update.effective_chat
        current_chat_id.set(self.services.chats.resolve(chat.id) if chat else None)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        kind = type(update).__name__ if update is not None else "none"
        logger.error("Error while handling an update (%s)", kind, exc_info=context.error)

    async def _after_login(self, application: Application) -> None:
        """Record who the bot is and register its commands. Must run after
        initialize(): PTB only calls post_init hooks from run_polling(), and
        this bot is started by hand next to the web admin."""
        me = await application.bot.get_me()
        name = me.first_name or me.username
        self.services.status.bot = BotIdentity(id=me.id, username=me.username, name=name)
        can_read = getattr(me, "can_read_all_group_messages", None)
        self.services.status.can_read_all_group_messages = can_read
        self.services.status.telegram_connected = True
        logger.info("Logged in as @%s (id %s); can_read_all_group_messages=%s",
                    me.username, me.id, can_read)
        if not can_read:
            logger.warning("Group Privacy is on: turn it off in BotFather, then remove and "
                           "re-add the bot to each group, or it only sees mentions.")
        await self._set_commands(application)

    async def _set_commands(self, application: Application) -> None:
        bot = application.bot
        try:
            await bot.delete_my_commands(scope=BotCommandScopeDefault())
            await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
            owner = self.services.bootstrap.owner_user_id
            if owner is not None:
                await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(owner))
        except TelegramError as exc:
            logger.warning("Could not register the command menu: %s", exc)

    def polling_error(self, exc: TelegramError) -> None:
        """Called by the updater when get_updates fails."""
        self.services.status.telegram_connected = False
        self.services.status.telegram_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Telegram polling error: %s", exc)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.application.initialize()
        await self._after_login(self.application)
        await self.application.start()
        await self.application.updater.start_polling(
            allowed_updates=ALLOWED_UPDATES,
            error_callback=self.polling_error,
        )

    async def stop(self) -> None:
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
