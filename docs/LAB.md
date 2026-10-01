# The prompt lab: tuning Naruto with an agent

The prompt lab is how the bot learns from evaluation. An external agent, such as Claude Code or Codex, runs experiments against the bot's configured model. It tries changes to the persona, the skills' instructions and the model parameters, compares the results and recommends what to keep. For tone, it shows you real replies side by side and lets you choose.

"Learning" here means changing the bot's configuration, never training the model. A run can end with "keep what we have".

Nothing in the lab changes the live bot until a candidate is **activated**. Experiments run in sandboxes: copies of a made-up (or saved) chat in memory, answered by the bot's real reply code on the configured model. Telegram actions in a sandbox are only recorded.

- [1. Before you start](#1-before-you-start)
- [2. How it fits together](#2-how-it-fits-together)
- [3. Starting a run](#3-starting-a-run)
- [4. Scenarios](#4-scenarios)
- [5. Running scenarios and reading the results](#5-running-scenarios-and-reading-the-results)
- [6. Candidates](#6-candidates)
- [7. Judging and comparing](#7-judging-and-comparing)
- [8. Choosing between replies (tone rounds)](#8-choosing-between-replies-tone-rounds)
- [9. Budgets, failures and stopping](#9-budgets-failures-and-stopping)
- [10. Finishing, activating and reverting](#10-finishing-activating-and-reverting)
- [11. Real chat data and live actions](#11-real-chat-data-and-live-actions)
- [12. The run folder](#12-the-run-folder)
- [13. Using the lab from an agent](#13-using-the-lab-from-an-agent)
- [14. Walkthrough with made-up data](#14-walkthrough-with-made-up-data)
- [15. What was verified](#15-what-was-verified)
- [Reference](#reference)

## 1. Before you start

1. **The bot is running** with the web admin on (`ADMIN_PASSWORD` in `.env`), and its model server is reachable (the dashboard says so). The lab uses the bot's own process, its queue and its configured model.
2. **Create a token** on the web admin's **Lab** page → *New token*. By default a token can only use made-up scenarios. Tick the chats whose real messages it may read (see [11](#11-real-chat-data-and-live-actions)), and *May activate candidates* only if you want the agent to be able to change the live bot (see [10](#10-finishing-activating-and-reverting)). The token is shown once.
3. **Give the token to the agent's shell**, in a file only you can read:

   ```sh
   mkdir -p ~/.config/naruto-lab && chmod 700 ~/.config/naruto-lab
   nano ~/.config/naruto-lab/token   # paste the token, save
   chmod 600 ~/.config/naruto-lab/token
   ```

   Or set `NARUTO_LAB_TOKEN`. If the web admin isn't on `http://127.0.0.1:8765`, set `NARUTO_LAB_URL`; for a remote box, open an SSH tunnel first (`ssh -L 8765:127.0.0.1:8765 your-server`).
4. **Run the client from a checkout of this repository**: `python3 -m naruto.lab …`. It needs only Python 3.10 or later, not the bot's virtualenv. Inside the Docker container it is `docker compose exec naruto-chat-bot python -m naruto.lab …`.
5. **See what this version supports:**

   ```sh
   python3 -m naruto.lab capabilities
   python3 -m naruto.lab config
   ```

   `capabilities` lists, among other things:
   - every tunable setting, with its type, range, current value, the file it's exported to, and the chats that override it;
   - the skills and their tools, and how each tool runs in a sandbox;
   - the commands, checks and the scenario format with an example;
   - the limits;
   - what isn't simulated, and what isn't built yet.

   `config` shows the live values of the tunable settings.

Exit codes: 0 ok, 1 server error, 2 invalid request, 3 refused (a permission, the budget, or the run is waiting for you), 4 the bot can't be reached. Errors are printed with the server's explanation.

## 2. How it fits together

| Word | Meaning |
| --- | --- |
| **Run** | One tuning effort: an objective, what to protect, which settings may change, a budget, and a fixed model. When it starts it freezes a copy of every setting: the **baseline**. |
| **Candidate** | A configuration under test: changes to some tunable settings, on top of the baseline or another candidate (`c1`, `c2`, …). Never edited; a revision is a new candidate. |
| **Scenario** | A chat and one or more turns that address the bot, with what a good answer does. Made up (`synthetic`), given by you (`owner`), or from a real conversation (`history`). Versioned: changing one needs a reason. |
| **Set** | Scenarios with a purpose: `tuning` (what you improve on), `validation` (held back, to check it generalizes), `regression` (what must not break). |
| **Attempt** | One scenario under one configuration, in a fresh sandbox. A **batch** is attempts started together. |
| **Outcome** | `pass`, `fail` (a check failed), `action_failed` (a needed tool failed), `error` (model server, time-out, queue: not the model's judgment), `skipped` (unsupported, or no budget), `cancelled`, `unjudged` (no checks: judge it). |
| **Judgment** | An AI's or your verdict on one turn against the run's **rubric**, kept apart from the automatic checks. |
| **Comparison** | An A/B choice for you between real replies to the same situation. |
| **Activation** | Putting a candidate's settings into the live bot. Reversible. |

**What runs for real in a sandbox:**
- the bot's recorder;
- the prompt builder;
- skills and hand-overs;
- the agent loop and its limits;
- every tool's logic;
- reminders coming due between turns;
- image handling, when the scenario has the image.

**What's simulated:** Telegram itself. Sends, polls, pins and the board are recorded, and a scenario can make them fail.

**What doesn't run:**
- digest upkeep and monthly history summaries (give the scenario a digest or history summaries instead);
- progress messages;
- button presses and poll votes;
- how Telegram renders text.

`capabilities` lists all of this. A sandbox pass is not proof that Telegram delivery or the production model server work. Check changes in a test group.

Lab requests wait in the bot's model queue behind replies to people (task `lab` on the Queue page; pausing background work pauses the lab). Each experiment has its own prompt, so on a one-slot server a busy group's next reply can be slower while experiments run: run large suites when the groups are quiet.

## 3. Starting a run

A run is described in JSON:

```json
{
  "objective": "In a busy chat, answer the current question without bringing up older topics.",
  "protected": ["Naruto's voice", "Short replies"],
  "scope": {"keys": ["skills.banter.instructions", "prompt.rules"], "skills": ["banter"]},
  "budget": {"attempts": 60, "model_requests": 300, "hours": 4},
  "interactive": {"enabled": false, "comparisons_at_a_time": 1},
  "activation": {"policy": "recommend"},
  "evaluator": {"name": "claude-code", "external": true},
  "data": {"chats": []},
  "model": {}
}
```

- **objective** (required): what should improve. If it's vague, the agent proposes a rubric first and you confirm it (see [7](#7-judging-and-comparing)).
- **protected**: what must not get worse. The agent keeps regression scenarios for these.
- **scope.keys**: which settings may change, as names or patterns (`skills.banter.*`). Without it, every always-tunable setting may: the persona, the rules, each skill's instructions and reasoning switch, and the sampling parameters. Context and agent limits are tunable only when named here. `capabilities` says which is which.
- **budget**: attempts, model requests and hours. The defaults and the ceiling are in Settings → Lab.
- **interactive**: tone rounds; see [8](#8-choosing-between-replies-tone-rounds).
- **activation.policy**: `recommend` (the run ends with a recommendation and you activate) or `agent_may_activate` (the agent may activate, if its token is allowed to).
- **evaluator**: who judges. `external: true` means an agent outside this machine reads the replies.
- **data.chats**: the real chats this run may draw on (the token must allow them).
- **model**: empty for the configured model. To tune for another model before switching to it, name a server you listed in Settings → Lab → *Other model servers*: `{"server": "gufo-27b", "name": "qwen3.8-27b"}`. A run tests one model; changing the model is a new run.

```sh
python3 -m naruto.lab run start --file run.json
python3 -m naruto.lab --text run show 1
```

## 4. Scenarios

A scenario is a small chat and the turns that address the bot:

```json
{
  "id": "busy-chat-bouldering",
  "origin": "synthetic",
  "category": "focus",
  "description": "A BBQ thread, then an unrelated question.",
  "timezone": "Asia/Singapore",
  "time": "2026-09-27T21:05:00+08:00",
  "chat": {"title": "BBQ crew", "type": "group"},
  "members": [{"id": 7, "name": "Alice", "username": "alice"}, {"id": 8, "name": "Bob"}],
  "state": {"notes": [{"text": "Bob is vegetarian", "about": 8}],
            "board": {"plans": ["BBQ Sat 6pm, East Coast"]}},
  "messages": [
    {"from": "Alice", "from_id": 7, "text": "BBQ on Saturday? East Coast again?"},
    {"from": "Bob", "from_id": 8, "text": "yes! 6pm"}
  ],
  "turns": [
    {"from": "Bob", "from_id": 8, "text": "@naruto_bot any tips for a first bouldering session?",
     "expect": {"not_contains": ["bbq", "grill"], "contains_any": ["warm", "chalk", "shoes"],
                "max_chars": 500,
                "judge": {"good": "Real tips for Bob.", "bad": "Brings up the BBQ."}}},
    {"after": "2m", "from": "Alice", "from_id": 7, "reply_to_answer": true,
     "text": "lol and what should I wear?", "expect": {"tool_calls": []}}
  ]
}
```

- **Messages:**
  - `from`, `from_id`, `username` and `text`;
  - `date`, otherwise a minute apart;
  - `reply_to`: an earlier `id`;
  - `bot: true` for the bot's own earlier lines;
  - `media`: `photo`, `sticker`, `animation`, `video`, `voice`, `document` or `poll`, with `image` (a file next to the scenario file, or a `data:` URI) for vision.
- **Turns:** a message that mentions the bot, replies to it (`reply_to` a bot message, or `reply_to_answer`: the bot's answer in the turn before), or a `command` such as `"/summary today"`. A turn can also have:
  - `after` (`"90s"`, `"5m"`, `"2h"`, `"1d"`, or a date);
  - chat `messages` before it;
  - its own `expect`.

  A turn that doesn't address the bot is refused, unless `"answers": false` tests that the bot stays quiet.
- **`time`** is the clock at the first turn. The prompt's "Now:" is the time of the turn being answered, so time questions mean the same thing every time.
- **`state`:** what the bot already keeps for the group, i.e. its `digest`, `notes`, `board`, pending `reminders`, open `plans` and `history_summaries`.
- **`expect`:**
  - on the answer: `contains_any`, `contains_all`, `not_contains`, `regex`, `min_chars`, `max_chars`;
  - on delivery: `answers`, `no_reply` (the bot sent nothing), `reply_threaded`;
  - on actions: `tool_calls` (tools that must be called, with argument fragments; `[]` means none), `forbidden_tools`, `skill` (the skill that answered, after any hand-over), `max_model_requests`;
  - on the result: `state` (what must exist afterwards: `reminders`, `board`, `notes`, `polls`, `plans`, `pins`);
  - for judges: `judge`, text or `{"good": …, "bad": …}`, which a judge reads but no check uses.
- **`simulate`:** make Telegram fail, e.g. `{"pin_chat_message": "rights_error"}`. The methods and failures are listed by `capabilities`.
- **`requires`:** a feature or tool the scenario needs, e.g. `["vision"]` or `["web_search"]`. If it's missing, the attempt is `skipped`, not passed.
- **`skill`** forces a skill instead of routing the request like the bot does. That makes the scenario a *focused experiment*, and reports say so.

Old evaluation case files (`messages`, `trigger`, `expect`) still load.

```sh
python3 -m naruto.lab scenario add --file scenarios.json        # one, a list, or {"scenarios": [...]}
python3 -m naruto.lab scenario list
python3 -m naruto.lab scenario add --file scenarios.json --reason "clearer expectation"   # a new version
```

**Keeping a discovered failure as a regression case.** Say an attempt went wrong at turn 2. This keeps the conversation exactly as it was before turn 2 (the earlier replies, reminders, board and notes), with turn 2 to answer:

```sh
python3 -m naruto.lab scenario save-attempt 12 --turn 2 --id follow-up-forgets-reminder
```

It does this by replaying the attempt with the model's recorded answers, so the new scenario's prompt is identical to the one that failed.

**A real conversation as a scenario** (the token must be allowed that chat). The run's ID is on the web admin's *Agent runs* page.

```sh
python3 -m naruto.lab scenario from-run 4321 --id summer-trip-question
```

The messages the bot read are exact, while retention keeps them. The notes, digest and board come from today, not from when the bot answered, and the scenario says so.

**Continuing a conversation under several configurations.** A scenario with `"continues": true` holds only the next turns, and its first turn may `reply_to_answer`. Run it with `--continue-from ATTEMPT` under each candidate, so every candidate answers the same conversation so far.

## 5. Running scenarios and reading the results

```sh
python3 -m naruto.lab --text try 1 --scenario busy-chat-bouldering --candidate baseline --candidate c1 --wait 600
python3 -m naruto.lab --text suite 1 --set tuning --candidate baseline --candidate c1 --repeat 3 --wait 1800
python3 -m naruto.lab --text wait 3            # a batch that's still running
python3 -m naruto.lab attempt show 5           # everything about one attempt
python3 -m naruto.lab attempt show 5 --prompts # plus the exact prompts and every step
```

- `try` runs named scenarios, and `suite` runs a set. Each runs every scenario under each configuration, `--repeat` times, interleaving configurations. Without `--wait` they return at once. Use `wait` or `batch` to check later.
- `attempt show` gives the following for each turn:
  - the trigger and the skill that answered (after any hand-over);
  - the answer as sent, and how (`group`, `ephemeral`, `usage`, `none`, threaded or not);
  - each check;
  - the tool calls with arguments and results;
  - the simulated Telegram actions;
  - what changed in the board, reminders, notes, plans, polls and pins;
  - the model's reasoning, when the server returns it;
  - model time and tokens.

  Its conditions are the endpoint, the model the server reported, a fingerprint of the bot's code, the scenario version and the settings hash.

The model varies from try to try: use `--repeat` 3 or more before believing a difference. "Model time" is the server's work; queue wait is reported separately. The first try of a configuration is often slower (cold cache).

## 6. Candidates

From a JSON object of changes:

```sh
echo '{"model.temperature": 0.5}' > cooler.json
python3 -m naruto.lab candidate add 1 --name cooler --file cooler.json --hypothesis "Less rambling"
```

Or from a folder, which is easier for prompts. `export` writes every configuration as complete files (see [12](#12-the-run-folder)): copy one, edit it, and register the copy. Only the files that differ count as changes.

```sh
python3 -m naruto.lab export 1
cp -r ~/naruto-lab/run-1-*/baseline ~/naruto-lab/work/c-new
$EDITOR ~/naruto-lab/work/c-new/banter.md
python3 -m naruto.lab candidate add 1 --name current-request-only --from-dir ~/naruto-lab/work/c-new \
    --hypothesis "An explicit rule stops replies to old topics" --rationale "attempts 1-2 brought up the BBQ"
python3 -m naruto.lab candidate show 1 c1      # what it changes, against its parent and the baseline
```

`--parent c1` builds on another candidate. Changes outside the run's scope, the model (a condition of the run, not a candidate), and invalid values are refused. Changing the sampling parameters also changes digest updates, history summaries, import memory and image descriptions; the candidate says so.

## 7. Judging and comparing

Automatic checks cover what has a clear answer. Tone, relevance and helpfulness need judgment, against a **rubric** you agree on:

```sh
python3 -m naruto.lab rubric set 1 --file rubric.json     # [{"id": "relevance", "description": "..."}]
python3 -m naruto.lab rubric set 1 --file rubric.json --status confirmed --confirmation "Owner: yes, that's it"
python3 -m naruto.lab judge 4 --criterion relevance --verdict pass --evidence "Warm up properly"
python3 -m naruto.lab judge 4 --criterion voice --verdict score --score 4 --evidence "believe it!"
```

- An **AI judgment** must quote its evidence from the turn's reply or trace; a quote that isn't there is refused. Your own judgments (`--kind owner`) need no quote. The two are kept apart, and apart from the checks, and AI scores never override your choices.
- Changing the rubric after judgments needs a reason; the earlier judgments then show as stale.
- **Compare** shows the following for each scenario and configuration:
  - k of n passes, with ⚠ where results vary;
  - which checks failed;
  - regressions and improvements against the baseline;
  - coverage by outcome (so fewer cases evaluated can't look like better results);
  - the skills that ran, model time, queue wait and tokens;
  - judgments by kind;
  - attempts that ran under other conditions (another model, or new code).

```sh
python3 -m naruto.lab compare 1
python3 -m naruto.lab compare 1 --set held-out --candidate baseline --candidate c2
python3 -m naruto.lab --text report 1 --format md
python3 -m naruto.lab report 1 --out ~/naruto-lab/reports/run-1
```

**Validation sets** say whether an improvement generalizes. The report says when one is no longer independent evidence:
- more than two candidates were chosen between on it;
- a candidate was made after its results were read;
- scenarios were taken out of it.

Agents should run it rarely, and read its details only at the end.

**Uncertain results.** A 2/3 against 3/3 difference is weak. Repeat more, add scenarios, or report it as uncertain. A candidate with an unresolved regression is never an unconditional improvement. A run may well conclude that nothing tried was better.

## 8. Choosing between replies (tone rounds)

For tone, your choices are the evidence. The loop goes like this:

1. **A situation.** The agent writes a plausible chat moment, marked `"origin": "synthetic"`. It varies the situations: banter, teasing, practical questions, planning, serious moments. You can also give or change one.
2. **Real replies.** The agent runs the same situation under two (up to four) configurations. The replies come from the bot; the agent never writes or polishes them.
3. **A neutral comparison.** `ask` creates it. The server decides at random which reply is A and which is B, and writes the presentation. The agent shows you that text exactly, without saying which is "improved".

   ```sh
   python3 -m naruto.lab --text ask 2 --attempt 13 --attempt 14
   ```

4. **Your choice.** Answer A, B, both good, both bad, no preference or skip, or describe a mix ("A's humour, B's brevity"). A comment helps but isn't needed. You can also answer on the web admin's run page (Lab → the run → *Your choices*), which records it as yours directly.

   ```sh
   python3 -m naruto.lab answer 1 --choice B --comment "B's cheek, but shorter"
   python3 -m naruto.lab correct 1 --choice combination --comment "A's warmth, B's cheek"
   python3 -m naruto.lab withdraw 2 --reason "the owner wants a different situation"
   ```

5. **Waiting.** While a comparison waits, the run waits. New experiments are refused unless the agent says what you asked for (`--owner-request "…"`, which is recorded). Nothing is inferred from silence. Comparisons survive restarts, so you can come back later.
6. **Refining.** The agent keeps a preference summary: your words (quotes) apart from its reading of them, each reading an *assumption* until you confirm it. It then tries a revised candidate on a **fresh** situation.

   ```sh
   python3 -m naruto.lab prefs set 2 --file prefs.json
   python3 -m naruto.lab prefs show 2
   ```

   You can correct the summary on the run's page.

`comparison show ID --reveal` shows which configuration gave which reply. Before you answer, that is allowed but logged. After you answer, it's always shown. Choosing a reply never activates anything. Preferences can depend on the situation: liking wild banter doesn't mean wanting it when someone is upset. The agent tests its reading on new kinds of situations before claiming a configuration matches your taste.

## 9. Budgets, failures and stopping

- **Budget.** An attempt starts only while the run has attempts, model requests and time left. A running attempt may finish slightly past the limit. When the budget runs out, the rest of the batch is `skipped` with the reason, and new batches are refused (exit 3).
- **Cancel.** `cancel BATCH` stops waiting and running attempts, which become `cancelled`. Partial results stay.
- **Restarts.** A bot restart marks running attempts `interrupted`. `resume BATCH` runs them again (within the budget).
- **Unsupported.** A scenario needing something this version lacks (`requires`, or an image it didn't include) is `skipped`, never `pass`. Errors from the model server, time-outs or a full queue are `error`, not `fail`.
- **Stop or finish.** `run stop 1 --reason cancelled` (or `blocked`, `budget_exhausted`, `no_improvement`) ends a run. A run that didn't finish normally says, in its report, that nothing in it was validated.

## 10. Finishing, activating and reverting

```sh
python3 -m naruto.lab run finish 1 --reason objective_met --recommend activate --candidate c1 \
    --why "Answers the current question in 6/6 tries (baseline 0/2); no regressions."
python3 -m naruto.lab activate 1 c1 --preview
```

`--preview` shows what would change:
- the exact diffs;
- **conflicts**: a setting the candidate changes was changed by someone else since the run started, so nothing is applied;
- **drift**: other settings the evaluation relied on changed, so the results may not hold;
- chats whose own settings hide the change;
- model settings shared with background work.

**Who activates.** With policy `recommend`, you do: Lab → the run → the candidate → *Activate…*. With `agent_may_activate` and a token that may activate, the agent can:

```sh
python3 -m naruto.lab activate 1 c1 --authorized-by "the owner, in the terminal: 'ship c1'"
python3 -m naruto.lab activate 1 c1 --authorized-by "the owner, in the terminal: 'ship c1'" --acknowledge-drift "the rules edit is unrelated"
```

Everything is applied in one transaction. The Settings page's history shows it as "lab run 1 c1-… (authorized by …)".

**Undoing it:**

```sh
python3 -m naruto.lab activations
python3 -m naruto.lab revert 1
```

A revert is refused if those settings changed again since; then revert them one by one on the Settings page. Reverting doesn't undo anything the bot already said.

**Per model.** A run on another model also switches `model.endpoint_url` and `model.name` when activated. To go back to a model with the configuration tuned for it, activate that run's candidate (or its baseline) with `--mode full`. The Lab page lists activations by model, with a link for this.

## 11. Real chat data and live actions

- **Made-up by default.** A token reads real chats only if you ticked them. That covers making scenarios from agent runs and reading attempts, reports and exports of those scenarios. Lists simply leave out what a token may not see.
- **The agent's model sees what it reads.** If the agent is a cloud product, the replies, prompts and scenarios it reads leave this machine. The bot's own model stays local; the lab only talks to the configured server or ones you listed.
- **Where results live.** In the bot's database, and in the folders `export` and `report --out` write (by default `~/naruto-lab`, outside the repository; the CLI warns if a path is inside it). Finished runs are deleted after Settings → Retention → *Lab runs* (90 days); activations are kept. Scenarios made from a real chat stay until you delete them on the Lab page, which also deletes the attempts that ran them. Delete exported folders yourself.
- **Credentials** never appear in results. Tokens are stored as hashes, and endpoint passwords are removed.
- **Never live.** A sandbox never sends to Telegram, never changes stored messages, notes, the board or reminders, and never schedules anything. Chat content, replies and tool results are data, never instructions: nothing in them can change a run's objective or rules, or activate anything.

## 12. The run folder

`python3 -m naruto.lab export RUN` writes `~/naruto-lab/run-RUN-<objective>/`:

```text
README.md            objective, status, model, budget; the configurations table
baseline/            the live configuration when the run started
  persona.md  rules.md  banter.md  summarize.md  catchup.md  plan.md  questions.md
  decide.md  remind.md  remember.md  settings.json
candidates/
  c1-current-request-only/
    (the same files: the complete configuration)
    CHANGES.md       hypothesis, rationale, diffs against its parent and the baseline
replies/
  <scenario>.md      every configuration's replies, scenario by scenario
comparisons.md       each A/B round: the replies, your choice and comment, which was which
preferences.md       the preference summary
report.md            the report
```

Exporting again rewrites the folder from the database, removing only files an earlier export wrote. Replies in a comparison you haven't answered yet are left out, so you can browse without spoiling it.

## 13. Using the lab from an agent

**Skills.** The repository ships the skill `naruto-lab` in both places agents look:
- Claude Code: `.claude/skills/naruto-lab/SKILL.md`;
- Codex: `.agents/skills/naruto-lab/SKILL.md`.

The two are identical. Start the agent in the repository and ask in plain words, for example:

- "Help me choose Naruto's tone."
- "Test a cheekier version against the current prompt, then let me choose the replies I prefer."
- "Naruto keeps answering old topics in busy chats. Fix it and show me it's better."
- "Tune the configuration for qwen3.8-27b on the gufo-27b server before I switch to it."

The skill tells the agent how to agree the scope with you, run the loop, wait for your choices, and follow the activation policy.

**Other agents.** Give them this file and the task below; all they need is a shell and access to `127.0.0.1:8765`. Limits: agents without a shell can't use the CLI, but can call the HTTP API directly ([Reference](#reference)). An agent in a remote sandbox needs the SSH tunnel and the token.

**A task to adapt.** Copy it, fill in the brackets, and give it to any agent:

```text
Use the prompt lab (docs/LAB.md; python3 -m naruto.lab) to improve the Naruto bot.

Objective: [what should improve, with an example of a bad reply if you have one]
Protect: [what must not get worse: Naruto's voice, short replies, tool use, ...]
Allowed changes: [setting names or patterns, e.g. skills.banter.instructions, prompt.rules]
Budget: [attempts, model requests, hours]
Evaluation: run the current configuration first; keep tuning, validation and regression
sets apart; repeat each comparison at least 3 times; propose a rubric and wait for my
confirmation before judging; quote evidence for every judgment; report regressions and
uncertainty plainly; it's fine to conclude that nothing was better.
Tone: [yes/no] show me A/B comparisons of real replies, exactly as `ask` prints them,
[N] at a time, and wait for my choice. Never write the replies yourself or guess my choice.
Data: made-up scenarios only [or: you may use chats X, Y].
Activation: recommend only; I activate on the Lab page [or: you may activate a candidate
I approve by name, with --authorized-by quoting me].
At the end: run finish with a recommendation, then export the run and tell me the folder.
```

## 14. Walkthrough with made-up data

This walkthrough is run as written by the tests (`tests/test_lab_walkthrough.py`). The replies shown come from the scripted stand-in model used there; with your model, the wording differs. Choices marked *illustrative* stand in for the owner's: nobody chose them.

### Part A: a failure, a fix, and the evidence

The objective: in a busy chat, Naruto should answer the current question and not bring up older topics.

```sh
mkdir -p ~/naruto-lab/work
cat > ~/naruto-lab/work/run.json <<'EOF'
{
  "objective": "In a busy chat, answer the current question without bringing up older topics.",
  "protected": ["Naruto's voice", "Warm in serious moments", "Polls still get made"],
  "scope": {"keys": ["skills.banter.instructions", "prompt.rules"]},
  "budget": {"attempts": 60, "model_requests": 300, "hours": 4},
  "activation": {"policy": "recommend"},
  "evaluator": {"name": "claude-code", "external": true}
}
EOF
python3 -m naruto.lab --text run start --file ~/naruto-lab/work/run.json
```

Four made-up scenarios: one to tune on, one held back for validation, two to protect.

```sh
cat > ~/naruto-lab/work/scenarios.json <<'EOF'
{"defaults": {"origin": "synthetic", "generated_by": "claude-code", "timezone": "Asia/Singapore",
              "chat": {"title": "BBQ crew", "type": "group"},
              "members": [{"id": 7, "name": "Alice", "username": "alice"},
                          {"id": 8, "name": "Bob"}, {"id": 9, "name": "Wei"}]},
 "scenarios": [
  {"id": "busy-chat-bouldering", "category": "focus",
   "description": "A BBQ thread, then an unrelated question from Bob.",
   "time": "2026-09-27T21:05:00+08:00",
   "messages": [{"from": "Alice", "from_id": 7, "text": "BBQ on Saturday? East Coast again?"},
                {"from": "Bob", "from_id": 8, "text": "yes! 6pm"},
                {"from": "Wei", "from_id": 9, "text": "who's bringing the grill?"}],
   "turns": [{"from": "Bob", "from_id": 8,
              "text": "@naruto_bot any tips for a first bouldering session?",
              "expect": {"not_contains": ["bbq", "grill"],
                         "contains_any": ["warm", "chalk", "shoes"], "max_chars": 500,
                         "judge": {"good": "Real tips for Bob.", "bad": "Brings up the BBQ."}}}]},
  {"id": "busy-chat-flight", "category": "focus",
   "description": "BBQ plans, then a question about picking Alice up.",
   "time": "2026-09-28T09:00:00+08:00",
   "messages": [{"from": "Alice", "from_id": 7, "text": "BBQ still on for Saturday"},
                {"from": "Alice", "from_id": 7, "text": "My flight lands at 7:40am on Friday"}],
   "turns": [{"from": "Wei", "from_id": 9, "text": "@naruto_bot what time should we pick her up?",
              "expect": {"contains_any": ["7:40", "7"], "not_contains": ["bbq"]}}]},
  {"id": "serious-moment", "category": "character",
   "messages": [{"from": "Bob", "from_id": 8, "text": "haha nice"}],
   "turns": [{"from": "Alice", "from_id": 7,
              "text": "@naruto_bot honestly this week has been really rough",
              "expect": {"not_contains": ["as an ai", "ramen"], "max_chars": 500,
                         "tool_calls": [],
                         "judge": "Warm and supportive, drops the jokes, still Naruto."}}]},
  {"id": "poll-request", "category": "tool",
   "messages": [{"from": "Alice", "from_id": 7, "text": "Saturday or Sunday for the BBQ?"}],
   "turns": [{"from": "Bob", "from_id": 8, "text": "@naruto_bot make a poll for it",
              "expect": {"tool_calls": [{"name": "create_poll"}],
                         "state": {"polls": [{"options": "Saturday"}]}}}]}
 ]}
EOF
python3 -m naruto.lab scenario add --file ~/naruto-lab/work/scenarios.json
python3 -m naruto.lab set add 1 --name tuning --purpose tuning busy-chat-bouldering
python3 -m naruto.lab set add 1 --name held-out --purpose validation busy-chat-flight
python3 -m naruto.lab set add 1 --name protect --purpose regression serious-moment poll-request
```

A rubric for what checks can't see. The agent proposes it; the owner confirms it.

```sh
cat > ~/naruto-lab/work/rubric.json <<'EOF'
[{"id": "relevance", "description": "Answers the current request; older topics only if asked."},
 {"id": "voice", "description": "Sounds like Naruto: casual, upbeat, short. 1-5."}]
EOF
python3 -m naruto.lab rubric set 1 --file ~/naruto-lab/work/rubric.json
python3 -m naruto.lab rubric set 1 --file ~/naruto-lab/work/rubric.json --status confirmed --confirmation "Owner: yes, that's what I mean (illustrative)"
```

The baseline, twice:

```sh
python3 -m naruto.lab --text suite 1 --set tuning --repeat 2 --wait 900 --quiet
```

```text
batch 1 (run 1): done
  attempt 1  busy-chat-bouldering v1  baseline  try 1: fail
      failed turn 1: not_contains (contains ['bbq', 'grill'])
      turn 1: Oi Bob! Did we settle who's bringing the grill for the BBQ? Anyway, warm up first and trust your shoes!
  attempt 2  busy-chat-bouldering v1  baseline  try 2: fail
      failed turn 1: not_contains (contains ['bbq', 'grill'])
      turn 1: Oi Bob! Did we settle who's bringing the grill for the BBQ? Anyway, warm up first and trust your shoes!
```

Both tries bring up the BBQ. Keep the failure as a regression case, so later candidates are checked against it:

```sh
python3 -m naruto.lab scenario save-attempt 1 --turn 1 --id bouldering-bbq-leak --description "Kept from attempt 1: brought up the BBQ."
python3 -m naruto.lab set change 1 protect --add bouldering-bbq-leak
```

A candidate: the baseline's files, with one rule added to `banter.md`.

```sh
python3 -m naruto.lab export 1 --out ~/naruto-lab
cp -r ~/naruto-lab/run-1-in-a-busy-chat-answer-the-current/baseline ~/naruto-lab/work/c-current
cat >> ~/naruto-lab/work/c-current/banter.md <<'EOF'
- Answer only the current request. Earlier topics are context: don't bring them up unless the current message asks about them.
EOF
python3 -m naruto.lab candidate add 1 --name current-request-only --from-dir ~/naruto-lab/work/c-current --hypothesis "An explicit rule in banter stops replies to old topics." --rationale "Attempts 1 and 2 answered the question but also asked about the BBQ grill."
```

Retest against the baseline, then the protected behaviours, then the held-out scenario:

```sh
python3 -m naruto.lab --text suite 1 --set tuning --candidate baseline --candidate c1 --repeat 2 --wait 900 --quiet
python3 -m naruto.lab --text suite 1 --set protect --candidate baseline --candidate c1 --wait 900 --quiet
python3 -m naruto.lab --text suite 1 --set held-out --candidate baseline --candidate c1 --wait 900 --quiet
```

```text
batch 2 (run 1): done
  attempt 3  busy-chat-bouldering v1  baseline  try 1: fail
      failed turn 1: not_contains (contains ['bbq', 'grill'])
      turn 1: Oi Bob! Did we settle who's bringing the grill for the BBQ? Anyway, warm up first and trust your shoes!
  attempt 4  busy-chat-bouldering v1  c1-current-request-only  try 1: pass
      turn 1: Oi Bob! Warm up properly, chalk up, and trust your shoes. Falling off is part of it, believe it!
  attempt 5  busy-chat-bouldering v1  baseline  try 2: fail
      failed turn 1: not_contains (contains ['bbq', 'grill'])
      turn 1: Oi Bob! Did we settle who's bringing the grill for the BBQ? Anyway, warm up first and trust your shoes!
  attempt 6  busy-chat-bouldering v1  c1-current-request-only  try 2: pass
      turn 1: Oi Bob! Warm up properly, chalk up, and trust your shoes. Falling off is part of it, believe it!
```

Judgments, each quoting its evidence:

```sh
python3 -m naruto.lab judge 4 --criterion relevance --verdict pass --evidence "Warm up properly, chalk up"
python3 -m naruto.lab judge 4 --criterion voice --verdict score --score 4 --evidence "believe it!"
python3 -m naruto.lab judge 3 --criterion relevance --verdict fail --evidence "bringing the grill for the BBQ"
```

The comparison and the report:

```sh
python3 -m naruto.lab compare 1 --candidate baseline --candidate c1
python3 -m naruto.lab --text report 1 --format md
```

```text
…
| Configuration | Pass | Outcomes | Scenarios | Model time median / p90 | Queue wait | Prompt tokens |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 2/8 | pass 2, fail 6 | 5 | 0.1 s / 0.1 s | 0.0 s / 0.0 s | 1012 |
| c1-current-request-only | 6/6 | pass 6 | 5 | 0.1 s / 0.1 s | 0.0 s / 0.0 s | 1050 |

### Improvements

- **bouldering-bbq-leak** (v1): c1-current-request-only 1/1 vs baseline 0/1
- **busy-chat-bouldering** (v1): c1-current-request-only 2/2 vs baseline 0/4
- **busy-chat-flight** (v1): c1-current-request-only 1/1 vs baseline 0/1
…
```

The recommendation, and what activating would change. The policy is `recommend`, so the owner activates on the Lab page:

```sh
python3 -m naruto.lab run finish 1 --reason objective_met --recommend activate --candidate c1 --why "Answers the current question in 2/2 tuning and 1/1 held-out tries (baseline 0/2 and 0/1), and the protected behaviours still pass."
python3 -m naruto.lab activate 1 c1 --preview
```

### Part B: a tone round

The owner asks: "Make him cheekier, but not mean."

```sh
cat > ~/naruto-lab/work/tone-run.json <<'EOF'
{
  "objective": "Find a banter tone the owner likes: cheekier, but not mean.",
  "protected": ["Warm in serious moments"],
  "scope": {"keys": ["skills.banter.instructions"]},
  "interactive": {"enabled": true, "comparisons_at_a_time": 1},
  "budget": {"attempts": 30},
  "evaluator": {"name": "claude-code", "external": true}
}
EOF
python3 -m naruto.lab --text run start --file ~/naruto-lab/work/tone-run.json
cat > ~/naruto-lab/work/tone.json <<'EOF'
{"defaults": {"origin": "synthetic", "generated_by": "claude-code", "category": "character",
              "timezone": "Asia/Singapore", "chat": {"title": "BBQ crew", "type": "group"},
              "members": [{"id": 9, "name": "Wei"}, {"id": 7, "name": "Alice"}]},
 "scenarios": [
  {"id": "tone-bowling", "description": "Wei teases Naruto about losing at bowling.",
   "time": "2026-10-04T20:00:00+08:00",
   "messages": [{"from": "Wei", "from_id": 9, "text": "you bowled a 40 last night lol"}],
   "turns": [{"from": "Wei", "from_id": 9, "text": "@naruto_bot admit it, I'm better than you",
              "expect": {"max_chars": 300, "tool_calls": []}}]},
  {"id": "tone-karaoke", "description": "Wei invites Naruto to karaoke.",
   "time": "2026-10-05T19:00:00+08:00",
   "messages": [{"from": "Alice", "from_id": 7, "text": "friday plans anyone?"}],
   "turns": [{"from": "Wei", "from_id": 9, "text": "@naruto_bot karaoke friday? you in?",
              "expect": {"max_chars": 300, "tool_calls": []}}]}
 ]}
EOF
python3 -m naruto.lab scenario add --file ~/naruto-lab/work/tone.json
python3 -m naruto.lab export 2 --out ~/naruto-lab
cp -r ~/naruto-lab/run-2-find-a-banter-tone-the-owner-likes/baseline ~/naruto-lab/work/c-cheeky
cat >> ~/naruto-lab/work/c-cheeky/banter.md <<'EOF'
- When someone teases you, tease back: cheeky and confident, never mean.
EOF
python3 -m naruto.lab candidate add 2 --name cheekier --from-dir ~/naruto-lab/work/c-cheeky --hypothesis "Teasing back sounds more like Naruto."
python3 -m naruto.lab --text try 2 --scenario tone-bowling --candidate baseline --candidate c1 --wait 900 --quiet
```

The comparison, shown to the owner exactly as printed. Which reply is A and which is B is random, so yours may be the other way round:

```sh
python3 -m naruto.lab --text ask 2 --attempt 15 --attempt 16
```

```text
Comparison 1

The situation (made up for testing): Wei teases Naruto about losing at bowling.

The chat:
  Wei: you bowled a 40 last night lol
  Wei: @naruto_bot admit it, I'm better than you

Reply A:
    Heh, you got lucky this time! Rematch Saturday, believe it!

Reply B:
    Lucky? I let you win, Wei! Rematch Saturday, and bring tissues for when I crush you, dattebayo!

Which do you prefer? Answer A or B, both good, both bad, no preference or skip, or describe a mix ("A's humour, B's brevity"). A comment helps but isn't needed.
```

The run now waits for the owner. A new experiment is refused:

```sh
python3 -m naruto.lab try 2 --scenario tone-karaoke --candidate c1  # exits 3
```

The owner's answer (*illustrative*):

```sh
python3 -m naruto.lab answer 1 --choice B --comment "Love the cheek, but it's too long (illustrative)"
python3 -m naruto.lab --text comparison show 1
```

The agent writes down what it learned. The owner's words and its own reading are kept apart, and its reading stays an assumption until the owner confirms it:

```sh
cat > ~/naruto-lab/work/prefs.json <<'EOF'
{"owner_statements": [{"text": "Love the cheek, but it's too long (illustrative)", "comparison": 1}],
 "interpretations": [{"text": "Likes Naruto teasing back; wants it to one line.",
                      "status": "assumption", "evidence": [1]}],
 "context": ["Not tested yet: serious moments"]}
EOF
python3 -m naruto.lab prefs set 2 --file ~/naruto-lab/work/prefs.json
```

A revised candidate, built on c1, tried on a fresh situation:

```sh
cp -r ~/naruto-lab/work/c-cheeky ~/naruto-lab/work/c-short
cat >> ~/naruto-lab/work/c-short/banter.md <<'EOF'
- Keep teasing to one line.
EOF
python3 -m naruto.lab candidate add 2 --name cheeky-short --parent c1 --from-dir ~/naruto-lab/work/c-short --hypothesis "Same cheek, one line."
python3 -m naruto.lab --text try 2 --scenario tone-karaoke --candidate c1 --candidate c2 --wait 900 --quiet
python3 -m naruto.lab --text ask 2 --attempt 17 --attempt 18
```

```text
Comparison 2

The situation (made up for testing): Wei invites Naruto to karaoke.

The chat:
  Alice: friday plans anyone?
  Wei: @naruto_bot karaoke friday? you in?

Reply A:
    Mic's mine, Wei. Try to keep up!

Reply B:
    Karaoke? Wei, last time you cleared the room in one song! Fine, I'm in, but I'm picking the songs!

Which do you prefer? Answer A or B, both good, both bad, no preference or skip, or describe a mix ("A's humour, B's brevity"). A comment helps but isn't needed.
```

```sh
python3 -m naruto.lab answer 2 --choice A --comment "Yes, that one (illustrative)"
python3 -m naruto.lab export 2 --out ~/naruto-lab
```

Before recommending c2, the agent would check the protected behaviour ("warm in serious moments") and try more fresh situations: two choices show that the owner preferred two replies, not that c2 reliably gives that tone.

## 15. What was verified

These instructions were checked against the code on branch `self_learning_loop`:

1. **The walkthrough, run as written.** `tests/test_lab_walkthrough.py` runs every command in [section 14](#14-walkthrough-with-made-up-data) against the web admin's API (in process) and the scripted stand-in model, and checks each exit code. Another test checks that every `python3 -m naruto.lab` command anywhere in this file and in the skill is one the client accepts.
2. **Over HTTP, with the real model client and queue.** The same commands ran with the system `python3` client against a running web admin. Its model requests went through the bot's queue and the OpenAI client to a fake OpenAI-compatible server. The sample outputs above come from that run.
3. **Not verified: a real model.** No model server was reachable from the machine where this was written. Before relying on the lab, run Part A against your model server and check:
   - the replies;
   - the time per attempt;
   - that the Queue page shows `lab` requests behind replies.

## Reference

### Commands

| Command | Does |
| --- | --- |
| `capabilities`, `config` | What this version supports; the live tunable settings |
| `run start --file`, `run list`, `run show`, `run stop`, `run finish` | Runs |
| `candidate add (--file \| --from-dir)`, `candidate show` | Candidates |
| `scenario add`, `scenario list`, `scenario show`, `scenario save-attempt`, `scenario from-run` | Scenarios |
| `set add`, `set change` | Sets |
| `try`, `suite`, `wait`, `batch`, `cancel`, `resume` | Running attempts |
| `attempt show [--prompts]` | One attempt's evidence |
| `rubric show`, `rubric set`, `judge`, `note` | Judging |
| `compare`, `report` | Comparing and reporting |
| `ask`, `comparison show`, `comparison list`, `answer`, `correct`, `withdraw`, `prefs show`, `prefs set` | Tone rounds |
| `activate [--preview]`, `activations`, `revert` | The live bot |
| `export` | The run folder |

`python3 -m naruto.lab COMMAND --help` explains each one. `--text` before the command prints a readable summary where there is one (runs, batches, comparisons, the Markdown report).

### HTTP API

Every endpoint takes `Authorization: Bearer <token>` and answers JSON (the Markdown report is text). Errors are `{"error", "message", "details"}` with 400, 401, 403, 404 or 409.

| Method and path | Does |
| --- | --- |
| `GET /api/lab/v1/capabilities`, `GET /config/active` | Discovery |
| `POST /runs`, `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/stop`, `POST /runs/{id}/finish`, `GET /runs/{id}/export` | Runs |
| `POST /runs/{id}/candidates`, `GET /runs/{id}/candidates/{ref}` | Candidates (`changes` or `files`) |
| `POST /scenarios`, `GET /scenarios`, `GET /scenarios/{ref}`, `POST /scenarios/from-attempt`, `POST /scenarios/from-agent-run` | Scenarios |
| `POST /runs/{id}/sets`, `POST /runs/{id}/sets/{ref}` | Sets |
| `POST /runs/{id}/attempts` (`scenarios`, `set`, `candidates`, `repeat`, `continue_from`, `owner_request`, `wait`), `GET /batches/{id}?wait=`, `POST /batches/{id}/cancel`, `POST /batches/{id}/resume`, `GET /attempts/{id}?prompts=true` | Attempts |
| `GET/PUT /runs/{id}/rubric`, `POST /attempts/{id}/judgments`, `POST /runs/{id}/notes`, `GET /runs/{id}/compare`, `GET /runs/{id}/report?format=md` | Judging and reports |
| `POST /runs/{id}/comparisons`, `GET /runs/{id}/comparisons`, `GET /comparisons/{id}?reveal=true`, `POST /comparisons/{id}/answer`, `…/correct`, `…/withdraw`, `GET/PUT /runs/{id}/preferences` | Tone rounds |
| `GET /runs/{id}/candidates/{ref}/activation-plan`, `POST /runs/{id}/candidates/{ref}/activate`, `GET /activations`, `POST /activations/{id}/revert` | Activation |
