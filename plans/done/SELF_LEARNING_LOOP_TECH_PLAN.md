# Technical plan: the self-learning loop (prompt lab)

Implements `plans/SELF_LEARNING_LOOP_PLAN.md` (the feature plan). It also replaces the model evaluation harness (`naruto/evaluation`). On 2026-10-01 the owner said they don't need that harness and would rather tune the bot for each model through this loop.

Status: implemented on branch `self_learning_loop` (2026-10-02). See "Implementation notes" at the end for deviations and what still needs checking against the real model server.

## Context

What exists today, from the code on branch `history_digests`:

- **`naruto/evaluation`** already runs a case in a throwaway in-memory database through the production `AgentRunner`, `ContextBuilder` and tools. Telegram actions go to a recording stand-in (`RecordingTelegram`), and the automatic checks are `contains_any`, `not_contains`, `tool_calls` and the like. This is the right core, but it has gaps:
  - It runs one trigger per case, with no follow-up turns and no state carried between turns.
  - Cases can't seed notes, a digest, the board or reminders.
  - It uses the wall-clock time.
  - It talks to the model server directly, outside the model queue.
  - It only has a CLI that writes report files.
- **Agent runs** record the exact prompt, every model request and tool call, timing and usage.
- **Settings** cover every prompt and sampling parameter, with validation, history, revert and per-chat overrides.
- **The model queue** sends replies first and runs background work within limits, and the Queue page shows both.
- **Skills** are instructions, a tool subset and a reasoning switch. Commands pick a skill, and banter hands over with `use_skill`.

What the feature plan needs on top of that:

- an interface an external agent can drive without the owner relaying anything;
- candidate configurations that never touch the live bot;
- conversations with several turns, with state and a known test time;
- a record of every attempt;
- comparisons and reports that separate checks, AI judgments and owner choices;
- interactive A/B preference rounds that wait for the owner;
- activation with conflict detection and rollback;
- the usage instructions, the prompt-testing skill and a verified walkthrough.

## Concerns (for the owner)

1. **Size.** This is about as big as the history-digest work: seven stages, roughly 5,000–6,000 lines including tests. Each stage leaves the bot working and the tests passing.
2. **Experiments share the model server with the group.** Lab requests go through the bot's queue at background priority, so replies to people still go first. But every lab request has a different prompt prefix. On a one-slot server it can push a busy chat's prompt out of the server's cache, and that chat's next reply is then a cold request (15–35 s on Halogen). Run large suites when the groups are quiet. The Queue page's pause also pauses the lab.
3. **Latency numbers are noisy.** They depend on the prompt cache (the first attempt of a candidate is cold, repeats are warm) and on time spent waiting in the queue. Reports keep model time and queue wait apart and show cold and warm attempts separately where usage data allows. Use latency to spot large differences only.
4. **The model varies from one try to the next.** A conclusion needs repeats: 20 scenarios × 2 candidates × 3 repeats is 120 attempts, about 30–90 minutes on Halogen.
5. **The external agent sees what it evaluates.** If the agent is a cloud product (Claude Code, Codex), every reply, trace and scenario it reads leaves this machine. The model under test stays local. Synthetic scenarios are the default. Real chat content is readable only for chats you grant to the agent's API token.
6. **Choices relayed through the terminal can't be verified.** The agent shows A/B comparisons and records your choice, but the server can't prove the agent relayed it faithfully. Each answer records whether it came through the agent or through the web admin's Lab page, and the Lab page is always available for answering yourself.
7. **Model parameters are global.** Activating a temperature, max-tokens or reasoning-effort change also affects digest updates, history summaries, import distillation and image descriptions. The lab only evaluates replies, so the report says which background tasks a candidate's changes reach without being tested.
8. **The persona is being rewritten elsewhere.** This plan changes no prompt wording; only runs you start do. If the persona changes outside the lab during a run (for example by the other persona work), activation stops and reports the conflict instead of overwriting it. Prompt layout is code and is not tunable.
9. **`python -m naruto.evaluation` goes away.** Its sandbox, Telegram stand-in and checks move into the lab. The lab's scenario format is a superset of the old case format, so any case files you made still load. "Which model is better?" becomes one tuning run per model: each run tests one model, and a run can target another local model before you switch to it (decision 6).
10. **What is simulated and what isn't.** The report marks the following explicitly:
    - Simulated: Telegram actions (sending, pins, polls, the board, plan buttons) and reminder delivery between turns.
    - Not run: digest upkeep, monthly history summaries, progress messages, `/catchup`'s ephemeral delivery and the look of rich messages.
    - `describe_image` works only with an image file in the scenario.
    - Web search (planned) will need a simulated tool when it arrives.
11. **A new door into the admin.** The API is served on the web admin's localhost port, protected by a bearer token you create on the Lab page. A token can read lab data, including scenarios made from real chats. Treat it like the admin password. The web admin must be enabled (`ADMIN_PASSWORD`) for the API to exist.
12. **Migration.** One migration adds the lab tables and one column to `model_requests`. No existing data changes. Back up `data/naruto.db` before deploying anyway.
13. **The test-time change is wide but mechanical.** About 60 places that read the wall clock (`now_ts()`, `time.time()`, `datetime.now()`) start reading it through the database or services, so a sandbox can fix the time. Production behaviour is unchanged.

## Decisions made in this plan

1. **The interface is an HTTP JSON API inside the bot's process, plus a small CLI.**
   - The API lives under `/api/lab/v1` on the web admin's port.
   - The CLI (`python3 -m naruto.lab`) uses only the standard library, so it runs from the host without the virtualenv, or inside the container.
   - Why in-process:
     - Lab requests join the existing queue, so replies stay first. This settles concern 4 of the history-digest plan, which noted the planned loop would bypass the queue.
     - Activation goes through `SettingsService`, so the running bot uses it immediately.
     - The owner can review runs and answer comparisons on a web page backed by the same data.
   - The CLI wraps every endpoint. Any agent with a shell can use it, and `curl` works too.
2. **Each attempt runs in its own sandbox:** a fresh in-memory SQLite database built from the scenario (about 5 ms to create and migrate). It runs through the production `AgentRunner`, `ContextBuilder`, tools, command routing and message recording. Nothing touches the bot's database or Telegram, and the sandbox gets no keeper, archiver, importer or real Telegram client.
3. **A focused experiment is labelled.**
   - A scenario that forces a skill (bypassing banter's routing) or replays a recorded prompt is marked "focused", never "end-to-end".
   - Mentions go to banter, as in production.
   - Commands go through the same routing function the Telegram handler uses (extracted from `SkillCommands`).
4. **Lab requests use the bot's model queue** at background priority, with the new task `lab`. The pause, limits, backlog caps and Queue page all apply. The lab runs at most `lab.parallel_attempts` attempts at once (default 1), so it can't flood the background backlog.
5. **Candidates are immutable overlays on a baseline snapshot.**
   - A run's baseline is a frozen copy of every global setting value.
   - A candidate is a set of key → value changes, with a parent (the baseline or another candidate), a hypothesis and a rationale.
   - Only tunable keys can change:
     - always: the persona, the rules, the skills' instructions and reasoning switches, and sampling parameters;
     - if the run's scope names them: context and agent-limit settings;
     - never: the endpoint, secrets or anything else.
   - Output templates are part of each skill's instructions in this codebase, so they are tuned there.
6. **The model is a run condition, not a candidate change.**
   - A run defaults to the configured endpoint and model.
   - It may name another model, either on the configured server or on a server you listed in Settings → Lab → Other model servers. The agent can't point the lab at a server you didn't list, which keeps inference local.
   - Activating a candidate from a run on another model switches the model and that configuration together.
   - Activations are listed by model, so going back to a model can re-apply the configuration tuned for it. You choose when; nothing switches automatically.
7. **Conditions are recorded and compared.** Each attempt records:
   - the endpoint;
   - the model ID the server reported;
   - a fingerprint of the code (a hash of the `naruto` package and prompts, so a restart with new code shows);
   - the schema version;
   - the scenario version;
   - the hash of the candidate's settings.

   Comparisons only pair attempts with matching conditions. Mismatches are reported as "not a clean comparison", and the run shows a drift warning.
8. **Scenarios are versioned and immutable once used.**
   - An edit creates a new version and needs a reason.
   - Scenarios are grouped into sets with a purpose: tuning, validation or regression.
   - Removing a scenario from a set, or changing an expectation or rubric, is logged with its reason.
   - Comparisons use attempts on the same version, so a weakened expectation can't silently improve a score.
9. **Outcomes have separate classes:**
   - `pass` / `fail`: deterministic checks;
   - `action_failed`: a needed tool failed;
   - `error`: model server, time-out, queue or crash;
   - `skipped`: unsupported feature or budget;
   - `cancelled`;
   - `unjudged`: no checks or judgments yet.

   Reports show coverage per class, so a candidate can't look better by having fewer cases evaluated.
10. **Judgments come in three kinds:**
    - deterministic checks, run by the server;
    - AI judgments, posted by the agent against a stated rubric criterion;
    - owner feedback.

    An AI judgment must quote evidence, and the server rejects quotes that don't appear in the attempt's answer or trace. AI style scores never override an owner's choice. The server doesn't run its own AI judge in this version.
11. **Comparisons are neutral by construction.**
    - The server randomly assigns which attempt is A and which is B, and keeps the mapping.
    - It renders the presentation text (the situation and the replies, verbatim), so the agent can relay it without rewording.
    - Revealing which candidate is which before the owner answers is allowed but logged.
    - While a comparison is pending, the run is "waiting for the owner", and new attempts are refused unless the call says what the owner asked for (logged).
12. **Activation is gated twice.**
    - The token needs the "may activate" permission, which you grant on the Lab page.
    - The run's activation policy must be `agent_may_activate`.

    You can always activate or revert from the Lab page yourself. Activation is one transaction (`SettingsService.set_many`) and is refused if any relevant live setting changed since the baseline.
13. **Everything stays in the bot's database**, outside the repository. The CLI warns when it writes a report inside the repository, as the evaluation CLI did.
14. **Nothing is deleted, and every configuration tested is kept as files you can read.**
    - Experiments never change the live settings or `naruto/prompts/*.md`. Those files are only the defaults, and the lab never writes them.
    - The live prompts change only on activation (stage 6), which keeps the previous values and can be reverted. The Settings page's history keeps every earlier value too.
    - The baseline and each candidate are kept whole in the database.
    - `lab export` writes a run as a folder, outside the repository by default (`~/naruto-lab/`). It holds each configuration's complete prompt files, what changed and why, and the replies grouped by scenario. The prompts can then be read and compared side by side with the replies they produced (layout in stage 3).
    - Owner's request, 2026-10-01.
15. **The skill ships in both discovery locations:**
    - `.agents/skills/naruto-lab/SKILL.md` for Codex, which scans the repository's `.agents/skills`;
    - `.claude/skills/naruto-lab/SKILL.md` for Claude Code.

    Both use the same `SKILL.md` format (frontmatter with `name` and `description`). The two files are identical copies, and a test keeps them in sync, so neither depends on symlink support.

## Interface overview

All endpoints are JSON under `/api/lab/v1` with `Authorization: Bearer <token>`. The CLI reads `NARUTO_LAB_URL` (default `http://127.0.0.1:8765`) and `NARUTO_LAB_TOKEN`, or `--token-file`, so the token never appears in the process list. It prints JSON by default and readable text with `--text`.

| Purpose (feature plan §5) | Endpoint | CLI |
| --- | --- | --- |
| Discover capabilities and settings | `GET /capabilities`, `GET /config/active` | `lab capabilities`, `lab config` |
| Start, inspect, stop and finish a run | `POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/stop`, `POST /runs/{id}/finish` | `lab run start --file`, `lab run show`, `lab run stop`, `lab run finish` |
| Candidates | `POST /runs/{id}/candidates`, `GET /candidates/{id}` (diff against the baseline) | `lab candidate add --file`, `lab candidate show` |
| Scenarios and sets | `POST /scenarios`, `POST /scenarios/{id}/versions`, `POST /scenarios/from-attempt`, `POST /scenarios/from-agent-run`, `POST /sets` | `lab scenario add/edit/save-attempt/from-run`, `lab set add` |
| Single requests and conversations | `POST /runs/{id}/attempts` (scenario or inline, candidate, repeats, `continue_from`, `wait`) | `lab try` |
| Suites | `POST /runs/{id}/batches` (set × candidates × repeats) | `lab suite` |
| Progress and cancel | `GET /batches/{id}`, `POST /batches/{id}/cancel`, `POST /batches/{id}/resume` | `lab wait`, `lab cancel`, `lab resume` |
| Evidence | `GET /attempts/{id}` (turns, prompts, steps, state changes, checks, judgments, conditions) | `lab attempt show [--prompt]` |
| Judgments and notes | `POST /attempts/{id}/judgments`, `PUT /runs/{id}/rubric`, `POST /runs/{id}/notes` | `lab judge`, `lab rubric set`, `lab note` |
| Compare and report | `GET /runs/{id}/compare`, `GET /runs/{id}/report?format=json\|md` | `lab compare`, `lab report --out` |
| Preference rounds | `POST /runs/{id}/comparisons`, `GET /comparisons/{id}`, `POST /comparisons/{id}/answer`, `.../correct`, `.../withdraw`, `GET/PUT /runs/{id}/preferences` | `lab ask`, `lab answer`, `lab prefs` |
| Activate and revert | `POST /candidates/{id}/activate`, `GET /activations`, `POST /activations/{id}/revert` | `lab activate`, `lab revert` |
| Configurations and replies as files | (the endpoints above) | `lab export --run N [--out DIR]`, `lab candidate add --from-dir DIR` |

Errors are JSON `{"error": code, "message": ..., "details": ...}` with fitting statuses: 400 invalid, 403 scope, 404, 409 (waiting for the owner, budget, conflict).

## Stage 1 — Sandbox engine (replaces `naruto/evaluation`)

**Test clock (feature plan §6: known test time)**
- `Database` gets a `clock` (default `time.time`) and `now()`. Repositories switch from `now_ts()` to `self.db.now()`; the module-level `now_ts()` stays for code without a database.
- `Services.clock` is the database's clock. `services.now()` returns an aware datetime in the configured time zone.
- These read `services.now()` instead of the wall clock:
  - `ContextBuilder.build` (default `now`, and the open-plans cutoff in `_shared_state`);
  - `tools/lookup.parse_day`;
  - `tools/skills.parse_since`;
  - `tools/reminders.set_reminder` and `parse_when`;
  - the header time in `tg/board.py`.

**Production paths made reusable** (no behaviour change)
- `tg/skill_commands.py`: a pure `command_request(command, args, *, replied_to, sender_last_message, tz, now) -> CommandRequest(skill, note, since, ephemeral)` holds what `summary`, `plan`, `questions`, `remember`, `remind` and `catchup` now compute inline (the summary scope, notes and the catch-up window). `SkillCommands` and the sandbox both call it. The `/catchup` trigger construction moves into a helper too.
- `tg/reminders.py`: `reminder_text(reminder)` (`"⏰ Reminder: …"`), so delivery between turns stores the same message.
- `llm.py`: `LLMClient(..., queue=None)` can share another client's queue. `RequestInfo` gets `lab_attempt_id`, and the queue knows task `lab`.

**New: `naruto/lab/scenario.py`** (absorbs `evaluation/cases.py`)
- The old case fields stay valid (`messages`, `trigger`, `expect`, `members`, `chat`, `timezone`, `settings`, `skill`).
- New fields:
  - `origin`: `synthetic`, `owner` or `history`, with `generated_by`;
  - `time`: the test clock at the first turn, defaulting to the last message's date plus one minute;
  - `turns`: follow-ups (below);
  - `state`: initial state, such as the digest with its update time, notes, the board, reminders, open plans and history summaries;
  - `simulate`: tool outcomes, e.g. `{"pin_message": "rights_error", "create_poll": "error"}`;
  - `requires`: features the scenario needs, such as `vision` or a tool name;
  - `rubric`: the criteria that apply.
- A turn has `from`/`from_id`/`text` or `command` (e.g. `"/summary today"`), plus:
  - optional `after` (`"5m"`, `"2h"`, or an ISO time);
  - optional `messages` (chat between turns);
  - optional `image`;
  - its own `expect`.
- `trigger` + `expect` is shorthand for one turn.
- A forced `skill` marks the scenario as focused.
- Validation errors name the scenario and field, as `CaseError` does now.

**New: `naruto/lab/sandbox.py`** (absorbs `evaluation/runner.py`)
- `Sandbox.create(scenario, settings_values, *, llm_factory)` does the following:
  - opens an in-memory database with the scenario clock;
  - applies the candidate's effective settings, then the scenario's `settings`;
  - loads the chat, members and messages (in order, so row IDs are the same every time);
  - seeds the state with the repositories' own write methods (notes attach to people as in production).
- `SandboxTelegram` replaces `RecordingTelegram`:
  - It returns real python-telegram-bot `Message` objects, as `tests/fakes.FakeBot` does. Sends then go through the production recorder (`to_new_message`) and are stored exactly as live.
  - It applies `simulate` outcomes as the matching `TelegramError` types, so pin-rights handling and fallbacks run their production paths.
  - For `get_file` it serves images from scenario files only. Otherwise `describe_image` reports "unsupported in the sandbox".
- `Sandbox.services.llm` is a `LabLLM`:
  - an `LLMClient` on the sandbox's settings (so the candidate's sampling applies) that shares the bot's queue;
  - it always submits at background priority with `RequestInfo(task="lab", lab_attempt_id=…, still_wanted=attempt_not_cancelled)`;
  - `describe_image`, which calls `services.llm`, goes the same way.
- `run_conversation(sandbox, scenario) -> AttemptResult`. For each turn:
  1. Advance the clock.
  2. Insert the messages in between.
  3. Deliver reminders that came due, using `reminder_text` and marking them sent.
  4. Insert the trigger (or build the ephemeral `/catchup` trigger).
  5. Check `is_trigger` and record "would not answer in production" if it fails.
  6. Route through `command_request` or banter, then run `AgentRunner` (no streaming, as in production).
  7. Store the answer as the bot's message (threaded when `[REPLY]`) through the recorder.
  8. Snapshot the state change: board, reminders, notes, plans, polls, pins.
- Each turn keeps the run trace (prompt, steps, model requests, tool calls, reasoning text as returned, usage, latency), the final user-visible text, the threaded, fallback and `[NO REPLY]` flags, the state change, and the simulated actions.
- `Sandbox.save(path)` and `Sandbox.restore(path)` use SQLite's backup API, so a conversation can continue from an attempt's final state under any candidate.

**New: `naruto/lab/checks.py`** (absorbs `evaluation/checks.py`)
- Existing checks: `contains_any`, `contains_all`, `not_contains`, `regex`, `min_chars`, `max_chars`, `reply_threaded`, `tool_calls`.
- New checks:
  - `forbidden_tools`;
  - `no_reply` (expects or forbids `[NO REPLY]`);
  - `max_model_requests`;
  - `state`, the sandbox after the turn. Examples: `{"reminders": [{"text": "grill", "due": "2026-10-04 17:00"}]}`, `{"board": {"plans": ["BBQ"]}}`, `{"notes": ["vegetarian"]}`, `{"polls": [{"options": "Saturday"}]}`, `{"pins": 1}`.
- `judge` (`good` / `bad`, with `manual` as an alias for `good`) describes what judges should look for.
- Outcome classification (decision 9). A check that depends on something the sandbox doesn't support is `skipped`, never `pass`. Examples: a `requires: ["vision"]` turn when the server refused images; a tool that doesn't exist.

**Removed:** `naruto/evaluation/` and the README's "Evaluating models" section. `tests/test_evaluation.py` becomes `tests/test_lab_sandbox.py`, keeping the fake streaming client and the six example cases (moved to `tests/fixtures/lab/scenarios.json`), and `BOT_REWORK_PLAN.md` §11 gets a note.

## Stage 2 — Lab storage, runs, candidates and the executor

**Migration 12**

| Table | Holds |
| --- | --- |
| `lab_tokens` | id, name, token hash (SHA-256), allowed chat IDs (JSON), may_activate, created_at, last_used_at, revoked_at |
| `lab_runs` | id, objective, protected (JSON), scope (keys, skills), budget, interactive (on, comparisons at a time), activation policy, evaluator (name, external), data access (chat IDs), model condition (endpoint, model), baseline (JSON of every setting), baseline settings-history high-water mark, code fingerprint, status, stop reason, recommendation, agent summary, created_by token, timestamps |
| `lab_candidates` | id, run_id, parent_id, name, changes (JSON), hypothesis, rationale, settings hash, created_at |
| `lab_scenarios` | id, slug, version, previous_id, body (JSON), origin, source chat ID (history only), focused, edit reason, created_at |
| `lab_sets`, `lab_set_items` | id, run_id (or global), name, purpose; items with added and removed timestamps and reasons |
| `lab_batches` | id, run_id, spec (JSON), status (`queued`, `running`, `done`, `cancelled`, `interrupted`, `budget_exhausted`), counts, timestamps |
| `lab_attempts` | id, run_id, batch_id, scenario_id, candidate_id (null is the baseline), repeat, continue_from, status, outcome, turns (JSON trace), checks, conditions (JSON), model requests, model time, queue wait, usage, state file, first_viewed_at, timestamps |
| `lab_judgments` | id, attempt_id, turn, kind (`ai`, `owner`), criterion, verdict (`pass`, `fail`, `score`), score, evidence (JSON quotes), judge, rubric version, created_at |
| `lab_rubrics` | run_id, version, criteria (JSON), status (`proposed`, `confirmed`), change reason |
| `lab_comparisons`, `lab_choices` | the label mapping, scenario, attempts, presentation, status (`pending`, `answered`, `withdrawn`), revealed_before_answer; choices with choice, comment, channel, supersedes |
| `lab_preferences` | run_id, version, owner statements and interpretations (JSON), edited_by |
| `lab_activations` | id, candidate_id, model, mode (`changes`, `full`), previous and new values, settings-history IDs, evidence (report snapshot), authorized_by, token, reverted_at, reverted_by |
| `lab_events` | an audit log per run: scope changes, budget hits, reveals, refused calls, scenario and rubric edits, drift |

Also `model_requests.lab_attempt_id`. New repository: `naruto/db/lab.py`.

**New: `naruto/lab/config.py`**
- `TUNABLE` (always) and `EXTENDED` (only when in the run's scope) key patterns. `PER_CHAT` keys are noted, and the run reports which chats override them (activation changes global values only).
- `snapshot(settings)`: every global value.
- `effective(baseline, candidate chain)`.
- `validate_changes()`: through the registry's `Setting.validate`, so ranges and choices are the same as in the web admin.
- Text diffs (`difflib.unified_diff`) for prompt changes.
- `code_fingerprint()`, computed once at start.
- Endpoint userinfo is redacted everywhere.

**New: `naruto/lab/executor.py`** (`services.lab`, started from `main.py`)
- Batches expand into attempts. A semaphore of `lab.parallel_attempts` runs them in order, interleaving candidates per scenario so cache effects even out.
- **Budget:** before starting an attempt it reserves `agent.max_model_requests × turns` from the run's remaining model requests, then settles the actual use. Attempts, model requests and the deadline are checked before each start. When the budget runs out the batch stops, and its remaining attempts are `skipped` (budget).
- **Cancel:**
  - attempts still waiting are marked `cancelled`;
  - a running attempt's queued model request is withdrawn through `still_wanted` and becomes `cancelled`;
  - a request already at the model finishes but is ignored.
- **Restart:** `recover()` marks running batches and attempts `interrupted`. `resume` re-queues the attempts that didn't finish, under the same budget.
- **Drift:** after each attempt the conditions are compared with the run's. A different model ID or code fingerprint puts a warning on the run, which the report shows.
- Shutdown cancels running attempts, as `ImportService.shutdown` does.

**Settings (new section "Lab")**

| Key | Default | Notes |
| --- | --- | --- |
| `lab.parallel_attempts` | 1 | 1–4 |
| `lab.model_servers` | `{}` | JSON: label → endpoint; other local servers a run may target |
| `lab.max_attempts_per_run` | 500 | ceiling for any run's budget |
| `lab.default_budget` | `{"attempts": 60, "model_requests": 300, "hours": 4}` | when a run gives none |
| `retention.lab_days` | 90 | finished runs, their attempts and saved states; activation records are kept |

## Stage 3 — HTTP API, tokens and the CLI

**`naruto/web/lab_api.py`**
- A router under `/api/lab/v1` with the `require_lab_token` dependency:
  - a bearer token compared by hash (`hmac.compare_digest`);
  - no session, so no CSRF;
  - revoked tokens are refused;
  - `last_used_at` is updated on each use.
- Chat scope is enforced wherever real data could come out: scenarios from agent runs, and attempts or reports on history scenarios.
- `POST /runs` captures the baseline and the model condition.
  - The endpoint must be the configured one or listed in `lab.model_servers`.
  - It refuses `agent_may_activate` unless the token may activate.
- `GET /capabilities` reports:
  - each tunable setting (type, range, choices, default, current value, per-chat overrides);
  - skills with their tool subsets;
  - every tool with its sandbox support (simulated, needs a scenario image, or unsupported);
  - the commands;
  - the check types;
  - the scenario schema with an example;
  - budget limits and model servers;
  - the code fingerprint and schema version;
  - known deferred features: web search, digest upkeep and the rest of concern 10.
- `POST /runs/{id}/attempts` with `wait` (≤ 300 s) returns finished attempts inline. It is meant for interactive use.

**`naruto/lab/client.py` + `__main__.py`**
- Standard library only (`urllib`, `json`, `argparse`), one subcommand per endpoint (see the interface table).
- `--file -` reads JSON from stdin.
- `lab wait` polls with backoff.
- `lab report --out DIR` writes `report.md` and `report.json` and warns inside the repository.
- Exit codes: 0 ok, 2 invalid input, 3 refused (409/403), 4 server unreachable.

**The run folder (`lab export`)**
- `lab export --run N` writes or refreshes `~/naruto-lab/run-N-<slug>/` (or `--out DIR`), warning when that is inside the repository. Each run of the command rewrites the folder from the database, so it always matches the run.
- Prompt settings become files named like the defaults in `naruto/prompts/`:
  - `persona.prompt` → `persona.md`;
  - `prompt.rules` → `rules.md`;
  - `skills.<name>.instructions` → `<name>.md`;
  - every other tunable value (reasoning switches, sampling) goes into `settings.json`.
- Every configuration folder holds the complete set, not only what changed, so any candidate can be read on its own.

  ```text
  run-3-cheekier-banter/
    README.md                  objective, scope, status, budget used; links to everything below
    baseline/                  the live configuration when the run started
      persona.md  rules.md  banter.md  summarize.md  catchup.md  plan.md
      questions.md  decide.md  remind.md  remember.md  settings.json
    candidates/
      c1-cheekier/
        (the same files, with the candidate's changes)
        CHANGES.md             hypothesis, rationale, and a diff against its parent and the baseline
      c2-cheekier-shorter/
    replies/
      banter-teasing-1.md      the scenario, then each configuration's replies under a heading
                               naming it and what it changed, with the outcome, checks and
                               judgments; "(A/B, not yet answered)" until the owner answers
    comparisons.md             each A/B round: the replies, the owner's choice and comment,
                               and which configuration each label was
    preferences.md             the current preference summary
    report.md                  the stage 4 report
  ```

  While a comparison is still pending, `replies/` and `comparisons.md` don't say which configuration is A and which is B. The owner can then open the folder at any time without spoiling a choice.
- `lab candidate add --run N --name X --from-dir DIR [--parent c1]` reads a folder in this layout and records only the files that differ from the parent as the candidate's changes. A candidate can be made by copying `baseline/` (or another candidate's folder) and editing it, by the agent or by the owner. Unknown files, and keys outside the run's scope, are refused.
- The skill tells the agent to re-export after every batch and every answered comparison.

**Web admin:** a **Lab → API tokens** card (create, shown once; chat access; may-activate; revoke), plus "Lab" in the navigation. The full Lab pages come in stage 5.

## Stage 4 — Evaluation, comparison and reports (feature plan §8–§11)

- **Judgments:** `POST /attempts/{id}/judgments` checks that the criterion exists in the run's current rubric and that every evidence quote occurs in the turn's answer or trace (after normalizing whitespace).
- **Rubric:** a rubric is `proposed` until the owner confirms it; the agent records the confirmation with the owner's words. A rubric change bumps its version. Judgments made under an older version are shown as stale, and the report asks for the baseline and candidates to be judged again.
- **Scenario integrity:** editing a scenario used in the run, or removing it from a set, records the reason and marks the affected comparisons "needs a re-run under the new version".
- **Saving a discovered failure:** `POST /scenarios/from-attempt {attempt_id, turn, expect, reason}` freezes everything up to that turn as a new regression scenario. That is the messages, the bot's earlier replies and the state, taken from the attempt's saved state, with the failing turn as the trigger.
- **History scenarios:** `POST /scenarios/from-agent-run {agent_run_id, mode}` works for runs still within `retention.agent_runs_days`, and the token must be allowed the chat. There are two modes:
  - `snapshot`: the stored messages before the trigger, with their IDs (exact), plus the chat's current notes, digest and board. These are marked "as of now, not as of the original run", and the scenario's provenance says which fields are exact.
  - `replay`: the recorded prompt itself, with only the system message rebuilt from the candidate's persona, rules and skill instructions. This is a focused experiment, and its tool calls run against a sandbox seeded from `snapshot`.
- **Compare** (`naruto/lab/report.py`):
  - per scenario × candidate: outcome counts and k-of-n passes, with flaky cases flagged when 0 < k < n;
  - check failures by type;
  - regressions (baseline passed, candidate didn't) and improvements, with evidence links;
  - coverage per outcome class;
  - per-skill coverage (were skills beyond the target run?);
  - model time and queue wait (median and p90; cold and warm when usage shows cached tokens);
  - prompt and output tokens where reported;
  - judgments grouped by kind and never merged into one score;
  - condition mismatches.
- **Validation:** a validation set is reported separately from the tuning sets. When more than two candidates were chosen between on it, or a validation scenario moved to tuning, the report says it no longer gives independent evidence (from attempt `first_viewed_at` and the events log).
- **Report** (`format=json|md`, feature plan §12):
  - the objective, baseline and conditions;
  - candidates with exact diffs and rationale;
  - the selected candidate;
  - results, representative replies and regressions;
  - uncertainty (variance, small samples, drift);
  - interactive rounds (stage 5);
  - coverage and skipped cases, with the list of simulated actions and what still needs a live check;
  - resources used against the budget;
  - the stop reason and recommendation;
  - the agent's notes (defects, observations, assumptions), kept separate.
  - An unfinished or stopped run says that its candidates were not validated.

## Stage 5 — Interactive preference rounds and the Lab pages (feature plan §9)

- `POST /runs/{id}/comparisons {scenario_id, attempt_ids, turn}` checks that the attempts used the same scenario version and matching conditions. The server assigns labels with `secrets.SystemRandom` and stores the mapping. The presentation text it returns is the situation (members, the chat shown to the bot, the request) and the replies under "A" and "B", verbatim.
- At most `interactive.comparisons_at_a_time` (default 1) can be pending. While any is pending, the run's status is `waiting_for_owner`. New attempts or batches get 409 unless the call passes `owner_request` (what the owner asked for, logged).
- `POST /comparisons/{id}/answer` takes:
  - `choice`: `A`, `B`, `both_good`, `both_bad`, `no_preference`, `skip` or `combination`;
  - an optional comment;
  - `channel`: `agent:<name>` or `web admin`.

  It keeps the choice with the exact replies and candidates. `correct` adds a superseding choice and keeps the old one, and `withdraw` closes the comparison without a choice.
- `GET /comparisons/{id}?reveal=true` returns the mapping. Before an answer, this is logged as revealed before the choice.
- `PUT /runs/{id}/preferences` stores a versioned summary:
  - owner statements: quotes, each linked to a comparison;
  - interpretations: `assumption`, `confirmed` or `corrected`, with evidence;
  - context-dependent notes (e.g. "cheeky in banter, not in serious moments").

  Edits from the Lab page are recorded as the owner's.
- Choices are tuning evidence. The report keeps "the owner preferred this reply" apart from "this configuration produced the preferred tone in N fresh situations".
- **Web admin** (`naruto/web/lab_pages.py`; templates `lab.html`, `lab_run.html`, `lab_attempt.html`, `_lab_comparison.html`):
  - the runs list (status, budget, waiting for the owner);
  - the run page: objective, scope, baseline and candidate diffs, the attempt matrix, pending comparisons with answer buttons and a comment box, the preference summary (editable), and the rendered report;
  - the attempt page, reusing `run_detail.html`'s step rendering.

  The dashboard shows "Lab: waiting for your choice" when a comparison is pending. The Queue page links `lab` requests to their attempt.

## Stage 6 — Activation and rollback (feature plan §12)

- `SettingsService.set_many(values, *, actor)` validates everything first, then writes all keys in one transaction with one history row per key, reloads once and notifies listeners per key.
- `POST /candidates/{id}/activate {authorized_by, mode}`:
  - **Gate:** the token may activate and the run's policy is `agent_may_activate`, or the request comes from the Lab page.
  - **Conflicts:** for every key the candidate changes, the live value must equal the baseline value. A difference is a conflict (409, listing the key, baseline, live value, who changed it and when).
  - **Drift:** a relevant key the candidate doesn't change but that changed live since the baseline (for example the rules) means the evaluation used older settings. The response says so, and activation needs `acknowledge_drift` with a reason, which is recorded.
  - **Mode** `changes` (default) applies the candidate's changes. `full` applies the candidate's whole effective configuration, for returning to a model and its tuned configuration; it shows the diff against the live values.
  - **Model:** a run on another model also sets `model.endpoint_url` and `model.name`.
  - **Per-chat:** the response warns about chats whose overrides hide the change.
  - The actor in the settings history reads "lab run N candidate M (authorized by …)". The activation row stores the previous and new values and a snapshot of the report.
- `POST /activations/{id}/revert`:
  - It restores the previous values if the live values still equal what was activated.
  - Otherwise it answers 409 with what changed since. You can then revert single settings on the Settings page.
  - Reverting doesn't undo anything the bot already did in chats.
- The Settings page shows a notice on keys whose current value came from a lab activation, with a link. The Lab page lists activations by model with their state.

## Stage 7 — Instructions, the skill and the walkthrough (feature plan §14)

- **`docs/LAB.md`** for the owner and the agent, linked from the README (replacing "Evaluating models"). It covers the ten required points with commands that were actually run, a reusable agent task template (objective, protected behaviours, allowed changes, budget, evaluation requirements, activation policy) and how to adapt it to agents without skills.
- **`.agents/skills/naruto-lab/SKILL.md`** and the identical **`.claude/skills/naruto-lab/SKILL.md`** cover:
  - prerequisites;
  - discovering capabilities;
  - agreeing on scope, budget and rubric with the owner, and asking only for what's missing;
  - the baseline;
  - generating varied synthetic situations;
  - candidates with one hypothesis each;
  - the interactive protocol: relay the server's presentation verbatim, never write or polish replies, never infer from silence, wait, record the choice and the owner's words;
  - refining;
  - regression and validation;
  - stopping conditions;
  - reports;
  - the activation policy;
  - the data rules.

  Example requests: "Help me choose Naruto's tone" and "Test a cheekier version against the current prompt, then let me choose the replies I prefer". The skill also states the compatibility limits: it needs a shell and network access to localhost, and other agents can be given `docs/LAB.md` and the task template.
- **The walkthrough in `docs/LAB.md`** uses synthetic data:
  - baseline failure → candidate → retest → regression comparison → recommendation;
  - and an interactive round: generated situation → real replies → recorded preference → revised candidate → fresh comparison.

  Any owner choices in it are labelled illustrative.
- **Verification** happens in three layers:
  1. `tests/test_lab_walkthrough.py` runs every documented CLI command against the app (`TestClient` behind the CLI's transport) and a scripted fake model.
  2. The same commands run against the real process with a fake OpenAI-compatible server.
  3. A real run on Halogen with the synthetic scenarios.

  The doc ends with what each layer verified and what it couldn't.
- `AGENTS.md` is not added. The README link and the skill folders are how agents find it; add one if Codex users want a repository-wide pointer.

## Reused code

- From `naruto/evaluation`: `load_case`, the recording stand-in, the checks and the case format; it then moves to `naruto/lab` and is deleted.
- `AgentRunner`, `ContextBuilder`, `ToolRegistry`, the skills and `claims.py`, unchanged.
- The recorder's `to_new_message`, and `tests/fakes.FakeBot`'s real `Message` objects.
- `SettingsService` (validation, history, `on_change`) and `Setting.validate`.
- `ModelQueue` and `RequestInfo`; `model_requests` for queue history.
- The agent-run trace format and the `run_detail.html` rendering.
- The HTMX partial refresh (`_queue_live.html`) and two-step confirmations (`confirm.html`).
- The pattern of marking unfinished work interrupted on restart (`model_requests.interrupt_open`, `ImportService.recover`).
- `_warn_if_in_repo` from the evaluation CLI.

## Verification

- **New tests:**
  - `tests/test_lab_sandbox.py`:
    - the six migrated cases;
    - scenario parsing with turns, state, `simulate` and `requires`;
    - a fixed clock gives byte-identical prompts across attempts;
    - follow-up turns see the earlier bot reply and tool state;
    - reminder delivery between turns;
    - commands give the same skill, note and since as `SkillCommands`;
    - `/catchup`'s ephemeral trigger;
    - simulated pin-rights and poll errors;
    - `describe_image` with and without a scenario image;
    - unsupported features become `skipped`;
    - continuing from a saved state;
    - nothing is written to the bot's database or sent to Telegram (a tripwire on the real `Services`).
  - `tests/test_lab_executor.py`:
    - lab requests at background priority, after replies;
    - pause holds them;
    - budget reservation and exhaustion, with the remaining attempts skipped;
    - cancel while queued and while running;
    - restart → interrupted → resume;
    - drift detection on a changed model ID or fingerprint;
    - candidate validation (ranges, keys outside the scope, unknown keys).
  - `tests/test_lab_api.py`:
    - token auth, revocation and chat scope;
    - the capabilities shape;
    - each endpoint's happy path and refusal;
    - 409 while waiting for the owner, and `owner_request`;
    - evidence quotes that must exist;
    - rubric and scenario versioning;
    - outcome-class coverage in compare;
    - validation-exposure flags;
    - report Markdown snapshots;
    - activation gates, conflict, drift acknowledgement, `full` mode, model switching, per-chat warnings, and revert with and without later edits;
    - `set_many` is atomic.
  - `tests/test_lab_cli.py`:
    - arguments, `--file -`, exit codes, the in-repo warning, the token from file or environment;
    - `export` writes complete configuration folders, `CHANGES.md` diffs and replies grouped by scenario;
    - A/B mappings stay hidden in the export while a comparison is pending;
    - exporting twice gives the same files;
    - `candidate add --from-dir` records only the changed files and refuses unknown files and keys outside the scope.
  - `tests/test_lab_walkthrough.py`: see stage 7.
  - The skill copies are identical, and every CLI command named in the skill and in `docs/LAB.md` exists in the CLI's parser.
  - A version-11 database migrates cleanly.
- **Updated tests:** `tests/test_llm.py` (shared queue, `lab` task), `tests/test_settings.py` (`set_many`), `tests/test_tg.py` (commands through `command_request`), `tests/test_context.py` (the services clock).
- **Run:** `.venv/bin/python -m pytest -q` after each stage. Today 479 tests pass. `test_web.py::test_import_upload_preview_and_run` failed once and then passed in later runs; it looks flaky and is unrelated to this plan.
- **Manual end to end:**
  - Start the bot against a test bot token and Halogen.
  - Create a token, then run the walkthrough with the CLI, watching `/queue` and the Lab pages.
  - Send a message in a test group while a suite runs: the reply should go first.
  - Restart mid-batch, then resume.
  - Activate and revert a harmless candidate (e.g. `skills.decide.reasoning`).
  - Then a real preference round with the owner.

## Delivery

Work on a new branch `self_learning_loop`, created from `history_digests` (it needs the model queue), with 2–4 commits per stage and no pushing. The order is stages 1 → 7. After stage 3 an agent can already run experiments through the CLI; stages 4–6 add the evaluation, the owner's rounds and activation; stage 7 delivers the required instructions and skill. Every stage leaves the bot working.

Coverage deferred until the rework features exist, to be listed in `docs/LAB.md` as each arrives:
- web search (a simulated `web_search` with scenario-provided results);
- simulated streaming, if it is ever built;
- evaluating background tasks (digest, history summaries, distillation) under a candidate's model parameters.

## Implementation notes (2026-10-02)

The work was built on branch `self_learning_loop`, created from `history_digests`, with one or two commits per stage. 552 tests pass: 478 on `history_digests`' last commit, minus the 21 evaluation tests (their cases moved into the lab's), plus 95 lab tests.

The walkthrough in `docs/LAB.md` §14 was checked twice:
- `tests/test_lab_walkthrough.py` runs it as written.
- It also ran over real HTTP: uvicorn serving the web admin, the system `python3` client, and the bot's own model client and queue, pointed at a fake OpenAI-compatible server. All 37 commands passed. The 20 model requests all went through the queue at background priority, as task `lab`, each linked to its attempt.

### Deviations

- **Budget:** there's no up-front reservation per attempt. An attempt starts while the run has room, and a running one may finish slightly past the limit, by its own requests. That's simpler, and the manual says so.
- **Scenarios from real chats:**
  - Only the "snapshot" mode was built: exact messages, and today's notes, digest and board, marked approximate. The "replay the recorded prompt" mode wasn't built.
  - Kept failures (`scenario save-attempt`) are exact instead: the attempt is replayed with the model's recorded answers, and a test checks that the new scenario's prompt is identical to the one that failed.
- **Scenario format additions:**
  - `continues` (turns that follow an earlier attempt, run with `continue_from`);
  - inline images as `data:` URIs (the API can't read files on the agent's machine; the CLI turns local image paths into data URIs);
  - poll messages;
  - `"answers": false` for turns that test the bot staying quiet.
- **Comparisons** take 2 to 4 replies (A to D). The owner can also answer on the run's Lab page, and the dashboard says when a run waits for a choice.
- **The Settings page** has no separate notice for keys set by an activation. The settings history entry names it instead: "lab run N cM (authorized by …)".
- **Cold and warm latency:** the report splits each configuration's first try from its later tries. It doesn't use cached-token counts, which the servers report inconsistently.
- **Real-chat scenarios** are deleted on the Lab page, together with the attempts that ran them, whose prompts hold the same messages. The chat page's data deletion doesn't remove them, and a group upgrade doesn't move them: their `chat_id` records where they came from.
- **Code moved out of `LabService`:**
  - `LabError` lives in `naruto/lab/errors.py`;
  - the "not simulated" lists live in `naruto/lab/sandbox.py`;
  - both moves avoid import cycles.
- **Found while writing the walkthrough:** a reply that says "I'll remind you…" with no reminder set makes the bot ask the model again. That is production behaviour (`agent/claims.py`), and real models can trigger it in lab attempts too; it shows as "Asked again" in the steps.

### Still to check on the real setup

- **Part A on the real model server** (Halogen or Gufo). Check:
  - the time per attempt;
  - that the Queue page shows `lab` requests behind replies;
  - the effect on a busy group's reply latency while a suite runs (prompt-cache eviction on a one-slot server).
- **The real model's replies** in a tone round, and whether 3 repeats are enough to separate configurations.
- **The skill:** that Claude Code (`.claude/skills/naruto-lab`) and Codex (`.agents/skills/naruto-lab`) find it when started in the repository, and follow the tone-round rules (verbatim presentation, waiting).
- **Docker:** running the client inside the container (`docker compose exec naruto-chat-bot python -m naruto.lab …`), and from the host through the published port.

