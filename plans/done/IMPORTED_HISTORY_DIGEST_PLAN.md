# Imported history digests and model request queue: feature plan

Status: proposed feature; not implemented by this document. Defaults below are recommendations to review during implementation.

Written: 2026-10-01.

Related plan: [Bot rework plan](BOT_REWORK_PLAN.md), especially history import (§6), memory (§9) and parallel requests. This plan revises those sections for selectable import ranges, historical digests and a shared model-request queue visible in the web admin.

## 1. Purpose

Let the owner upload a large chat export and independently choose:

- **Messages to import:** the date range of actual chat messages to keep and search.
- **History to summarize:** the date range to turn into lasting, dated digests, including very old messages that will never be stored as raw chat.

This preserves more of the group's history after raw messages expire. Historical digests should retain past conversations, events, decisions and jokes, while the existing rolling digest continues to describe what is happening now.

All AI generation should use one shared queue with owner-controlled parallelism. Actual user requests take priority over digests, import distillation and other background work, and the owner can see what is running and waiting in the web admin.

Digests are compressed summaries. They preserve useful context, but cannot guarantee every detail, exact quote or attachment from the original export.

## 2. Current behaviour and gaps

Checked against the current implementation on 2026-10-01:

- `naruto/importer/service.py` stores only messages before the chat's first retained live message and inside `retention.imported_messages_days`. There are no per-import date selections.
- A successful raw import replaces every previous completed raw import for that chat. A narrower import would therefore remove unrelated imported history under the current behaviour.
- `naruto/memory/distill.py` reads the export before that live-message boundary, including messages outside retention, into durable memory notes. This already retains some old facts, but does not preserve a timeline of old discussions.
- Its `_first_digest` runs only if the chat has no digest, and reads the last `import.digest_window_days` from stored import messages. Old messages excluded by retention cannot contribute. It passes at most 2,000 messages to a keeper update that may consume only one token-bounded batch.
- `naruto/db/digests.py` has one rolling digest per chat. Its message cursor and `naruto/prompts/digest.md` are designed to advance through new discussion and drop stale topics.
- Uploaded files are deleted when the current import/distillation job ends, including failures. Already-deleted history cannot be recovered without another export upload.
- Normal reply context includes the rolling digest and memory notes, with no historical-digest lookup.

The change needs a persistent archive of dated digests and a generation path that reads the upload directly, independently of raw-message retention.

## 3. Import experience

After upload and target-chat selection, add two independent sections to the preview:

| Section | Controls | Proposed default |
| --- | --- | --- |
| Import chat messages | On/off, From date, To date | On; available history within the current imported-message retention window |
| Generate historical digests | On/off, From date, To date, grouping | On; all eligible history in the export, grouped monthly |

The owner can import only recent chat messages while generating digests for years of older history. Either section can be disabled; at least one must have eligible messages. The ranges may overlap, be disjoint, or be identical. The digest range does not have to fit inside the raw-import range.

Example with an export spanning 2020-01-01 through 2026-09-30:

- Import actual chat: 2026-09-01 through 2026-09-30, subject to retention and the live-history boundary.
- Generate historical digests: 2020-01-01 through 2026-08-31, monthly.
- Result: recent messages remain individually searchable; older discussion survives as dated summaries after the export is deleted.

Support **monthly**, **weekly**, and **one summary for the selected range**. Monthly is a starting default for long histories. Warn that one summary of many years will lose more detail. Do not silently switch the owner's grouping.

Show the export's available dates and the timezone used by the form. Dates include the whole selected end day. Internally, use start-inclusive/end-exclusive timestamps based on local calendar boundaries; weekly periods start on Monday and monthly periods start on the first day. Clip the first and last periods to the selected range.

Refresh the preview when the target, dates or grouping changes. Show:

- Messages selected for raw import, actually eligible, and excluded by retention or live-history overlap.
- Messages selected for historical digests, including how many will be summarized without being retained raw.
- Non-empty digest periods and estimated model work; label estimates clearly.
- Existing imported messages or digests affected by replacement, and any partial-period coverage.

Validate both ranges on the server. Reject reversed ranges and enabled selections with no eligible messages. Explain exclusions before starting. A raw date selection does not override the retention policy: keeping older raw messages requires changing that setting first. Digest generation remains available even when zero raw messages can be retained.

## 4. Historical digests and the rolling digest

Keep three complementary kinds of memory:

| Kind | Purpose | Lifetime |
| --- | --- | --- |
| Rolling digest | Current topics, plans and unresolved discussion | Rewritten as new messages arrive |
| Historical digests | What happened in a particular period | Kept until explicitly edited, replaced or deleted |
| Memory notes | Durable facts about people and the group | Existing note policy |

A historical digest records notable topics, people, events, decisions, outcomes and unresolved questions **as of that period**. Use absolute dates including the year. Do not present an old plan as a current commitment, infer that an event occurred merely because it was planned, or discard an event because it is now old.

Use a dedicated historical-summary prompt with the existing treatment of messages as data and sensitive information. Archive generation should not automatically rewrite present-day memory notes, create reminders, or publish a board or Telegram message.

Keep existing memory distillation as a separate option. In the revised flow, explain its scope explicitly: when historical digests are enabled, distill the selected digest range; otherwise use the selected raw-import range, including messages excluded by retention. Do not silently read unselected years into notes. Distillation and archive generation must have independent statuses, and `import.distill_memory` must not gate historical digests.

Archive generation must not overwrite the rolling digest or move its message cursor. Keep recent digest initialization separate, under the chat's normal update lock, and only when no rolling digest exists. Seed from a clearly bounded recent period, consume all eligible batches, and avoid treating a years-old export as current context. Ordinary keeper updates must also respect that recent initialization policy; otherwise they could pull all newly stored old messages into an empty rolling digest. Never rewind an existing cursor to incorporate backfilled history.

## 5. Reading the selected history

Generate from the uploaded export while it is available, rather than querying only stored messages:

1. Freeze the selected ranges, timezone, target chat, retention cutoff and eligible live-history boundary for the job. Revalidate against current chat state at start.
2. Select digest messages independently of the raw-import filter. Retention does not exclude a message from historical summarization.
3. Group eligible messages into calendar periods and order them chronologically with a stable tie-breaker for equal timestamps. Handle exports that are not already sorted.
4. Within each period, process every message through bounded chunks. Carry a period-local summary forward, or reduce intermediate summaries, until the entire period has been read.
5. Validate and save a completed digest with its coverage metadata, then checkpoint progress. Start the next period without inheriting the previous period's summary.

Do not reuse the one-shot `_first_digest` path for archival generation. A dense month must not be silently truncated by a message limit or token budget. Account for prompt overhead and carried summaries in the full request budget. Report any message-text truncation or unreadable records as limitations.

Skip empty periods; an empty period is not a model failure. Store actual first/last message dates and the count summarized, alongside the requested period boundaries. Make incomplete export coverage visible rather than claiming that a digest represents every conversation in that month.

Initially preserve the import's conservative pre-live boundary for both raw imports and export-based historical digests, so recorded live discussion is not summarized twice. Explain this restriction in the preview. The current earliest-live timestamp can move after retention cleanup; implementation should persist a stable recording boundary and use it for future imports. For existing chats, use the earliest surviving live message as an explicitly imperfect migration fallback. Accurate recovery of an already-lost recording boundary needs owner input or older metadata.

Do not add automatic live-history archival generation in this first feature. The owner can request digests from still-stored imported messages later; messages already removed and never summarized require re-uploading the export.

## 6. Persistence, reimports and deletion

Add historical-digest storage alongside the current rolling-digest record. Preserve:

- Chat, calendar grouping, timezone, selected period boundaries and actual message coverage.
- Summary text, source import/job, message count and content fingerprint.
- Creation/update time, actor, completeness status and limitations.

Provenance must survive raw-message and upload deletion. Keep export identifiers/timestamps where useful; do not depend exclusively on message-row foreign keys that will disappear. A reference to a deleted source is metadata, not an available quote.

Persist selected import options and separate raw-import, digest-generation and distillation progress. Record completed periods and unfinished chunk work sufficiently to resume without repeating successful model calls or duplicating side effects. Choose the precise schema during implementation; no external storage service is required by this plan.

Reimport rules:

- Re-uploading identical content with the same coverage, grouping and generation settings reuses matching completed digests. An explicit regenerate action permits rebuilding them.
- A narrower raw import replaces only imported messages inside its effective selected range, after the new raw stage succeeds. Preserve imported messages outside that range. Digest-only jobs replace no raw imports.
- Preserve historical digests outside the selected replacement coverage. Preview overlaps; do not silently replace a full-month digest with a partial-month digest or keep overlapping summaries that would double-count history in lookup.
- For changed or overlapping coverage, require a deliberate replacement choice in the import preview. Stage replacements and publish them atomically only after all replacement summaries needed for that overlap are complete. Preserve old digests if generation fails.
- Treat owner edits as deliberate content: show them in replacement conflicts and require an explicit replace-edited-digests choice. Track digest edits without altering original coverage metadata.

Raw-message retention does not expire historical digests. The admin can view, edit and delete them independently. Deleting messages alone leaves summaries in place; deletion copy must explain this. Full chat-data deletion removes archived digests and pending work as well. Include new chat-scoped records in group-ID migration so archives move when a group becomes a supergroup.

## 7. Progress and failure handling

Run model work in the shared request queue described in §9, at background priority. Foreground replies remain responsive, and only one overlapping history-generation job per chat may run at a time. A large export submits its next bounded chunk when ready, rather than filling the request queue with every future chunk.

Show separate outcomes such as “messages imported; 63 of 80 digest periods completed; digest generation paused after an error.” A successful raw import must not imply that archival work succeeded. A digest period is complete only after all its chunks succeeded; do not publish an apparently complete summary when a chunk failed.

Checkpoint completed periods and retry only unfinished work. Recover interrupted jobs after restart when their source is still present. Bound repeated model failures and offer retry/cancel actions without discarding successfully completed non-conflicting periods.

Keep the temporary upload until every requested stage succeeds or the owner explicitly abandons unfinished work. Failed/paused jobs need a bounded retry lifetime: propose seven days after pausing, shown on the result page, then delete the temporary source and mark unfinished work as requiring re-upload. Active jobs retain their source. On success, delete the file promptly. This replaces the unconditional deletion in the current `_job` cleanup.

Trace historical-generation calls with their job, period and chunk so failures and model work can be inspected. Traces follow existing trace retention; archive provenance must not depend on them surviving.

## 8. Using the preserved history

Add a historical-digests section to the chat admin page: date and text filters, coverage, source import, and view/edit/delete actions. Link completed import results to their generated digests.

Give the agent a bounded, chat-scoped historical-digest lookup by date range and topic. Integrate it with the history-oriented skills so requests such as “What were we planning in summer 2021?” can retrieve useful context after raw messages have expired. Bound returned results and expose pagination or refinement when more periods match.

Return each result with its dates and coverage limitations. The bot should distinguish a preserved summary from a verbatim message and say when the original is unavailable. Prefer raw history for exact wording when it remains accessible.

Keep the full archive out of ordinary reply context; retrieve relevant periods when answering historical questions. The current rolling digest and selected memory notes remain the default context. Historical lookup is part of initial delivery, because preserving summaries only in the admin would not let the bot use that history.

## 9. Shared model-request queue and web visibility

### Current foundation

`naruto/llm.py` already applies `model.parallel_requests` process-wide and gives foreground calls priority over background calls waiting for a slot. Digests and memory distillation submit calls with `background=True`. The dashboard's `naruto/web/templates/_status.html` shows aggregate running/waiting counts, and existing tests cover reply priority and concurrent model calls.

Extend this into an explicit scheduler with identifiable requests and a web-visible queue. The current implementation replaces its semaphore when parallelism changes; requests still holding or waiting on the old semaphore can coexist with a new semaphore's full allowance. Replace that approach with one scheduler accounting for every active request, so live setting changes cannot create independent slot pools.

### Priorities and scheduling

Use two priority classes initially:

| Priority | Examples | Scheduling |
| --- | --- | --- |
| Foreground | Mention/reply responses, user-triggered summaries, follow-up model calls in an active user conversation | Next available eligible slot, ahead of waiting background requests |
| Background | Rolling-digest upkeep, historical-digest chunks, import memory distillation, future automatic analysis | Starts only when no eligible foreground request is waiting and background capacity allows it |

Classify by the request's purpose. Clicking “update digest now” or starting an import in the admin keeps that maintenance work at background priority. A summary requested by a member during conversation is foreground. Every generation call, including tool-loop continuations and retries, passes through the same scheduler; no caller gets a separate concurrency pool.

Use FIFO within each priority class. Dispatch priority selection and slot accounting together so a background waiter cannot take a newly freed slot ahead of an already-waiting foreground request. Dependent chunks from one import remain sequential; independent work can run concurrently within the limits. Submit background work incrementally and coalesce duplicate pending upkeep for the same chat so one long import or repeated timer ticks do not flood the queue.

An already-running model call finishes normally. Priority changes which request starts next; it does not promise to interrupt inference already sent to the server. Release capacity between background chunks and reconsider foreground demand before dispatching the next chunk. Sustained foreground traffic may delay background work; show that clearly instead of automatically promoting background work above user requests.

### Owner-controlled capacity

Keep the existing total parallel-request setting and add separate background capacity and foreground reservation controls in Model settings and the queue page. Proposed defaults:

| Control | Meaning | Default |
| --- | --- | --- |
| Total parallel requests | Maximum active generation requests across the whole process | Existing `model.parallel_requests`, currently 1 |
| Background parallel requests | Maximum background generation requests active at once | 1 |
| Slots reserved for foreground | Capacity that background work cannot occupy, even when foreground is currently idle | 1 when total parallelism is greater than 1; effectively 0 at total 1 |

The effective background limit is the smaller of the configured background cap and total capacity minus the effective foreground reservation. Clamp reservations to at most total minus one so a single-slot setup still permits background work. Show effective values if changing the total makes saved values larger than available capacity. Allow a zero reservation when the owner prefers full utilization, and a zero background cap to pause background dispatch.

For example, with total 4, background cap 1 and reservation 1, at most one background request runs and foreground work can use the other three slots. With total 2, one background call leaves one slot available for a new reply. With total 1, a reply may wait for the current background chunk to finish, but goes ahead of the next chunk. Configure the total for the model server's actual capacity; the app setting does not create server-side inference slots.

Apply changes through the same scheduler immediately. Increasing limits permits more admissions. Decreasing limits leaves running calls intact and prevents additional dispatch until running counts fall within the new limits. Show any temporary excess while existing calls finish; do not cancel useful work or release phantom slots to enforce a decrease.

### Web admin queue

Make queue visibility part of initial delivery, with a dashboard summary linking to a dedicated Model queue page. Refresh it using the existing admin partial-update pattern. Show:

- Configured/effective limits, active foreground/background counts, waiting counts by priority, background pause state and oldest wait time.
- Individual running and waiting requests: ID, chat, task type, priority, queued time, wait duration, running duration and linked agent run or import job.
- For import work, the period and chunk currently queued or running, plus a link to overall job progress. Unsubmitted future chunks belong to job progress rather than the request queue.
- A short bounded history of completed, failed, expired and cancelled requests, including queue-wait time separately from model duration.

Support filtering by chat, task, priority and state. Running requests do not have a queue position. Waiting requests can show position within their priority class; positions can change as foreground work arrives. Do not present a precise completion ETA without sufficient timing data. Keep prompts and chat contents in the existing detail views rather than displaying them in the queue table.

Provide controls to change capacity and pause/resume background dispatch without affecting foreground traffic. Pausing prevents new background starts; running calls finish and jobs retain their checkpoints and sources. Show paused jobs explicitly, including the temporary-upload expiry policy from §7. Background jobs should stop submitting new chunks while dispatch is paused.

Allow cancelling a queued request or pausing/cancelling its parent background job. Notify the owning coroutine/job so it can stop cleanly, record the outcome and preserve completed digests. Removing a queue row must not leave its caller waiting forever. Cancelling a queued foreground request stops its parent response run and records that decision. Running generation cancellation is outside the first version unless the backend can reliably stop inference; the UI should explain when cancellation takes effect after the active call.

### Request lifecycle and operational limits

Track each submitted generation request as queued, running, then completed/failed/cancelled/expired, with a stable link to its parent run/job. Recheck whether a foreground chat/request is still eligible before dispatch. Reject or expire stale requests rather than answering long after the run ended or the chat was disabled.

Queue waiting counts toward the existing foreground agent-run deadline. Remove expired/cancelled waiters before they reach the model. Separate wait time from inference time in traces and the UI. Retries retain their original priority and parent linkage, observe the same capacity accounting, and do not bypass queue limits. Backoff between attempts should release model capacity. Audit any client-level automatic retry behaviour so active-attempt accounting and displayed timings remain truthful.

Bound pending requests and retained queue metadata. Preserve capacity for foreground admission when background work reaches its own backlog limit; background producers should wait or defer through their parent jobs. Return a clear busy/expired outcome if foreground capacity is exhausted instead of allowing unlimited memory growth or silently dropping messages. Choose concrete backlog limits during implementation from expected traffic.

Persist enough metadata for recent queue history and interrupted-state reporting, following existing agent-trace retention. Do not persist duplicate full prompts merely to render the queue. On restart, mark interrupted foreground requests terminal rather than replaying old replies; resumable background jobs recreate unfinished requests from their checkpoints. Do not promise exactly-once inference if a restart occurs after the model responds but before a checkpoint is saved. Applying a completed result must be idempotent to avoid duplicate digests or notes.

Model discovery and lightweight health checks do not consume generation capacity. Display connection failures separately, and prevent failed background jobs from retrying in a tight loop while the model is unavailable.

## 10. Delivery and acceptance

Build in four stages:

1. **Shared queue and visibility:** safe concurrency accounting, foreground/background scheduling, configurable capacity, lifecycle tracking and web queue controls. Keep existing generation paths working through the scheduler.
2. **Storage and import controls:** independent ranges/toggles, server validation, accurate preview, scoped raw replacement and migrations.
3. **Archive generation:** dedicated prompt, complete chunk coverage, checkpoints, failure/restart handling and replacement safety; each chunk uses the shared background queue.
4. **Recall and management:** admin browsing/editing/deletion, bounded agent lookup, updated settings descriptions and README import/queue instructions.

The feature is ready when:

- The web admin shows individual running/waiting model requests, their priorities, timing and parent jobs/runs, with accurate aggregate counts.
- A foreground request arriving behind waiting digest/import work starts first when capacity becomes available; each priority class preserves FIFO order.
- Foreground follow-up calls and retries keep their priority, and all generation callers share the same total limit.
- Total/background/reservation settings apply safely during active requests and waiters, including increases and decreases, without admitting work through obsolete slot pools.
- Background work obeys its cap and leaves configured foreground capacity available. Single-slot priority behaviour is explicit and verified.
- Pausing background dispatch leaves replies working, and cancelled or expired waiters never call the model or leave their owners hanging.
- Bounded backlogs, model failures and restarts produce clear queue/job outcomes without duplicated archived results or replayed stale replies.
- An export containing years of messages can retain only recent raw chat and create historical digests from older dates independently.
- Digest-only and raw-only jobs work, including archive generation with no retained raw messages or with memory distillation disabled.
- Both ranges respect selected local dates, end-day inclusion, period boundaries and live-history overlap rules.
- A dense period larger than one model input is processed fully, including stable handling of equal timestamps and unsorted input.
- Existing rolling digest text/cursor survives archive generation; empty rolling digests do not absorb stale history as current discussion.
- Digests remain usable after deleting their raw sources and uploaded file.
- Reimports preserve unrelated history, avoid duplicate coverage, protect edited digests, and preserve old summaries when replacements fail.
- Model errors and restarts preserve completed work, report missing coverage and support retry while source files remain available.
- The bot can retrieve the right old period in its own chat, explains summary limitations and cannot retrieve another chat's archive.
- Explicit archive deletion, full chat-data deletion and group-ID migration include the new records.

Use synthetic multi-year exports for validation, including empty months, partial periods, a dense month, boundary timestamps, repeated uploads and interrupted generation. Use controlled concurrent model calls to validate dispatch order, live resizing, reservations, timeouts, cancellation and pause/resume. Check responsiveness and model work on the owner's actual local model before choosing final chunk sizes, summary-length defaults and backlog limits.

Automatic archives of live chat, yearly rollups, embeddings/vector search and recovery of already-deleted unsummarized history remain future work.
