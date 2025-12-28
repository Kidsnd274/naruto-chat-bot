import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm gonna be Hokage some day!")

async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This will now respond to any text message
    user_message = update.message.text
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=f"You said: {user_message}"
    )

if __name__ == '__main__':
    load_dotenv()  # reads .env in the current working directory (if present)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Put it in .env or export it in your shell.")

    application = ApplicationBuilder().token(token).build()
    
    start_handler = CommandHandler('start', start)
    respond_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, respond)

    application.add_handler(start_handler)
    application.add_handler(respond_handler)
    
    application.run_polling()