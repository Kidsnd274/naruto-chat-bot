import asyncio
from datetime import datetime
import logging
import os

from chat_history import chat_history
from chat_metadata import chat_metadata
from config import config
from dotenv import load_dotenv
from telegram import ReplyParameters, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai_client

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("telegram_bot")

NUDGE_HINT = "(nudge — no message text; look at recent chat and respond appropriately)"


def build_context_message(
    chat_info: dict,
    chat_type: str,
    bot_username: str,
    current_speaker: dict,
    now: datetime,
) -> dict:
    """Assemble the per-call system context block.

    chat_info: output of chat_metadata.get_chat_info(chat_id)
    current_speaker: {"display_name": str, "username": Optional[str]}
    """
    lines = ["## Chat Context"]

    chat_name = chat_info.get("chat_name") or ""
    if chat_name:
        lines.append(f"Chat: {chat_name} ({chat_type})")
    else:
        lines.append(f"Chat type: {chat_type}")

    lines.append(f"Time: {now.strftime('%Y-%m-%d %H:%M %z').strip()}")
    lines.append(f"You are: @{bot_username}")

    speaker_username = current_speaker.get("username")
    speaker_display = current_speaker.get("display_name") or "Unknown"
    if speaker_username:
        lines.append(f"Currently replying to: {speaker_display} (@{speaker_username})")
    else:
        lines.append(f"Currently replying to: {speaker_display} (no @username)")

    users = chat_info.get("users") or []
    if users:
        lines.append("")
        lines.append("## Members")
        for u in users:
            handle = f"@{u['username']}" if u.get("username") else "no @username"
            alias_part = ""
            if u.get("aliases"):
                alias_part = f" [aliases: {', '.join(u['aliases'])}]"
            lines.append(f"- {u['display_name']} ({handle}){alias_part}")

    return {"role": "system", "content": "\n".join(lines)}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/start command received from user {user.id} (@{user.username}) in chat {chat_id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm gonna be Hokage some day!")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/clear command received from user {user.id} (@{user.username}) in chat {chat_id}")
    chat_history.clear(chat_id)
    await context.bot.send_message(chat_id=chat_id, text="My memory is wiped!")


def _parse_alias_args(args: list[str]) -> tuple[str, str] | None:
    """Returns (target_username, alias) or None if args are malformed."""
    if not args or len(args) < 2:
        return None
    target = args[0]
    if not target.startswith("@") or len(target) < 2:
        return None
    alias = " ".join(args[1:]).strip()
    if not alias:
        return None
    return target, alias


async def alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/alias from user {user.id} (@{user.username}) in chat {chat_id}: {context.args}")

    parsed = _parse_alias_args(context.args)
    if parsed is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: /alias @user <alias>",
        )
        return

    target_username, alias_name = parsed
    target_id = chat_metadata.find_user_id_by_username(chat_id, target_username)
    if target_id is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"I don't know {target_username} yet — they have to send a message in this chat first.",
        )
        return

    chat_metadata.add_alias(chat_id, target_id, alias_name)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Got it — {target_username} is now also known as '{alias_name}'.",
    )


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/removealias from user {user.id} (@{user.username}) in chat {chat_id}: {context.args}")

    parsed = _parse_alias_args(context.args)
    if parsed is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: /removealias @user <alias>",
        )
        return

    target_username, alias_name = parsed
    target_id = chat_metadata.find_user_id_by_username(chat_id, target_username)
    if target_id is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"I don't know {target_username} in this chat.",
        )
        return

    ok = chat_metadata.remove_alias(chat_id, target_id, alias_name)
    if not ok:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{target_username} doesn't have the alias '{alias_name}'.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Removed alias '{alias_name}' from {target_username}.",
    )


async def clearaliases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/clearaliases from user {user.id} (@{user.username}) in chat {chat_id}")
    chat_metadata.clear_aliases(chat_id)
    await context.bot.send_message(chat_id=chat_id, text="All aliases cleared in this chat.")


async def group_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    logger.info(f"/group_info from user {user.id} (@{user.username}) in chat {chat_id}")

    info = chat_metadata.get_chat_info(chat_id)

    lines = []
    chat_name = info.get("chat_name") or "(unknown)"
    lines.append(f"Chat: {chat_name} ({chat_type})")
    lines.append(f"History length: {chat_history.get_curr_len(chat_id)} messages")

    users = info.get("users") or []
    if users:
        lines.append("")
        lines.append("Members:")
        for u in users:
            handle = f"@{u['username']}" if u.get("username") else "no @username"
            alias_part = ""
            if u.get("aliases"):
                alias_part = f" [aliases: {', '.join(u['aliases'])}]"
            lines.append(f"- {u['display_name']} ({handle}){alias_part}")
    else:
        lines.append("")
        lines.append("Members: (none seen yet)")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_message_id = update.message.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id
    user = update.effective_user

    logger.debug(f"Message received: chat_id={chat_id}, user_id={user.id}, type={chat_type}")

    # Whitelist check - explicit handling for each chat type
    if config.whitelist.enabled:
        if chat_type in ("group", "supergroup"):
            if update.message.chat_id not in config.whitelist.groups:
                logger.warning(f"Blocked group chat {chat_id} - not in whitelist")
                return
        elif chat_type == "private":
            if update.message.chat_id not in config.whitelist.ids:
                logger.warning(f"Blocked private chat from user {user.id} (@{user.username}) - not in whitelist")
                return
        else:
            logger.warning(f"Blocked unsupported chat type '{chat_type}' from chat {chat_id}")
            return

    # Update roster / chat metadata before any early returns so we learn about lurkers too.
    display_name = user.full_name or user.username or f"User {user.id}"
    chat_metadata.update_user(chat_id, user.id, display_name, user.username)
    chat_title = update.effective_chat.title or update.effective_chat.first_name or ""
    chat_metadata.set_chat_name(chat_id, chat_title)

    # Add user message to history
    sender_name = display_name
    chat_history.add_user_message(chat_id, sender_name, user_message)

    # Trigger logic for groups: must include @bot_username. Bare mention -> nudge.
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in user_message:
            logger.debug(f"Ignoring group message without bot mention in chat {chat_id}")
            return
        user_message = user_message.replace(f"@{bot_username}", "").strip()
        if not user_message:
            logger.info(f"Bare @mention nudge from user {user.id} in chat {chat_id}")
            user_message = NUDGE_HINT

    if not user_message:
        logger.debug(f"Empty message after processing, ignoring (chat {chat_id})")
        return

    logger.info(f"Processing message from user {user.id} (@{user.username}) in {chat_type} chat {chat_id}: {user_message[:50]}{'...' if len(user_message) > 50 else ''}")

    async def keep_typing():
        while True:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING
            )
            await asyncio.sleep(4.5)

    typing_task = asyncio.create_task(keep_typing())

    # Build context message + history
    context_msg = build_context_message(
        chat_info=chat_metadata.get_chat_info(chat_id),
        chat_type=chat_type,
        bot_username=context.bot.username,
        current_speaker={"display_name": display_name, "username": user.username},
        now=datetime.now().astimezone(),
    )
    messages = [context_msg] + chat_history.get_chat_history(chat_id)

    if config.chat_history.debug:
        logger.info(f"=== Conversation for chat {chat_id} ({len(messages)} messages incl. context) ===")
        for i, msg in enumerate(messages):
            logger.info(f"  [{i}] {msg['role']}: {msg['content'][:200]}{'...' if len(msg['content']) > 200 else ''}")
        logger.info("=== End ===")

    # Send to AI
    try:
        response = await ai_client.chat(messages)
    except Exception as e:
        logger.error(f"Error getting AI response for chat {chat_id}: error={e}")
        typing_task.cancel()
        return
    finally:
        typing_task.cancel()

    if response is None or not hasattr(response, 'choices') or not response.choices:
        logger.error(f"Invalid AI response for chat {chat_id}: response={response}")
        bot_response = "Sorry, I couldn't get a response right now. Please try again later."
    else:
        bot_response = response.choices[0].message.content
        chat_history.add_assistant_message(chat_id, bot_response)

    reply_params = (
        ReplyParameters(message_id=user_message_id)
        if chat_type in ("group", "supergroup")
        else None
    )

    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=bot_response,
            parse_mode="Markdown",
            reply_parameters=reply_params,
        )
    except Exception as e:
        logger.warning(f"Markdown parsing failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=bot_response,
            reply_parameters=reply_params,
        )


def setup(token):
    global logger

    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('clear', clear))
    application.add_handler(CommandHandler('alias', alias))
    application.add_handler(CommandHandler('removealias', removealias))
    application.add_handler(CommandHandler('clearaliases', clearaliases))
    application.add_handler(CommandHandler('group_info', group_info))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, respond))

    logger.info("Bot handlers registered, starting polling...")
    application.run_polling()
    logger.info("Bot stopped")


if __name__ == '__main__':  # Outdated usage
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Put it in .env or export it in your shell.")

    logger.info("Starting bot...")
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('clear', clear))
    application.add_handler(CommandHandler('alias', alias))
    application.add_handler(CommandHandler('removealias', removealias))
    application.add_handler(CommandHandler('clearaliases', clearaliases))
    application.add_handler(CommandHandler('group_info', group_info))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, respond))

    application.run_polling()
