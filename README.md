# naruto-chat-bot

A Telegram bot powered by OpenAI API that responds like Naruto.

## Setup

1. Create `.env` file
    ```
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

1. Build image: `docker build --no-cache -t naruto-chat-bot .`
2. Run container: `docker run -v %cd%/config.json:/app/config.json --env-file .env naruto-chat-bot`

Or use the batch files:

- Build: `docker\build_image.bat`
- Run: `docker\run_image.bat`

## Environment Variables

| Variable | Description |
| -------- | ----------- |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `OPENAI_API_KEY` | Your OpenAI API key |
| `OPENAI_BASE_URL` | Your OpenAI base URL (e.g., custom endpoint) |
| `OPENAI_MODEL` | Model name to use (default: qwen3-30b-a3b-2507-instruct) |
| `WHITELIST_ENABLED` | Enable whitelist checking (default: true) |
| `CHAT_HISTORY_ENABLED` | Enable chat history (default: false) |
| `CONFIG_PATH` | Path to config.json (default: config.json) |
