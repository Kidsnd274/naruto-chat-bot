# naruto-chat-bot

A Telegram group assistant that talks like Naruto. It runs against a local OpenAI-compatible model server (for example Gufo serving Qwen), records the group's conversation in SQLite, and comes with a small web admin for approving groups, browsing what it stored and changing settings.

- **Group-only.** It answers when someone mentions it or replies to it. Private messages are only for the owner, to approve groups.
- **Approve once.** A group the bot is added to stays pending until the owner approves it. Pending and disabled groups get no replies and nothing is recorded.
- **Remembers the chat.** Every message in an enabled group is stored (text, sender, replies, media as markers such as `[photo]`), with full-text search. Images are only downloaded when someone asks about one.
- **Gets things done.** It can look further back in the chat, keep a pinned board of plans, decisions and open questions, post a plan with Confirm / Change buttons, start polls and pin messages. Each answer is a bounded agent run: a few model requests and tool calls at most.
- **Remembers the group.** A rolling digest of what's going on and long-term memory notes (people's preferences, traditions, running jokes) survive after old messages are deleted by the retention setting. Members can ask it to remember or forget things, and the owner can edit everything in the web admin.
- **Web admin** on localhost: dashboard, chats, message browser, board, agent traces, settings, logs.

## Setup

### 1. Telegram (BotFather)

- **Group Privacy: off** (`/setprivacy` → Disable), so the bot receives every group message. The setting applies when the bot joins a group: if it joined while privacy was on, remove it and add it again. The dashboard warns if privacy is still on.
- After adding the bot to a group, make it an **admin with “Pin messages”** (optionally “Delete messages”), for the pinned board and pins. In a basic group where every member may pin, admin isn't needed. The web admin shows missing rights; it checks them soon after start, every six hours, and whenever a pin works or is refused.

### 2. `.env`

`.env` holds only secrets and bootstrap values. Everything else is a setting in the database, edited in the web admin. Start from the commented template:

```bash
cp .env.example .env
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

**In a group:** mention the bot or reply to one of its messages. Other commands: `/group_info`, `/alias @user name` and `/removealias @user name` (aliases apply in every chat), and `/clearaliases` (owner only). There is no `/clear`: delete stored messages from the chat's page in the web admin.

What it can do when asked, besides chatting:

- **Look things up** further back than the recent messages it sees, including imported history ("what time did Mei say her flight lands?").
- **The board:** one pinned message per group with 🗓 Plans, ✅ Decided and ❓ Open questions, edited in place ("put the BBQ on the board", "mark booking the pit done"). It is sent as a Telegram rich message, or as a plain formatted message if rich messages are refused (Settings → Board).
- **Plans:** "lock in the plan" posts the plan with **✅ Confirm** and **✏️ Change** buttons. Anyone can confirm; a confirmed plan goes on the board. A new plan with the same title replaces an open one.
- **Polls** ("make a poll for Saturday or Sunday"). Votes show up in what the bot reads, including who voted for what in non-anonymous polls.
- **Pins** ("pin the address").
- **Memory:** "remember that Sam is vegetarian", "forget that", "what do you remember about me?". It also keeps durable facts on its own, when someone mentions one while talking to it and while it updates the digest (Settings → Memory → Automatic notes), and never keeps health, money or relationship details unless asked to.
- **Reminders** ("remind us Saturday at 5pm to bring the grill", "remind me in 20 minutes…"), posted in the group when due. If Telegram is briefly unreachable, it tries again.
- **Older images:** "what was in the photo Bob sent this morning?" It downloads the image on demand, describes it once and keeps the description (never the image).

Commands (each goes straight to a focused skill):

| Command | What it does |
| --- | --- |
| `/summary` | Summarize the recent discussion. `/summary today`, `/summary yesterday`, `/summary 3h`, `/summary 2 days`, `/summary <topic>`, or reply to a message with `/summary` to summarize everything since it. |
| `/catchup` | Only you see it (ephemeral): what you missed since you last spoke. If Telegram refuses the private reply, it comes as a DM, or in the group as a last resort. |
| `/plan` | Pull the plan being discussed together and post it with Confirm / Change buttons, putting open points on the board. |
| `/questions` | List the open questions and keep them on the board. |
| `/board` | Show the board again (a new pinned message). |
| `/remember <fact>` | Save a memory note (or reply to a message with `/remember`). |
| `/remind <when> <what>` | Set a reminder. |

**Web admin** (`http://127.0.0.1:8765/`):

| Page | What it does |
| --- | --- |
| Dashboard | Bot, Telegram and model status, pending groups, recent errors |
| Chats | Every group with status, admin rights, message and memory-note counts; enable, disable, leave. Each chat has its roster (with aliases), its memory notes, the digest (view, edit, update now), reminders, the board (edit, send, clear) and proposed plans, a searchable message browser and data deletion (messages, digest, board, memory). |
| Memory (per chat) | Every memory note: filter by person, category or text; add, edit, lock (the bot and members can't change a locked note) and delete; each note shows who created it, the messages it came from and its change history. |
| People | Everyone across chats: a display name and aliases that apply in every chat; merge two accounts of one person, or split them. |
| Import | Upload a Telegram Desktop export to add history from before the bot joined (see below). |
| Agent runs | One row per bot response: the exact prompt sent, every model request and tool call (arguments and results), the answer, timing and errors. |
| Settings | Every setting with validation, history, revert and reset. Changes apply immediately. Some (persona, digest frequency, automatic notes, board, images, recent window, progress message) can also be set for one chat on that chat's **Settings for this chat** page. |
| Logs | Application logs with level, chat and logger filters, and a live tail |

### Importing older history

The bot only sees messages from when it joined. To give it older history:

1. In Telegram Desktop, open the group → ⋮ → **Export chat history**, choose **Machine-readable JSON**, and untick photos, videos, voice messages, stickers and files (media becomes markers such as `[photo]`).
2. On the web admin's **Import** page, upload `result.json`. The preview shows the message count, date range, participants and how much would be kept, and pre-selects the group by chat ID (or name).
3. Under **People in this export**, check the names: each sender is matched to their Telegram account, and the name box starts with the name the export uses (your contact name for them). Pick "Same person as" if someone is really another entry.
4. Click **Import**.

Only messages from before the bot's first recorded message are imported (so nothing is duplicated), and only those inside the imported-messages retention period. Importing the same group again replaces the previous import. You can also import into a group the bot hasn't joined yet; it is created as pending.

After the import, the bot reads the **whole** export in chunks, including messages older than the retention period, and turns what's worth remembering into memory notes; if the group has no digest yet, it builds the first one from the import's last two weeks. The Import page shows the progress. This keeps the model busy for a while (replies to people still go first); turn it off under Settings → Import. The uploaded file is deleted when everything is done.

### Model server notes

- **Reasoning:** most skills let the model think briefly before answering (Settings → Persona and skills → reasoning, and Model → Reasoning effort, default *low*). Without it, Qwen3.8 often said “Reminder set!” without setting one. If an answer still skips the tool a request needs, the bot asks the model once more (shown as “Asked again” on the Agent runs page).
- **Prompt cache:** the start of each request stays the same from one message to the next (the time now, the board and reminders come last), so the server only reads what's new. A request that misses the cache, such as the first `/summary` in a while or the first reply after a digest update, is noticeably slower.
- **Images** need a model server with vision (for Halogen, `HALOGEN_VISION_TOWER`). Without it the bot answers without seeing the image.
- **Parallel requests** (Model settings, default 1): raise it to the server's number of slots (Halogen: 4, see its `/props`) so replies in different chats and background digest updates don't wait for each other.
- **Progress messages:** when a summary, plan or list of open questions takes longer than 8 seconds, the bot posts “Reading back through the chat…” and then replaces it with the answer (Settings → Behaviour; 0 turns it off).

### Evaluating models

`python -m naruto.evaluation` runs a set of cases against one or more models through the same prompt builder the bot uses, and reports the pass rate of automatic checks, time to first token and total latency, plus every answer for review by hand. Cases come from real chats, so keep them and the reports **outside the repository** (the tool warns if you don't).

```bash
# Make a case skeleton from an export: the 60 messages before message 1234567 become the chat.
.venv/bin/python -m naruto.evaluation extract --export ~/Downloads/ChatExport/result.json \
    --trigger 1234567 --out ~/naruto-eval/cases/bbq-plan.json
# ...merge cases into one file and fill in "expect", then compare models:
.venv/bin/python -m naruto.evaluation run ~/naruto-eval/cases.json \
    --model qwen3.8-27b@http://localhost:8080/v1 --model qwen3.8-flash-next@http://localhost:8081/v1 \
    --settings-db data/naruto.db --repeat 3 --out ~/naruto-eval/reports/2026-10-01
```

See [`tests/fixtures/eval_cases.json`](tests/fixtures/eval_cases.json) for the case format (synthetic examples, one per category) and `naruto/evaluation/cases.py` for every field. Checks: `contains_any`, `contains_all`, `not_contains`, `regex`, `min_chars`, `max_chars`, `reply_threaded`; `manual` describes what to judge by hand. `--settings-db` uses the prompts and sampling settings from the bot's database. Every case runs through the bot's agent loop with its tools (Telegram actions such as polls are only recorded), so `tool_calls` checks that the right tools were called, e.g. `[{"name": "create_poll", "arguments": {"options": "saturday"}}]`; `[]` means no tool may be called.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The code lives in the `naruto` package: `db/` (SQLite schema and repositories), `settings/` (registry, service, one-time seed), `tg/` (Telegram handlers, recorder, approval, sending, board, plans, polls, commands, reminders), `agent/` (prompt building, the agent loop in `runner.py`, skills and `tools/`), `memory/` (digest and notes upkeep, import distillation), `web/` (FastAPI admin), plus `llm.py` (model client with tool calls and a reply-first queue) and `media.py` (media download and conversion). Plans live in [`plans/`](plans/).

### Upgrading from the Redis version

There is no data migration: the old Redis store had no persistence. Start the new version with your existing `.env` and `config.json` mounted, and the database is seeded from them; older chat history can be imported from a Telegram Desktop export.
