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
    groups: list[int] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    
@dataclass
class ChatHistoryConfig:
    enabled: bool = False
    max_history: int = 1
    storage_type: ChatHistoryType = ChatHistoryType.MEMORY
    debug: bool = False
    
@dataclass
class RedisConfig:
    host: str = "localhost"
    port: int = 6379
    db: int = 0

@dataclass
class MediaConfig:
    enabled: bool = False
    max_bytes: int = 20 * 1024 * 1024
    estimated_image_tokens: int = 2048

@dataclass
class ModelParams:
    """Optional sampling overrides. Any field left as None is omitted from
    the request entirely so the inference server's default is used."""
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    chat_template_kwargs: Optional[dict] = None

_DockerRedisDefault = RedisConfig(host="redis", port=6379, db=0)

# --- Singleton AppConfig ---

class AppConfig:
    _instance: Optional["AppConfig"] = None
    
    def __new__(cls) -> "AppConfig":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
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
        self.system_prompt = self._load_system_prompt()
        self.max_model_tokens = self._load_max_model_tokens(raw)
        self.media = self._load_media_config(raw)
        self.model_params = self._load_model_params(raw)

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
        debug = os.getenv("DEBUG_CHAT_HISTORY", "false").lower() == "true"
        cfg = ChatHistoryConfig(enabled=enabled, debug=debug)
        
        if enabled:
            max_history = raw.get("max_chat_history", 3)
            if max_history < 3:
                logger.warning("max_chat_history < 3, clamping to 3")
                max_history = 3
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
            cfg.max_history = 1
            cfg.storage_type = ChatHistoryType.MEMORY  # Disable Redis since it's unnecessary
            
        return cfg
    
    def _load_system_prompt(self) -> str:
        path = os.getenv("SYSTEM_PROMPT_PATH", "system_prompt.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
        except FileNotFoundError:
            logger.info(f"No system prompt file at {path} — proceeding without persona.")
            return ""
        if not prompt:
            logger.warning(f"System prompt at {path} is empty — proceeding without persona.")
            return ""
        logger.info(f"Loaded system prompt from {path} ({len(prompt)} chars).")
        return prompt

    def _load_max_model_tokens(self, raw: dict) -> Optional[int]:
        """Load the maximum estimated input tokens allowed per LLM call."""
        value = os.getenv("MAX_MODEL_TOKENS")
        if value is None:
            value = raw.get("max_model_tokens")

        if value is None or value == "":
            logger.info("No max_model_tokens configured — input context is not token-limited.")
            return None

        if isinstance(value, bool):
            logger.warning("Invalid max_model_tokens value; token limiting is disabled.")
            return None

        try:
            max_tokens = int(value)
        except (TypeError, ValueError):
            logger.warning(f"Invalid max_model_tokens '{value}'; token limiting is disabled.")
            return None

        if max_tokens <= 0:
            logger.warning("max_model_tokens must be greater than zero; token limiting is disabled.")
            return None

        logger.info(f"Model input context limit set to approximately {max_tokens} tokens.")
        return max_tokens

    def _load_media_config(self, raw: dict) -> MediaConfig:
        """Load opt-in media ingestion and image token accounting settings."""
        enabled = os.getenv("MEDIA_ENABLED", "false").lower() == "true"
        max_bytes = self._positive_int_setting(
            os.getenv("MAX_MEDIA_BYTES", raw.get("max_media_bytes")),
            default=20 * 1024 * 1024,
            name="max_media_bytes",
        )
        estimated_tokens = self._positive_int_setting(
            os.getenv(
                "ESTIMATED_IMAGE_TOKENS",
                raw.get("estimated_image_tokens"),
            ),
            default=2048,
            name="estimated_image_tokens",
        )
        cfg = MediaConfig(
            enabled=enabled,
            max_bytes=max_bytes,
            estimated_image_tokens=estimated_tokens,
        )
        logger.info(
            "Media ingestion %s — max_bytes=%s, estimated_image_tokens=%s.",
            "enabled" if enabled else "disabled",
            max_bytes,
            estimated_tokens,
        )
        return cfg

    @staticmethod
    def _positive_int_setting(value, default: int, name: str) -> int:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            logger.warning("Invalid %s value; using %s.", name, default)
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid %s value %r; using %s.", name, value, default)
            return default
        if parsed <= 0:
            logger.warning("%s must be greater than zero; using %s.", name, default)
            return default
        return parsed

    def _load_model_params(self, raw: dict) -> ModelParams:
        block = raw.get("model_params") or {}
        params = ModelParams(
            temperature=block.get("temperature"),
            top_p=block.get("top_p"),
            top_k=block.get("top_k"),
            min_p=block.get("min_p"),
            repeat_penalty=block.get("repeat_penalty"),
            chat_template_kwargs=block.get("chat_template_kwargs"),
        )
        set_fields = [k for k, v in params.__dict__.items() if v is not None]
        if set_fields:
            logger.info(f"Model params set: {', '.join(set_fields)}")
        else:
            logger.info("No model_params configured — server defaults will apply.")
        return params

    def _load_redis_config(self) -> RedisConfig:
        is_docker = util.is_docker()
        cfg = RedisConfig()
        
        cfg.host = os.getenv("REDIS_HOST", _DockerRedisDefault.host if is_docker else "localhost")
        cfg.port = int(os.getenv("REDIS_PORT", _DockerRedisDefault.port if is_docker else 6379))
        cfg.db = int(os.getenv("REDIS_DB", _DockerRedisDefault.db if is_docker else 0))

        return cfg
        
        

config = AppConfig()
