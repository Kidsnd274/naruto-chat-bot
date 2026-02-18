import asyncio
from chat_history import chat_history
import logging
import os

from config import config
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

async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This will now respond to any text message
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
            # Block all other chat types (channels, etc.)
            logger.warning(f"Blocked unsupported chat type '{chat_type}' from chat {chat_id}")
            return

    # Add user message to history  
    sender_name = user.first_name or user.username or f"User {user.id}"
    chat_history.add_user_message(chat_id, sender_name, user_message)

    # Check if received message should be ignored
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

    # DEBUG: Print conversation history
    if config.chat_history.debug:
        logger.info(f"=== Conversation history for chat {chat_id} ({chat_history.get_curr_len(chat_id)} messages) ===")
        for i, msg in enumerate(chat_history.get_chat_history(chat_id)):
            logger.info(f"  [{i}] {msg['role']}: {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}")
        logger.info("=== End history ===")

    # Send Message to AI
    try:
        response = await ai_client.chat(chat_history.get_chat_history(chat_id))
    except Exception as e:
        logger.error(f"Error getting AI response for chat {chat_id}: error={e}")
        return
        
    finally:
        typing_task.cancel()

    # Check AI response
    if response is None or not hasattr(response, 'choices') or not response.choices:
        logger.error(f"Invalid AI response for chat {chat_id}: response={response}")
        bot_response = "Sorry, I couldn't get a response right now. Please try again later."
    else:
        bot_response = response.choices[0].message.content
        chat_history.add_assistant_message(chat_id, bot_response)
    
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




if __name__ == '__main__':  # Outdated usage
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