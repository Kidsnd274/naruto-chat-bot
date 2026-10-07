# Telegram permissions, own-message deletion and quieter replies

Status: proposed; this document does not change the running bot.

Reviewed: 2026-10-02, then revised the same day after the owner's review. Scope: current repository, one supplied chat screenshot (described in §3) and current Telegram documentation. No live chat was changed or queried.

## 1. Outcome

Naruto should leave the group with one useful result, rather than a trail of placeholders, repeated explanations and duplicate plan cards.

The non-negotiable rule is: **every Telegram deletion performed by this application must target a message sent by this bot account. It must never delete a member's message or another bot's message, even when Telegram would permit it.** This is enforced in code, below the model's tools; a prompt instruction alone is not enough.

Who decides what gets deleted:

- **Code** deletes only one thing on its own: a run's progress placeholder, once the run's result is in the chat (§6). Nothing else is cleaned up automatically.
- **The model** deletes only when someone in the group asks it to delete messages, through one tool that can only reach the bot's own messages (§5).

First release:

1. A progress placeholder that is replaced by the result, or turned into the error message when the run fails.
2. A `delete_messages` tool for explicit requests, restricted to the bot's own messages.
3. Plans tracked on the board only: no more plan cards or Confirm/Change buttons. People settle plans themselves; the bot keeps track (§7).
4. Prompt wording changes, written out in §8 for the owner to apply; the implementation does not edit prompts.
5. Corrected permission advice in the README.

## 2. Out of scope

**The board.** The board was just redesigned (commits 2586826, b2d80a2, 8ee749e, e6e1a90), and this plan does not change it. Untouched: `naruto/tg/board.py`, `naruto/db/board.py` and its migrations, the `update_board` tool (title, plan details, questions "for" someone and the question pings), `/board`, the `board.*` settings, the web board editor, and the board instructions in `decide.md` and `questions.md`. The plan skill will record plans with the existing `update_board` (§7), which adds a caller but changes no board code.

As a result:

- Board messages are never deleted. The deletion guard refuses the current board message, and older board copies left behind by `/board` stay where they are.
- Nothing links to the board; the pinned message is the way to find it.
- The "couldn't pin it… I need to be a group admin" note in the board's publish result stays in the code. §8 only asks the model not to repeat it unprompted.

**Prompts and the persona.** The implementation does not edit any prompt file (`naruto/prompts/*.md`) or prompt setting, and `persona.md` is not touched at all. Where the plan needs different wording, §8 gives the exact text and location, and the owner applies it.

**Moderation.** No deletion of members' messages, join/service messages or old group history, and no promotion or group-management tools.

## 3. What the screenshot shows

The screenshot was taken before the board redesign. It shows the old `Board · updated …` layout with one bulleted plan line, so the board's appearance is already addressed. The sequence:

1. The owner mentions the bot: "can u summarize the plan. it's at 8pm and [a member] will be fetching ppl".
2. The bot replies with a long paragraph. It says the plan is up with Confirm/Change buttons, restates every detail, brings up an unconfirmed driving arrangement from two days earlier, and says it couldn't pin the board and someone with admin rights should do it.
3. A plan card follows with the same five details. The owner confirms it, so it ends with "✅ Confirmed by …".
4. A board message follows, with the same plan as one line.
5. The owner asks: "the plan has a duplicate, fix it. delete ur unnecessary msgs".
6. The bot answers that the board is cleaned up, but "I can't delete my own messages from here though, so those two are stuck unless someone with admin deletes them."

So one request produced three copies of the same plan, and the cleanup request produced a fourth message plus a false claim.

Likely causes in the code (the screenshot alone doesn't show which tool calls ran):

- A request to *summarize* a plan ran the plan skill, which proposed a new card.
- `plan.md` asks for "one or two lines" after posting, while the `propose_plan` result says not to repeat the plan. The model wrote a paragraph anyway.
- The reply revived a stale open point from earlier days instead of sticking to the current discussion.
- The board tool's pin-failure note was passed on to the group.
- Confirming keeps the full card, and the board shows the same plan again.
- No tool can delete a message, so the model invented a reason. In fact, Telegram lets a bot delete its own messages for 48 hours without admin rights.

## 4. What exists today

| Area | Current implementation | Relevant finding |
| --- | --- | --- |
| Progress and typing | `tg/progress.py`, `tg/responder.py` | After 8 s (`behaviour.progress_after_seconds`), the summarize, plan and questions skills post one placeholder as a reply, without silent delivery. A text answer is edited into it, so the answer itself notifies nobody. `Progress.discard()` deletes it when there is no text, and when the chat was disabled meanwhile. `progress.stop()` already joins the posting task. |
| Failure text | `agent/runner.py` | Deadline, stuck and exception paths finish with canned text (`fallback=True`), which today also replaces the placeholder. |
| Transcript of the bot's own messages | `tg/recorder.py`, `tg/content.py`, `db/messages.py` | `record_sent` stores replies, plan cards, polls, question pings and reminders from Telegram's send responses. `from_bot` is `sender.id == bot_id`. Rows carry `source` (live/imported), `origin_chat_id` and `thread_id`. Placeholders, boards, command replies and the public catch-up fallback are not stored. |
| Plan cards | `agent/tools/group.py`, `tg/plans.py`, `db/plans.py` | A same-title open proposal is cancelled and its card edited to "Replaced by a newer plan"; the new card is a separate message. Confirming keeps the full card and adds the plan to the board. The callback checks the plan ID only, not the chat or message it came from. |
| Pins | `agent/tools/group.py`, `tg/access.py` | `rights_of()` and `note_pin()` track `can_pin` from member status and from real pin results. Only a missing pin right is shown as missing. |
| Action claims | `agent/claims.py` | Re-asks the model once when an answer claims a reminder, memory, poll or board action that no tool performed. |
| Topics | `tg/sending.py` | `send_text` passes no `message_thread_id`. Only the first chunk is a reply, so in a forum group the second and later chunks of a long answer land in General. |

All source paths are relative to `naruto/`.

## 5. Deleting the bot's own messages on request

### A. Ownership evidence: the transcript

No separate message registry is needed. A transcript row is valid evidence when all of these hold:

- `source` is live, not imported;
- `from_bot` is set and `sender_id` equals the current bot account's ID;
- `origin_chat_id` is the current chat (a group that migrated to a supergroup is refused rather than guessed);
- the message was sent less than 48 hours ago.

These fields come from Telegram's own send responses (`record_sent`), never from the model, names, quoted text or imports. Bot messages that are not in the transcript are out of reach for the tool. Boards are never deleted anyway, placeholders are handled by the run (§6), and command replies and the catch-up fallback stay undeletable. The tool says so rather than guessing IDs.

### B. One deletion function for the whole application

Add `naruto/tg/own_messages.py` and make it the only place that calls Telegram's `deleteMessage`. Use it for the progress placeholder too, replacing the direct call in `Progress.discard()`. The placeholder path checks the `Message` Telegram returned when the placeholder was sent: the same sender and chat checks, without a transcript row.

Before any API call, every target in the batch must pass the checks in A, and must not be the current board message (`boards.get(chat).message_id`, read only). If any target fails, **no** delete call is made for the batch. Broad admin rights change nothing.

After validation, delete one message at a time and report each result: deleted, already gone, too old (Telegram's 48-hour limit), not the bot's message, protected (the board) or failed. A "message to delete not found" error counts as already gone. On a rate limit, wait once as Telegram asks; there are no scheduled retries. A generic error is never reported as a deletion.

### C. The tool

`delete_messages(message_ids: list[int])` takes the transcript `[id]`s the model already sees, up to 10 per call, and only within the current chat.

- Available in the banter, plan, decide and questions skills, because cleanup requests can arrive mid-plan.
- The tool's own description (in code) says: use it only when someone asks you to delete messages, and only for your own. A matching `rules.md` line is proposed in §8 for the owner to add.
- "Delete that" in reply to a bot message targets that message (the `↩id`). "Delete your unnecessary messages" means the bot's own redundant messages about the current request, not every bot message in the group.
- Who may ask: any member of an enabled group. These are the bot's own messages and the board is protected, so no admin check is added. Change this if pranks become a problem.
- The tool's result tells the model exactly what happened (for example "2 deleted, 1 too old: Telegram only allows 48 hours"), so it never invents a permissions story again.
- `agent/claims.py`: add a check for answers that claim a deletion, and requests that ask for one, without a successful `delete_messages` call. Deletion is not a posting tool, so `POSTING_TOOLS` is unchanged.

### D. Transcript state

Add a `deleted_at` column to `messages` (migration). Deleted rows are left out of recent messages and search, but kept so the chat's history and summaries stay consistent. Memory notes and digests are unaffected.

## 6. Progress placeholder: rules in code

The model never manages the placeholder; the responder does.

1. **Fast runs:** typing only. The 8-second threshold and the three skills that get a placeholder stay as they are.
2. **Slow runs:** one placeholder, sent silently (`disable_notification`) as a reply in the request's topic.
3. **Stage updates:** the runtime edits the placeholder when the run reaches a new stage it actually knows about, for example "Reading further back…" when a history tool runs and "Writing it up…" at the final model request. At most one edit every 5 seconds; no percentages and no model reasoning text.
4. **Success with a text answer:** send the answer as a normal reply, which notifies the group as an answer should, then delete the placeholder. This replaces today's "edit the answer into the placeholder": that edit notifies nobody, while the placeholder itself was the message that pinged.
5. **Success without text** (a poll or a board update was the result): delete the placeholder.
6. **The run failed** (deadline, stuck, exception: `outcome.fallback`): edit the placeholder into the error message and **do not delete it**. Only if that edit fails is the error sent as a new message, with the placeholder then deleted so a stale "Pulling the plan together…" doesn't remain.
7. **The answer couldn't be sent:** edit the placeholder into a short failure notice; do not delete it.
8. **The chat was disabled during the run:** delete the placeholder, as now. The bot says nothing more there.
9. **The placeholder couldn't be deleted:** log it and leave it. Cleanup never posts new messages.

Topics: give `send_text` the request's `message_thread_id` so every chunk stays in the topic, and send the placeholder the same way.

Not in the first release: tidying up placeholders left by a crash or restart. That would need the placeholder's ID stored on the run row; add it only if orphans show up in practice.

## 7. Plans live on the board only

The owner's decision: the bot doesn't run a confirmation step. People settle plans among themselves, and the bot keeps track of them on the board. This removes the plan card, which was the duplicate in §3, and the board is untouched: it already shows plans with details as ⏳ (not settled yet) or ✅ (settled).

- **Remove the plan card:** the `propose_plan` tool and `_replace_older` (`agent/tools/group.py`, including its registration and its `POSTING_TOOLS` entry), its access in the plan and decide skills (`agent/skills.py`), `send_plan`, and the Confirm/Change handling in `tg/plans.py` and `tg/bot.py`.
- **Cards already in chats:** keep a small handler for the plan button prefix. When someone presses an old button, it answers "Plans are kept on the board now" and removes that card's buttons. The card's text and details stay as they are.
- **Open proposals in the database:** close them in a migration (`CANCELLED`, "plan cards removed"), and drop the open-proposals section from the model's context (`agent/context.py`). The `plans` table stays as history: the web board page and the lab snapshot still read it, and that code doesn't change.
- **How plans get tracked instead:** the plan and decide skills use the existing `update_board`. The plan goes in with its details, marked not done until the group says it's settled, and is updated in place rather than added again. This needs the `plan.md` and `decide.md` wording in §8.
- **Lab and docs:** remove `propose_plan` from `lab/capabilities.py` (`SIMULATED`), and rewrite the README's "Plans" bullet.

**Ordering:** today's `plan.md` and `decide.md` tell the model to call `propose_plan`. Remove the tool only after the owner has applied the §8 wording, including in the deployed prompt settings. Otherwise the model keeps calling a tool that no longer exists.

## 8. Proposed prompt wording (the owner applies it)

The implementation doesn't change any of this. `persona.md` and `questions.md` are not affected. Prompt settings live in the database after seeding, so the deployed wording has to change too (Settings, or a prompt-lab candidate), not just the Markdown files.

**`naruto/prompts/plan.md`**: replace the whole file (needed before §7 ships):

```markdown
## Your task: keep track of the plan

Work out the plan the group is making in the recent discussion (or the one the request names).

1. Collect what is settled: what, when (day, date and time), where, who does or brings what, costs.
2. Collect what is still open, from the current discussion only. Don't bring back old points nobody has raised again.
3. Put the plan on the board with update_board (section "plans"): a short name and one detail per item, done=false until the group says it's settled, done=true once it is. If the plan is already on the board, update it instead of adding it again (keep the other plans already there). Put the open points in section "questions", keeping questions already there that are still open.
4. If the group has to choose between options (for example the date), start a poll with create_poll.

Then reply with one short line in your voice: what is still open, or just that the board is updated. Don't repeat what's on the board. If someone only asked for a summary of a plan the board already shows correctly, reply with a short summary and leave the board alone.

If the discussion started further back than the messages you can see, read further with get_earlier_messages or search_chat first. Formatting: Telegram Markdown only. No headings, no tables.
```

**`naruto/prompts/decide.md`**: replace lines 3–8 (needed before §7 ships). The `update_board` line, from the board work, stays the same:

```markdown
Someone wants to settle something: record a decision or take a vote.

- If the group has to choose between options, start a poll with create_poll, using the options people mentioned (2 to 10, short).
- If they already agreed, record it on the board with update_board: as details of the plan it belongs to, or as a new plan with done=true (keep the plans already there).
- Then one short line in your voice, or just `[NO REPLY]` if the poll says everything. Don't repeat what the tool posted.
```

**`naruto/prompts/rules.md`**: add under "## Tools", after "Never say you did something unless a tool did it in this response." (optional; the delete tool's own description already says this):

```markdown
- Only delete messages when someone asks you to, and only your own. The tool tells you what it deleted and why anything was refused; say exactly that.
- If a tool reports a side problem (for example the board couldn't be pinned), mention it only when the request was about that.
```

**`naruto/prompts/banter.md`**, line 8: change "(a poll, a plan, the board)" to "(a poll, the board)". This is optional and only tidies the wording once plan cards are gone.

## 9. Telegram permissions

Bots can delete their own outgoing messages in groups and supergroups without any admin right, for messages under 48 hours old. An administrator in a basic group can delete any message; in a supergroup that needs `can_delete_messages`. Pinning in groups needs administrator status with `can_pin_messages`. See [deleteMessage](https://core.telegram.org/bots/api#deletemessage) and [pinChatMessage](https://core.telegram.org/bots/api#pinchatmessage).

| Permission | Recommendation |
| --- | --- |
| Pin messages | Keep as today: needed for the pinned board. |
| Delete messages | Not needed and not recommended; the bot deletes its own messages without it. The ownership check applies even if it is granted. |
| Ban, invite, change info, promote, topics, video chats | Not needed. |

Changes:

- README: replace "(optionally “Delete messages”)" with a note that the bot doesn't need the right to delete its own messages.
- `chat.can_delete` is never consulted for self-deletion.
- `rights_of()` stays as it is. It already prefers real evidence: `note_pin()` records every actual pin success or refusal, and the README's basic-group note describes observed behaviour. Rewriting it to match the documentation would risk breaking pins that work.

## 10. Later, optional

Not part of the first release; each can ship on its own afterwards.

- Close the bot's own poll ([stopPoll](https://core.telegram.org/bots/api#stoppoll)), recording the decision once.
- List, edit and snooze reminders, instead of cancelling and re-creating them.
- A safer catch-up fallback: if ephemeral and DM delivery both fail, post a short public notice rather than the full catch-up.
- A "Stop" button on the placeholder that cancels the run and turns the placeholder into "Stopped."

## 11. Implementation sequence

1. **Guard and placeholder:** `tg/own_messages.py`, `Progress` lifecycle (§6) including the error edit, topic IDs in `send_text`, README.
2. **Delete tool:** the `deleted_at` migration and context/search filtering, `delete_messages`, skill access, the claims check, trace visibility.
3. **Plans on the board only (§7):** waits until the owner has applied the §8 wording. Then remove the plan card and its buttons, add the handler for old buttons, close open proposals, and update the lab and README.
4. **Optional additions** from §10.

## 12. Acceptance checks

Ownership:

- A member's message, another bot's, an imported row, another chat's row, an unknown ID, a message over 48 hours old and the current board message each cause zero delete calls. Repeat the test with the bot as a basic-group admin and with `can_delete_messages`.
- A batch with one invalid target makes no delete calls at all.
- The bot's own messages can be deleted with no admin rights. A second deletion reports "already gone" and posts nothing.
- A deleted row drops out of the model's recent messages and search.

Placeholder:

- A fast run never shows a placeholder.
- A slow text run: placeholder, then the answer as a new reply, then the placeholder deleted.
- A slow run whose result is a poll or a board update: placeholder deleted, and no extra text.
- A failed run: the placeholder shows the error and is not deleted.
- A failed send: the placeholder shows the failure.
- A chat disabled mid-run: placeholder deleted.
- Topic requests stay in their topic, including later chunks.

Plans:

- No run posts a plan card, and `propose_plan` is gone from every skill and from the lab.
- Pressing a button on an old card answers "Plans are kept on the board now", removes the buttons and keeps the card's text.
- After the migration, no open proposals remain, and the model's context has no open-proposals section.
- A synthetic version of the §3 scenario, run with the owner's new wording, ends with one board entry for the plan and at most one short reply.

Tests go in `tests/test_tg.py`, `tests/test_agent.py`, `tests/test_db.py` and `tests/test_e2e.py`, using `tests/telegram_fake.py` to assert the actual API calls made (delete targets and final visible messages), not just tool result strings. The lab doesn't simulate placeholders, button presses or rendering, so those rely on handler tests and a trial in a test group. There is no separate metrics work: run traces already show tool calls and reply message IDs, which is enough to review the test-group trial.
