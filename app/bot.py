import asyncio
from datetime import datetime
import logging
import os
import re
from typing import NamedTuple

from chat_history import chat_history, merge_consecutive_roles
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

# Portable approximation for OpenAI-compatible servers using different model
# tokenizers. The actual tokenizer may vary, so logs explicitly call this an
# estimate. Per-message overhead accounts for role/template delimiters.
_ESTIMATED_BYTES_PER_TOKEN = 4
_ESTIMATED_MESSAGE_OVERHEAD_TOKENS = 4
_ESTIMATED_REPLY_PRIMER_TOKENS = 2

# Optional [REPLY] prefix the LLM can use to ask Telegram to thread its
# response as a reply to the triggering message. Tolerates whitespace and
# common markdown wrapping (e.g. **[REPLY]**) since local models sometimes
# decorate the marker.
_REPLY_MARKER = re.compile(r'^\s*\**\s*\[reply\]\s*\**\s*', re.IGNORECASE)


def parse_reply_marker(text: str) -> tuple[bool, str]:
    """Detect a leading [REPLY] marker. Returns (should_reply, cleaned_text)."""
    if not text:
        return False, text
    m = _REPLY_MARKER.match(text)
    if m:
        return True, text[m.end():]
    return False, text


def _strip_bot_mentions(messages: list[dict], bot_username: str) -> list[dict]:
    """Return a copy of `messages` with `@{bot_username}` removed from each
    content string. History stays faithful to what users typed; we just hide
    the handle from the LLM at call time because local models tend to mirror
    it back into their own responses."""
    token = f"@{bot_username}"
    cleaned = []
    for m in messages:
        content = m.get("content", "")
        if token in content:
            content = content.replace(token, "")
            content = re.sub(r" {2,}", " ", content)
        cleaned.append({**m, "content": content})
    return cleaned


class MessageBuildResult(NamedTuple):
    messages: list[dict]
    limit_reached: bool
    estimated_tokens_before: int
    estimated_tokens_after: int
    dropped_history_messages: int


def estimate_message_tokens(messages: list[dict]) -> int:
    """Estimate request tokens without depending on a model-specific tokenizer."""
    estimated = _ESTIMATED_REPLY_PRIMER_TOKENS
    for message in messages:
        content_bytes = len(str(message.get("content", "")).encode("utf-8"))
        content_tokens = (
            content_bytes + _ESTIMATED_BYTES_PER_TOKEN - 1
        ) // _ESTIMATED_BYTES_PER_TOKEN
        estimated += _ESTIMATED_MESSAGE_OVERHEAD_TOKENS + content_tokens
    return estimated


def build_llm_messages(
    context_message: dict,
    raw_history: list[dict],
    max_model_tokens: int | None,
) -> MessageBuildResult:
    """Build a valid, bounded LLM transcript from chronological raw history.

    The system message and newest history message are always preserved. Oldest
    messages are removed first, and any assistant message orphaned by storage
    or token trimming is removed with its preceding user turn.
    """
    retained = [dict(message) for message in raw_history]
    dropped = 0

    # A fixed-size store can cut through a turn. Never send an assistant reply
    # whose corresponding user message has already been evicted.
    while retained and retained[0].get("role") == "assistant":
        retained.pop(0)
        dropped += 1

    def assemble() -> list[dict]:
        return [dict(context_message)] + merge_consecutive_roles(retained)

    messages = assemble()
    estimated_before = estimate_message_tokens(messages)
    limit_reached = (
        max_model_tokens is not None
        and estimated_before >= max_model_tokens
    )

    # Keep at least the newest raw history entry (normally the current user
    # message). If that plus the system context is too large, send both and let
    # the warning log make the unavoidable overflow visible.
    while (
        max_model_tokens is not None
        and estimate_message_tokens(messages) > max_model_tokens
        and len(retained) > 1
    ):
        retained.pop(0)
        dropped += 1

        # Removing a user entry can expose its assistant response. Drop that
        # response too so the remaining transcript begins with a user turn.
        while len(retained) > 1 and retained[0].get("role") == "assistant":
            retained.pop(0)
            dropped += 1

        messages = assemble()

    estimated_after = estimate_message_tokens(messages)
    return MessageBuildResult(
        messages=messages,
        limit_reached=limit_reached,
        estimated_tokens_before=estimated_before,
        estimated_tokens_after=estimated_after,
        dropped_history_messages=dropped,
    )


# message_id of the most recent message we've seen per chat — either an
# incoming user message we processed or an outgoing bot reply we sent.
# Used to suppress redundant `[replying to ...]` prefixes when a user replies
# to the immediately preceding message. In-memory only; resets on restart.
_last_seen_message_id: dict[int, int] = {}


def build_reply_prefix(
    reply_to_message,
    bot_id: int,
    last_seen_message_id: int | None,
) -> str:
    """Return `[replying to X: "..."] ` if this message is a Telegram reply
    AND the referent isn't the previous entry in this chat. Returns "" when
    not a reply, or when the referent is the most recently-seen message
    (in which case the prefix would just be noise — the LLM can see the
    referent on the line above)."""
    if reply_to_message is None:
        return ""
    if (
        last_seen_message_id is not None
        and reply_to_message.message_id == last_seen_message_id
    ):
        return ""

    from_user = reply_to_message.from_user
    if from_user and from_user.id == bot_id:
        quoted_name = "you"
    elif from_user:
        quoted_name = from_user.full_name or from_user.username or "someone"
    else:
        quoted_name = "someone"

    quoted_text = (reply_to_message.text or reply_to_message.caption or "").strip()
    if quoted_text:
        return f'[replying to {quoted_name}: "{quoted_text}"] '
    return f"[replying to {quoted_name}] "


def build_context_message(
    chat_info: dict,
    chat_type: str,
    bot_username: str,
    current_speaker: dict,
    now: datetime,
    persona: str = "",
) -> dict:
    """Assemble the per-call system message.

    Combines an optional persona prompt with the runtime chat-context block
    into a single `system` message — keeping the array shape friendly to
    local models that don't love multiple consecutive system messages.

    chat_info: output of chat_metadata.get_chat_info(chat_id)
    current_speaker: {"display_name": str, "username": Optional[str]}
    persona: optional system prompt loaded from disk (config.system_prompt)
    """
    lines = []
    if persona:
        lines.append(persona)
        lines.append("")
    lines.append("## Chat Context")

    chat_name = chat_info.get("chat_name") or ""
    if chat_name:
        lines.append(f"Chat: {chat_name} ({chat_type})")
    else:
        lines.append(f"Chat type: {chat_type}")

    lines.append(f"Time: {now.strftime('%Y-%m-%d %H:%M %z').strip()}")

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

    lines.append("")
    lines.append("## Reply behavior")
    lines.append(
        "By default your message is sent as a normal chat message. "
        "If you want Telegram to thread your response as a reply to the "
        "triggering message (e.g. you're directly answering a question or "
        "addressing the user who pinged you), START your response with the "
        "literal token [REPLY] followed by a space. Do not use [REPLY] for "
        "general chatter or asides — only when you're directly responding to "
        "the most recent message."
    )

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

    # Add user message to history, annotating Telegram replies so the LLM
    # can see what the user is responding to. Done BEFORE the trigger checks
    # so the bot observes the whole group conversation, not only the messages
    # that ping it — otherwise it loses all the surrounding context.
    sender_name = display_name
    reply_prefix = build_reply_prefix(
        update.message.reply_to_message,
        context.bot.id,
        _last_seen_message_id.get(chat_id),
    )
    chat_history.add_user_message(chat_id, sender_name, reply_prefix + user_message)
    _last_seen_message_id[chat_id] = update.message.message_id

    # Trigger logic for groups: must @mention the bot OR reply to one of its messages.
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        is_reply_to_bot = (
            update.message.reply_to_message is not None
            and update.message.reply_to_message.from_user is not None
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        has_mention = f"@{bot_username}" in user_message
        if not has_mention and not is_reply_to_bot:
            logger.debug(f"Ignoring group message without bot mention or reply in chat {chat_id}")
            return
        # Letting the bot see its own @handle in user turns — the identity hint
        # in build_context_message tells it that @{bot_username} refers to
        # itself. Uncomment to revert to stripping.
        # user_message = user_message.replace(f"@{bot_username}", "").strip()
        # if not user_message:
        #     logger.info(f"Bare @mention nudge from user {user.id} in chat {chat_id}")
        #     user_message = NUDGE_HINT

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
        persona=config.system_prompt,
    )
    raw_messages = _strip_bot_mentions(
        [context_msg] + chat_history.get_raw_chat_history(chat_id),
        context.bot.username,
    )
    message_build = build_llm_messages(
        context_message=raw_messages[0],
        raw_history=raw_messages[1:],
        max_model_tokens=config.max_model_tokens,
    )
    messages = message_build.messages

    if message_build.limit_reached:
        unavoidable = (
            " System context and the current message alone exceed the limit."
            if message_build.estimated_tokens_after > config.max_model_tokens
            else ""
        )
        logger.warning(
            "Max model token count reached for chat %s: limit=%s, "
            "estimated_before=%s, estimated_after=%s, "
            "dropped_history_messages=%s.%s",
            chat_id,
            config.max_model_tokens,
            message_build.estimated_tokens_before,
            message_build.estimated_tokens_after,
            message_build.dropped_history_messages,
            unavoidable,
        )

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
        should_reply = False
    else:
        raw_response = response.choices[0].message.content
        should_reply, bot_response = parse_reply_marker(raw_response)
        # Store the cleaned response (without the marker) in history so it
        # doesn't influence future turns or leak into the visible transcript.
        chat_history.add_assistant_message(chat_id, bot_response)

    reply_params = (
        ReplyParameters(message_id=user_message_id)
        if should_reply and chat_type in ("group", "supergroup")
        else None
    )

    try:
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=bot_response,
            parse_mode="Markdown",
            reply_parameters=reply_params,
        )
    except Exception as e:
        logger.warning(f"Markdown parsing failed: {e}")
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=bot_response,
            reply_parameters=reply_params,
        )
    _last_seen_message_id[chat_id] = sent.message_id


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
