from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Optional
from config import config, ChatHistoryType
import json
import logging
import redis

logger = logging.getLogger("chat_metadata")


# ==================== Interface (Abstract Base) ====================
class IChatMetadata(ABC):
    """Abstract base class for per-chat metadata (chat name + user roster + aliases)."""

    @abstractmethod
    def update_user(self, chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
        """Upsert a user in the roster. Preserves existing aliases. Last-seen wins for name/username."""

    @abstractmethod
    def set_chat_name(self, chat_id: int, chat_name: str) -> None:
        """Set / refresh the chat's display name."""

    @abstractmethod
    def add_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        """Add an alias for a user. Returns False if user not in roster."""

    @abstractmethod
    def remove_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        """Remove an alias for a user. Returns False if user/alias not found."""

    @abstractmethod
    def clear_aliases(self, chat_id: int) -> None:
        """Wipe all aliases in the chat (keeps the roster itself)."""

    @abstractmethod
    def find_user_id_by_username(self, chat_id: int, username: str) -> Optional[int]:
        """Look up a user_id by current @username (case-insensitive, leading @ optional)."""

    @abstractmethod
    def get_chat_info(self, chat_id: int) -> dict:
        """Return {chat_name, users: [{user_id, display_name, username, aliases}, ...]}."""


# ==================== Implementation: In-Memory ====================
class InMemoryChatMetadata(IChatMetadata):
    def __init__(self):
        self._chat_names: dict[int, str] = {}
        self._users: dict[int, dict[int, dict]] = defaultdict(dict)

    def update_user(self, chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
        entry = self._users[chat_id].get(user_id)
        if entry is None:
            self._users[chat_id][user_id] = {
                "display_name": display_name,
                "username": username,
                "aliases": [],
            }
        else:
            entry["display_name"] = display_name
            entry["username"] = username

    def set_chat_name(self, chat_id: int, chat_name: str) -> None:
        if chat_name:
            self._chat_names[chat_id] = chat_name

    def add_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        entry = self._users.get(chat_id, {}).get(user_id)
        if entry is None:
            return False
        if alias not in entry["aliases"]:
            entry["aliases"].append(alias)
        return True

    def remove_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        entry = self._users.get(chat_id, {}).get(user_id)
        if entry is None or alias not in entry["aliases"]:
            return False
        entry["aliases"].remove(alias)
        return True

    def clear_aliases(self, chat_id: int) -> None:
        for entry in self._users.get(chat_id, {}).values():
            entry["aliases"] = []

    def find_user_id_by_username(self, chat_id: int, username: str) -> Optional[int]:
        target = username.lstrip("@").lower()
        for user_id, entry in self._users.get(chat_id, {}).items():
            current = (entry.get("username") or "").lower()
            if current and current == target:
                return user_id
        return None

    def get_chat_info(self, chat_id: int) -> dict:
        users = [
            {
                "user_id": user_id,
                "display_name": entry["display_name"],
                "username": entry["username"],
                "aliases": list(entry["aliases"]),
            }
            for user_id, entry in self._users.get(chat_id, {}).items()
        ]
        return {
            "chat_name": self._chat_names.get(chat_id, ""),
            "users": users,
        }


# ==================== Implementation: Redis ====================
REDIS_NAME_PREFIX = "chat:meta:name:"
REDIS_USERS_PREFIX = "chat:meta:users:"


class RedisChatMetadata(IChatMetadata):
    def __init__(self):
        host = config.redis_config.host
        port = config.redis_config.port
        db = config.redis_config.db
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        try:
            logger.info("Connecting to Redis (metadata)...")
            self.r.ping()
            logger.info("Connected to Redis (metadata) successfully")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis at {host}:{port} - {e}")
            raise SystemExit("Redis connection failed. Exiting...")

    def _users_key(self, chat_id: int) -> str:
        return f"{REDIS_USERS_PREFIX}{chat_id}"

    def _name_key(self, chat_id: int) -> str:
        return f"{REDIS_NAME_PREFIX}{chat_id}"

    def _get_user(self, chat_id: int, user_id: int) -> Optional[dict]:
        raw = self.r.hget(self._users_key(chat_id), str(user_id))
        return json.loads(raw) if raw else None

    def _put_user(self, chat_id: int, user_id: int, entry: dict) -> None:
        self.r.hset(self._users_key(chat_id), str(user_id), json.dumps(entry))

    def update_user(self, chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
        entry = self._get_user(chat_id, user_id)
        if entry is None:
            entry = {"display_name": display_name, "username": username, "aliases": []}
        else:
            entry["display_name"] = display_name
            entry["username"] = username
        self._put_user(chat_id, user_id, entry)

    def set_chat_name(self, chat_id: int, chat_name: str) -> None:
        if chat_name:
            self.r.set(self._name_key(chat_id), chat_name)

    def add_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        entry = self._get_user(chat_id, user_id)
        if entry is None:
            return False
        if alias not in entry["aliases"]:
            entry["aliases"].append(alias)
            self._put_user(chat_id, user_id, entry)
        return True

    def remove_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        entry = self._get_user(chat_id, user_id)
        if entry is None or alias not in entry["aliases"]:
            return False
        entry["aliases"].remove(alias)
        self._put_user(chat_id, user_id, entry)
        return True

    def clear_aliases(self, chat_id: int) -> None:
        key = self._users_key(chat_id)
        raw_map = self.r.hgetall(key)
        if not raw_map:
            return
        pipe = self.r.pipeline()
        for field, raw in raw_map.items():
            entry = json.loads(raw)
            if entry.get("aliases"):
                entry["aliases"] = []
                pipe.hset(key, field, json.dumps(entry))
        pipe.execute()

    def find_user_id_by_username(self, chat_id: int, username: str) -> Optional[int]:
        target = username.lstrip("@").lower()
        raw_map = self.r.hgetall(self._users_key(chat_id))
        for field, raw in raw_map.items():
            entry = json.loads(raw)
            current = (entry.get("username") or "").lower()
            if current and current == target:
                return int(field)
        return None

    def get_chat_info(self, chat_id: int) -> dict:
        chat_name = self.r.get(self._name_key(chat_id)) or ""
        raw_map = self.r.hgetall(self._users_key(chat_id))
        users = []
        for field, raw in raw_map.items():
            entry = json.loads(raw)
            users.append({
                "user_id": int(field),
                "display_name": entry["display_name"],
                "username": entry["username"],
                "aliases": list(entry.get("aliases", [])),
            })
        return {"chat_name": chat_name, "users": users}


# ==================== Factory + Singleton ====================
def create_chat_metadata() -> IChatMetadata:
    match config.chat_history.storage_type:
        case ChatHistoryType.REDIS:
            return RedisChatMetadata()
        case ChatHistoryType.MEMORY:
            return InMemoryChatMetadata()
        case _:
            logger.warning("Unknown storage type, defaulting to MEMORY")
            return InMemoryChatMetadata()


chat_metadata: Optional[IChatMetadata] = None


def setup():
    global chat_metadata
    logger.info("Setting up chat metadata")
    chat_metadata = create_chat_metadata()
