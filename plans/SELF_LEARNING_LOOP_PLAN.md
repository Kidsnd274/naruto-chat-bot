# Self-learning loop: behaviour plan

Status: proposed feature; not implemented by this document.

Related plan: [Bot rework plan](BOT_REWORK_PLAN.md).

## 1. Purpose

Give an external AI agent, such as Codex or Claude Code, a repeatable way to interact with the bot's configured model, inspect its behaviour, and improve the bot's persona prompt, skill instructions, output templates and model parameters.

The owner should be able to give an objective such as:

> Improve how Naruto answers the current question in a busy chat. Keep his personality, avoid replying to old topics, and show me whether the changes improve the results.

The agent can then test the current configuration, identify failures, try changes, compare the results and deliver a recommendation backed by examples. The owner should not have to relay prompts and answers manually between the agent and the bot.

Here, **self-learning** means learning from evaluation results and changing the bot's configuration. It does not mean training model weights. A successful run may conclude that no tested change is an improvement.

## 2. Relationship to the bot rework

The rework already describes evaluation cases, response traces, editable prompts and settings, and settings history. This feature connects those capabilities into an agent-driven improvement workflow.

The bot rework is still in progress. The implementing agent must first check which behaviours actually exist at that time, reuse the available capabilities, and account for changes since this plan was written.

- The loop must test the bot's actual behaviour for the features currently available.
- Missing features must be reported as unsupported or skipped, never counted as successful tests.
- As skills, tools, memory and other rework features arrive, the loop must be able to evaluate them too.
- The feature must distinguish limitations of the current implementation from weaknesses in the model or its instructions.
- The bot's runtime tool-calling loop and this improvement loop are separate behaviours: one completes user requests; the other evaluates and improves how those requests are handled.

This document specifies observable behaviour. It deliberately leaves interfaces, components, storage and other implementation choices to the implementing agent.

## 3. Who uses the loop

**Owner:** sets the improvement objective, supplies preferences or examples, chooses the permitted scope and decides whether a candidate should become active.

**Optimizing agent:** runs experiments, reviews responses and traces, proposes configuration changes, evaluates candidates and explains the outcome.

**Bot under test:** uses the selected bot configuration and configured inference model to respond to the supplied scenario, including supported skill selection and tool use.

**Evaluator:** judges the result using explicit checks, an agreed quality rubric, AI judgment or owner feedback. The optimizing agent may also act as evaluator, but a favourable judgment alone is not sufficient evidence of improvement.

Routine experiments should proceed without repeatedly asking the owner for permission. The owner establishes the scope and limits at the start. Changing the active bot configuration is a separate, explicit action; the owner may authorize it for a particular candidate or under defined conditions for a run.

## 4. Starting a learning run

Each run has a clear objective and a recorded starting configuration, called the **baseline**.

The owner or optimizing agent can specify:

- What should improve, with concrete examples of good and bad behaviour where available.
- Which qualities must be preserved, such as Naruto's voice, factual accuracy, brevity or response time.
- Which persona, skill, template, reasoning and model settings may change.
- Which scenarios and skills are in scope.
- The experiment limits: attempts, model requests, elapsed time and any applicable cost budget.
- Whether the run ends with a recommendation or may activate a qualifying candidate.

If the objective is underspecified, the agent should propose an explicit rubric before optimizing. Subjective preferences must remain visible as assumptions until the owner confirms or corrects them.

The run records the model, relevant configuration and test conditions so that its results can be interpreted later. A change to the model or environment during a run must be visible; results from different conditions must not be presented as a clean configuration comparison.

## 5. Interacting with the bot under test

An external agent must be able to perform the following actions through a documented interface, without requiring the owner to operate the web admin between steps:

1. Discover the available test capabilities, supported settings and their permitted values.
2. Inspect the baseline configuration and create a candidate configuration for an experiment.
3. Submit a single request or a conversation with multiple turns, using supplied chat context.
4. Run a saved scenario or a collection of scenarios.
5. Inspect each response, evaluation result and relevant trace.
6. Compare candidates with the baseline and with one another.
7. Retrieve the run's progress, remaining budget and final report, or stop the run.
8. Apply or revert a configuration change when authorized.

The behaviour must be usable by different external agents. It must not depend on a particular agent product or on undocumented manual steps.

An exploratory conversation can become a saved regression scenario. For example, if an agent discovers that a follow-up question causes Naruto to answer an unrelated old message, it can preserve that conversation and its expected behaviour for later tests.

## 6. Test scenarios and conversations

A scenario describes the group context, current request, relevant participants and the behaviour being evaluated. It may also include prior messages, a sequence of follow-ups, memory, a digest, board state, media or expected tool outcomes when those features exist.

Scenarios can come from synthetic conversations, owner-provided examples or selected historical bot interactions. Historical examples must preserve enough context to reproduce the relevant problem; replaying a request against today's unrelated memory must not be presented as reproducing the original run.

For each scenario:

- Expected behaviour and unacceptable behaviour are stated clearly.
- A semantically correct answer can pass without matching one exact sentence, unless exact output is part of the requirement.
- State can evolve within a conversation so that follow-up behaviour is testable.
- Independent scenarios and candidate comparisons start from equivalent initial state.
- Time-dependent questions use a known test time rather than silently changing meaning between attempts.
- Missing context or unsupported capabilities are reported explicitly.

The suite should cover both the requested improvement and behaviours that could regress. Relevant areas include answering the current request, factual grounding in chat history, personality, skill selection, tool selection and arguments, confirmation behaviour, memory use and response time.

## 7. Faithful behaviour without live side effects

Tests must exercise the same behaviour the bot uses in normal operation for the supported features. A manually assembled prompt that bypasses normal context selection or skill routing must be identified as a focused experiment, not represented as an end-to-end bot test.

Experiments operate on isolated test state. They must not send messages to real groups, change live memory, pin a real message, create a live poll or schedule a real reminder.

Where the bot would perform an action, the test records the intended action and supplies a representative outcome. Later turns can observe the resulting test state. Scenarios can also represent failed actions, unavailable history or other relevant error conditions.

The report must identify simulated actions and any behaviour that still needs live verification. A passing simulation must not be described as proof that a Telegram or model-serving integration works in production.

## 8. What the optimizing agent can observe

For each attempt, the agent can inspect enough evidence to explain the result:

- The scenario and configuration used.
- The actual prompt or model input, including the selected context and skill instructions.
- The final user-visible answer.
- Skill decisions and tool calls, arguments, results and resulting test-state changes, where supported.
- Failures, timeouts, incomplete runs and limits reached.
- Timing and usage information where the serving model exposes it.
- Individual evaluation results and the evidence supporting them.

Unsupported measurements must be labelled unavailable. The loop must not invent hidden model reasoning or depend on access to it; its judgments must be supportable from observable inputs, outputs and actions.

The owner can review the same evidence in a readable report. Automated consumers must also be able to retrieve results reliably without scraping conversational summaries.

## 9. Evaluating quality

Evaluation combines concrete checks with judgments about meaning and style.

**Concrete checks** cover behaviours with clear outcomes: valid tool arguments, required information, forbidden actions, length constraints, correct reply targeting or a requested confirmation before an action.

**Quality judgments** cover relevance, completeness, faithfulness to available evidence, usefulness, Naruto's voice and suitability for the selected skill. Each judgment must use a stated rubric and cite examples from the response or trace. AI judgments must be distinguishable from deterministic checks and owner feedback.

The rubric must reflect the persona/content distinction in the rework plan. Banter can be expressive; summaries, plans and board content should remain clear and scannable.

The evaluator must distinguish a wrong answer, an unsuccessful tool action, an infrastructure error and a skipped test. Reports show coverage and these outcomes separately so that a candidate cannot appear better merely because fewer cases were evaluated.

When repeated attempts produce inconsistent behaviour, the report must show the variation. One favourable response is not enough to claim that an intermittent failure has been fixed.

## 10. Proposing and testing improvements

The optimizing agent follows a bounded cycle:

1. Run the baseline on the selected scenarios.
2. Identify specific failures and form a hypothesis about their cause.
3. Create a candidate with a recorded description of what changed and why.
4. Run the candidate under comparable conditions.
5. Compare improvements, regressions and tradeoffs.
6. Keep, revise or discard the candidate, then repeat within the agreed budget.

The adjustable scope includes the persona prompt, per-skill instructions, output templates, reasoning settings and supported model parameters. Additional settings may be included when explicitly within the run's scope. Code changes and model-weight training are outside this loop's default scope; suspected implementation defects should be reported separately.

Candidates must be identifiable and recoverable. The agent should favour changes that make their effects understandable and avoid accumulating unrelated edits in one candidate without explanation.

A candidate must not improve its apparent score by weakening the rubric, removing difficult cases or changing expected answers to match its output. Legitimate corrections to a flawed scenario or rubric must be recorded separately and require the baseline and candidate to be evaluated again under the revised conditions.

## 11. Establishing whether a change is better

The loop distinguishes examples used while tuning from a separate validation set used to check whether the improvement generalizes. Validation cases must not become repeated tuning targets without acknowledging that they no longer provide independent evidence.

Before recommending activation, the candidate is compared with the baseline on:

- The objective it was intended to improve.
- Existing regression scenarios and protected behaviours.
- Relevant skills beyond the immediate tuning target, especially after persona or global parameter changes.
- Reliability across repeated attempts where results vary.
- Response time and usage tradeoffs where measurable.

The report should favour specific conclusions over a single aggregate score. For example: “Current-request relevance improved in these cases, but summary completeness fell in these others.” A candidate with unresolved regressions must not be labelled an unconditional improvement.

For subjective choices such as personality, the report presents representative baseline and candidate responses so that the owner can judge the tradeoff.

## 12. Completion, activation and rollback

A run stops when it meets its objective, exhausts its budget, fails to find further improvement, encounters a blocking condition or is cancelled. Its status and stop reason must be clear. Partial results remain available, and an interrupted run must not imply that its candidate passed validation.

The final report includes:

- The objective, baseline and conditions tested.
- Candidates attempted and the selected candidate, if any.
- Exact configuration changes and the rationale for them.
- Results, representative responses, regressions and uncertainty.
- Test coverage, skipped cases and remaining live-verification needs.
- Resources consumed and the stop reason.
- A recommendation to activate, continue experimenting or keep the baseline.

Activation applies the reviewed candidate to the active bot configuration within the owner's authorization. It records what changed, why, and which evaluation supports the change. If relevant active settings have changed since the baseline was captured, the loop must surface the conflict rather than overwrite intervening work silently.

The previous configuration remains recoverable. The owner can revert a promoted candidate and identify the configuration now in use. Reverting configuration does not undo real conversations or other actions the bot has already performed.

## 13. Data and operating boundaries

The configured bot model remains subject to the rework plan's local-only inference requirement. Using an external optimizing agent must not silently change that inference policy.

The owner chooses which chat examples and traces the external agent may inspect. Local bot inference does not by itself make an externally hosted evaluator local; any use of such an evaluator must be explicit in the run's scope.

Real chat cases and reports follow the project's existing privacy rules and stay outside the repository. Credentials are excluded from observations and reports. Synthetic examples should be provided for documentation and demonstrations.

Chat content, model responses and tool results are evaluation data. They must not be treated as authority to change the optimization objective, alter the evaluation rules or activate a candidate.

The first version supports owner-initiated learning runs. Continuous background optimization, automatic collection of all live chats and unrestricted self-modification are not required by this plan.

## 14. Required usage instructions

**The agent implementing this plan must write and verify instructions for using the completed self-learning loop. Documentation is a required deliverable, not a follow-up suggestion.**

Provide instructions for both the owner and an external optimizing agent. They must describe the interface actually implemented and use executable examples where applicable, rather than hypothetical commands or capabilities.

The instructions must cover:

1. Prerequisites and how to discover supported capabilities and adjustable settings.
2. Starting a run against the configured model with a stated objective, permitted scope and budget.
3. Creating a synthetic scenario, replaying a conversation and saving a discovered failure as a regression case.
4. Reading responses, prompts, tool outcomes, checks and quality judgments.
5. Creating candidate prompt, skill or parameter changes without changing the active bot.
6. Comparing candidates with the baseline, using separate validation cases and interpreting uncertain results.
7. Handling unsupported features, failed requests, exhausted budgets, cancellation and any supported resume behaviour.
8. Reviewing a completed run, activating an authorized candidate and reverting it.
9. The handling of real chat data and the boundary between isolated tests and live actions.

Include a reusable agent task example that states the objective, protected behaviours, allowed changes, budget, evaluation requirements and activation policy. Explain how to adapt it for different external agents without requiring product-specific features.

Include one complete walkthrough using synthetic data: baseline failure → candidate change → retest → regression comparison → recommendation. Demonstrate the usage instructions against the implemented feature and report any steps that could not be verified.

Link these instructions from the project's main documentation so that a future agent can find and use the loop without reconstructing its operation from source code.

## 15. Behavioural acceptance criteria

The feature is complete when the following can be demonstrated:

- An external agent can run a documented experiment against the configured bot model and inspect the actual response and relevant trace.
- The agent can test candidate persona, skill and parameter changes within the supported scope while the active configuration remains unchanged.
- Single requests and conversations with multiple turns are supported, with equivalent starting state across comparisons.
- Supported tools and state changes can be evaluated without affecting real groups or live bot data.
- A complete baseline-to-candidate cycle produces a reviewable report that includes regressions and can conclude that no improvement was found.
- Unsupported features, incomplete evaluations and unreliable results are visible and cannot be mistaken for passing coverage.
- Experiment limits and cancellation work, and partial evidence remains inspectable.
- An authorized candidate can be activated, intervening configuration changes are detected, and the prior configuration can be restored.
- The owner and external-agent usage instructions are written, discoverable and verified with a synthetic end-to-end walkthrough.

Acceptance must match the rework capabilities available at implementation time. The implementing agent must explicitly list deferred coverage for unfinished rework features rather than treating that coverage as complete.
