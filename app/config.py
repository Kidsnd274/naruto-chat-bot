import json
import logging
import os
import util
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("config")

whitelist_enabled = False
whitelisted_groups = []
whitelisted_ids = []
max_chat_history = 1  # Chat History of 1 = Disabled

class ChatHistoryType(Enum):
    """Chat history storage type."""
    MEMORY = "memory"
    REDIS = "redis"

# --- Sub-configs: each feature own its settings ---
@dataclass
class WhitelistConfig:
    enabled: bool = True  # by default turn on the whitelist
    groups: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    
@dataclass
class ChatHistoryConfig:
    enabled: bool = False
    max_history: int = 1
    storage_type: ChatHistoryType = ChatHistoryType.MEMORY
    
@dataclass
class RedisConfig:
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    
_DockerRedisDefault = RedisConfig(host="redis", port=6379, db=0)

# --- Singleton AppConfig ---

class AppConfig:
    _instance: Optional["AppConfig"] = None
    
    def __new__(cls) -> "AppConfig":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.initialized = False
        return cls._instance
    
    def __init__(self) -> None:
        if not hasattr(self, "_initialized"):
            self._initialized = False

    def setup(self) -> None:
        """Load config from file and environmental variables. Call once at startup."""
        if self._initialized:
            logger.warning("AppConfig.setup() called more than once — skipping.")
            return

        # Default to ../config.json (parent directory) for standalone runs
        # In Docker, config.json will be copied/mounted to /app/config.json
        config_path = os.getenv("CONFIG_PATH", "config.json")
        raw: dict = {}
        
        try:
            with open(config_path, "r") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning(f"Config file not found or invalid at {config_path}. Using defaults.")
            
        self.whitelist = self._load_whitelist(raw)
        self.chat_history = self._load_chat_history(raw)
        self.redis_config = self._load_redis_config()
        
        self._initialized = True
        logger.info("Appconfig ready.")
        
    def _load_whitelist(self, raw: dict) -> WhitelistConfig:
        enabled = os.getenv("WHITELIST_ENABLED", "true").lower() == "true"
        cfg = WhitelistConfig(enabled=enabled)
        
        if enabled:
            cfg.groups = raw.get("whitelisted_groups", [])
            cfg.ids = raw.get("whitelisted_ids", [])
        else:
            logger.info("Whitelist disabled.")
        
        return cfg
    
    def _load_chat_history(self, raw: dict) -> ChatHistoryConfig:
        enabled = os.getenv("CHAT_HISTORY_ENABLED", "false").lower() == "true"
        cfg = ChatHistoryConfig(enabled=enabled)
        
        if enabled:
            max_history = raw.get("max_chat_history", 1)
            if max_history < 1:
                logger.warning("max_chat_history < 1, clamping to 1")
                max_history = 1
            cfg.max_history = max_history
            
            storage_type_raw = os.getenv("CHAT_HISTORY_TYPE", ChatHistoryType.MEMORY.value)
            try:
                cfg.storage_type = ChatHistoryType(storage_type_raw)
            except ValueError:
                logger.warning(f"Unknown CHAT_HISTORY_TYPE '{storage_type_raw}', defaulting to MEMORY.")
                cfg.storage_type = ChatHistoryType.MEMORY

            logger.info(f"Chat history enabled — max={cfg.max_history}, storage={cfg.storage_type.value}.")
        else:
            logger.info("Chat history disabled.")
            
        return cfg
    
    def _load_redis_config(self) -> RedisConfig:
        is_docker = util.is_docker()
        cfg = RedisConfig()
        
        host = os.getenv("REDIS_HOST", _DockerRedisDefault.host if is_docker else "localhost")
        port = os.getenv("REDIS_PORT", _DockerRedisDefault.port if is_docker else 6379)
        db = os.getenv("REDIS_DB", _DockerRedisDefault.db if is_docker else 0)
        
        cfg.host = host
        cfg.port = port
        cfg.db = db
        
        return cfg
        
        

config = AppConfig()