from collections import defaultdict, deque
from typing import Optional
from config import config, ChatHistoryType
import logging

logger = logging.getLogger("chat_history")

from abc import ABC, abstractmethod

# ==================== Interface (Abstract Base) ====================
class IChatHistory():
    """Abstract base class for chat history implementations."""
    
    @abstractmethod
    def clear(self, chat_id: int) -> None:
        """Clear chat history for a specific chat."""
        pass
    
    @abstractmethod
    def add_user_message(self, chat_id: int, sender_name, sender_message) -> None:
        """Add a user message to chat history."""
        pass
    
    @abstractmethod
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        """Add an assistant message to chat history."""
        pass
    
    @abstractmethod
    def get_chat_history(self, chat_id: int) -> list:
        """Get chat history for a specific chat."""
        pass
    
# ==================== Implementation: In-Memory (Deque) ====================
class InMemoryChatHistory(IChatHistory):
    """In-memory chat history using deque"""
    
    def __init__(self):
        self._history = defaultdict(lambda: deque(maxlen=config.chat_history.max_history))
    
    def clear(self, chat_id: int):
        logger.info("Clearing Chat History")
        self._history[chat_id].clear()
    
    def add_user_message(self, chat_id: int, sender_name, sender_message) -> None:
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
        return list(self._history[chat_id])


# Factory Pattern
def create_chat_history() -> IChatHistory:
    match config.chat_history.storage_type:
        case ChatHistoryType.REDIS:
            return InMemoryChatHistory()
        case ChatHistoryType.MEMORY:
            return InMemoryChatHistory()
        case _:
            logger.warning("Unknown storage type, defaulting to MEMORY")
            return InMemoryChatHistory()

# Global Instance
chat_history: IChatHistory = Optional[IChatHistory]

def setup():
    global chat_history
    logger.info("Setting up chat history")
    if not config.chat_history.enabled:
        chat_history = InMemoryChatHistory()  # Default to this, but it's set to 1
    else:
        chat_history = create_chat_history()