# naruto-chat-bot

A Telegram bot powered by OpenAI API that responds like Naruto.

## Setup

1. Create `.env` file
    ```env
    TELEGRAM_BOT_TOKEN=xxx
    OPENAI_API_KEY=xxx
    OPENAI_BASE_URL=https://chat.xxx
    OPENAI_MODEL=naruto-chat-bot
    CHAT_HISTORY_ENABLED=true
    WHITELIST_ENABLED=true
    ```

2. Create `config.json` (optional - for whitelist & chat history)

    ```json
    {
        "whitelisted_groups": [123, 456],
        "whitelisted_ids": [111, 222],
        "max_chat_history": 200
    }
    ```

   See [`config.example.json`](config.example.json) for reference.

3. Run `python3 ./app/main.py`

## Docker

### Using Docker Compose (Recommended)

The [`docker-compose.yml`](docker-compose.yml) file is provided for easy deployment with all dependencies.

1. Ensure you have a `.env` file with the required environment variables (see [Environment Variables](#environment-variables))
2. Ensure you have a `config.json` file (optional)
3. Run:

    ```bash
    docker-compose up -d
    ```

### Manual Docker (Legacy)

If you prefer not to use Docker Compose:

1. Build image: `docker build --no-cache -t naruto-chat-bot .`
2. Run container: `docker run -v %cd%/config.json:/app/config.json --env-file .env naruto-chat-bot`

Or use the batch files:

- Build: `docker\build_image.bat`
- Run: `docker\run_image.bat`

## Docker with Redis (Chat History)

Redis is included by default in [`docker-compose.yml`](docker-compose.yml). No additional configuration is needed - just enable chat history in your `.env`:

```env
CHAT_HISTORY_ENABLED=true
CHAT_HISTORY_TYPE=redis
```

> The app automatically detects if you are running in Docker and sets the Redis Host & Port automatically

### Specify Own Redis Server

If you want to use an external Redis server instead of the included one, add this to your `.env`:

```env
REDIS_HOST=your-redis-host
REDIS_PORT=6379
REDIS_DB=0
```

## Environment Variables

| Variable | Description | Default |
| -------- | ----------- | ------- |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token | *Required* |
| `OPENAI_API_KEY` | Your OpenAI API key | *Required* |
| `OPENAI_BASE_URL` | Your OpenAI base URL (e.g., custom endpoint) | *Required* |
| `OPENAI_MODEL` | Model name to use | `qwen3-30b-a3b-2507-instruct-unsloth-settings` |
| `WHITELIST_ENABLED` | Enable whitelist checking | `true` |
| `CHAT_HISTORY_ENABLED` | Enable chat history | `false` |
| `CHAT_HISTORY_TYPE` | Storage type: `memory` or `redis` | `memory` |
| `DEBUG_CHAT_HISTORY` | Log conversation history before each AI request | `false` |
| `MAX_CHAT_HISTORY` | Max messages per chat (in memory mode) | `1` |
| `CONFIG_PATH` | Path to config.json | `config.json` |
| **Redis (optional)** | | |
| `REDIS_HOST` | Redis server hostname | `localhost` (Docker: `redis`) |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_DB` | Redis database number | `0` |
| `REDIS_PASSWORD` | Redis authentication password (currently not implemented) | *None* |
