import logging
import os

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

whitelisted_groups = []
whitelisted_ids = []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm gonna be Hokage some day!")

async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This will now respond to any text message
    user_message = update.message.text
    chat_type = update.effective_chat.type

    # # Whitelist check - explicit handling for each chat type
    # if chat_type in ("group", "supergroup"):
    #     if update.message.chat_id not in whitelisted_groups:
    #         return
    # elif chat_type == "private":
    #     if update.message.chat_id not in whitelisted_ids:
    #         return
    # else:
    #     # Block all other chat types (channels, etc.)
    #     return

    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in user_message:
            return
        user_message = user_message.replace(f"@{bot_username}", "").strip()

    if not user_message:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    response = await ai_client.chat([
        {
          "role": "user",
          "content": user_message
        }
      ])

    if response is None or not hasattr(response, 'choices') or not response.choices:
        bot_response = "Sorry, I couldn't get a response right now. Please try again later."
    else:
        bot_response = response.choices[0].message.content
    
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=bot_response,
            parse_mode="Markdown"
        )
    except Exception as e:
        # Fallback to plain text if markdown parsing fails
        logger.warning(f"Markdown parsing failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=bot_response
        )

def setup(token):
    global logger

    logger.info("Starting Telegram Bot...")
    application = ApplicationBuilder().token(token).build()
    
    start_handler = CommandHandler('start', start)
    respond_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, respond)

    application.add_handler(start_handler)
    application.add_handler(respond_handler)
    
    application.run_polling()




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