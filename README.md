# naruto-chat-bot

A Telegram group assistant that talks like Naruto. It runs against a local OpenAI-compatible model server (for example Gufo serving Qwen), records the group's conversation in SQLite, and comes with a small web admin for approving groups, browsing what it stored and changing settings.

- **Group-only.** It answers when someone mentions it or replies to it. Private messages are only for the owner, to approve groups.
- **Approve once.** A group the bot is added to stays pending until the owner approves it. Pending and disabled groups get no replies and nothing is recorded.
- **Remembers the chat.** Every message in an enabled group is stored (text, sender, replies, media as markers such as `[photo]`), with full-text search. Images are only downloaded when someone asks about one.
- **Gets things done.** It can look further back in the chat, keep a pinned board of plans, decisions and open questions, post a plan with Confirm / Change buttons, start polls and pin messages. Each answer is a bounded agent run: a few model requests and tool calls at most.
- **Remembers the group.** Messages are kept until the owner deletes them, and stay searchable. A rolling digest of what's going on and long-term memory notes (people's preferences, traditions, running jokes) come from live chat; each reply reads only a bounded recent window plus the digest and notes, however long the history. Members can ask it to remember or forget things, and the owner can edit everything in the web admin.
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
- **Remember older times** from history summaries, dated summaries of past months or weeks ("what were we planning in summer 2021?"), and by searching the stored messages. It says when an answer comes from a summary rather than the messages themselves.
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
| Chats | Every group with status, admin rights, message and memory-note counts; enable, disable, leave. Each chat has its roster (with aliases), its memory notes, the digest (view, edit, update now), history summaries, reminders, the board (edit, send, clear) and proposed plans, a searchable message browser and data deletion (messages older than N days, before a date, or all; the digest, board, memory and history summaries). |
| History (per chat) | Every history summary: search, filter by date, edit (earlier versions are kept) and delete; live months waiting for their summary, or failed (with **Retry**); and the date live recording began. |
| Memory (per chat) | Every memory note: filter by person, category or text; add, edit, lock (the bot and members can't change a locked note) and delete; each note shows who created it, the messages it came from and its change history. |
| People | Everyone across chats: a display name and aliases that apply in every chat; merge two accounts of one person, or split them. |
| Import | Upload a Telegram Desktop export to add history from before the bot joined (see below). |
| Agent runs | One row per bot response: the exact prompt sent, every model request and tool call (arguments and results), the answer, timing and errors. |
| Lab | Tuning runs (budget, your A/B choices, configurations, attempts, the report), activating and reverting a tested configuration, and the API tokens agents use. See [docs/LAB.md](docs/LAB.md). |
| Queue | Every model request running and waiting (replies first, then background work such as digests and history summaries), the capacity limits, pausing background work, cancelling a waiting request, and a history of recent requests with waiting and model time. |
| Settings | Every setting with validation, history, revert and reset. Changes apply immediately. Some (persona, digest frequency, automatic notes, board, images, recent window, progress message) can also be set for one chat on that chat's **Settings for this chat** page. |
| Logs | Application logs with level, chat and logger filters, and a live tail |

### Importing older history

The bot only sees messages from when it joined. To give it older history:

1. In Telegram Desktop, open the group → ⋮ → **Export chat history**, choose **Machine-readable JSON**, and untick photos, videos, voice messages, stickers and files (media becomes markers such as `[photo]`).
2. On the web admin's **Import** page, upload `result.json`. The preview pre-selects the group by chat ID (or name).
3. Choose what to do, each with its own dates (whole days in the configured time zone):
   - **Import chat messages:** kept as searchable chat, however old, until you delete them.
   - **Make history summaries:** dated summaries, monthly, weekly or one for the whole range, of any dates in the export. The bot looks them up when asked about earlier times. They are made from the uploaded file, so redoing them later needs the file again.

   An import never adds memory notes or starts the rolling digest: those come from live chat only. Imported messages are still in the bot's recent context and searches.

   The estimate below updates as you change things: how many messages are kept, summarized or skipped and why, how many model requests it takes, and which existing summaries would be reused or replaced.
4. Under **People in this export**, check the names: each sender is matched to their Telegram account, and the name box starts with the name the export uses (your contact name for them). Pick "Same person as" if someone is really another entry.
5. Click **Start**.

Nothing from after live recording began (shown on the chat's History page; set when the bot was enabled) is imported or summarized from the export, so nothing is counted twice. Importing messages for some dates replaces earlier imported messages in those dates only. Uploading the same export again reuses the summaries that would come out the same; to rebuild summaries that overlap the chosen dates, tick **Replace them** (and **including edited ones** to replace summaries you corrected). Replaced summaries stay in use until all their replacements are done. You can also import into a group the bot hasn't joined yet; it is created as pending.

Summaries keep the model busy in the background (replies to people go first; see the Queue page). The Import page shows each stage's progress, and can **pause**, **resume** and **cancel the rest**; pausing or cancelling takes effect at once, even while the import waits for the model. If a summary keeps failing, the import pauses; the uploaded file is kept for 7 days from the first pause (Settings → History) so it can resume, and a restart continues where it stopped. A partly written summary is continued only if the summary settings and the messages it read are unchanged; otherwise that period starts over. Old summaries being replaced stay in use until every replacement in their group is done, and one you edit meanwhile is left as it is. The file is deleted when everything is done.

**Live chat** gets a history summary too: once a month is over, its live messages are summarized (Settings → History → Summarize live chat monthly; it can be turned off per chat). The messages themselves stay either way. A month whose summary keeps failing (three attempts, counted across restarts) is marked failed and later months go ahead; **Retry** on the History page tries it again.

### Keeping and deleting messages

Live and imported messages are kept until you delete them; nothing deletes them on a timer. Each reply still reads only a bounded recent window (Context settings), the digest and some notes, and older messages and summaries are looked up on demand, so a long history doesn't make requests bigger. To delete stored messages, use **Delete data** on the chat's page: messages older than N days (N × 24 hours before you review the deletion), from before a date, or all of them, live, imported or both. You see how many messages match before anything is deleted. This deletes the bot's copies, not the messages in Telegram. Summaries, the digest and memory notes made from them stay (history summaries of those dates note that their messages were deleted); delete those separately if needed. Deletion is refused while an import of the group is running or paused, or while a history summary is being written.

Logs, agent runs, lab runs, the queue's request history and finished reminders are cleaned up after Settings → Cleanup.

### Model server notes

- **Reasoning:** most skills let the model think briefly before answering (Settings → Persona and skills → reasoning, and Model → Reasoning effort, default *low*). Without it, Qwen3.8 often said “Reminder set!” without setting one. If an answer still skips the tool a request needs, the bot asks the model once more (shown as “Asked again” on the Agent runs page).
- **Prompt cache:** the start of each request stays the same from one message to the next (the time now, the board and reminders come last), so the server only reads what's new. A request that misses the cache, such as the first `/summary` in a while or the first reply after a digest update, is noticeably slower.
- **Images** need a model server with vision (for Halogen, `HALOGEN_VISION_TOWER`). Without it the bot answers without seeing the image.
- **Parallel requests** (Model settings or the Queue page, default 1): raise it to the server's number of slots (Halogen: 4, see its `/props`) so replies in different chats and background work don't wait for each other. Replies always start before waiting background work. Background work may use at most **Background parallel requests** (default 1) and never the **Slots kept for replies** (default 1; on a one-slot server a reply waits for the request in progress, then goes first). Each slot of a llama-server-style server gets part of its context, so check that a reply (Context → Input token budget), a digest update (Memory → Tokens per update) and a history summary (History → Tokens per request), each plus its output limit, fit one slot. These limits are estimates of the whole request: a reply that would go over drops its oldest recent messages first, then shortens long tool results, every round.
- **History summaries take time:** every period is at least one request; a busy month takes several. The import preview estimates the requests and tokens. On Halogen (about 160 tokens/s for an uncached prompt), a 50,000-message export takes a couple of hours per pass, and memory notes are a second pass. Pause background work on the Queue page if the server is needed for something else; replies are not affected.
- **Progress messages:** when a summary, plan or list of open questions takes longer than 8 seconds, the bot posts “Reading back through the chat…” and then replaces it with the answer (Settings → Behaviour; 0 turns it off).

### Tuning the bot (prompt lab)

The bot learns from experiments, not training. An agent such as Claude Code or Codex can run the lab for you. It tries changes to the persona, the skills' instructions and the model parameters on made-up chats, and compares them with the current configuration. It shows you real replies side by side so you can pick the tone you like, and recommends what to keep. Experiments run in sandboxes through the bot's real reply code and its configured model, behind replies to people in the model queue. Nothing changes the live bot until you (or an agent you allow) activate a tested configuration, which can be reverted.

1. On the web admin's **Lab** page, create a token and save it in `~/.config/naruto-lab/token`.
2. Start Claude Code or Codex in this repository and ask, for example, "Help me choose Naruto's tone". The `naruto-lab` skill (in `.claude/skills/` and `.agents/skills/`) tells it how. The client is `python3 -m naruto.lab`.

The manual, for you and for agents: **[docs/LAB.md](docs/LAB.md)**. It also covers tuning for another model before switching to it, keeping real chat data out unless you allow it, and a walkthrough.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The code lives in the `naruto` package: `db/` (SQLite schema and repositories), `settings/` (registry, service, one-time seed), `tg/` (Telegram handlers, recorder, approval, sending, board, plans, polls, commands, reminders), `agent/` (prompt building, the agent loop in `runner.py`, skills and `tools/`), `memory/` (digest and notes upkeep, history summaries), `web/` (FastAPI admin), `lab/` (the prompt lab: scenarios run in sandboxes), plus `llm.py` (model client with tool calls and a reply-first queue) and `media.py` (media download and conversion). Plans live in [`plans/`](plans/).

Schema changes are migrations in `naruto/db/migrations.py`, applied in order at start (`PRAGMA user_version`), so an existing database is upgraded in place. Version 13 ([plan](plans/done/MEMORY_SIMPLIFICATION_AND_STABILITY_PLAN.md) §8) drops the old import memory columns, keeps one summary per history period (an edited duplicate is kept, detached), and makes unfinished summaries start over once.

### Upgrading from the Redis version

There is no data migration: the old Redis store had no persistence. Start the new version with your existing `.env` and `config.json` mounted, and the database is seeded from them; older chat history can be imported from a Telegram Desktop export.
