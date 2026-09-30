# Bot rework plan

Status: phases 1–4 implemented on branch `bot_rework` (phases 3–4 on 2026-10-01); phase 5 not started. See §18 for what was built, deviations from this plan and what still needs checking against live Telegram and Gufo. Replaces `OLD_AGENTIC_FEATURE_PLAN.md`; parts of that plan (context layout, bounded agent loop, image descriptions) are carried forward where noted.

## 1. Goal

Make the bot a useful personal assistant for the group that still feels like Naruto:

- Works in a new group with minimal friction: approve once, and it is useful straight away.
- Reads the conversation reliably, including older discussion.
- Helps the group get things done: summarizes topics, consolidates plans and open questions, confirms plans, and pins the result.
- Uses a local model that handles tools reliably, with a detailed persona prompt to keep the Naruto personality.
- Is easy to operate: a small web admin page shows which chats it monitors, what it stored, what it did and why.

## 2. Decisions so far

| Topic | Decision |
| --- | --- |
| Inference | Local only, no cloud. Serve with [Gufo](https://github.com/gufo-org/gufo) on the 128 GB Strix Halo box. |
| Model family | Qwen (better tool use and reliability), trading some roleplay quality. Compensate with a detailed persona prompt. Exact model: see §11. |
| Scope | **Group-only.** DMs are used only for owner admin (approvals); no DM assistant features. |
| Chat history | The bot records every message itself in durable local storage (SQLite). Older history comes from a **manual Telegram Desktop JSON export uploaded through the web admin** (§6). No userbot, no MTProto backfill (most groups are basic groups, where it cannot work). |
| Admin interface | A small password-protected **web admin** in the same process as the bot (§8). |
| Retention | **Configurable, default 30 days** for raw data: live and imported messages, agent traces and logs (Retention settings, §8). Long-term knowledge survives as **group memory** (§9), which does not expire. |
| `/clear` | **Removed.** The new prompt no longer depends on wiping history to recover from confusion, and deleting data is an owner action in the web admin (§8). |
| Web admin access | **Localhost only.** |
| Web search | **Not now.** No external calls. |
| Settings | **The database is the source of truth for all non-secret settings**, and every setting stored there is editable in the web admin. `.env` keeps only secrets and bootstrap values; `config.json` becomes a one-time seed (§8). |
| Bot permissions | BotFather **Group Privacy is off** (already the case), so the bot receives all group messages. The bot is also made a group admin with only the **Pin messages** right (optionally **Delete messages**), needed for the pinned board and for ephemeral replies after 15 seconds. |
| Old plan | Renamed to `OLD_AGENTIC_FEATURE_PLAN.md`. Its "Gemma 12B only" and "no alternate model" decisions no longer apply. |

## 3. Why the current bot is unreliable

From the checked-out code:

- **History does not survive restarts.** `docker-compose.yml` has the Redis volume commented out.
- **History is short.** A 200-message cap, and the token budget drops the oldest messages first.
- **The bot cannot see anything from before it joined.** The Bot API only delivers new messages.
- **The prompt is hard to read.** The whole transcript is merged into one large user message. The model has to find the current request, the speaker and the relevant topic by itself.
- **One model call, no tools.** It cannot look further back, search, pin, or confirm anything.
- **Reasoning is disabled** (`enable_thinking: false`), which hurts summarizing and planning.
- **Whitelisting needs a file edit and a restart.** `config.json` is mounted read-only.
- **No visibility.** There is no way to see what the bot stored, what prompt it sent, or why it answered the way it did, other than server logs.

## 4. Target architecture

```text
Telegram ── Bot account (python-telegram-bot, Bot API 10.x)
              - receives every message (Group Privacy off), edits, commands,
                button presses, poll answers, group upgrades
              - sends normal, rich and ephemeral messages; edits, pins, polls
              │
              ├── Recorder: every message → SQLite
              ├── Agent runner ── skills (prompt + template + tool subset)
              │         │
              │   Gufo (OpenAI-compatible, local)
              │
              └── Web admin (FastAPI, same process): chats, messages,
                  imports, agent traces, logs

SQLite (one file, mounted volume)
  - messages + full-text search (FTS5), imported and live
  - owner, chats (pending / enabled / disabled), aliases
  - per-chat memory: digest, pinned board, plans, reminders
  - image descriptions
  - agent run traces
```

Redis is removed. Only the bot token is used to access Telegram.

## 5. Recording chat history

### What Telegram allows

- **Bot API:** no method for reading chat history. A bot only receives messages from the time it joins.
- **MTProto:** `messages.getHistory` and search are user-account only. Bots may fetch messages by known ID; in supergroups that could backfill by counting IDs backwards, but in basic groups message IDs are not per-chat, so it does not work. Most of the target groups are basic groups, so this is not pursued.
- **New AI-bot features (Bot API 10.x)** do not change this; see "Options considered".

### Approach

1. **Receive every message.** BotFather's Group Privacy is off. The setting is applied when the bot joins a group, so a group the bot joined while it was on keeps the old behaviour until the bot is removed and re-added. At startup, log `getMe().can_read_all_group_messages` (should be `true`).
2. **Store durably.** Only for enabled chats: text or caption, sender ID and name, time, message ID, reply-to ID, thread ID, media reference (Telegram `file_id`, not the bytes), and edits (`edited_message` updates). `source = live`. No fixed 200-message cap; retention is configurable.
3. **Search locally.** SQLite FTS5 provides `search_chat`.
4. **Media on demand.** Keep the `file_id`; download and describe an image only when asked (via `getFile`), then cache the description. No image bytes are stored.

### Group upgrades (basic group → supergroup)

Giving the bot admin rights, passing 200 members, or some settings changes can upgrade a basic group to a supergroup. The chat gets a new `-100…` ID and Telegram sends `migrate_to_chat_id` / `migrate_from_chat_id`. The bot must move the chat's approval, stored messages, memory and board to the new ID, and keep the old ID as an alias so imports that name either ID still resolve.

How to tell the type: supergroup IDs start with `-100`; basic group IDs are shorter negative numbers. The web admin shows the type for each chat.

### Options considered and rejected

| Option | Why not |
| --- | --- |
| Userbot (a user account run by code) | Reads full history, but a server-side login with full account access is a permanent risk. |
| MTProto backfill with the bot token | Only works in supergroups; most target groups are basic groups. |
| Guest Mode (Bot API 10.0) | Bot can be summoned in chats it has not joined, but gets **no chat history or member list**, and only **one reply** per summon. |
| Business / Secretary Mode | Connects a bot to a user's account for private-chat automation. Not a way to read group history. |

## 6. History import (Telegram Desktop export)

### Export (you, once per group)

Telegram Desktop → open the group → ⋮ → **Export chat history**:

- Format: **Machine-readable JSON** (produces `result.json`).
- Untick photos, videos, voice messages, stickers and files: text only keeps the file small. Imported media becomes a marker such as `[photo]`.
- Optionally limit the date range.

### Format (confirmed from a real export, 2026-09-30)

A 40-message sample from a basic group confirmed:

- **Top level:** `name`, `type`, `id` (integer), `messages` (array, ascending by `id`).
- **Chat type and ID mapping:** `type` was `private_group` (basic group) with a 9-digit positive `id`; the Bot API chat ID is **`-id`**. For supergroups (`private_supergroup` / `public_supergroup`) the export `id` is expected to be the channel ID, so the Bot API ID is **`-100` + `id`**. This lets the import page match the target group automatically.
- **Message IDs** were in the millions and non-contiguous: they are the exporting account's IDs and do not match the bot's (as expected for basic groups).
- **Per message:** `id`, `type` (`message`), `date` (local time, no time zone), `date_unixtime` (string; **use this** for timestamps), `from` (display name), `from_id` (`user<digits>`), `text`, `text_entities`.
- **`text`** is usually a string (empty for media without caption) but can be an array mixing strings and entity objects (seen: `{"type": "link", "text": …}`). `text_entities` is a flat list of `{type, text}` (seen: `plain`, `link`). Build the text from `text_entities` or by joining `text` parts.
- **Optional fields seen:** `edited` / `edited_unixtime`; `forwarded_from` / `forwarded_from_id`; `reactions` (`[{type: "emoji", count, emoji, recent}]`); `photo` (relative path such as `photos/…jpg`, with `photo_file_size`, `width`, `height`); `file` (either a relative path or the string `"(File not included. Change data exporting settings to download.)"`), `file_name`, `file_size`, `media_type` (seen: `animation`, `sticker`), `mime_type`, `duration_seconds`, `sticker_emoji`, `thumbnail`.
- **Not in the sample:** `reply_to_message_id` and `type: "service"` messages (with `action`, `actor`, `actor_id`). Handle them as documented, and ignore unknown fields rather than failing.
- **Media:** a `result.json` alone references files that are not present. Store media as markers (`[photo]`, `[sticker 😂]`, `[GIF]`, `[file: name.pdf]`). If a zip of the whole export folder is uploaded later, photos could be described during import; not in the first version.

**Privacy:** the real sample contains friends' messages. It stays outside the repository; tests use a synthetic fixture with the same structure.

### Import flow in the web admin

1. **Upload** `result.json` on the Import page (size limit from settings; stream-parse large files).
2. **Preview:** chat name and type from the file, message count, date range, participants, how many messages fall inside the retention window, and how much overlaps what the bot already recorded.
3. **Choose the target group.** The page pre-selects the group whose chat ID matches the export (`-id` or `-100id`), falling back to a name match; you confirm.
4. **Import** in the background with a progress bar:
   - Only messages **before the first live-recorded message** in that chat, so live and imported history never overlap (export message IDs do not match the bot's).
   - `from_id` `user123` → user ID 123, added to the chat roster with the display name from the export.
   - Stored with `source = import` and the export's IDs kept separately; reply links resolved within the import.
   - Service messages (joins, renames, pins) are skipped or stored as short markers.
   - Re-importing the same chat replaces the previous import, so it is safe to repeat.
5. **Distill memory** (§9): the model reads the import in chunks, including messages older than the retention period, and proposes group memory notes (people, preferences, decisions, recurring plans, running jokes). Only raw messages within the retention period are kept; older ones are discarded after this step.
6. **Build the first digest** from the most recent period.
7. Optionally **propose an initial board** from the digest for you to confirm.

### End-to-end onboarding with import

1. Add the bot to the group, and make it admin with pin rights.
2. Send `/enable` in the group (ephemeral, owner only), or approve from the DM prompt or the web admin. The bot starts recording.
3. Export from Telegram Desktop and upload on the web admin's Import page, pointed at that group.
4. The bot now has live history from step 2 onwards, plus imported history before it.

Import also works for a chat that is still pending, if you want the history in before enabling.

## 7. Onboarding and whitelist

- `OWNER_USER_ID` in `.env`. The chat list (pending / enabled / disabled) lives in SQLite; `config.json` only seeds it.
- When the bot is added to a group (`my_chat_member` update), the chat appears as **pending** in the web admin and the bot DMs the owner: "Added to *Group X* by *Y*. **[Approve] [Leave]**".
- Three equivalent ways to enable or disable: owner-only `/enable` / `/disable` in the group (declared as **ephemeral commands** via `BotCommand.is_ephemeral`, so the group never sees them), the DM buttons, or the web admin.
- Private chats: only the owner. The current `whitelisted_ids` private-chat whitelist is removed; the bot ignores DMs from anyone else.
- Pending or disabled chats: the bot stays silent and records nothing. Optionally it leaves after a timeout.
- On enable: check the bot's admin rights with `getChatMember` (pin required, delete optional) and show what is missing, both to the owner and in the web admin.

## 8. Web admin

### Stack and hosting

- **FastAPI + Jinja2 templates + HTMX**: server-rendered pages, no JavaScript build step, same Python codebase.
- Runs **in the same process** as the bot (python-telegram-bot started on the same asyncio loop instead of `run_polling`), sharing the SQLite store and the live log buffer. One container.
- **Access: localhost only.** Bind to `127.0.0.1`. In Docker, publish the port as `127.0.0.1:<port>:<port>` so it is not reachable from the network. For remote use, an SSH tunnel is enough. It still has a single admin password from `.env` (session cookie) and CSRF protection on every form, since it exposes every stored message of every group.

### Pages

| Page | Contents |
| --- | --- |
| **Dashboard** | Bot online, Telegram connection, Gufo reachable and which model is loaded, requests in progress, recent errors. |
| **Chats** | Every group the bot has seen: name, ID, type (group / supergroup), status (pending / enabled / disabled), admin rights, stored message counts (live / imported), last activity. Actions: enable, disable, leave. |
| **Chat detail** | Message browser with search and filters (sender, date, live / imported); the digest (view and edit); the current board; import history for this chat. |
| **Delete data** (per chat) | Owner-only replacement for `/clear`: delete messages (all, or older than a date), the digest, the board, or memory notes, each separately and with a confirmation step. |
| **Memory** (per chat) | All group memory notes (§9): filter by person or category, add, edit, delete, lock; each note shows where it came from and its change history. |
| **Import** | The upload wizard from §6, with progress. |
| **Agent runs** | One row per bot response: trigger message, skill chosen, prompt size, tool calls with arguments and results, model latency, final text, errors. Expand to see the exact prompt sent. This is the main tool for telling "bad context" from "model not smart enough". |
| **Logs** | Live tail of application logs with level and chat filters. |
| **Settings** | Every setting stored in the database, grouped by section, with validation, defaults and change history (see below). |

Agent traces follow the retention setting like messages, since full prompts contain chat content.

### Settings in the database

Rule: **anything stored in the database can be viewed and changed in the web admin.** That covers the per-chat data above (chat status, aliases, digest, board, reminders, stored messages) and all application settings.

**Where each setting lives:**

| Location | Contents |
| --- | --- |
| `.env` (not editable in the web admin) | Secrets and bootstrap only: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ADMIN_PASSWORD`, `OWNER_USER_ID`, database path, web admin bind address and port. |
| Database (editable) | Everything else. |
| `config.json` | Read once on first start to seed the database (e.g. existing `whitelisted_groups`, `model_params`). Ignored afterwards; the web admin shows a notice if it differs from the database. |

**Settings sections (initial):**

| Section | Examples |
| --- | --- |
| Model | Endpoint URL, model name, sampling parameters (`temperature`, `top_p`, `top_k`, `min_p`, `repeat_penalty`), `chat_template_kwargs`, max output tokens |
| Persona and skills | Persona prompt (replaces `system_prompt.md`, which only seeds it); per-skill instructions and output templates; reasoning on/off per skill |
| Context and memory | Recent-window size, input token budget, digest update frequency, digest size, automatic memory notes on/off, max notes per chat |
| Agent limits | Model requests per run, tool calls per run, run deadline |
| Import | Max upload size, digest window for imports |
| Media | Enabled, max media size, image description length |
| Retention | Days to keep live messages, imported messages, agent traces and logs (default 30 each) |
| Behaviour | Auto-leave timeout for pending groups, progress placeholders on/off |

**Behaviour:**

- **Typed and validated:** each setting has a type, allowed range, default and short description, defined in one code registry that also renders the Settings page. Invalid values are rejected on save.
- **Applied without restart:** the bot reads settings from an in-memory cache that is refreshed on every save. Settings that genuinely need a restart are marked as such.
- **Change history:** every change records the time, the old and new value, and who made it. One-click revert to the previous value or reset to default.
- **Per-chat overrides (optional, later):** e.g. a different persona tone or digest frequency for one group, falling back to the global value.

## 9. Memory and prompt

Four kinds of stored knowledge per group:

| Kind | What it holds | Lifetime | Who writes it |
| --- | --- | --- | --- |
| **Messages** (live + imported) | Raw chat | Retention setting (default 30 days) | Recorder, import |
| **Digest** | "What's going on now": active topics, open threads, short summary | Rolling; replaced as it updates | Bot, after N new messages or a quiet gap, and on `/summary` |
| **Board** | Plans, decisions, open questions the group sees | Until items are done or removed | Bot (with confirmation), members via commands |
| **Group memory (notes)** | Durable facts worth remembering | **No expiry**; changed or deleted explicitly | Bot, members, owner |

### Group memory

Because raw messages expire after the retention period, notes are how the bot remembers things long-term. Each note:

- **Content:** one short fact, e.g. "Sam is vegetarian", "The group does a BBQ every National Day", "Jon's birthday is 3 March", "Running joke: Wei is always late".
- **Category:** person, preference, date, decision, recurring plan, group fact, running joke.
- **Subject:** the person it is about (user ID), if any.
- **Source:** message IDs it came from (links work while the messages are within retention), and who created it (bot / member / owner).
- **Timestamps and history:** created, updated, previous versions.
- **Lock flag:** the owner can lock a note so the bot will not change or delete it.

How notes are written:

1. **Explicitly:** "@bot remember that…", `/remember`, or the `remember` tool. "@bot forget that…" / `forget` removes one.
2. **Automatically** during digest updates and imports. The model proposes notes only for durable facts: things people say about themselves, group decisions, recurring events. Contradicting facts update the existing note rather than adding a duplicate. It should not record sensitive details (health, finances, relationships) unless explicitly asked to remember them.
3. **By the owner** in the web admin.

How notes are used:

- Notes about the people in the current conversation, plus general group notes, go into the prompt as background (bounded by count and tokens).
- A `search_memory(query)` tool finds other notes.
- Members can ask "@bot what do you remember about me?" and get their notes back. The bot is open about what it keeps.

There is no `/clear` command. Deleting messages, the digest, the board or memory notes is done by the owner in the web admin (§8). Members can remove individual notes with "@bot forget that…".

### Search

SQLite FTS5 over live and imported messages: `search_chat(query, from_user?, since?)` and `get_messages_around(message_id)` for anything older or more specific than the recent window, within the retention period.

Prompt layout, carried forward from the old plan §5:

1. Persona and operating rules (stable, cache-friendly prefix)
2. Chat info and members
3. Group memory notes and digest, labelled as background
4. Recent messages, one per line: `[id] Name (time): text`, with reply links
5. **Current request**, clearly labelled, included once

Do not merge everything into one user message. Keep the prefix stable so Gufo's conversation caching reuses it (§11).

## 10. Assistant features

### Telegram features to use

| Feature | Use in this bot | Notes |
| --- | --- | --- |
| **Rich messages** (`sendRichMessage`) | Board, plans, summaries: headings, task lists, tables, collapsible details, embedded buttons | Works in groups. Verify that rich messages can be edited and pinned; otherwise use HTML text for the board. |
| **Ephemeral messages** (`ephemeral_message_parameters`, `receiver_user_id`) | Private replies inside the group: "what did I miss" catch-ups, owner confirmations, confirmation-button feedback | A non-admin bot has only 15 seconds after the user's action; an admin bot can send at any time. Delivery is not guaranteed if the user is offline. |
| **Ephemeral commands** (`BotCommand.is_ephemeral`) | `/enable`, `/disable`, `/catchup` invisible to the group | |
| **Inline buttons, polls** | Plan confirmation, votes | Existing Bot API |
| ~~Draft streaming~~, ~~topics in private chats~~ | Not used | Private chats only; the bot is group-only (§13) |

### The pinned board

One pinned rich message per chat that the bot owns and edits in place:

```text
📌 Board — updated 30 Sep, 7:05 pm
🗓 Plans
  ☑ Sat 4 Oct — BBQ at East Coast, 6 pm (confirmed)
  ☐ Book the pit (Sam)
✅ Decided
  • Split costs on Splitwise
❓ Open questions
  • Who brings the grill?
```

This avoids pinning a new message for every update.

### Tools

| Tool | Purpose |
| --- | --- |
| `get_recent_messages`, `get_messages_around`, `search_chat` | Reading beyond the default window (SQLite, live + imported) |
| `update_board(section, items)` | Edit the pinned board |
| `pin_message(message_id)` / `unpin` | Explicit pins |
| `propose_plan(plan)` | Posts the plan with **[✅ Confirm] [✏️ Change]** buttons; on confirm, moves it to the board |
| `create_poll(question, options)` | Native poll for dates and choices |
| `set_reminder(when, text)` | Scheduled message in the chat |
| `remember(fact)` / `forget(fact)`, `search_memory(query)` | Group memory notes (§9) |
| `describe_image(message_id)` | On-demand vision on a chat image (downloaded via `file_id`, description cached; not available for imported media) |

Keep the tool set per request small. Each skill exposes only what it needs.

### Commands (deterministic entry points)

`/summary [today|since <msg>]`, `/remember`, `/catchup` (ephemeral: what happened since you last spoke), `/plan`, `/questions`, `/board`, `/remind`, `/enable`, `/disable`, plus the existing alias commands. The existing `/clear` command is removed. A command selects the skill directly, so the model does not have to guess the intent.

### Skills

A skill is: focused instructions + output template + tool subset + reasoning on/off.

| Skill | Trigger | Reasoning |
| --- | --- | --- |
| Banter (default) | Mention or reply with no task | Off (brief since 2026-10-01, §18) |
| Summarize topic | `/summary`, `/catchup`, or "what did we talk about…" | On |
| Consolidate plan | `/plan`, or planning talk | On |
| Consolidate questions | `/questions` | On |
| Confirm / decide | Plan ready, or "let's vote" | Off |
| Reminder | `/remind`, or "remind us…" | Off (brief since 2026-10-01, §18) |

Routing: commands map directly. For free-form mentions, the model chooses via a `use_skill` tool or a short classification step, falling back to banter.

### Persona vs content

Naruto voice for chat and for a one-line intro to structured output. Plans, summaries and the board themselves stay clean and scannable.

## 11. Model and serving

### Gufo's supported text models

| Model | Vision | Notes |
| --- | --- | --- |
| Qwen3.8 27B (dense) | Yes | Q4_K_XL or Q8_K_XL; 656 tok/s prompt, up to ~70 tok/s generation |
| Qwen3.8 Flash-Next (MoE) | Yes | Q4_K_XL; 1,628 tok/s prompt, up to ~59 tok/s generation |
| DeepSeek V4 Flash | No | 2-bit quant, ~27 tok/s; not a fit (no vision, slower) |

### Recommendation

Start with **Qwen3.8 27B at Q8_K_XL** as the single model for chat, tools and vision.

- A dense model is usually steadier than a sparse MoE at multi-step tool calls, instruction following and staying in character. That matches the reason for leaving Gemma.
- Q8 costs little here: roughly 30 GB of weights on a 128 GB machine, leaving room for context, and for Lemonade if both run.
- Vision support keeps everything in one model.

Keep **Flash-Next as the fallback** if latency is the problem. Its prompt processing is about 2.5× faster, which matters because prompt size, not generation, dominates response time for this bot. For example, 15k prompt tokens take about 23 s on the 27B and about 9 s on Flash-Next before any cache reuse.

Caveat: these model versions are newer than my training data. The recommendation rests on Gufo's published figures and the general dense-vs-MoE pattern. The evaluation below settles it.

### Evaluation (decides the model)

About 20 cases taken from real group chats (the Telegram exports from §6 are a ready source; keep the evaluation set outside the repository), run against both models through Gufo:

- Answer a direct question in a busy chat without addressing old topics.
- Summarize a 100-message discussion; extract plan, decisions and open questions.
- Choose and call the correct tool with valid arguments (board, poll, search).
- Answer a question needing `search_chat` for older context.
- Stay in character in banter (judged by you).
- Describe an image.

Record pass rate, time to first token and total latency.

### Serving notes

- **Prompt caching:** keep the system prompt and older context as a stable prefix, and put new material at the end so Gufo's conversation cache is reused.
- **Context budget:** aim for 8–16k tokens per request; use search rather than larger windows.
- **Reasoning per skill:** on for summarize and plan; banter, reminders and memory think briefly too (reasoning effort low), because without it Qwen skipped tool calls (§18, 2026-10-01).
- **Fedora:** `sudo setsebool -P container_use_devices true` for `/dev/kfd` in containers. Check port 8080 against Lemonade and Halogen. Watch shared memory if several servers load models.

## 12. Persona prompt

Current `system_prompt.md` (v1, written by the owner) has good foundations: sparing catchphrases, "give real answers", focus on the current question, short replies. Changes for Qwen:

1. **More character.** Qwen fills gaps with a generic upbeat assistant. Add who Naruto is and how he talks: blunt and loud, competitive, fiercely loyal, never gives up on a friend, gets serious and warm when someone is struggling; speech habits (e.g. "Oi!", "Heh", "Alright!", nicknames for people); what he would never say.
2. **Examples.** 6–10 short exchanges: banter, a real recommendation, a serious moment, a summary intro, confirming a plan, admitting he does not know or cannot see something. Qwen follows examples more closely than descriptions.
3. **Drop "or something big you haven't addressed yet".** It invites the model to bring up old topics, which is the "answers everything" problem. Unfinished business goes to the board and `/questions` instead.
4. **Move formatting rules out of the persona.** "No headers or tables" is right for chat replies but wrong for the board and summaries, which will use rich messages. Formatting instructions belong to each skill's output template.
5. **Assistant role and honesty.** He is the group's organizer: tracks plans, decisions and open questions, and says plainly when something is not in the chat history rather than guessing.

The same persona is used for every skill; skills add task and formatting instructions after it.

## 13. Streaming responses

The bot is group-only, and Telegram's native draft streaming (`sendMessageDraft`, `sendRichMessageDraft`) works only in private chats, so it is not used.

In groups:

- **Default:** keep the typing indicator (current behaviour). Optionally show progress for slow skills with a short placeholder ("Reading back through the chat…") that is replaced by the final answer.
- **Optional, later:** simulated streaming by sending a placeholder and editing it in throttled steps (about one edit per second, within group rate limits). Stream only the final answer, not tool-calling turns. Try it before committing, since it adds visual noise.
- Priority: low. Tool loops and prompt time dominate latency.

## 14. Phases

1. **Foundations:**
   - Upgrade python-telegram-bot (currently 22.5, which predates Bot API 10.x). If it still lacks rich or ephemeral messages, call them through `Bot.do_api_request`.
   - SQLite store + message recorder, including edits and group upgrades; remove Redis.
   - Chat list with pending / enabled / disabled, `/enable`, `/disable`, DM approval.
   - Settings registry in the database, seeded from `config.json` and `system_prompt.md`; `.env` reduced to secrets and bootstrap values.
   - Web admin skeleton: Dashboard, Chats, Chat detail (message browser), Settings, Logs.
   - Gufo running Qwen3.8 27B.
2. **Import and context:**
   - Export parser + Import page (§6), with automatic group matching.
   - New prompt layout (§9); persona prompt v2 (§12).
   - Evaluation set from real exports; run it; settle the model.
3. **Agent loop + core tools:** bounded loop (limits from the old plan §4); search tools; board as a rich message, pin, propose-plan with buttons, poll; Agent runs page.
4. **Memory and skills:** digest updates; group memory notes (explicit, automatic, from imports) and the Memory page; retention cleanup job (daily, using the retention settings); commands and skills; `/catchup` as ephemeral; reminders; on-demand image description.
5. **Polish:** progress placeholders or simulated streaming in groups (if wanted); per-chat setting overrides; persona tuning from real use.

## 15. Handoff notes for implementation

- **Approach: restructure in place, not a from-scratch rewrite.** Stay in this repository and branch. Build the new structure alongside the old modules, move proven pieces over with their tests, and delete each old module once nothing uses it. Patching the existing modules one by one is not recommended either: storage, config and prompt building change at their core, and `respond()` in `app/bot.py` (about 200 lines) mixes all of them.
  - **Carry over (with tests):** `app/media.py` (download, conversion, ffmpeg frame extraction); from `app/bot.py`: `[REPLY]` marker parsing, bot-mention stripping, reply-prefix building, trigger rules, Markdown send with plain-text fallback, typing indicator, per-chat single-flight gating; `app/ai_client.py` (small; extend for tools); alias command parsing.
  - **Replace:** `app/chat_history.py` and `app/chat_metadata.py` (Redis / in-memory → SQLite); `app/config.py` (→ settings registry); prompt assembly in `app/bot.py` (→ context builder); `app/main.py` startup (→ one asyncio loop running bot and web admin).
  - **Suggested layout:** a proper package with `db/` (schema, migrations, repositories), `settings/`, `telegram/` (handlers, recorder, commands, callbacks, sending), `agent/` (runner, context builder, skills, tools), `memory/` (digest, notes, retention), `importer/` (export parser), `web/` (FastAPI routes, templates), plus `media.py`. Switch from the current flat imports (`from config import config`) to package imports.
  - **No Redis data migration:** production Redis has no persistence, so its history is already disposable. Seed the database from `config.json` (whitelisted groups, model parameters) and `system_prompt.md`, then use import for older history. Alias data can be re-entered or seeded if the owner wants it.
- **Current code map:** `app/bot.py` (Telegram handlers, trigger logic, prompt assembly, sending), `app/chat_history.py` (Redis / in-memory transcript; to be replaced by SQLite), `app/chat_metadata.py` (roster and aliases; to move into SQLite), `app/config.py` (env + `config.json`; to become the settings registry), `app/ai_client.py` (OpenAI-compatible client), `app/media.py` (media download and conversion; reuse for on-demand image description). Tests are in `tests/`.
- **Keep working behaviour** while replacing internals: trigger rules (mention or reply to the bot), `[REPLY]` marker threading, bot-mention stripping, aliases and `/group_info`, and the per-chat single-flight gating from the latest commit. **Remove** the `/clear` command (and its handler registration and tests).
- **Work in phase order** (§14), keeping the bot runnable at the end of each phase. Extend the existing tests; add tests for the export parser, group-upgrade migration, settings validation and the web admin's auth.
- **Do not touch production data** (the live Redis instance or the server's `config.json`) without the owner's go-ahead; provide a migration path instead.
- **Check before relying on:** python-telegram-bot support for Bot API 10.x (rich and ephemeral messages), whether rich messages can be edited and pinned, export fields not present in the sample (replies, service messages, supergroup IDs), and Gufo's served model ID and tool-calling behaviour.
- **Export sample:** the owner has a real export at `~/Downloads/ChatExport_2026-09-30 (3)/result.json`. Use it to check the parser locally, but never copy it into the repository, tests or logs; build a synthetic fixture instead.
- **Ask the owner** about anything not settled here rather than assuming.

## 16. Settled questions

| Question | Answer (2026-09-30) |
| --- | --- |
| Export format | Confirmed from a real sample (§6). |
| Web admin access | Localhost only. |
| Retention | Configurable, default 30 days for raw data; group memory notes do not expire (§9). |
| `/clear` | Removed; data deletion moves to the web admin. |
| Web search | Not now. |

Remaining details (e.g. max notes per chat, digest frequency) are settings with defaults, adjustable in the web admin.

## 17. Sources

Checked 2026-09-30:

- [Telegram bots overview](https://core.telegram.org/bots#natively-integrate-ai-chatbots) and [Bot Features: AI agents](https://core.telegram.org/bots/features): guest mode, streaming, bot-to-bot, managed bots, business bots, rich and ephemeral messages.
- Telegram Bot API reference and changelog (via Context7): `sendMessageDraft`, `sendRichMessageDraft`, `sendRichMessage`, ephemeral messages and commands (15-second rule), `readBusinessMessage`, Bot API 10.0 guest mode, Bot API 10.3 `can_stop`, `migrate_to_chat_id`.
- Telethon's MTProto reference (via Context7): `messages.getHistory` is restricted to user accounts; `messages.getMessages` / `channels.getMessages` are available to bots by ID.
- Telegram Desktop export format: confirmed against a 40-message real export from a basic group (structure only; content not recorded here).
- Gufo figures are from the user-supplied summary of the [gufo-org/gufo README](https://github.com/gufo-org/gufo) and have not been independently verified.

## 18. Implementation status (2026-09-30)

### Built (phases 1–2)

- `naruto/` package replacing `app/`: SQLite store (`db/`), settings registry with one-time seed (`settings/`), Telegram handlers (`tg/`), prompt building (`agent/`), history import (`importer/`), web admin (`web/`), evaluation harness (`evaluation/`). Redis, `/clear` and the private-chat whitelist are gone.
- Recorder for enabled groups (edits, group upgrades with chat-ID aliases, media as `file_id` plus markers), chat approval (pending by default, owner DM buttons, ephemeral `/enable` / `/disable`, web admin), admin-rights check.
- Web admin on `127.0.0.1:8765`: Dashboard, Chats (roster, aliases, message browser with FTS search, two-step deletion), Import, Agent runs, Settings (validation, history, revert, reset, drift notice), Logs (live tail).
- New prompt layout (§9) with a recent window whose start moves in steps (`context.window_step`) so the prefix stays cacheable; persona v2 (§12) as the default persona.
- Telegram Desktop import (§6) with automatic group matching, overlap and retention rules, replace-on-reimport and a progress bar.
- `python -m naruto.evaluation` (`run`, `extract`) for §11's model comparison.

### After the first live test (2026-10-01)

- Fixed: the bot never learned its own identity, because python-telegram-bot only runs `post_init` from `run_polling()`, so it ignored every mention, showed "Not logged in" and skipped rights checks. End-to-end tests now run the real bot against a fake Bot API.
- `/enable`, `/disable`, the DM buttons and the web admin now say when a chat is already in that state.
- Added global **people** (migration 4): one person per human across chats, with Telegram accounts (user IDs are global) linked to them, a chosen display name and shared aliases. Prompts and the web admin name messages by person, so an import's contact names and live Telegram names no longer look like different people. The People page merges and splits; the import preview maps each sender and pre-fills the export's name. Group memory in phase 4 should attach notes to people.

### Built (phase 3, 2026-10-01)

- **Agent loop** (`naruto/agent/runner.py`): model → tool calls → model, bounded by settings (Agent limits): 4 model requests, 6 tool calls and a 150-second deadline per run, including time queued for the model server. The last allowed request is reserved for the answer (its tool results say no more tools are available); tool calls on it are ignored, and a run that still has no answer sends a short "got stuck" line. Tool results are capped (`agent.tool_result_chars`). Runs in one chat take turns; runs in different chats share the server.
- **LLM client:** OpenAI-style `tools`; parses structured `tool_calls` and, as a fallback, Qwen's inline `<tool_call>{…}</tool_call>` blocks; optional streaming (used by the evaluation for time to first token, including streamed tool calls); replies are served before background requests (ready for phase 4's digests).
- **Tools** (`naruto/agent/tools/`): `search_chat` (words, person, date range), `get_messages_around`, `get_earlier_messages`, `update_board`, `pin_message`, `unpin_message`, `propose_plan`, `create_poll`. Arguments are checked against each tool's schema; errors go back to the model as text so it can retry within the limits.
- **Board** (migration 5): one per chat, three sections (plans, decided, questions), shown to the model as background. Sent as a rich message (`sendRichMessage` with Markdown, through `do_api_request`), pinned silently, and edited in place with `editMessageText` + `rich_message`; if Telegram refuses rich messages it falls back to an HTML message (Settings → Board). Editable, re-sendable and clearable from the chat page.
- **Plans:** posted as HTML messages with Confirm / Change buttons; Confirm (anyone) marks the plan confirmed, edits the message and adds it to the board; Change asks for a mention with the changes. A new proposal with the same title replaces an open one. Open proposals from the last 7 days are background.
- **Polls:** native, non-anonymous by default. The bot now also receives `poll` and `poll_answer` updates, so the stored poll shows vote counts and who voted for what.
- **Agent runs page:** tool names and request counts in the list; every model request (timing, finish reason, reasoning, text, calls) and tool call (arguments, result, errors) on the run page.
- **Evaluation:** every case runs through the agent loop with a recording Telegram stand-in; `tool_calls` expectations are checked and tool calls appear in the report.
- **Fix:** the web admin loads all templates once at startup (`auto_reload=False`). A running process had picked up an edited `base.html` that called `take_flashes()`, which its older Python code didn't provide ("'take_flashes' is undefined" on the dashboard).

### Built (phase 4, 2026-10-01)

- **Digest** (migration 6, `naruto/memory/keeper.py`): per chat, rewritten in the background once 60 new messages arrived, or 10 or more after a 30-minute quiet gap, or on request (`/summary`, the web admin). Each update reads the messages the digest hasn't seen (bounded by tokens; a long backlog takes several updates), and the model answers with JSON: the new digest plus note changes. Updates run at background priority (replies go first), back off 15 minutes after a failure, and are traced as agent runs (skill `digest`).
- **Group memory notes:** per chat, attached to people (`person_id`), with category, source messages, creator, lock flag and full change history (deletions keep the last content). Written explicitly (`remember` / `forget` / `search_memory` tools, `/remember`), automatically during digest updates (Settings → Memory → Automatic notes), from imports, and by the owner. Automatic changes add and update but never delete, and never touch locked notes. Up to 40 notes go into each request, those about the people in the conversation and general notes first, in a stable order for the prompt cache.
- **Prompt background** now also has the notes (`[n12]`), the digest and pending reminders, before the board and open plans (slowest-changing first).
- **Skills** (`naruto/agent/skills.py`): banter (default), summarize, catchup, plan, questions, decide, remind, remember, each with its own instructions, reasoning switch (on for summarize, catchup, plan and questions) and tool subset. Commands pick a skill directly; for free-form mentions banter can hand over with `use_skill` (summarize, plan, questions, decide), which restarts the run with the new skill's prompt and tools within the same limits.
- **Commands:** `/summary [today|yesterday|week|3h|2 days|<topic>]` (or as a reply: since that message), `/catchup` (ephemeral: since the sender's last message; falls back to a DM, then the group), `/plan`, `/questions`, `/board`, `/remember`, `/remind`. Visible skill commands are stored in the transcript and answered as a threaded reply.
- **Reminders:** `set_reminder` / `cancel_reminder` (local `YYYY-MM-DD HH:MM` or "in 2 hours"), sent by a 30-second job (late ones say so), listed and cancellable on the chat page.
- **describe_image:** downloads an older image by its `file_id`, describes it in a separate request (counted in the run's limits) and caches the description with the message; the transcript then shows `[photo: …]`. Imported media can't be described.
- **Import distillation** (`naruto/memory/distill.py`): after an import, the whole export (including messages outside retention) is read in chunks into notes, then the first digest is built from the import's last 14 days if the chat has none. Progress and results on the import page; a restart or three failed parts in a row stops it (notes so far are kept).
- **Retention:** live and imported messages past their retention are deleted by the hourly maintenance job; messages the digest hasn't read yet get 7 more days. Sent and cancelled reminders follow the live-message retention; image descriptions go with their messages.
- **Web admin:** Memory page per chat; digest (view, edit, update now), reminders and memory link on the chat page; deleting the digest and all notes (two steps); distillation progress on the import page; skill hand-overs and image requests in run traces.
- **Fix:** web admin notices were lost when a second one was queued before a page was shown (Starlette 1.7 sessions only save changes made through their own methods).

### Deviations from the plan

- Banter keeps the everyday tools itself (memory, reminders, polls, pins, board, images) and only hands over to summarize, plan, questions and decide, which benefit from their own instructions and reasoning. A hand-over costs a request and a fresh prompt (the system prompt changes), so it is kept for the heavier tasks.
- Automatic note changes never delete (only `forget` and the owner do), to avoid silently losing memory.
- Retention runs hourly rather than daily, with a 7-day grace for messages the digest hasn't read.
- Import distillation doesn't propose an initial board (optional in §6).
- `get_recent_messages` is `get_earlier_messages` (reads back from a message, default: from the start of the recent window); the recent messages are already in the prompt, and the clearer name keeps the model from calling it for them.
- `sendRichMessage` has no `reply_markup`, so plan proposals are ordinary HTML messages with an inline keyboard; only the board is a rich message.
- Tools are offered on every request of a run (not dropped on the last one), because Qwen's chat template puts them in the system prompt and removing them would invalidate the server's prompt cache; the last request is steered by a note instead. `tool_choice` is not sent, since server support varies.
- The new prompt layout landed in phase 1: the responder had to be rewritten anyway once history moved to SQLite.
- A minimal agent-run trace (prompt, answer, timing, errors) and the Agent runs page were pulled forward from phase 3, to inspect the new prompt and the evaluation. Tool calls get added with the agent loop.
- The Telegram subpackage is `naruto/tg/`, not `telegram/`, so it can't be confused with the python-telegram-bot package.
- Images: the trigger's image and the image it replies to are downloaded on demand; older images are markers only (the old bot kept Base64 for every image). Cached descriptions and `describe_image` stay in phase 4.
- Retention: logs and agent runs are cleaned daily, and imports honour the imported-messages retention, but **live messages are not deleted yet**; that waits for group memory in phase 4, so nothing is lost before notes exist.
- `system_prompt.md` moved to `naruto/prompts/persona.md` (the persona setting's default); a `system_prompt.md` present on first start still seeds the database.

### Found in the live test (2026-10-01, Lemonade)

- Gemma 4 31B on Lemonade (nightly ROCm llama-server, MTP speculative decoding, one slot) answered a chat request with the digest update's format, `{"digest": "", "notes": []}`, even when "digest" appeared nowhere in the request (7 of 8 replays). A canary written by the preceding request never carried over, so it looks like server-side state from earlier, similar requests rather than the last one. Mitigated in the bot: the rules no longer describe a Background section (each Background explains itself when present), and a reply that starts with internal JSON is not posted: text after it (or its `reply` field) is kept, otherwise the identical request is sent once more, and failing that the "got stuck" line is sent. The server itself still needs checking: reload the model, and if it recurs, run it without `--spec-type draft-mtp`.

### Phase 3–4 live test fixes (2026-10-01, Halogen + Qwen3.8 Flash-Next)

The owner tested phases 3–4 in a test group, first on Lemonade (Gemma 4 31B), then on Halogen serving `halogen-qwen3.8-flash-next` (4 slots, prompt cache, no vision tower). Stored requests were replayed against Halogen to separate model, prompt and server problems.

- **"Internal JSON instead of a reply"** (every failed `/summary` and "remember that…"): only on Lemonade, where a chat request came back in the digest's JSON format with the digest's field names. Halogen answered a digest request followed by a chat request normally, so this stays a Lemonade/llama-server problem; the guard from the previous test remains.
- **Reminders were never set.** Answers said "I've set a reminder" with no `set_reminder` call, and once those lines were in the transcript the next requests copied them (the transcript shows the bot's words, not its tool calls). With thinking off, Qwen didn't call the tool even with a clean transcript, an explicit rule, or only the two reminder tools; with brief thinking (`reasoning_effort: low`) it called it every time. Fixes: reasoning on by default for banter, reminders and memory, with a **Reasoning effort** setting (default low; Halogen's default is xhigh, which made `/summary` take 43 s); a rule against claiming actions no tool did; and a one-time re-ask when the request clearly asks for an action, or the answer claims one, and no tool did it (`agent/claims.py`, "Asked again" on the run page).
- **Slow replies (17–30 s) were prompt-cache misses.** The header's "Now: …, 01:48" changed every minute; members were listed by last activity (reorders whenever someone else speaks); and the board, open plans and pending reminders sat before the transcript, so every bot action invalidated it. The time now moved to the current request, members are listed by name, and the board, plans and reminders moved next to the current request. Replies after an action now take 5–9 s. Halogen processes an uncached prompt at only about 160 tokens/s, so a cold request (first of a skill, after a digest update) still takes 15–35 s.
- **Polls:** the model can answer `[NO REPLY]` after a poll, plan or board update, and nothing more is sent.
- **Images:** Halogen without a vision tower refuses image input with a 400; the run now asks again without the images instead of failing.
- **Pin right "not checked yet"** in the group where the bot was an admin: rights were only read on join or promotion and on `/enable`. Enabled chats are now checked soon after start and every six hours; a member counts as able to pin where the group lets everyone pin (basic groups do by default); every real pin updates the right.
- **Pin service messages** ("Naruto pinned …") looked like replies to the bot and logged "Trigger message … was not recorded".
- **Memory notes** are listed on each chat's page and counted on the Chats page (they were only behind a button in the digest card), and `remember` may be used on the bot's own initiative (notes marked as the bot's).
- From a code review: a reply queued behind another run still answered after the chat was disabled; merging two people orphaned the source's memory notes; a network error, time-out or flood limit failed a reminder for good (now retried with backoff; migration 7).
- `set_reminder` accepts "in a minute", "90 mins", "1h 30m", "17:30", "tomorrow 9am".

### Needs checking against live Telegram and Gufo

- `/catchup`: a non-admin bot may only answer an ephemeral command within 15 seconds, and a catch-up with reasoning usually takes longer; check which fallback (DM or group) people get, or make the bot an admin.
- Qwen3.8's JSON for digest updates and distillation (parsing tolerates code fences and prose; failures show on the Agent runs page and the chat's digest card), and how long a digest update takes (it holds the model while it runs, so a reply can wait up to that long).
- Distillation time for a large export (one request per 6,000-token chunk).

- python-telegram-bot 22.8 (latest) knows Bot API 10.0. Ephemeral commands and replies are sent through `api_kwargs` using the 10.3 fields (`BotCommand.is_ephemeral`, `ephemeral_message_parameters`, `reply_parameters.ephemeral_message_id`). Check that `/enable` in a group stays invisible and the reply arrives; if an incoming ephemeral message lacks `message_id`, `ResilientBot` patches it rather than stalling polling.
- `getMe().can_read_all_group_messages` and pin-right detection in basic groups.
- Gufo: served model ID (an empty `model.name` uses the first listed model), streaming for time to first token, `reasoning_content`, and image input with Qwen3.8.
- Rich messages: that the board's Markdown (`###` headings, `-` lists, backslash escapes) renders as intended, and that editing and pinning a rich message work (the API docs say yes; `sendRichMessage` has no `reply_markup`). If it looks wrong, switch Settings → Board → format to `html`.
- Gufo + Qwen3.8 tool calling: whether the server returns structured `tool_calls` or inline `<tool_call>` text (both are handled), and that tool results in `role: tool` messages with `tool_call_id` are accepted.

### Next

1. Deploy Gufo with Qwen3.8 27B, build ~20 cases from real exports with `python -m naruto.evaluation extract`, run them against 27B and Flash-Next, and settle the model (§11).
2. Try phases 3–4 live in a test group: a board update, a plan, a poll, `/summary`, `/catchup`, `/remember`, `/remind`, a question about an older photo; then check the Agent runs, Memory and chat pages. Re-import an export to see distillation.
3. Tune the persona and skill prompts from real use (phase 5), and decide on progress placeholders and per-chat overrides.

