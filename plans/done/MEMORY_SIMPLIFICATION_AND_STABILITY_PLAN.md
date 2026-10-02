# Memory and history simplification plan

Written: 2026-10-01. Revised: 2026-10-02 after owner review, and again on 2026-10-02 after a code review that trimmed the mechanisms (section 10 lists what changed and why).

Status: implementation plan; this document does not change code, runtime settings or stored data. The application is not live. The owner accepts resetting its development database if migration would add substantial complexity. No reset is performed by this planning change.

This supersedes the design decisions in [the history feature plan](IMPORTED_HISTORY_DIGEST_PLAN.md), [its technical plan](IMPORTED_HISTORY_DIGEST_TECH_PLAN.md), and earlier revisions of this document where they conflict. Preserve those documents as implementation history. Use this document for the next implementation pass.

## 1. Owner decisions and rationale

The owner wants dependable historical recall with fewer automatic side effects. Keeping messages must be independent of how much context reaches the AI. The chosen solution is to rework history storage and job behavior, **not merely set the existing retention settings to zero**.

| Part | Decision | Why |
| --- | --- | --- |
| Original live and imported messages | Keep indefinitely, unless explicitly deleted or replaced through an owner action. Remove automatic age-based expiry. | Originals preserve exact wording, searchable details and the source for rebuilding summaries. AI input is bounded separately. |
| Rolling digest | Keep as current context, updated automatically from live messages only. | It is a changing view of current conversation, not a historical archive. |
| Memory notes | Keep existing notes, explicit remember/forget, owner edits and live-chat automatic upkeep. | Durable facts are useful, but importing history should not initiate automatic fact extraction. |
| Historical summaries from exports | Keep explicitly started jobs, with monthly, weekly or selected-range grouping. | They provide useful overviews of imported history. |
| Automatic monthly live summaries | Keep as an optional per-chat feature, independent of source retention. | They are a convenience over stored originals, not the only surviving record. Failure need not cause data loss. |
| Import memory extraction and digest initialization | Remove the dedicated extraction pass and exclude imported rows from automatic keeper processing. | Suppressing notes only during an imported batch still lets its facts flow through the digest into later live-batch notes. A live-only keeper is simpler. |
| Pause, failure and retry | Retain valid work and make retries idempotent. Report failure honestly; require manual retry after bounded attempts. | Repeating work must not duplicate results, lose replacement coverage or run forever. |
| Explicit message deletion | Extend the existing per-chat admin action with “older than N days,” a preview, and confirmation. | The owner chooses what to remove instead of a timer silently deciding. |
| Database compatibility | Prefer a clean development database if preserving the current schema/jobs is costly. | The bot is not live; elaborate compatibility work is not a product requirement. |
| Infrastructure | Keep one Python process, SQLite, current repositories and the shared model queue. | No new worker service, vector database, distributed locks or generic workflow engine is needed. |

A short raw-history window plus permanent summaries is a legitimate space-saving policy, but it is not this product's chosen policy. It loses source detail, prevents accurate regeneration and introduces retention holds and deadlines. Database storage and model context are separate concerns. Do not reintroduce automatic message expiry as a default or hidden maintenance behavior in this pass.

Do not add an exhaustive fact-provenance system or promise that a model can never learn an imported fact indirectly. Remove the deliberate automatic import-to-keeper path. Explicit remember requests and ordinary historical lookup remain useful features.

## 2. Target behavior and invariants

### 2.1 What each store is for

- **Messages:** original history, indexed and searchable within its chat. Retention is indefinite unless an owner explicitly deletes a scope or replaces imported rows.
- **Rolling digest:** one changing summary per chat for current context; older versions are not a searchable timeline.
- **Memory notes:** selected durable facts, with existing owner-edit/lock behavior.
- **Historical summaries:** dated, lossy overviews of stored live messages or an uploaded export. They remain until explicitly deleted or replaced. They never substitute for an exact quote from an original.

Normal replies read a bounded recent window, the rolling digest and selected notes. Older messages and historical summaries are retrieved in bounded tool results. Storing years of conversation must not cause years of messages to be loaded into each reply.

Monthly summaries remain useful when originals are retained: a summary answers “what happened that month?” efficiently; original-message search answers detailed questions and verifies wording. Summary failure leaves the originals available.

### 2.2 Job invariants

1. One work item has at most one generated result for its current generation. Retry does not insert another result for completed work.
2. Pause or retryable failure preserves valid checkpoints and staged replacement results. Cancellation or explicit source-file expiry discards unpublished replacements. These operations are different.
3. Old active summaries remain visible until **all** required results in their connected replacement group exist and can be published atomically.
4. Owner edits and deletion win over a late model response. A saved source/configuration mismatch invalidates positional progress.
5. A job reports success only when every requested stage and required publication has completed. Failure is visible, bounded and does not block unrelated later months.
6. Nothing automatically deletes original messages because they are old, unread, unsummarized, or associated with a failed job.
7. Explicit message deletion does not silently delete derived notes/summaries and does not allow unfinished producers to restore the deleted rows.
8. Every model request has a bounded full input, including follow-up tool results, with output headroom. Database size is not a model-input control.

Idempotency means safe application of retries, not exactly-once inference. A crash after receiving a model response but before checkpointing can repeat that call. It must not duplicate published data.

### 2.3 Imports and source availability

Keep two selectable stages: store selected raw messages, then generate selected historical summaries. Keep raw-only, summaries-only and combined imports. Default the raw range to the export's selected eligible range without any age cutoff; show both stage ranges clearly. Keep conservative live-recording boundary and overlap protections for now. This does not add a gap-repair import mode.

Remove rolling-digest initialization/catch-up from imports entirely. Imported messages remain available to normal reply context and search; they do not enter automatic keeper batches. Historical generation never writes the rolling digest or notes.

A retry needs its source:

- Live monthly jobs use stored live messages, retained unless manually deleted.
- An unfinished export job keeps its upload under the existing bounded paused-file lifetime. Pause/failure shows the expiry date. Global queue pause alone does not start that expiry.
- Export summaries are always generated from the upload. Once the upload is gone, retrying or regenerating them requires re-uploading the export; say so clearly. Rebuilding export summaries from stored imported messages is out of scope for this pass.

Keep temporary-upload cleanup distinct from permanent message storage. Source-file expiry may end unfinished export work but must not remove committed messages or old active summaries.

## 3. Rework storage and remove expiry machinery

### 3.1 Remove the feature, not just its defaults

Remove the runtime settings, UI controls, descriptions, planning fields and behavior for:

- `retention.live_messages_days`
- `retention.imported_messages_days`
- `history.live_hold_days`

Remove automatic raw-message cleanup and its scheduling: `cleanup_live_messages`, `cleanup_imported_messages`, `_expire_messages`, `UNREAD_GRACE_DAYS`, `mark_missed_live_months`, `mark_missed_months` and `unarchived_live_start`, or their equivalent paths after refactoring. Do not leave the “missed after seven days” path running when deletion has been removed. That existing path runs independently of the message-retention settings.

Remove import age-cutoff computation, rejection, default-range clipping, “too old” counts and retention wording from planning, preview, execution and frozen options. Selected valid old messages must be importable. Preserve explicit range selection, duplicate/replacement checks and the separate live-recording boundary.

Remove archive-specific retention holds, deadlines and missed-deadline outcomes. Summary scheduling does not control the lifetime of original messages.

Primary areas: `naruto/jobs.py`, `naruto/memory/history.py`, `naruto/settings/registry.py`, `naruto/importer/planning.py`, `naruto/importer/service.py`, `naruto/web/imports.py`, import/settings/history templates, README and tests. Search all call sites rather than only deleting setting definitions.

### 3.2 Retain operational cleanup without coupling it to chat history

Keep diagnostic log and terminal model-run/request-history cleanup and temporary-upload cleanup. Never prune metadata for a still queued/running request. These are not the chat-history expiry feature.

`cleanup_reminders` currently borrows the live-message retention setting. Decouple it explicitly: preserve cleanup of finished/cancelled reminders using a separate operational reminder-retention setting, retaining the current 30-day default; pending reminders never expire through that cleanup. Do not leave a missing-setting lookup or silently make completed reminders permanent because message expiry was removed.

Do not retain a generic “Retention” page that implies chat messages still expire. Group remaining settings as log/run, completed-reminder and temporary-upload cleanup.

### 3.3 Database growth and reads

Keep existing message/search indexes and bounded queries. Do not solve read performance by deleting history or adding a second search platform.

Measured on 2026-10-02 against the real schema with one chat: `recent_window()` took 5.6 ms with 100k stored messages and 55 ms with 1M, growing linearly. About half of that is the `OFFSET` scan; selecting the same rows with `ORDER BY date DESC, id DESC LIMIT n` and reversing them takes 0.2 ms. The remaining `COUNT(*)` (about 30 ms at 1M) is what keeps the window's start moving in whole steps for prompt caching; it is negligible next to inference and stays. Change the select to the descending form; no counter tables or cursors.

**Acceptance:** messages older than months/years survive repeated maintenance regardless of summary success; old exports are accepted within selected ranges; removed settings have no runtime references; logs/uploads/completed reminders still clean up; recent-window size and model input do not grow with total stored history.

## 4. Remove automatic import memory work

Remove `Distiller`, its import stage and recovery path, import-only prompt, `import.distill_memory`, `import.distill_chunk_tokens`, `memory.distill_instructions`, and distillation options, estimates and new queue submissions. New imports have only raw/archive progress. Update shared reasoning/output setting descriptions for their remaining uses.

Make the keeper's automatic unread query and unread counts **live-only**, including timer scheduling, explicit keeper catch-up and admin progress displays. Filter before batching so imported rows do not advance its cursor or suppress unrelated live work. Defensively reject/filter imported rows supplied directly to keeper update helpers. Preserve owner-edit protection and never rewind the current live cursor merely because an import completed.

In practice this is `source = 'live'` in `DigestRepository._unread_where()` (both branches) plus a filter in `MemoryKeeper._batch()` for explicitly supplied messages. Imported rows always predate the live-recording boundary, and the cursor is stored by value (date, row ID), so no cursor migration is needed.

Remove `import.digest_window_days`, `MemoryKeeper.imported_since()` and the import initialization hook once no consumers remain. `naruto/web/memory.py` and `naruto/db/digests.py` need inspection alongside `naruto/memory/keeper.py`. Do not replace them with a new “import digest-only” mode.

**Rationale:** a guard that disables notes in a mixed/import batch is insufficient: imported facts persist in the digest and can become notes on a later live-only batch. A scripted probe reproduced that path. Excluding imports from automatic keeper inputs removes this intentional path without another model pass or per-fact classification.

Preserve explicit remember/forget and live-only automatic note behavior. On a preserved database, keep existing notes and owner edits; do not claim the change purges previously imported knowledge. A fresh database naturally starts without that old derived state.

**Acceptance:** raw-only, summaries-only and combined imports make zero extraction/keeper requests as part of import processing. Automatic unread counts/batches contain only live rows even with old/new imported rows interleaved. A subsequent live batch is not given an import-seeded digest created by the new implementation. Live updates, cursor progression, owner edits, explicit remember and normal raw-history search still work.

## 5. Core generation, publication and request fixes

### 5.1 Atomic completion and idempotent retry

Replace separate `history.add()` and period completion writes with one repository transaction. Enforce one generated result per work item (for example, a unique non-null `period_id`). Repeating completion returns the recorded result. Explicit regeneration is a new controlled generation/replacement, not an accidental duplicate retry.

Completed staged results are durable work. When a monthly summary is being replaced by several weekly summaries, pausing after two weeks must retain those two results. Before publishing, verify **every** required member has its expected digest row, correct state and matching generation. A `done` flag or non-null dangling `digest_id` is not enough. Missing results cause a visible incomplete/retry outcome; never publish a subset and retire the whole old summary.

Check the replaced targets again in the publication transaction. If the owner edited a target meanwhile and the import was not allowed to replace edited summaries, leave that group's targets alone: drop its staged results and mark its periods cancelled with the same reason the planner uses for an edited summary ("an existing summary covers this period and replacing it wasn't chosen"). A target the owner deleted meanwhile needs no approval; publish the group. This gives the same outcome as if the edit had happened before Start, without a renewed-approval flow. Keep publication atomic per connected overlap group. Cancel/expiry removes unpublished replacements and clears their work-item references consistently; already published unrelated groups survive.

**Acceptance:** fault injection before/between/after completion writes; repeated retry after commit; pause with two of four replacements ready; delete a staged result before publication; cancel after one independent group publishes. No duplicate result, missing coverage, overwritten owner edit or dangling successful result remains.

### 5.2 Stop controls and restart

Implement owner pause/cancel by **cancelling the job's asyncio task** after setting the pause/cancel flag, rather than threading a stop event through every wait. `ModelQueue.acquire()` already withdraws a cancelled waiter's ticket, and `asyncio.sleep()`/lock waits are cancellation points, so one mechanism covers the job lock, queue slot (even with background dispatch paused or a zero cap), retry delays and source loading. The job catches `CancelledError`, records the paused/cancelled outcome and re-raises only for shutdown. Only the raw import worker thread needs its existing cooperative flag, because a thread cannot be cancelled: the job waits for it to finish its rollback before recording the outcome or deleting the source.

A raw worker thread must stop cooperatively and finish rollback/finalization before source deletion or acknowledged completion. An in-flight model call is aborted by the cancellation; its chunk is simply redone on resume from the last checkpoint. No follow-up call starts after stop acknowledgement. Shutdown preserves resumable work; it is not owner cancellation.

Pause and retryable failure preserve source, valid checkpoints and staged results. Repeated pause does not extend the paused-upload deadline. Cancellation/expiry stops workers, removes unpublished work, and cleans the upload safely. Display global queue pause as a waiting reason, not a new persistent stage or a source-expiry trigger.

Derive overall completion from requested stages and actual publication. Recover after status commit/file-unlink crashes without leaking uploads or deleting committed rows. Retry must not rerun a committed raw replacement destructively. Check upload identity before reuse; missing/changed sources yield a clear re-upload/restart outcome.

**Acceptance:** stop during each wait and during the final call; repeated controls; process restart after each transition; interrupted source unlink. No orphan queue ticket, extra call, lost staged result, prematurely deleted source or held job lock remains.

### 5.3 Detect changed inputs and restart, instead of freezing them

Do not persist a full snapshot of the generation settings and thread it through the job. Instead, each period's work item stores two short hashes when it is created or checkpointed:

- **Settings hash:** the settings that shape a summary (instructions, summary size, output limit, reasoning, chunk size, per-message character limit) plus the period's timezone.
- **Source hash:** an ordered hash over the messages already consumed (date, sender ID and normalized body, in the stable date/position order). Do not use database row IDs as the identity; they change on re-import.

On resume, recompute both. If either differs, discard the partial summary and restart that period from its first message with the current settings, and say so in the period's status. A restart costs a few model calls for one period; it is the same "never mix old partial text with new settings or changed sources" guarantee at a fraction of the code. Completed summaries are never rewritten because inputs changed later.

The export preview's reuse fingerprint (whether an identical summary already exists) stays an estimate; execution recomputes it. Raw-only imports must not depend on model discovery. Do not build model-weight attestation or a backend compatibility service.

### 5.4 Atomic raw replacement and visibility

Commit deletion of replaced imported rows, replacement bookkeeping and raw-stage completion together. Keep old rows visible until commit. Hide incomplete incoming import batches from replies, searches and keeper reads with one consistent visibility rule. Recovery distinguishes an uncommitted batch from a committed replacement; it never rolls back the latter by deleting its incoming rows.

Keep explicit date ranges and unrelated imports intact. Repair reply links and dependent records/FTS in the same operation. Coordinate replacement with deletion as described in section 7.

### 5.5 Bound every model request

Keeping all messages is safe for AI context only if every request remains bounded. The current code trims the initial recent window but can exceed the budget with oversized fixed context, and the agent appends tool results without reapplying the total budget.

- Apply a shared full-request budget check immediately before **every** model call, including agent follow-ups and rolling/historical summaries.
- There is no "model context capacity" setting, and this pass does not add one. `context.input_token_budget` becomes the cap on the **estimated input of every agent request**, including follow-up rounds with tool results, not only the first request. Output headroom is the owner's server configuration: the server's context must hold the input budget plus the output limit; say so in the setting's description. Summary jobs keep their own input settings (`memory.digest_input_tokens`, `history.chunk_tokens`), now counted as the whole request rather than only the messages.
- Count system text, roster/names, digest/notes, current request, images using the configured estimate, tool schemas, assistant/tool-call history and tool results. State that estimates are not exact server-token counts.
- Remove oldest optional recent context first, then use deterministic limits for optional background and retrieved content. Preserve the current request, required instructions and valid tool-call/result relationships. If required input alone cannot fit, return an actionable error instead of silently exceeding the limit.
- Keep bounded search result counts and tool-result lengths. Do not stuff all stored history into the prompt. Avoid an additional model call merely to compress context in this stabilization pass.
- For summary chunks, include carried-summary/prompt overhead and shorten an oversized single message with a recorded limitation. Reject a fixed prompt that cannot fit.
- Reject empty, cut-short (`finish_reason=length`) and unusable summary outputs. Validate after final trimming; never count invalid output as processed coverage. Use bounded attempts and a visible failure outcome.

**Acceptance:** large fixed prompts, long names/Unicode messages, images, carried summaries and several tool rounds fit the configured estimate or fail clearly. A large database does not enlarge a normal request. No invalid tool transcript, silently vanished source message or truncated answer is reported as completed summary coverage.

### 5.6 Dates, coverage and operational metadata

Do not interpret earliest/latest dates as continuous coverage or a quiet beginning of a month as proof of deletion: drop the "messages had already expired" limitation, which no longer applies. Record known manual-deletion/unreadable/truncation limitations and expose them in lookup results.

Keep the conservative export/live boundary for now. Say “excluded by the live-recording boundary,” not “already recorded,” where coverage is unverified. Exports cannot yet fill post-boundary gaps; that is separate work.

Keep queue foreground priority, live capacity controls and race tests. Only prune terminal request metadata.

**Moved out of this pass** (unrelated to memory simplification; track separately): calendar-day arithmetic instead of `until + 86400` across daylight-saving changes, using each summary's stored timezone for labels, showing global person-name changes before import Start, and labelling retry timings as latest-attempt timings.

## 6. Monthly history without retention coupling

Monthly work reads stored live messages after the calendar month closes. There is no retention hold or “finish within seven days” deadline. Keep a unique per-chat period/generation identity, a concrete frozen timezone and conservative overlap validation. Repeated timer ticks/restarts do not create duplicate months.

### 6.1 Failure and retry policy

Use a small bounded request retry policy (initial target: three attempts per chunk). Persist the failure count for the unfinished chunk so restarts do not grant infinite new automatic attempts. A successful checkpoint resets the consecutive count for the next chunk.

After exhaustion, mark the period visibly failed and stop automatically selecting it. The timer must continue to later eligible months rather than retry the oldest failed month forever. Add an owner-facing Retry action for failed/paused work, protected by the same per-chat coordination as imports. Repeated Retry clicks must not spawn concurrent copies. Explicit retry grants a new bounded attempt allowance and validates the source before resuming.

Retries reuse unchanged checkpoints/staged results. If the source changed, restart only the unfinished affected work from the remaining source and disclose known missing coverage. Completed summaries are never silently rewritten because new source/configuration differs; use explicit regeneration/replacement approval.

An empty or wholly unreadable source has an explicit no-source/no-readable-content outcome, not a successful empty summary and not an endless retry loop. A genuinely empty month needs no digest.

**Rationale:** the owner accepts honest failure. With originals retained, retry can be an owner decision. An endlessly self-healing scheduler is unnecessary.

### 6.2 Source mutation, deletion and enablement

Validate canonical ordered source identity on resume and recheck source/owner-action validity before finalization. Comparing only `len(lines)` with a consumed count misses edits, equal-count replacements and deletion of an already consumed prefix. Use short database transactions and narrow source revision checks where appropriate; do not hold a transaction across inference or persist hidden copies of deleted source just for resumption.

Freeze unfinished period boundaries; timezone/recording-boundary changes must not create overlapping active coverage. Keep non-content suppression/work metadata for explicitly deleted summaries so the timer does not recreate them. Only explicit regeneration may clear that suppression. An affected active job stopped by raw-message deletion stays stopped until an owner chooses retry on the remaining source.

Disabling monthly generation stops its queued/ongoing work safely and prevents late publication; retain completed summaries and valid paused work. Re-enabling may resume valid work but must not automatically reset exhausted failures or owner-deletion suppression. Respect effective per-chat settings.

**Acceptance:** fail an early month and still archive a later month; restart with an exhausted retry count; retry unchanged and changed sources; delete/edit with unchanged message count; delete consumed prefixes; change boundaries; disable during an active final call; delete a completed summary. No source expiry, duplicate month, skipped message or automatic recreation of deleted summaries occurs.

## 7. Explicit deletion of messages older than N days

### 7.1 Scope and interface

Extend the existing authenticated per-chat admin deletion action in `naruto/web/chats.py` and its chat template. Reuse its preview/confirmation pattern and repository deletion operation rather than adding a separate deletion service or scheduled job.

Required new mode: **Delete messages older than N days**. Scope is one selected chat, with live/imported/both selection; default to both and show it clearly. Preserve the existing explicit before-date and delete-all actions, visibly separate from the age mode. No automatic repeating cleanup, cross-chat bulk deletion or new Telegram command is required.

Accept a positive whole number of days. Reject zero, negatives, fractions, malformed/non-finite values, overflow and unknown modes/sources. Never turn invalid/missing input into “delete all.” Define an age-day as an elapsed 24 hours. At preview, compute `cutoff = preview_time_utc - N * 86400`; match original message timestamps with `message.date < cutoff`. A message exactly at the cutoff stays. This is message age, not upload age. Display the exact cutoff with timezone so the owner can verify it. The existing calendar-date mode remains calendar-based.

### 7.2 Preview and execution

1. Preview the selected chat, source scope, N, the computed cutoff and the matching count. Clearly say this removes stored message copies from the bot, not messages from Telegram.
2. Carry the computed cutoff timestamp to the confirmation as a hidden form field, exactly like the existing before-date mode carries its date. Do not recompute “now minus N days” at confirmation. No server-side preview token or replay protection: the owner is authenticated, the form is CSRF-protected, and the same owner can already delete everything in the chat, so a tampered cutoff grants nothing new. Validate the hidden cutoff as an integer timestamp in the past.
3. On confirmation, delete the scoped rows and related bookkeeping in one transaction (section 7.4 covers running work) and report the actual count. A zero-match operation is valid.
4. Log a concise non-content entry: chat, source, cutoff and count, as the existing deletion already does. Do not copy deleted message bodies into a new deletion log.

Use the existing auth and CSRF protections. No general-purpose approval framework is needed.

### 7.3 What deletion does and does not remove

Delete matching raw rows and their dependent descriptions/media metadata, FTS entries and dangling reply/source references as appropriate. Do not leave original text retrievable through a stale search index or dependent message cache. Preserve newer rows, other chats and unselected sources. Do not require immediate physical file shrinkage or add automatic VACUUM to the request path.

Most of this already holds and needs no new code: the `messages_fts_delete` trigger removes search entries, `media_descriptions` cascades on delete (foreign keys are on), `delete_for_chat()` already clears dangling reply links, and a reply's `reply_to_snippet` copy is only stored when its target was *not* stored, so deleting a stored target leaves no copy behind. Add a test asserting these rather than new deletion code.

By default, **retain historical summaries, rolling digest text, memory notes and their edit history**. The preview must state this explicitly: “Summaries and memory notes may still contain information from these messages. Delete them separately if needed.” Retained diagnostic traces follow their separate cleanup policy. This action is raw-history deletion, not a promise to erase every derived fact or backup.

Preserve existing separate summary/note/digest deletion controls and their protections. Do not build an approximate automatic cascade that guesses which facts to erase. Mark affected historical coverage/source availability honestly so later retries do not claim intact originals.

### 7.4 Interaction with running work

Do not coordinate with running producers; avoid them instead:

- **Imports:** refuse every message deletion (all modes) while an import of that chat is running or paused, using the same `busy_import()` check that already stops a second import from starting: “Import #N of this group is paused. Let it finish, or cancel the rest of it, before deleting messages.” This rules out recovery reinserting deleted rows from a retained upload, and raw replacement racing the deletion.
- **Monthly live summaries:** hold the chat's history lock (`history_locks[chat_id]`) for the deletion; if it is taken, say a summary is being written and ask the owner to retry shortly. A month whose unfinished partial summary read now-deleted rows fails the source-hash check from section 5.3 on its next run and restarts from the remaining messages.
- **Rolling digest:** an update already in flight may still save text based on rows deleted meanwhile. That is derived data, which section 7.3 already retains by design. The cursor is stored by value (date, row ID), so deleting its anchor row does not reset it or replay surviving history.

Explicit future re-import is a new owner action, not blocked by a deletion watermark.

**Acceptance:** test N validation, exact cutoff, old messages uploaded today, each source filter, zero matches, a foreign-chat attempt, refusal while an import is running or paused, refusal while the history lock is held, and a partially summarized month whose consumed messages were deleted (it restarts). Newer/unselected rows and existing summaries/notes remain; deleted rows cannot be searched; FTS, media descriptions and reply references stay consistent.

## 8. Schema strategy for a pre-live application

A clean development database is acceptable and is the preferred path if migrating old distillation stages, job snapshots, duplicate periods or retention metadata would complicate the new design. Do not implement elaborate legacy-job normalization solely to preserve disposable test data. The coding agent should record which path it chose and why.

For a fresh-database path:

- Implement and test the intended schema directly, without keeping dead distillation/retention fields solely for compatibility. Existing migration history may remain if harmless; rebuilding obsolete job tables in a forward migration is also acceptable. Do not build two schema implementations.
- Document how to stop the application, reset/recreate the intended development database and clear associated temporary uploads, then reconfigure/reimport. An explicit development reset command is acceptable. Never reset automatically on startup or silently delete an unknown database because its schema is old.
- If legacy databases are unsupported, detect them with an actionable reset/migration message before background jobs or old expiry settings run. Fresh initialization and reopening/restarting the new schema must both work.
- The owner's permission to accept a development reset is already recorded here; do not block implementation on a complex migration or repeatedly ask whether test history must be preserved. Confirm the target is the intended development instance before executing a reset. This planning task itself performs none.

If a small forward migration is straightforward, it is also acceptable. In that case, disable obsolete expiry/extraction before recovery, preserve existing notes/summaries/owner edits, handle duplicates before unique constraints and restart incompatible unfinished periods honestly. Do not silently discard conflicting owner-edited summaries. Conditional migration checks apply only if that path is implemented; legacy compatibility is not a release requirement for the reset path.

**Rationale:** correctness on the new lifecycle is required; compatibility with disposable pre-live jobs is not. Spend the effort on clear invariants and tests, not on preserving complexity the owner explicitly wants removed.

**Chosen path: one small forward migration (V13).** The reset path would still need code to detect an old database and refuse to start with a reset message, which is about as much work as migrating, and it throws away the owner's test history for no gain. Migrations are already sequential (`PRAGMA user_version`), so V13 is a short SQL script:

- De-duplicate history digests per `period_id`: keep the one its period references; delete unreferenced unedited duplicates; detach (set `period_id` to NULL) any unreferenced **edited** duplicate so no owner edit is lost. Then add a unique index on `history_digests (period_id) WHERE period_id IS NOT NULL`.
- Add `settings_hash` and `source_hash` to `history_periods`. Existing unfinished periods have neither, so they fail the check on resume and restart honestly.
- Drop the distillation and retention-skip columns from `imports`. An unfinished import whose only remaining stage was distillation then has nothing left to do and completes when resumed or recovered.
- Leave stored values of removed settings in the `settings` table: `SettingsService.reload()` already ignores keys that are no longer registered, and they remain as history.

Fresh initialization runs every migration, so the fresh path is covered by the same code.

## 9. Evidence, implementation order and release checks

### 9.1 Findings that motivated this plan

Earlier review passed 33 history/queue tests and used isolated fake-model probes to reproduce the following failures. A later review passed the history/memory/queue suites; a context/retention check passed 17 tests. These are observations about the old implementation, not verification that this plan has been implemented.

| Finding | New treatment |
| --- | --- |
| Crash between digest insertion and period completion creates duplicates. | Atomic completion and one result per work item. |
| Distillation skips a failed chunk yet completes and deletes its upload. | Remove the entire extraction stage. |
| Expired archive hold permanently marks an available month missed. | Remove expiry/hold/deadline machinery entirely. |
| Positional live resumption skips messages after source changes. | Ordered input validation and restart of invalid unfinished work. |
| Parent cancellation is stuck behind globally paused dispatch. | Interrupt every wait and withdraw queued tickets. |
| Saved settings fingerprint differs from actual generation inputs. | Use a frozen, application-controlled generation snapshot. |
| Proposed pause rule deleted completed staged replacements. | Retain staged results on pause/failure; validate complete publication membership. |
| Batch-only import guard still lets imported facts flow through the digest into later notes. | Live-only automatic keeper; remove import digest initialization. |
| Failed oldest live month can monopolize retries. | Persist bounded failures, expose manual Retry, continue later months. |
| Initial-context fitting does not bound later tool rounds or oversized fixed input. | Full-input enforcement before every model request. |

Additional deletion/replacement/coverage requirements above come from code inspection and explicit product decisions. Do not describe all of them as reproduced production incidents.

### 9.2 Implementation order

| Phase | Work | Completion gate |
| --- | --- | --- |
| 1 — Establish the new storage policy | Choose schema/reset path; remove message expiry, import age cutoffs and archive holds/deadlines; decouple operational cleanup. | Fresh database works; maintenance cannot age-delete messages; old selected exports can be stored. |
| 2 — Remove automatic import memory work | Remove distillation and initialization; make keeper queries/counts live-only; simplify two-stage import UI. | No import-driven keeper/note extraction; live upkeep and explicit remember still work. |
| 3 — Make writes and controls safe | Atomic raw/completion/publication; preserve staged results; edited-target check at publication; pause/cancel by task cancellation; restart handling. | Crash, pause, failure, retry and cancellation satisfy the invariants. |
| 4 — Finish history jobs and context limits | Settings/source hashes with restart on mismatch; full-request budgets; bounded monthly failures and Retry; truthful coverage. | Later months proceed after failures; every request fits or fails clearly; no mismatched-source resume. |
| 5 — Add age-based manual deletion | Extend existing admin flow with the age mode; refuse while an import is busy or the history lock is held; verify. | All section 7 cases pass without unrelated data loss. |
| 6 — Integrate and document | Remove obsolete copy/tests/settings; exercise the complete user flows and full suite. | Documentation and UI describe the actual new behavior. |

Prefer small reviewable commits by behavior. Preserve unrelated staged changes already present in the repository. Do not replace the scheduler or expand into semantic search, fact provenance, recording-interval gap repair, a generic workflow framework or an extra summary hierarchy.

### 9.3 Verification

Add focused tests at transaction, queue, source-revision and lifecycle boundaries. Assert persisted rows, result membership, visible coverage, FTS/references, note changes, source files and queue counts—not only status strings.

Extend relevant suites: `test_history.py`, `test_importer.py`, `test_memory.py`, `test_context.py`, `test_agent.py`, `test_model_queue.py`, `test_llm.py`, `test_web.py`, `test_db.py` and startup/shutdown tests. Test clean initialization and restart; add legacy migration tests only for a supported migration path. Run focused suites per behavior, then the complete `.venv/bin/python -m pytest -q` suite after integration.

Manual verification in a temporary database/test chat:

- Import old history raw-only, summaries-only and combined; verify raw retention, range selection, source expiry/re-upload messaging and no automatic note extraction.
- Generate monthly summaries, fail an early month, complete a later month, retry the failed month and restart after completion without duplication.
- Pause after part of a replacement group finishes; resume without losing staged results or exposing partial replacement coverage.
- Change source/settings and edit/delete a target while generation runs; verify explicit conflict/restart outcomes.
- Delete messages older than N days; inspect cutoff, count and affected-job preview; verify raw search, retained summaries/notes and no recovery-based resurrection.
- Exercise multi-round historical search with long results and large background context; verify the complete input budget each round.
- Advance maintenance time by months with old messages stored; only operational logs/traces/uploads/completed reminders should expire.

Run a small real-model quality sample early enough to inform implementation, then repeat after relevant generation changes. Seed early/late topics, dated decisions, cancelled plans and multiple speakers in a dense period. Verify useful attribution and early-fact survival, and that a summary is distinguished from an exact quote. If the quality is inadequate, adjust grouping/prompt/limits within this design; do not silently add another summary pipeline. Fake-model tests establish mechanics, not summary quality. Keep real exports outside the repository.

Release is complete when originals remain until explicit owner action, import extraction and expiry machinery are absent, all model inputs are bounded, history jobs fail/retry honestly and idempotently, manual deletion cannot be undone by background recovery, and the chosen fresh-schema or migration path is documented and tested. No database reset or runtime change is performed merely by editing this plan.

## 10. Revision of 2026-10-02: what was trimmed and why

A code review against the implementation kept the owner decisions in section 1 and the invariants in section 2, and replaced several mechanisms with simpler ones that give the same guarantees for a single-owner, pre-live bot:

| Area | Before | Now | Why |
| --- | --- | --- | --- |
| Deletion during running work (7.4) | Coordinate and cancel affected producers, show affected jobs in the preview | Refuse while an import of the chat is running/paused; take the history lock; source hash catches stale partial summaries | Same rule `start()` already uses for a second import; no cross-job cancellation states. |
| Deletion confirmation (7.2) | Server-side preview token, replay protection, change detection | Hidden cutoff field like the date mode | The authenticated owner can already delete everything; CSRF protects the form. |
| Generation inputs (5.3) | Persist a versioned settings snapshot and use it across resume | Store a settings hash and a consumed-source hash; restart the period on mismatch | Same "never mix" guarantee; costs a few model calls instead of plumbing a snapshot. |
| Stop controls (5.2) | Stop event checked at every wait point | Cancel the job's asyncio task; keep the cooperative flag only for the raw worker thread | `ModelQueue.acquire()` already withdraws a cancelled waiter. |
| Edited target at publication (5.1) | Pause for renewed replacement approval | Leave the edited group alone, as the planner does for edited summaries | No new approval flow. |
| Context capacity (5.5) | Reserve headroom against "configured context capacity" | `context.input_token_budget` caps every agent request including tool rounds | There is no capacity setting; the owner sizes the server for budget + output. |
| Recent window (3.3) | Investigate and possibly add cursors | Measured; descending `LIMIT` select, keep the count | 55 ms at 1M messages, half of it removable with a one-line query change. |
| Export regeneration from stored messages (2.3) | Allowed when coverage can be verified | Out of scope; re-upload | It is a new feature, not a simplification. |
| Grab-bag items (5.6) | DST arithmetic, stored-timezone labels, name-change preview, retry timing labels | Moved out of this pass | Unrelated to memory simplification. |
| Schema (8) | Prefer reset | Small forward migration V13 | Reset still needs legacy detection; migrating is about the same work and keeps test history. |

Already in place and needing only tests, not code: FTS cleanup on delete (trigger), media descriptions (cascade), dangling reply links (`delete_for_chat()`), reply snippets (not stored when the target is stored), the digest cursor stored by value, and suppression of deleted live summaries (their period stays `done`).

## 11. Implementation status (2026-10-02)

Implemented on branch `memory_simplification`, phases 1–6, with the full test suite passing after each phase. Notes for review:

- **Schema:** migration V13 as described in section 8; tested by upgrading a version-12 database with duplicate summaries, an edited duplicate and a paused distillation stage.
- **Setting meanings changed:** `history.chunk_tokens` ("Tokens per request", default 8,000, minimum 2,000) and `memory.digest_input_tokens` ("Tokens per update", default 12,000, minimum 2,000) now count the whole request, not only its messages, so their defaults were raised to keep about as many messages per request. `retention.reminders_days` (default 30) replaces the reminders' use of the removed live-message retention. A stored value for a removed setting is ignored.
- **Pause during a period's last request** keeps that request's result as the period's checkpoint rather than publishing it; resuming completes the period without another request.
- **Visibility of an import's incoming messages:** hidden from replies, searches and the message browser while its raw stage runs (`MessageRepository._incoming`). The extra lookup runs only while an import of that chat is storing messages.
- **Not done in this pass** (as planned in section 10): DST-aware calendar filters, stored-timezone labels, a name-change preview before import Start, retry-timing labels, rebuilding export summaries from stored messages, and a real-model summary quality sample (fake-model tests cover the mechanics only).
