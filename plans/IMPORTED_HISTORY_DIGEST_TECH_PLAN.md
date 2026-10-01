# Technical plan: history digests, live archiving and the model queue

Implements `plans/IMPORTED_HISTORY_DIGEST_PLAN.md` (the feature plan), plus monthly archiving of live chat (owner's choice, 2026-10-01). After approval, save this plan as `plans/IMPORTED_HISTORY_DIGEST_TECH_PLAN.md`. Implementation starts only when the owner asks.

## Context

Today an import keeps only the last 30 days of an export as raw messages. Older history survives only as memory notes. The rolling digest describes "now" and forgets old topics, so the group's past is lost once raw messages expire. The owner wants three things:

- choose separately which dates to import as raw messages and which dates to summarize into lasting, dated **historical digests**;
- let the bot look those digests up when someone asks about the past;
- send every model request through one visible queue: replies first, owner-controlled capacity, and a web page showing what is running and waiting.

The owner also chose to archive live chat month by month, so the archive has no gap after the bot joined, and to keep two separate passes over imports (summaries, then memory notes), both on by default.

## Concerns (for the owner)

1. **Size.** This is the largest change since phase 4: five stages, roughly 3,000–4,000 lines including tests. Each stage ends with the bot working and tests passing.
2. **Model time.** Summarizing years of history takes hours of background work. On Halogen (~160 tokens/s uncached), a 50,000-message export is about 2 hours per pass, and distillation is a second pass. Replies still go first. The preview shows estimated requests and tokens; run large imports overnight.
3. **Context per server slot.** llama-server-style servers split their context across slots. With 4 slots, each slot may get a quarter of it. A summary request is about chunk + 3k tokens and a reply up to 12k; check Halogen's per-slot context before raising parallel requests or chunk size.
4. **The queue only sees this process.** `python -m naruto.evaluation` and the planned self-learning loop talk to the model server directly. They aren't counted against the queue's limits or shown on the queue page.
5. **Summaries can be wrong.** A local model can misremember who did what in 2021. Results are labelled as summaries with dates and coverage, the bot is told they aren't verbatim, and the owner can edit them.
6. **Live archiving delays deletion.** A month's live messages are kept until that month's digest exists, at most 7 days (setting) after the month ends. Effective retention can reach about 5½ weeks for early-month messages. The setting text says so.
7. **The recording boundary for existing chats is a best guess** (earliest surviving live message). Live recording started on 2026-10-01, so nothing has expired yet and the guess is accurate today. The chat page lets the owner correct it.
8. **Reuse is conservative.** Changing the summary prompt or digest length marks every period as changed, so re-uploading the same export then needs an explicit replace.
9. **Prompts.** A new summary prompt and tool descriptions are added. `persona.md` and `rules.md` aren't touched, since the persona is being reworked separately. The bot learns about the archive from the new tool's description and a one-line background note.
10. **Migration risk.** `imports` must be rebuilt (SQLite can't change a CHECK constraint). Back up `data/naruto.db` before deploying. A test migrates a version-8 database.
11. **Banter gets a 14th tool.** Local models pick tools less reliably as the list grows. Watch the Agent runs page; if it hurts, move the tool behind the summarize hand-over.
12. The feature plan mentions "full chat-data deletion", which doesn't exist today. Archive deletion is added next to the existing per-type deletions instead.

## Decisions made in this plan

- **Two separate passes**, summaries first, then distillation; both on by default. Their statuses are independent.
- **Live archiving** reuses the same summarizer. Its source is stored live messages instead of the export.
- Summaries are **plain text, not JSON**. This avoids the JSON confusion seen on Lemonade and makes failures easy to see.
- Long periods are summarized **chunk by chunk, carrying the summary so far forward** (refine). Resuming needs only the carried text and how many messages have been read.
- The export is read **into memory once per job run**, sorted by (date, position in file). The 200 MB upload cap keeps this under about 1 GB worst case; a typical export needs a few MB.
- **Fingerprints come from per-day content hashes** computed during the preview pass. The preview can then predict exactly which periods would be reused or changed, without reading the file again.
- **A replacement range must fully contain** every existing digest it overlaps. Otherwise the owner is told which dates to use, and no partial-month digest replaces a full one.
- **Pausing background work** is a setting (`model.background_paused`), so it survives restarts and shows in the settings history.

## Stage 1 — Shared model queue (feature plan §9)

**New: `naruto/model_queue.py`** (no imports from `llm.py`, to avoid an import cycle)
- `RequestInfo(task, chat_id=None, run_id=None, import_id=None, period_id=None, chunk=None, still_wanted=None)`. Tasks: `reply`, `image`, `digest`, `history`, `distill`, `live_archive`.
- `ModelQueue(settings, requests_repo)`:
  - One waiting heap per priority class, ordered by sequence number (FIFO within a class). Running counts are tracked per class.
  - `_dispatch()` is the only place that starts requests. It runs on submit, release, cancel, and through `settings.on_change` for `model.*` keys.
  - Foreground waiters always go first. Background starts only when no foreground request is waiting, `running_bg < effective_bg` and the queue isn't paused.
  - `effective_bg = min(background cap, total − min(reserve, total − 1))`. When limits shrink, running calls finish and no new ones start until the counts fit.
  - `still_wanted()` is checked at dispatch. A request no longer wanted is marked expired and never reaches the model.
- `async with queue.slot(info, priority, seq=None)`:
  - If the caller is cancelled while waiting (e.g. the run deadline), its ticket is removed.
  - If it was dispatched just as it was cancelled, the slot is released (the race is handled explicitly).
- Other methods: `cancel(request_id)` (waiting requests only); `snapshot()` (limits, running and waiting rows with position in class, oldest wait).
- Backlog caps: 50 foreground and 20 background waiters. Beyond that the queue raises a "full" error.
- Errors: `QueueRefused(reason)` with reasons `cancelled`, `expired` or `busy`.

**`naruto/llm.py`**
- `LLMClient.chat(..., background=False, info=None)` goes through the queue. The `_semaphore` / `_acquire` / `_foreground_waiting` code and the 50 ms polling loop are removed.
- `QueueRefused` becomes `RequestNotRun(LLMError)` with `.reason`, so existing `except LLMError` handlers still work.
- Retries: `max_retries=0` on the OpenAI client. `chat()` retries once itself for connection errors, 5xx and 429 (not time-outs), after releasing its slot and waiting 2 s. The retry keeps its original sequence number, so it doesn't lose its place. `LLMError.transient` is set in `request_completion`.
- The `in_flight` / `waiting` properties now come from the queue snapshot (the dashboard uses them).

**Callers pass `info`**
- `agent/runner.py` `_request`: `task=reply`, run_id, and `still_wanted` (the chat is still enabled). `RequestNotRun` finishes the run with status error and sends nothing when cancelled or expired. When the queue is full, the existing `FAILURE_TEXT` is sent.
- `agent/tools/media.py`: `task=image`.
- `memory/keeper.py`: `task=digest`.
- `memory/distill.py`: `task=distill`.
- `evaluation/runner.py` `TargetLLM.chat` accepts and ignores `info`.

**Storage (migration 9):** a `model_requests` table with id, chat_id, task, priority, state (`queued`, `running`, `done`, `failed`, `cancelled`, `expired`, `interrupted`), run_id, import_id, period_id, chunk, attempts, queued_at, started_at, finished_at and error. Prompts are not stored.
- New `naruto/db/model_requests.py` repository.
- At startup, open rows are marked `interrupted`.
- Cleanup follows `retention.agent_runs_days` (in `jobs.run_maintenance`).
- The table is added to `CHAT_SCOPED_TABLES`.

**Web: `naruto/web/queue.py` with `queue.html` and `_queue_live.html`**
- The live part refreshes every 2 s, like `_status.html`. It shows limits (configured and effective), counts by class, the paused state and the oldest wait.
- It lists running and waiting rows with chat, task, priority, timings and links (run, import with period and chunk). Waiting rows have a cancel button.
- Below that, a history from `model_requests` with filters for chat, task, priority and state.
- A small form sets total, background and reserved slots, plus pause/resume.
- The dashboard's `_status.html` links to `/queue` and shows "paused".

**Settings (Model section):** `model.background_requests` (1, 0–16), `model.foreground_reserved` (1, 0–16) and `model.background_paused` (false). The `model.parallel_requests` text is updated.

## Stage 2 — Import options, storage and raw-range import (feature plan §3, §5, §6)

**Migration 10**
- `chats.recording_since`, backfilled from the earliest live message, or from `status_changed_at` for enabled chats with none. `ChatRepository.set_status(ENABLED)` sets it if it's empty. `migrate()` keeps the earlier of the two values when merging chats.
- Rebuild `imports`:
  - status values: `preview`, `running`, `paused`, `done`, `partial`, `failed`, `replaced`, `discarded`;
  - new columns: `options` (JSON, frozen at start), `raw_status`, `archive_status`, `archive_total`, `archive_done`, `archive_error`, `skipped_range`, `paused_at`, `source_expires_at`, `limitations` (JSON).
  - Stage statuses are TEXT: skipped, waiting, running, paused, done, failed, expired, cancelled.
- `history_digests`: id, chat_id, status (`active`, `staged`, `replaced`), source (`export`, `live`), grouping (`month`, `week`, `range`), timezone, period_start, period_end (end-exclusive), first_message_at, last_message_at, message_count, import_id, fingerprint, text, limitations (JSON), edited, created_at, updated_at, updated_by.
  - It gets an FTS5 table `history_digests_fts` with the same triggers as `messages_fts`.
  - `history_digest_edits` keeps the previous text on every owner edit.
- `history_periods` (work items and checkpoints): id, chat_id, source, import_id, grouping, period_start, period_end, status (`waiting`, `running`, `done`, `reused`, `failed`, `cancelled`), message_count, consumed, chunks_done, partial (carried summary), fingerprint, replaces (JSON ids), digest_id, attempts, error, updated_at.
- All new tables are added to `CHAT_SCOPED_TABLES`.
- New `naruto/db/history.py` repository (`HistoryRepository`), wired into `Services`.

**Preview pass (`ImportService.analyse`)**
- Its UTC day histogram becomes per **local day** in the configured time zone: `[day_start, count, tokens, day_hash]`, plus `tz`.
- `day_hash` is an order-independent sum of per-message hashes over export id, date, sender and text. A helper in `naruto/memory/history.py` is shared with the summarizer.
- If the time-zone setting changed since upload, the preview pass runs again (the file still exists).

**New: `naruto/importer/planning.py`**
- `ImportOptions` with raw on/off and from/to dates; archive on/off, from/to dates and grouping; distill; regenerate; replace existing; replace edited.
  - `ImportOptions.defaults(preview, settings)`: raw = retention window ∩ export dates; archive = the whole export, monthly; distill = `import.distill_memory`.
  - `ImportOptions.from_form()` reads the form.
- `make_plan(preview, options, chat, services, now) -> ImportPlan` is a pure function used by both the estimate and start (start checks it again). It works out:
  - raw counts: selected, eligible, excluded by retention, excluded by the live boundary;
  - archive counts: selected, summarized without raw, non-empty periods, estimated requests and tokens, partial periods, and predicted reused or changed periods;
  - overlaps with existing digests, including edited ones;
  - raw messages from earlier imports that would be replaced;
  - distillation scope and estimate;
  - errors (reversed range, nothing eligible, partial overlap of an existing digest, replace not ticked) and warnings (one summary for a long range, partial periods).
- Dates are local days. The end day is inclusive (stored as the next local midnight). The live boundary is `min(recording_since, first stored live message)` for raw, and `recording_since` for summaries.

**Import page (`naruto/web/imports.py`, `import_detail.html`, `_import_estimate.html`)**
- Two sections, plus distill, regenerate and replace checkboxes.
- The form shows the time zone and the export's date range. Any change refreshes `_import_estimate.html` (`hx-trigger="change"`).
- The Start button is disabled while the plan has errors.

**Raw stage (`ImportService.run`)**
- Filters by the planned range, retention and the live boundary, and counts `skipped_range`.
- On success, `MessageRepository.delete_imported_range(chat_id, start, end, except_import_id)` removes earlier imports' messages inside the effective range only, and clears reply links that pointed at deleted rows.
- An earlier import left with no messages becomes `replaced`. A summaries-only job deletes nothing.

## Stage 3 — Summarizing, distillation scope and the rolling-digest start (feature plan §4, §5, §7)

**New: `naruto/memory/history.py`**
- `plan_periods(start, end, grouping, tz)`: calendar months, weeks starting on Monday, or one range. Clipped to the selection and correct across daylight-saving changes.
- `ExportSource`: reads the export once and keeps messages in the summary range before `recording_since`. Each becomes `ArchiveLine(date, seq, export_id, sender_id, sender_name, text)`, sorted by (date, seq). `messages(start, end)` uses bisect.
  - The source also notes messages shortened to `context.max_message_chars` and the reader's unreadable entries as limitations.
- `fingerprint(lines, grouping, tz, start, end, settings_hash)`.
- `HistoryWriter.process(period, source)`:
  - Chunks from `consumed` onward by `history.chunk_tokens` (frozen in the job options), so resuming gives the same chunks.
  - Each chunk is one background request (`task=history`, period_id, chunk). The prompt has the system text (new `naruto/prompts/history.md` with `{bot_name}`, `{period}` and `{max_chars}` filled in), the chat name, the members, the summary so far and the dated message lines.
  - The answer is checked: not empty, `strip_internal_json`, capped near `history.digest_max_chars`.
  - After each chunk one transaction saves `partial`, `consumed`, `chunks_done` and an agent-run trace (skill `history`).
  - After the last chunk: a `history_digests` row (`active`, or `staged` if it replaces something) with coverage and limitations, and the period is marked `done`.
- `publish_staged(import_id)`: once every period that replaces something is done, one transaction marks the old digests `replaced` and the staged ones `active`. If the job fails, the old digests stay.
- Failures: each chunk gets 3 attempts (back-off 30 s, then 2 min). After that the period is marked `failed`, the job pauses (`archive_status=paused`, `paused_at`, `source_expires_at = paused_at + history.source_keep_days`), and the error shows on the import page. Owner actions: Resume (retries unfinished work), Cancel unfinished work (deletes the file; completed periods stay).
- While `model.background_paused` is on, the writer waits before submitting its next chunk.

**Import job (`ImportService._job`)**
- Order: raw stage (worker thread, as now) → summaries → distillation → rolling-digest start.
- Each stage updates its own status. The overall status is `done`, `partial` or `paused`.
- A per-chat lock (`HistoryLocks`, shared with stage 5) allows one job per chat.
- The uploaded file is deleted only when every requested stage has finished, or on cancel or expiry. This replaces the unconditional `finally: _delete_file`.
- `recover()` at startup:
  - an interrupted raw stage is rolled back and run again if the file still exists;
  - summaries and distillation resume from their checkpoints (`resume_all()` is started from `main.py`);
  - with no file, unfinished stages become `expired`.
- `jobs.run_maintenance` expires paused jobs past `source_expires_at`.

**Distillation (`naruto/memory/distill.py`)**
- `export_chunks(..., since, until)` reads the summary range if summaries are on, otherwise the raw range (including messages outside retention), always before `recording_since`, sorted the same way.
- It resumes from `distill_done` and traces runs as skill `distill`.
- `_first_digest` is removed.

**Rolling-digest start (`naruto/db/digests.py`, `naruto/memory/keeper.py`)**
- `unread()` / `unread_count()` gain `start_after`. With no cursor, the keeper reads only messages newer than `now − import.digest_window_days` (description updated: "A chat with no digest yet starts it from this many days back").
- New `keeper.catch_up(chat, max_batches=10)` runs under the chat's keeper lock and loops `update()` until nothing is unread in that window. The import job calls it when the chat has no digest text.
- An existing cursor is never moved back.

## Stage 4 — Using and managing the archive (feature plan §8)

**Agent tool (new `naruto/agent/tools/archive.py`):** `search_history_summaries(query?, since?, until?, page?)`
- Uses FTS on text and/or date overlap, scoped to `ctx.chat`, active digests only, up to `history.lookup_results` (3) per page.
- Each result is headed with the period, actual dates, message count, partial-coverage notes and "a summary, not the original messages". The result ends with "page 1 of N".
- Added to the **banter** and **summarize** skills in `naruto/agent/skills.py`.

**Prompt background (`ContextBuilder._background`):** one line after the digest when digests exist, e.g. "Summaries of earlier history: Jan 2020 – Aug 2026 (search_history_summaries)". It only changes when the archive's coverage changes, so the prompt cache stays warm.

**Web (new `naruto/web/history.py`, `history.html`)**
- `/chats/{id}/history`: list with filters (date range, words, status), coverage, source (import link or "live"), and an edited badge. Each digest can be viewed, edited (records `history_digest_edits` and sets `edited`) or deleted (two steps).
- "Delete all history digests" (two steps) goes next to the existing deletions on the chat page.
- The message-deletion confirmation now says that history digests are kept.
- The import result page links to the import's digests and shows the three stages with Resume, Pause and Cancel.
- The chat page shows and edits "Recorded live since".

**Docs:** add an import/history/queue section to the README, update the settings descriptions, and add a short entry to §18 of `BOT_REWORK_PLAN.md`.

## Stage 5 — Monthly live archiving

New `LiveArchiver` in `naruto/memory/history.py`, run every 10 minutes from `jobs.start_background_jobs`:
- For each enabled chat with `history.live_archive` on (per chat), it finds the oldest **closed** local month with stored live messages on or after `recording_since` and no live digest or unfinished live period.
- It creates a `history_periods` row (source `live`, grouping month) and processes it with `HistoryWriter` and a `StoredSource` (`messages` where source = 'live', ordered by date and id). `task=live_archive` at background priority, one period per chat at a time, under `HistoryLocks`.
- Coverage limitations: "recording began on …" when the month started earlier; "messages before … had already expired" when the earliest surviving message is later than expected.
- Retention hold: `jobs._expire_messages(LIVE)` won't delete messages from a month that has no live digest yet until `month_end + history.live_hold_days` (7). After that the period is marked `failed` ("missed") and retention resumes. This combines with the existing digest grace (whichever is earlier wins).
- A failure backs off 15 minutes (like the keeper) and shows on the history page.

## Settings added (`naruto/settings/registry.py`, new section "History")

| Key | Default | Notes |
| --- | --- | --- |
| `history.instructions` | `prompts/history.md` | text |
| `history.chunk_tokens` | 6000 | 1000–100k |
| `history.digest_max_chars` | 2500 | 300–10k |
| `history.max_output_tokens` | 2000 | |
| `history.reasoning` | false | |
| `history.lookup_results` | 3 | 1–10 |
| `history.source_keep_days` | 7 | how long a paused job keeps its upload |
| `history.live_archive` | true | per chat |
| `history.live_hold_days` | 7 | 0–30 |

Plus the three Model settings from stage 1.

## Reused code

- `ExportReader` (`naruto/importer/export_parser.py`)
- `message_body` (`naruto/markers.py`)
- `estimate_text_tokens` and `strip_internal_json` (`naruto/agent/text.py`)
- `fill` (`naruto/memory/keeper.py`)
- the agent-run tracing pattern from `keeper._update`
- `Database.transaction()`
- the FTS5 trigger pattern in `naruto/db/migrations.py`
- `CHAT_SCOPED_TABLES` and `ChatRepository.migrate`
- `parse_day` (`naruto/agent/tools/lookup.py`) for tool dates
- the HTMX partial-refresh pattern (`_status.html`, `_import_progress.html`)
- two-step confirmations (`confirm.html`)
- `settings.on_change` and `settings.for_chat`

## Verification

- **New tests**:
  - `tests/test_model_queue.py`: FIFO per class; a foreground request overtakes waiting background; background cap and reserve; total 1; resizing up and down while busy, with no extra slots; pause/resume; cancelling a waiter (the caller gets `RequestNotRun`); expiry via `still_wanted`; deadline cancellation and the dispatch race; full backlog; a retry keeps its priority and place; request rows and interrupted-on-restart.
  - `tests/test_history.py`: `plan_periods` (Europe/London across daylight saving, Monday weeks, end-day inclusion, clipping); a synthetic multi-year export builder in `tests/fakes.py` (empty months, a partial first month, a dense month over 3 chunks, equal timestamps, unsorted input); summaries only; raw only; summaries with nothing kept raw; distillation off; resume after a simulated crash between chunks; failure → pause → resume; re-upload reuse; staged replacement swapped in one step; old digests kept when a replacement fails; edited digests need the extra tick; the rolling digest's text and cursor are untouched; a chat with no digest ignores old history; scoped raw replacement keeps messages outside the range; group upgrade moves the archive; deletion.
  - Live archive: a closed month is archived, the retention hold, hold expiry.
  - Tool: chat scoping, pages, labels.
  - Web: preview validation, the history page, queue cancel and pause, CSRF.
  - Migration: a version-8 database upgrades cleanly.
- **Updated tests:** `tests/test_llm.py` (queue in place of the semaphore), `tests/test_importer.py` (options, statuses, file kept until done), `tests/test_memory.py` (first digest via `catch_up`).
- **Run:** `.venv/bin/python -m pytest -q` after each stage.
- **Manual end to end:**
  - Start the bot locally against a test bot and Halogen. Upload a synthetic multi-year export, choose raw = last month and summaries = everything else, and watch `/queue` and the import page.
  - Ask the bot "what were we planning in <old month>?" in a test group and check the Agent runs page.
  - Then one real export (kept outside the repository). Measure the time per chunk, then tune `history.chunk_tokens`, `history.digest_max_chars` and the backlog caps.

## Delivery

Work on a new branch `history_digests`, created from `bot_rework`, with 2–4 commits per stage and no pushing. The order is stages 1 → 5, and every stage leaves the bot working.
