# naruto-chat-bot

A Telegram group assistant that talks like Naruto. It runs against a local OpenAI-compatible model server (for example Gufo serving Qwen), records the group's conversation in SQLite, and comes with a small web admin for approving groups, browsing what it stored and changing settings.

- **Group-only.** It answers when someone mentions it or replies to it. Private messages are only for the owner, to approve groups.
- **Approve once.** A group the bot is added to stays pending until the owner approves it. Pending and disabled groups get no replies and nothing is recorded.
- **Remembers the chat.** Every message in an enabled group is stored (text, sender, replies, media as markers such as `[photo]`), with full-text search. Images are only downloaded when someone asks about one.
- **Web admin** on localhost: dashboard, chats, message browser, settings, logs.

## Setup

### 1. Telegram (BotFather)

- **Group Privacy: off** (`/setprivacy` → Disable), so the bot receives every group message. The setting applies when the bot joins a group: if it joined while privacy was on, remove it and add it again. The dashboard warns if privacy is still on.
- After adding the bot to a group, make it an **admin with “Pin messages”** (optionally “Delete messages”), for the pinned board planned in a later phase. The web admin shows missing rights.

### 2. `.env`

`.env` holds only secrets and bootstrap values. Everything else is a setting in the database, edited in the web admin.

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
OPENAI_API_KEY=anything-for-a-local-server
ADMIN_PASSWORD=choose-a-long-password
OWNER_USER_ID=123456789
```

| Variable | Description | Default |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | *required* |
| `OPENAI_API_KEY` | API key for the model server (local servers usually ignore it) | placeholder |
| `ADMIN_PASSWORD` | Web admin password. Without it the web admin is off. | *unset* |
| `OWNER_USER_ID` | Your Telegram user ID (ask [@userinfobot](https://t.me/userinfobot)). Approvals go to this account; everyone else's DMs are ignored. | *unset* |
| `DATABASE_PATH` | SQLite file | `data/naruto.db` (`/data/naruto.db` in Docker) |
| `WEB_HOST` | Web admin bind address | `127.0.0.1` (`0.0.0.0` inside Docker) |
| `WEB_PORT` | Web admin port | `8765` |

### 3. First start: seeding from the old configuration

On the very first start (empty database) the bot copies these into the database once:

- `OPENAI_BASE_URL`, `OPENAI_MODEL`, `MEDIA_ENABLED`, `MAX_MODEL_TOKENS`, `MAX_MEDIA_BYTES`, `ESTIMATED_IMAGE_TOKENS` from the environment,
- `model_params`, `max_model_tokens`, `max_media_bytes`, `estimated_image_tokens` and `whitelisted_groups` (which become enabled groups) from `config.json` (see [`config.example.json`](config.example.json)),
- the persona from `system_prompt.md`, if that file exists and is not empty.

After that these sources are ignored; the Settings page shows a notice when they no longer match the database. Without them the defaults apply: the endpoint defaults to `http://localhost:8080/v1`, and an empty model name uses the first model the server lists.

## Running

### Docker Compose (recommended)

```bash
docker compose up -d --build
```

The database lives in the `naruto-data` volume. The web admin is published on the host's `127.0.0.1:8765` only; from another machine use an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 your-server
# then open http://127.0.0.1:8765/
```

To seed from an existing `config.json` or `system_prompt.md`, uncomment the matching lines in [`docker-compose.yml`](docker-compose.yml) before the first start. On Fedora with SELinux, add `:Z` to bind mounts.

### Without Docker

Requires Python 3.14 and `ffmpeg` on `PATH` (for frames of videos without a Telegram thumbnail).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m naruto
```

## Using it

**Approving a group** — any of these:

- Tap **Approve** in the DM the bot sends you when it is added (or send it `/start` to list pending groups).
- Send `/enable` in the group. It is an ephemeral command: only you and the bot see it, and only the owner can use it. `/disable` stops the bot.
- Use **Enable** on the web admin's Chats page.

**In a group:** mention the bot or reply to one of its messages. Other commands: `/group_info`, `/alias @user name`, `/removealias @user name`, `/clearaliases`. There is no `/clear`: delete stored messages from the chat's page in the web admin.

**Web admin** (`http://127.0.0.1:8765/`):

| Page | What it does |
| --- | --- |
| Dashboard | Bot, Telegram and model status, pending groups, recent errors |
| Chats | Every group with status, admin rights and message counts; enable, disable, leave. Each chat has its roster (with aliases), a searchable message browser and data deletion. |
| Import | Upload a Telegram Desktop export to add history from before the bot joined (see below). |
| Agent runs | One row per bot response: the exact prompt sent, the answer, timing and errors. |
| Settings | Every setting with validation, history, revert and reset. Changes apply immediately. |
| Logs | Application logs with level, chat and logger filters, and a live tail |

### Importing older history

The bot only sees messages from when it joined. To give it older history:

1. In Telegram Desktop, open the group → ⋮ → **Export chat history**, choose **Machine-readable JSON**, and untick photos, videos, voice messages, stickers and files (media becomes markers such as `[photo]`).
2. On the web admin's **Import** page, upload `result.json`. The preview shows the message count, date range, participants and how much would be kept, and pre-selects the group by chat ID (or name).
3. Click **Import**.

Only messages from before the bot's first recorded message are imported (so nothing is duplicated), and only those inside the imported-messages retention period. Importing the same group again replaces the previous import. The uploaded file is deleted when the import finishes. You can also import into a group the bot hasn't joined yet; it is created as pending.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The code lives in the `naruto` package: `db/` (SQLite schema and repositories), `settings/` (registry, service, one-time seed), `tg/` (Telegram handlers, recorder, approval, sending), `agent/` (prompt building), `web/` (FastAPI admin), plus `llm.py` (model client) and `media.py` (media download and conversion). Plans live in [`plans/`](plans/).

### Upgrading from the Redis version

There is no data migration: the old Redis store had no persistence. Start the new version with your existing `.env` and `config.json` mounted, and the database is seeded from them; older chat history can be imported from a Telegram Desktop export.
