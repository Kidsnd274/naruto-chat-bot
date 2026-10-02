# Telegram permissions, own-message deletion and quieter replies

Status: proposed; this document does not change the running bot.

Reviewed: 2026-10-02. Scope: current repository, supplied chat screenshot and current Telegram documentation. The screenshot is evidence of the experience, not an instruction to act in that group. No live chat was changed or queried.

## 1. Outcome

Naruto should leave the group with one useful result, rather than a trail of progress updates, repeated explanations and obsolete cards.

The non-negotiable rule is: **every Telegram deletion performed by this application must target a message sent by this bot account. It must never delete a member's message or another bot's message, even when Telegram would permit it.** Enforce this in application code, below the model's tools. A prompt instruction alone is insufficient.

Recommended first release:

1. A guarded `delete_own_messages` tool for explicit cleanup requests.
2. One temporary, editable progress message per task, removed when its final result is delivered.
3. One canonical plan or board representation, edited in place; no unnecessary explanatory reply after a successful post.
4. Accurate permission reporting and a setup recommendation that does not request broad deletion rights.

Additional capabilities are ranked in section 8; they need not all ship with deletion.

## 2. What exists today

| Capability | Current implementation | Relevant finding |
| --- | --- | --- |
| Mention/reply chat and focused skills | `tg/responder.py`, `agent/runner.py`, `agent/skills.py` | Enabled groups only; runs are serialized per chat. Ordinary replies already ask for 1–3 sentences. |
| Temporary progress and typing | `tg/progress.py`, `tg/responder.py` | After 8 seconds by default, summary/plan/questions skills post one static placeholder. Usually it is edited into the answer. `Progress.discard()` already calls `delete_message` when there is no final text or the placeholder cannot be reused. |
| Pinned board | `tg/board.py`, `db/board.py` | Normally edits one message. `/board` deliberately uses `fresh=True`; a replacement unpins the old board after a successful new pin but leaves its message behind. Board sends bypass the normal transcript recorder. |
| Plan cards and Confirm/Change buttons | `agent/tools/group.py`, `tg/plans.py`, `db/plans.py` | Same-title open proposals are replaced by new cards; old cards are marked replaced. Confirmation retains the full card and adds a board item. Change currently displays instructions to mention the bot again. |
| Board deduplication | `db/board.py` | Normalizes whitespace and case for exact text deduplication. It has no stable plan identity, so differently phrased descriptions of one event can coexist. |
| Pin/unpin tools | `agent/tools/group.py` | Can target recorded live messages, including members' messages. This is separate from deletion; the new ownership restriction must not accidentally disable legitimate requested pins. |
| Native polls and vote recording | `agent/tools/group.py`, `tg/polls.py` | Creates polls and records updates. No model tool to close a poll. |
| Reminders | `agent/tools/reminders.py`, `tg/reminders.py` | Set/cancel and scheduled delivery with retries. No general reminder edit/list tools. |
| History, summaries and catch-up | `agent/tools/history.py`, `archive.py`, `tg/skill_commands.py` | Searches live/imported history and dated summaries; `/summary` supports ranges. `/catchup` attempts ephemeral delivery, then DM, then a public group message. |
| Memory and people | `agent/tools/memory.py`, `memory/`, `tg/commands.py` | Remember/forget/search, automatic notes, digest, aliases and member information. Local data changes are distinct from deleting Telegram messages. |
| Images/media | `agent/tools/media.py`, `media.py` | On-demand image understanding and cached descriptions; ordinary media is recorded as metadata. No need to expand moderation permissions. |
| Group access and admin UI | `tg/access.py`, `db/chats.py`, `web/` | Owner approval, enable/disable/leave, rights refresh, settings, logs, traces, imports, local data deletion and prompt lab. Tracks `can_pin` and `can_delete`; only pinning currently counts as a missing required right. |
| External web search | `plans/WEB_SEARCH_PLAN.md` | Planned, not currently registered as a tool. Any future search should use the same progress/delivery lifecycle. |

All source paths above are relative to `naruto/` unless prefixed with `plans/`.

### Why the screenshot becomes noisy

The screenshot shows the same event represented by an explanatory paragraph, an interactive plan card and a board entry. A later cleanup request gets another explanatory reply without removing the obsolete messages.

The code provides plausible causes; the screenshot alone does not establish which tool calls or database-backed prompt overrides ran:

- `propose_plan` creates a visible card; confirming it also publishes the board.
- `banter.md` permits `[NO REPLY]`, but `plan.md` and `decide.md` still ask for a closing line, and `questions.md` asks to repeat questions after putting them on the board.
- Tool results encourage silence, but there is no structured delivery policy that reliably prevents redundant prose.
- A generic request to summarize an existing plan can be routed into creating a new proposal.
- Board publication returns prose, mixing “saved”, “posted”, “edited”, “pin failed” and “publish failed”. `update_board` records an action before publication succeeds. The runner cannot reliably infer what people actually saw from an action count.
- The progress placeholder is not a dynamic stage indicator, and there is no general cleanup tool available to the model.

The solution needs message lifecycle and delivery changes as well as shorter prompts.

## 3. Telegram permission policy

Telegram permits bots to delete their own outgoing group/supergroup messages without broad deletion rights, normally only while messages are under 48 hours old. Basic-group administrators can delete any message; supergroup administrators need `can_delete_messages` for that broader ability. Certain creation service messages cannot be deleted. See [deleteMessage](https://core.telegram.org/bots/api#deletemessage).

The documented group pin/unpin requirement is administrator status with `can_pin_messages`. See [pinChatMessage](https://core.telegram.org/bots/api#pinchatmessage) and [unpinChatMessage](https://core.telegram.org/bots/api#unpinchatmessage).

### Recommended configuration

| Permission or setting | Recommendation for Naruto |
| --- | --- |
| Read group conversation | Keep the existing privacy-mode setup for history/summary features; this is a receiving configuration, not permission to modify members' messages. |
| Send messages / polls | Retain the ordinary group permissions needed for existing replies, plans, reminders and polls. |
| Pin messages | Grant only if automatic pinning is wanted. An unpinned board should remain usable. |
| Delete messages | Do not request or recommend it for self-cleanup. Leave it off where configurable. Ownership checks still apply if it is granted later. |
| Ban/restrict members, invite users, change group info, promote admins, manage topics/video chats, channel/story administration | No current requirement; do not add these rights as part of this work. |

Telegram permissions alone cannot express our complete ownership policy, especially for a basic-group administrator. The guarantee is enforced by Naruto's shared deletion boundary; it is not a claim that Telegram removes every broader power from the bot token.

Update the README's “optionally Delete messages” advice. Separate “broad Telegram deletion permission” from “can attempt deletion of a verified own message” in the admin UI and model context. Never gate self-deletion on `chat.can_delete`.

The existing `rights_of()` also accepts member-level pin permissions and treats absent administrator pin fields optimistically. Reconcile this with documented requirements and an integration check; retain actual successful pin results as evidence instead of promising that all basic-group members can pin. A missing pin right should not be repeated in every group reply, and should not be reported as required when `board.pin` is off.

## 4. Guarded own-message deletion

### A. Durable ownership evidence

Introduce an outgoing-message registry with a stable internal reference and:

- Actual Telegram chat ID and message ID, bot account ID, sent timestamp and optional topic ID.
- Logical chat ID, trigger/requester and run ID when applicable.
- Purpose: progress, answer, plan, board, poll, reminder or command response.
- Linked plan/board identity, replacement reference and lifecycle state.
- Deletion attempt/result metadata, without requiring message text to prove ownership.

Populate it from trusted successful Telegram send results, including raw rich-message responses. Cover every public group send path: replies and split chunks, progress, boards, plans, polls, reminders, commands and public catch-up fallback. Store edits in the transcript as well as the registry so obsolete text does not remain the apparent current answer.

`StoredMessage` already distinguishes live/imported data and preserves `sender_id`, `origin_chat_id` and `from_bot`; reuse these distinctions. In this repository `from_bot` specifically means the configured bot, but do not trust that flag alone as a lasting authorization boundary.

For existing messages, allow backfill only from trusted live records whose actual sender matches the current bot account and whose original chat/message IDs are known. Never infer ownership from names, an imported export, quoted text, a forwarded origin or model-supplied claims. Legacy board/progress messages without enough evidence remain undeletable until ownership can be established through a trusted source. Do not guess message IDs or sweep numeric ranges.

Keep ownership records independent of ordinary transcript cleanup and agent-run retention. If Telegram accepted a send but the response/registry write was lost, report that the message is not tracked; do not invent a recovery ID.

### B. One deletion service used by every caller

Create a shared service, for example `naruto/tg/own_messages.py`. Route `Progress.discard()` and every new cleanup path through it. Keep the actual Telegram deletion call inside this boundary; do not offer a raw Telegram API tool.

Before any API call, validate:

1. Trusted ownership evidence matches the current authenticated bot ID.
2. The target belongs to the current logical chat and has the correct original Telegram destination. For the initial release, reject pre-migration targets rather than trying their old message IDs in a new supergroup.
3. The reference names an ordinary, supported bot-sent message, not an import, user message, other bot message, anonymous sender or guessed identifier.
4. The target is within the supported age window and policy scope.
5. Active shared artifacts are not being removed by routine cleanup. A current board, active proposal or open poll requires an explicit artifact-specific action; deleting its message must not silently cancel its underlying state.

Normal model-triggered deletion also requires an enabled chat and an authorized current request. Internal progress cleanup may finish a previously authorized run after the chat is disabled, but can delete only that run's registered progress message.

Ownership failure means **zero Telegram deletion calls**, regardless of broad admin rights. Centralize this rule so an owner command, callback, background cleanup or future tool cannot bypass it.

### C. Tool contract and scope

Propose `delete_own_messages(message_refs: [...])`, limited initially to 10 resolved references per call. Expose a bounded `list_own_messages` lookup returning internal references, purpose, age, short preview and eligibility, scoped to the current chat/topic. Tool arguments must not accept arbitrary destination chat IDs.

This complements existing transcript search because boards and temporary statuses are not consistently recorded there today. Make cleanup available from ordinary conversation and relevant plan/decide/questions workflows; adjust `agent/skills.py` and the tool registry accordingly.

- “Delete that reply”: resolve the replied-to bot message.
- “Remove your unnecessary messages”: target this requester's recent related responses and superseded artifacts, not every bot message in the group.
- Default authority: members may clean up ordinary outputs associated with their own request; the bot owner or verified group admins may clean up other ordinary outputs. Automatic cleanup is limited to progress and registered superseded artifacts. This is a proposed product default, separate from the mandatory ownership check.
- If the target is ambiguous or the requested batch exceeds the bound, ask one short scope question. Do not repeatedly call the tool to evade the limit.
- Validate the entire candidate batch before deletion; reject a mixed-ownership batch without sending any delete calls. Once validated, execute per-message calls so partial failures are visible.

Return structured per-target outcomes: deleted, already deleted by this application, missing/unconfirmed, too old, ownership refused, protected artifact, retry scheduled or failed. A generic Telegram error is never proof of deletion. Retrying a known deletion must not create more chat messages.

Add deletion to action-claim checking, but use actual outcomes as evidence. Do not simply add it to `POSTING_TOOLS`: deletion is a successful side effect, not a newly delivered result.

### D. State and failure handling

Mark deleted messages as removed from Telegram; exclude them from normal recent context and default search, while keeping a minimal audit record. Explicit historical inspection can show a deletion marker. This does not imply erasing memory, digest facts or local retention data.

Clear or repair artifact message pointers after explicit artifact deletion. Cancel stale inline callbacks and prevent an old deleted proposal from being confirmed later. Preserve the underlying plan unless cancellation was requested separately.

Use bounded retry/backoff for transient errors and respect Telegram rate-limit delays. Too-old messages remain; optionally mark a superseded own card with a short replacement pointer if editing succeeds. Do not request broader rights as a workaround for the age limit.

## 5. One progress message, then one final result

Extend `Progress` into a runtime-owned lifecycle; the model should not need to spend tool calls maintaining it.

1. Fast requests show typing only. Keep the existing configurable 8-second starting threshold.
2. Slow work creates at most one silent progress message in the originating topic, registered immediately.
3. Edit it on meaningful stage transitions: queued for model, reading history, preparing result. Use known runtime events, not invented percentages or model reasoning text. Throttle edits, initially to at most one per 5 seconds.
4. Stop and join the progress task before finalization so a delayed send cannot appear after the answer.
5. Deliver one final answer or the requested card/poll. Once delivery succeeds, delete the progress message through the guarded service. This near-simultaneous handoff leaves only the result, while avoiding loss of the only visible status if final delivery fails.
6. If a tool already supplied the complete result, that artifact is the final delivery; do not send another answer. An edited existing board may need one short acknowledgment or link because no new result appeared in the conversation.

Cover success, model failure, cancellation, deadline, empty output, skill handover and disabled chat. A failed deletion must not prevent final delivery. If final delivery fails, retain or edit the existing status to one concise failure notice when possible; log unresolved failures. Do not recursively create status messages for status cleanup.

Persist enough lifecycle metadata to reconcile registered orphan progress messages after restart, within the deletion window. Do not claim exactly-once delivery across an ambiguous network timeout; track uncertainty rather than blindly reposting a final result.

Preserve topic routing throughout. The existing recorder stores `thread_id`, but the sending helpers and progress object need explicit propagation. Calls, callbacks and background sends touching the same artifact should share serialization/version checks; the responder lock alone does not cover all these paths.

## 6. Stop generating duplicate plans and boards

### Intent determines the output

| Request | Desired result |
| --- | --- |
| Summarize the existing plan | One compact summary or link to the current card; no proposal or board mutation unless requested. |
| Propose a plan | One interactive card; unresolved points appear there or in one existing board location without repeated prose. |
| Change a plan | Update the same plan/card by stable identity; reset confirmation when meaningful details change. |
| Confirm a plan | Commit the confirmation, publish/update the canonical board entry, then retire the proposal's full duplicate representation. |
| Show the board | Link to or privately show the existing board where supported; update it in place. Resend publicly only when requested or the existing message is known missing. |
| Clean up duplicate bot messages | Preserve the canonical result and delete verified eligible obsolete copies. |

### Stable identity and successful publication

Add stable board item IDs and an optional `plan_id` association. Upsert a confirmed plan's board entry by identity instead of appending another paraphrase. For older unlinked entries, merge only when correspondence is clear; keep uncertain entries separate rather than deleting distinct events with similar titles.

Use plan IDs and revisions for edits and callback validation, rather than title equality. Confirming an obsolete revision must not confirm newly changed details. Keep the existing “any member can confirm” behavior unless deliberately changed later, but bind callbacks to the actual chat, current plan message and revision.

After successful board publication, delete the superseded proposal if eligible; otherwise compact it to a confirmed/replaced pointer when editing works. If publication fails or the board is full, keep the plan card as the useful result. A failed pin is not a failed publication: an unpinned delivered board can still be canonical.

Batch multiple section changes into one board publication per run. Return structured saved/published/edited/pinned/error results. Transient edit failures should not automatically spawn replacement boards. When replacement is required, first deliver and persist the replacement, then clean up the verified old board; never delete an active board before its replacement exists.

## 7. Shorter replies without losing useful detail

Use a shared delivery policy in the runner/responder, backed by structured tool outcomes:

- A newly posted complete card or poll normally needs no extra prose.
- An update to an older message needs at most one short acknowledgment/link if it would otherwise be invisible to the requester.
- A real partial failure or unanswered question may need one concise explanation. Do not suppress it merely because some other tool succeeded.
- Routine banter: usually one sentence, occasionally two. Practical answers: a short lead and up to roughly five compact bullets. Longer detail remains available when requested or necessary.
- Avoid repeating names, dates and logistics already visible in the artifact. Keep Naruto's voice, but remove obligatory closings and filler on utility tasks.
- Treat old unresolved details as historical context until current evidence supports carrying them forward. Do not revive stale questions just to fill a plan summary.
- Keep pin-right warnings in the admin UI, with a concise user-facing notice only when relevant to the requested action. Do not repeatedly ask the group to grant permissions.

Unify `rules.md`, `banter.md`, `plan.md`, `decide.md` and `questions.md` around this policy. Remove contradictory mandatory follow-up instructions. Ensure trusted tool-use/delivery rules live in the prompt or runtime rather than depending on prose instructions embedded in tool results.

Prompt settings are stored in the database after seeding. Editing Markdown files alone will not update an existing deployment: provide a reviewed settings migration or prompt-lab candidate activation that preserves customized overrides. Do not globally reduce output tokens and risk truncating summaries or tool arguments.

Later tone evaluation should use the `naruto-lab` workflow with agreed budget and activation policy, using synthetic examples rather than copying the supplied conversation into the repository. No experiment or activation is part of this planning task.

## 8. Other useful additions, ranked

| Priority | Addition | Benefit and boundary |
| --- | --- | --- |
| P0 | Own-message deletion and dynamic progress | Required by this request; ownership enforcement first. |
| P0 | Edit plan, stable board entries and consistent delivery policy | Addresses repeated content at its source. |
| P1 | “Done”, “Cancel”, “Change” actions on canonical plans | Manage the existing artifact without a fresh conversation-sized response. Use callback acknowledgments; validate chat, actor and revision. |
| P1 | List/edit/snooze reminders | Avoid cancel-and-recreate chatter; edit pending state and acknowledge briefly. Do not silently remove already-delivered reminders. |
| P1 | Close the bot's own poll | Resolve decisions in place. Telegram supports stopping bot-sent polls via [stopPoll](https://core.telegram.org/bots/api#stoppoll); reuse ownership checks and record the resulting decision once. |
| P1 | Quiet per-chat behavior setting | Short utility responses, silent progress, no redundant follow-ups; keep intentional due reminders noticeable. Start with one preset rather than many independent switches. |
| P1 | Safer private catch-up fallback | Preserve ephemeral/DM delivery. If both fail, offer a short public notice instead of dumping the full private catch-up into the group. |
| P2 | Optional reaction acknowledgment | Use the bot's own reaction for simple acknowledgments; never replace necessary answers or failures with an emoji. Check chat reaction availability. See [setMessageReaction](https://core.telegram.org/bots/api#setmessagereaction). |
| P2 | Private “Details” view and board navigation | Expand long information only for the requester using existing ephemeral infrastructure. Telegram supports user-specific group responses and private button views; see [ephemeral messages](https://core.telegram.org/bots/features#ephemeral-messages). Test delivery limitations rather than assuming a slow response is always eligible. |
| P2 | Stop current work button | Cancel queued/model work and clean up progress. Do not undo already-completed side effects automatically. |

Do not add member moderation, deletion of join/service messages, mass cleanup of the group's history, promotion or group-management tools in this project. Deleting a member's command to reduce clutter is also outside the ownership rule. Prefer ephemeral commands where appropriate.

## 9. Implementation sequence

### Phase 1 — ownership and deletion

- Add migration/repository for outgoing ownership and lifecycle state; integrate all group send paths and trusted legacy backfill.
- Add shared guard and structured deletion outcomes; move existing progress deletion behind it.
- Add bounded lookup/delete tools, skill access, action-claim handling and trace visibility.
- Correct README and permission labels. Do not change live Telegram permissions automatically.

Primary files: `naruto/db/migrations.py`, new outgoing-message repository, `naruto/services.py`, `naruto/tg/recorder.py`, `sending.py`, `progress.py`, `board.py`, `plans.py`, `commands.py`, `skill_commands.py`, `reminders.py`, `naruto/agent/tools/`, `agent/skills.py`, `agent/claims.py`, relevant admin templates.

### Phase 2 — progress and delivery

- Add structured run delivery effects: visible result, existing artifact updated, partial failure, and cleanup required.
- Implement single-message stage updates and final-result handoff with restart/error cleanup.
- Propagate topic IDs and coordinate artifact writes across responder, callbacks, scheduled work and web admin.
- Replace action-count assumptions used for silence/last-request handling with verified outcome handling.

Primary files: `naruto/tg/progress.py`, `responder.py`, `sending.py`, `naruto/agent/runner.py`, `agent/tools/base.py`, `agent/tools/group.py`, `naruto/settings/registry.py`.

### Phase 3 — plans, board and wording

- Add plan-linked board items, in-place plan revisions and valid callback transitions.
- Batch board publication, retire obsolete copies and change `/board` away from unconditional reposting.
- Apply consistent short-response instructions through the actual settings path.
- Evaluate synthetic planning/banter/serious-message cases before recommending a prompt candidate.

### Phase 4 — selected additions

Add P1 items independently after the lifecycle is stable. P2 features remain optional; none should block shipping the requested self-cleanup behavior.

## 10. Acceptance checks

### Ownership is mandatory

- Human, other-bot, imported, unknown, wrong-chat, forged-name and pre-migration references cause no delete API calls.
- Repeat these checks with `can_delete_messages` enabled and basic-group administrator status.
- Valid own messages can be deleted with broad deletion rights absent.
- Mixed batches fail validation before any deletion; confirmed deletions are idempotent.
- Check just below/at/above the age cutoff, mismatched bot accounts, stale callbacks, protected active artifacts, disabled chats, transient errors and partial execution failures.
- Legacy messages without ownership evidence remain untouched. Local transcript deletion cannot accidentally create permission to delete a Telegram message.

### Chat stays readable

- Fast reply: no progress message. Slow reply: one progress message edited during work, one final result, progress removed.
- Tool-only success: the card/poll is the result; no unnecessary text reply.
- An existing-board edit gets at most one useful acknowledgment; partial failure stays visible.
- Cancellation, shutdown, restart and send/delete failures do not produce a continuing stream of status messages.
- Synthetic plan scenario ends with one canonical full representation after confirmation, not a paragraph plus two copies of the plan.
- Repeated confirmations and revised plans update one logical board item. Failed board publication preserves the useful proposal.
- Topic-specific requests stay in their topic. Concurrent callbacks and web edits do not overwrite a newer revision.

### Verification approach

Extend focused tests in `tests/test_tg.py`, `tests/test_agent.py`, `tests/test_db.py` and `tests/test_e2e.py`, plus dedicated deletion/lifecycle tests and `tests/telegram_fake.py`. Check actual API-call targets and final visible-message state, not just tool response strings.

Update the lab's simulated Telegram adapter and capability reporting for new tools. The lab currently excludes progress messages, plan button presses, actual rendering and real ephemeral delivery; those cannot be reported as passing from lab results. Use deterministic handler tests and a dedicated test group for those behaviors during implementation.

Measure new public messages per request, redundant follow-up rate, orphan progress count, deletion failures by reason and falsely claimed successful actions. The hard safety target is zero attempted non-owned deletions. The normal completed-task target is one substantive result and no leftover progress message.

Planning validation: repository paths and behavior inspected; deletion and permission rules checked using Context7 and official Telegram documentation. No runtime tests or model experiments were run because only this plan was created.
