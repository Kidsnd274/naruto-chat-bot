from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Optional
from config import config, ChatHistoryType
import json
import logging
import redis

logger = logging.getLogger("chat_history")

# ==================== Interface (Abstract Base) ====================
class IChatHistory(ABC):
    """Abstract base class for chat history implementations."""

    @abstractmethod
    def clear(self, chat_id: int) -> None:
        """Clear chat history for a specific chat."""

    @abstractmethod
    def add_user_message(self, chat_id: int, sender_name: str, sender_message: str) -> None:
        """Add a user message to chat history."""

    @abstractmethod
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        """Add an assistant message to chat history."""

    @abstractmethod
    def get_chat_history(self, chat_id: int) -> list:
        """Get chat history for a specific chat."""

    @abstractmethod
    def get_curr_len(self, chat_id: int) -> int:
        """Get chat history length for a specific chat."""
    
# ==================== Implementation: In-Memory (Deque) ====================
class InMemoryChatHistory(IChatHistory):
    """In-memory chat history using deque"""
    
    def __init__(self):
        self._history = defaultdict(lambda: deque(maxlen=config.chat_history.max_history))
    
    def clear(self, chat_id: int):
        logger.info("Clearing Chat History")
        self._history[chat_id].clear()
    
    def add_user_message(self, chat_id: int, sender_name: str, sender_message: str) -> None:
        self._history[chat_id].append({
            "role": "user",
            "content": f"{sender_name}: {sender_message}"
        })
    
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        self._history[chat_id].append({
            "role": "assistant",
            "content": bot_response
        })
    
    def get_chat_history(self, chat_id: int) -> list:
        return merge_consecutive_roles(list(self._history[chat_id]))
    
    def get_curr_len(self, chat_id: int) -> int:
        return len(self._history[chat_id])
    
# ==================== Implementation: Redis ====================
REDIS_CHAT_PREFIX = "chat:history:"

class RedisChatHistory(IChatHistory):
    """In-memory chat history using Redis"""
    
    def __init__(self):
        host = config.redis_config.host
        port = config.redis_config.port
        db = config.redis_config.db
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        try:
            logger.info("Connecting to Redis...")
            self.r.ping()
            logger.info("Connected to Redis successfully")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis at {host}:{port} - {e}")
            raise SystemExit("Redis connection failed. Exiting...")
    
    def clear(self, chat_id: int):
        logger.info(f"Clearing Chat History for chat {chat_id}")
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        self.r.delete(key)
    
    def add_user_message(self, chat_id: int, sender_name, sender_message) -> None:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        message = json.dumps({
            "role": "user",
            "content": f"{sender_name}: {sender_message}"
        })
        self.push_new_message(key, message)
    
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        message = json.dumps({
            "role": "assistant",
            "content": bot_response
        })
        self.push_new_message(key, message)

    
    def get_chat_history(self, chat_id: int) -> list:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        
        messages = self.r.lrange(key, 0, -1)
        return merge_consecutive_roles([json.loads(msg) for msg in messages])
    
    def get_curr_len(self, chat_id):
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        return self.r.llen(key)
    
    def push_new_message(self, key, message):
        pipe = self.r.pipeline()
        pipe.rpush(key, message)
        pipe.ltrim(key, -config.chat_history.max_history, -1)
        pipe.execute()


# --------- Util Functions ---------
def merge_consecutive_roles(messages: list) -> list:
    merged_messages = []
    for message in messages:
        if merged_messages and merged_messages[-1]['role'] == message['role']:
            merged_messages[-1]['content'] = '\n'.join([merged_messages[-1]['content'], message['content']])
        else:
            # Copy so the in-place content merge above never mutates the
            # caller's stored dicts (the in-memory deque hands out live
            # references; mutating them duplicates/corrupts history on reads).
            merged_messages.append(dict(message))
    return merged_messages

# Factory Pattern
def create_chat_history() -> IChatHistory:
    match config.chat_history.storage_type:
        case ChatHistoryType.REDIS:
            return RedisChatHistory()
        case ChatHistoryType.MEMORY:
            return InMemoryChatHistory()
        case _:
            logger.warning("Unknown storage type, defaulting to MEMORY")
            return InMemoryChatHistory()

# Global Instance
chat_history: Optional[IChatHistory] = None

def setup():
    global chat_history
    logger.info("Setting up chat history")
    if not config.chat_history.enabled:
        chat_history = InMemoryChatHistory()  # max_history is forced to 1 when disabled
    else:
        chat_history = create_chat_history()