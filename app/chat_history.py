from collections import defaultdict, deque
import config
import logging

logger = logging.getLogger("chat_history")

# Initializing Chat History
chat_history = defaultdict(lambda: deque(maxlen=config.max_chat_history))

# Clear Chat History
def clear(chat_id):
    global chat_history
    logger.info("Clearing Chat History")
    chat_history[chat_id].clear()
    
def add_user_message(chat_id, sender_name, sender_message):
    global chat_history
    chat_history[chat_id].append({
        "role": "user",
        "content": f"{sender_name}: {sender_message}"
    })
    
def add_assistant_message(chat_id, bot_response):
    global chat_history
    chat_history[chat_id].append({
        "role": "assistant",
        "content": bot_response
    })
    
def get_chat_history(chat_id) -> list:
    return list(chat_history[chat_id])