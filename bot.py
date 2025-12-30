import asyncio
import logging
import os
from collections import defaultdict, deque

from dotenv import load_dotenv
from openai.types.responses import response
from telegram import Update
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

try:
    from config import whitelist, whitelisted_groups, whitelisted_ids, max_chat_history
except ImportError:
    logger.warning("config.py not found, please create the file according to the example. Whitelist disabled.")
    logger.warning("Chat History Disabled")
    whitelist = False
    whitelisted_groups = []
    whitelisted_ids = []
    max_chat_history = 0

# Initializing Conversation History
conversation_history = defaultdict(lambda: deque(maxlen=max_chat_history))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/start command received from user {user.id} (@{user.username}) in chat {chat_id}")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm gonna be Hokage some day!")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"/clear command received from user {user.id} (@{user.username}) in chat {chat_id}")
    conversation_history[chat_id].clear()

async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This will now respond to any text message
    user_message = update.message.text
    user_message_id = update.message.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id
    user = update.effective_user

    logger.debug(f"Message received: chat_id={chat_id}, user_id={user.id}, type={chat_type}")

    # Whitelist check - explicit handling for each chat type
    if whitelist:
        if chat_type in ("group", "supergroup"):
            if update.message.chat_id not in whitelisted_groups:
                logger.warning(f"Blocked group chat {chat_id} - not in whitelist")
                return
        elif chat_type == "private":
            if update.message.chat_id not in whitelisted_ids:
                logger.warning(f"Blocked private chat from user {user.id} (@{user.username}) - not in whitelist")
                return
        else:
            # Block all other chat types (channels, etc.)
            logger.warning(f"Blocked unsupported chat type '{chat_type}' from chat {chat_id}")
            return

    # Add user message to history  
    sender_name = user.first_name or user.username or f"User {user.id}"
    conversation_history[chat_id].append({
        "role": "user",
        "content": f"{sender_name}: {user_message}"
    })

    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in user_message:
            logger.debug(f"Ignoring group message without bot mention in chat {chat_id}")
            return
        user_message = user_message.replace(f"@{bot_username}", "").strip()

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

    # # DEBUG: Print conversation history
    # logger.debug(f"=== Conversation history for chat {chat_id} ({len(conversation_history[chat_id])} messages) ===")
    # for i, msg in enumerate(conversation_history[chat_id]):
    #     logger.debug(f"  [{i}] {msg['role']}: {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}")
    # logger.debug("=== End history ===")

    try:
        response = await ai_client.chat(list(conversation_history[chat_id]))
    finally:
        typing_task.cancel()

    if response is None or not hasattr(response, 'choices') or not response.choices:
        logger.error(f"Invalid AI response for chat {chat_id}: response={response}")
        bot_response = "Sorry, I couldn't get a response right now. Please try again later."
    else:
        bot_response = response.choices[0].message.content
        conversation_history[chat_id].append({
            "role": "assistant",
            "content": bot_response
        })
    
    if chat_type not in ("group", "supergroup"):
        user_message_id = None

    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=bot_response,
            parse_mode="Markdown",
            reply_to_message_id=user_message_id
        )
    except Exception as e:
        # Fallback to plain text if markdown parsing fails
        logger.warning(f"Markdown parsing failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=bot_response,
            reply_to_message_id=user_message_id
        )

def setup(token):
    global logger

    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(token).build()
    
    start_handler = CommandHandler('start', start)
    respond_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, respond)

    application.add_handler(start_handler)
    application.add_handler(respond_handler)
    
    logger.info("Bot handlers registered, starting polling...")
    application.run_polling()
    logger.info("Bot stopped")




if __name__ == '__main__':
    load_dotenv()  # reads .env in the current working directory (if present)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Put it in .env or export it in your shell.")

    logger.info("Starting bot...")
    application = ApplicationBuilder().token(token).build()
    
    start_handler = CommandHandler('start', start)
    respond_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, respond)

    application.add_handler(start_handler)
    application.add_handler(respond_handler)
    
    application.run_polling()