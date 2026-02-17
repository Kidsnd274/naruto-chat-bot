import json
import logging
import os

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("config")

whitelist_enabled = False
whitelisted_groups = []
whitelisted_ids = []
max_chat_history = 1  # Chat History of 1 = Disabled

def setup():
    global whitelist_enabled, whitelisted_groups, whitelisted_ids, max_chat_history
    # Default to ../config.json (parent directory) for standalone runs
    # In Docker, config.json will be copied/mounted to /app/config.json
    CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

    chat_history_enabled = os.getenv("CHAT_HISTORY_ENABLED", False)
    whitelist_enabled = os.getenv("WHITELIST_ENABLED", True)

    # Try Reading config path
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        logger.warning(f"Config file not found or invalid at {CONFIG_PATH}. Features disabled.")
        return

    # Setup Whitelist Config
    if whitelist_enabled:
        whitelisted_groups = data.get("whitelisted_groups", [])
        whitelisted_ids = data.get("whitelisted_ids", [])
        logger.info("Whitelist Enabled")
    else:
        logger.info("Whitelist Disabled")

    # Setup Chat History Config
    if chat_history_enabled:
        read_chat_history = data.get("max_chat_history", 1)
        if read_chat_history < 1:
            logger.warning("Max Chat History can't be less than 1, changing it to 1")
            max_chat_history = 1
        else:
            max_chat_history = read_chat_history
        logger.info("Setting Max Chat History to " + str(max_chat_history))
    else:
        logger.info("Chat History Disabled")
        
    logger.info("Config Setup Ready")