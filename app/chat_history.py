from abc import ABC, abstractmethod
from collections import defaultdict, deque
from copy import deepcopy
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
    def add_user_message(
        self,
        chat_id: int,
        sender_name: str,
        sender_message: str,
        *,
        telegram_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        """Add a user message to chat history."""

    @abstractmethod
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        """Add an assistant message to chat history."""

    @abstractmethod
    def get_chat_history(self, chat_id: int) -> list:
        """Get chat history for a specific chat."""

    @abstractmethod
    def get_raw_chat_history(self, chat_id: int) -> list:
        """Get unmerged chat history in chronological storage order."""

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
    
    def add_user_message(
        self,
        chat_id: int,
        sender_name: str,
        sender_message: str,
        *,
        telegram_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        self._history[chat_id].append(_build_user_record(
            sender_name,
            sender_message,
            telegram_message_id=telegram_message_id,
            reply_to_message_id=reply_to_message_id,
            attachments=attachments,
        ))
    
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        self._history[chat_id].append({
            "role": "assistant",
            "content": bot_response
        })
    
    def get_chat_history(self, chat_id: int) -> list:
        return merge_consecutive_roles(self.get_raw_chat_history(chat_id))

    def get_raw_chat_history(self, chat_id: int) -> list:
        return deepcopy(list(self._history[chat_id]))
    
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
    
    def add_user_message(
        self,
        chat_id: int,
        sender_name,
        sender_message,
        *,
        telegram_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        message = json.dumps(_build_user_record(
            sender_name,
            sender_message,
            telegram_message_id=telegram_message_id,
            reply_to_message_id=reply_to_message_id,
            attachments=attachments,
        ))
        self.push_new_message(key, message)
    
    def add_assistant_message(self, chat_id: int, bot_response: str) -> None:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        message = json.dumps({
            "role": "assistant",
            "content": bot_response
        })
        self.push_new_message(key, message)

    
    def get_chat_history(self, chat_id: int) -> list:
        return merge_consecutive_roles(self.get_raw_chat_history(chat_id))

    def get_raw_chat_history(self, chat_id: int) -> list:
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        messages = self.r.lrange(key, 0, -1)
        return [json.loads(msg) for msg in messages]
    
    def get_curr_len(self, chat_id):
        key = f"{REDIS_CHAT_PREFIX}{chat_id}"
        return self.r.llen(key)
    
    def push_new_message(self, key, message):
        pipe = self.r.pipeline()
        pipe.rpush(key, message)
        pipe.ltrim(key, -config.chat_history.max_history, -1)
        pipe.execute()


# --------- Util Functions ---------
def _build_user_record(
    sender_name: str,
    sender_message: str,
    *,
    telegram_message_id: int | None = None,
    reply_to_message_id: int | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    record = {
        "role": "user",
        "content": f"{sender_name}: {sender_message}",
    }
    # Omitting these fields for legacy callers keeps old text-only records and
    # their tests byte-for-byte compatible. The Telegram handler supplies them
    # for every newly observed message, including an empty attachment list.
    if telegram_message_id is not None or attachments is not None:
        record.update({
            "sender_name": sender_name,
            "telegram_message_id": telegram_message_id,
            "reply_to_message_id": reply_to_message_id,
            "attachments": deepcopy(attachments or []),
        })
    return record


def render_history_message(message: dict) -> dict:
    """Convert one stored event to an OpenAI-compatible chat message."""
    rendered = {"role": message["role"], "content": message.get("content", "")}
    if message.get("role") != "user":
        return rendered

    message_id = message.get("telegram_message_id")
    sender_name = message.get("sender_name")
    attachments = message.get("attachments") or []
    if message_id is None and not attachments:
        return rendered

    if message_id is not None and sender_name:
        text = f"[Telegram message {message_id} from {sender_name}]\n{rendered['content']}"
    elif message_id is not None:
        text = f"[Telegram message {message_id}]\n{rendered['content']}"
    else:
        text = rendered["content"]

    if not attachments:
        return {"role": "user", "content": text}

    parts: list[dict] = []
    total = len(attachments)
    for index, attachment in enumerate(attachments, start=1):
        label = (
            "[Image 1 follows]"
            if total == 1
            else f"[Image {index} of {total} follows]"
        )
        parts.append({
            "type": "text",
            "text": f"{text}\n{label}" if index == 1 else label,
        })
        mime_type = attachment.get("mime_type") or "image/jpeg"
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{attachment.get('base64', '')}",
            },
        })
    return {"role": "user", "content": parts}


def _as_content_parts(content) -> list[dict]:
    if isinstance(content, list):
        return deepcopy(content)
    return [{"type": "text", "text": str(content)}]


def merge_consecutive_roles(messages: list) -> list:
    merged_messages = []
    for message in messages:
        if merged_messages and merged_messages[-1]['role'] == message['role']:
            previous = merged_messages[-1]['content']
            current = message['content']
            if isinstance(previous, str) and isinstance(current, str):
                merged_messages[-1]['content'] = '\n'.join([previous, current])
            else:
                merged_messages[-1]['content'] = (
                    _as_content_parts(previous)
                    + [{"type": "text", "text": "\n"}]
                    + _as_content_parts(current)
                )
        else:
            # Copy so the in-place content merge above never mutates the
            # caller's stored dicts (the in-memory deque hands out live
            # references; mutating them duplicates/corrupts history on reads).
            merged_messages.append(deepcopy(message))
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
