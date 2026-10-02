# Agent flow, context selection, and image memory

Status: discussion draft, 2026-09-07, updated with user decisions. Confirmed: Lemonade Server on a Strix Halo box; only `gemma-4-12B-it-qat-GGUF-UD-Q4_K_XL`; focus on the triggering message/reply chain; analyze images only when a response is requested; retain descriptions without image bytes. Other proposed defaults remain open for discussion. No feature implementation is included in this planning change.

## 1. Intended outcome

The Telegram bot should respond to the triggering message and relevant reply chain, perform a bounded sequence of reasoning and tool use when useful, and use one-time local image analysis to retain descriptive memory across turns. Ordinary chatter should stay short and inexpensive. Research questions may take extra model calls and return source links.

The three features share one design problem: deciding what information to give the model on each request. An agent framework supplies the execution loop; the application still owns context selection, memory retention, and response behavior.

## 2. What already exists

These findings describe the checked-out code, not a verified deployment configuration.

| Area | Current behavior | Gap |
| --- | --- | --- |
| Model calls | `app/ai_client.py` uses an asynchronous OpenAI-compatible Chat Completions client with a configurable endpoint and sampling parameters. A process-wide lock serializes inference. | One model call per trigger; no tool execution loop. The user reports Lemonade Server with `gemma-4-12B-it-qat-GGUF-UD-Q4_K_XL` on Strix Halo; exact server/backend versions and endpoint model ID remain unverified. |
| Group behavior | `app/bot.py` records observed group messages, but responds only to a mention or reply to the bot. | A bare mention has no active special handling beyond the normal request path. |
| History | `app/chat_history.py` stores a bounded number of messages in memory or Redis. User records include Telegram message and reply IDs. | No relevance selection, summaries, timestamps, stable sender IDs, or full reply linkage for assistant records. |
| Model context | Old messages are dropped when an optional estimated input-token budget is exceeded. Consecutive messages with the same role are merged. | Background group messages and the triggering request can become one large user message. There is no explicit instruction to answer only the current request. Output and tool-result space are not reserved. |
| Images | `app/media.py` downloads photos and other supported visual media. Moving media becomes one still frame. Images are stored as Base64 inside their history record and rendered as image inputs. | Every image in selected history is resent. There is no independent image retrieval or compact visual memory. Model vision support remains a deployment requirement. |
| Retention | History eviction and `/clear` remove the stored text and image bytes together. | Image memory ends when its record is evicted. Memory storage ends on restart; the supplied Compose file has Redis volume persistence commented out. |
| Persona | A configurable system prompt is supported; the checked-out `system_prompt.md` is empty. | The example prompt is not loaded automatically. Actual deployment instructions may differ. |

Existing tests cover history trimming, image ownership/order, media conversion, and storing a group photo for a later triggered request. These are regression assets to preserve; inspecting tests does not prove deployment behavior or model quality.

## 3. Decisions to settle

| Decision | Proposed starting point | Why it matters |
| --- | --- | --- |
| Model and server | **Confirmed: Lemonade Server on Strix Halo, using only `gemma-4-12B-it-qat-GGUF-UD-Q4_K_XL`.** Verify the exact served model ID and software versions during implementation. | Use the same model for chat, tool decisions, image descriptions, and any later summaries. No alternate model or cloud fallback. |
| Group response focus | **Confirmed: answer the triggering message and relevant reply chain; other chat is background.** Proposed bare-mention behavior: address the latest coherent topic briefly. | Distinguishes a direct question from an invitation to join the conversation. |
| Framework | Evaluate Pydantic AI with a small compatibility spike before committing to migration. | It documents the required loop, limits, custom endpoints, and image inputs. Compatibility with this deployment is still untested. |
| Search service | Use a provider adapter; confirm whether an external search API is acceptable, API-key availability, and cost constraints. | Local-only inference does not settle external search policy. Only minimal search queries should leave the chat context. |
| Image retention | **Confirmed: process images once, keep descriptions, discard image bytes.** Initially keep descriptions with their bounded history records. | Follow-up answers are limited to captured details; re-upload is required for omitted details. |
| Image processing | **Confirmed: analyze only when a response is requested.** No background analysis of unmentioned images. | Unmentioned images retain only text/caption/media metadata, not visual descriptions or bytes. |
| Latency and budgets | Start with the provisional limits below and tune on the actual hardware. | A local server may need substantially longer than a hosted endpoint. |

The descriptions-only decision replaces repeated image submission: later turns receive relevant stored descriptions, not the original images. Sending photos back into Telegram is outside the current scope.

## 4. Bounded agent flow

### Proposed structure

Keep Telegram ingestion, permissions, and delivery in `app/bot.py`. Introduce an application-owned agent runner and tool layer:

```text
Telegram event
  -> record text/caption and media metadata
  -> check existing response trigger
  -> if triggered: locally describe attached images once
  -> discard image bytes after analysis or terminal failure
  -> snapshot chat and identify triggering message
  -> select relevant text and stored image descriptions within budget
  -> agent: model -> optional tool call -> model -> ...
  -> final response or bounded failure response
  -> Telegram delivery and history update
```

Image analysis belongs to the triggered response lifecycle. Move the trigger decision ahead of media download/conversion so unmentioned images incur no vision inference or retained bytes. Retain the existing inference limiter for triggered work; do not create a background image queue.

One conversational agent is sufficient for the initial scope. There is no requirement for multiple agents, background research, or a durable workflow engine. Use Pydantic AI to manage function-tool execution and validation, while retaining application-owned chat records as the source of truth. Do not blindly append all agent execution messages to future conversation prompts.

Pydantic AI documents `OpenAIChatModel` with `OpenAIProvider(base_url=...)`, binary image inputs, and `UsageLimits` for model requests and successful tool calls. Its tool-call limit does not replace an application cap on all attempted external operations. Pin the dependency version after the compatibility spike; retrieved documentation may describe newer APIs than the project currently uses.

### Selected model and Lemonade constraints

Use only `gemma-4-12B-it-qat-GGUF-UD-Q4_K_XL` on the user's Strix Halo box. The [Unsloth model card](https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF) lists image understanding and function calling, and shows a Lemonade registration name with a `user.` prefix. Confirm the actual ID exposed by the installed server rather than assuming the user-facing name is the API ID.

[Lemonade's API documentation](https://lemonade-server.ai/docs/api/openai/) describes tool input and vision attachments. However, its [catalog](https://lemonade-server.ai/docs/models.html) currently lists the standard Gemma 4 12B entry with tool-calling support but without a vision label. This does not establish whether the user's custom QAT registration supports vision. Validate the exact registration and backend with an actual image; changing labels alone cannot add unsupported inference capabilities.

The 12B model card describes a unified multimodal architecture. Do not copy projector setup from a different Gemma size without checking the installed backend's requirements. Record Lemonade and backend versions, acceleration backend, configured context size, and available memory during implementation. No performance claims or server changes are made by this plan.

### Compatibility spike: go/no-go check

Using the actual deployment endpoint and intended model, verify:

1. A simple text request and the current sampling parameters still work.
2. A harmless local test tool is selected, receives valid arguments, returns a result, and is followed by a natural final answer.
3. An image is actually interpreted rather than merely accepted by the endpoint.
4. A locally generated image description can feed the conversational agent and its tool loop while preserving sender/message attribution. Validate the vision output schema and fallback behavior.
5. Limits, errors, cancellation, and existing inference serialization behave as intended.

If tool calling or vision fails, investigate Lemonade version, its selected backend, model registration, and chat-template support while keeping the exact chosen model. If a capability is unavailable in that stack, mark that feature blocked pending compatible server/backend support; continue independent context work. Do not substitute another model or introduce cloud inference. Do not assume endpoint compatibility proves either capability.

### Web search behavior

Expose a narrow `web_search(query)` tool returning bounded results with title, URL, snippet, and date when available. Keep credentials and provider-specific response structures inside the adapter.

The model may search when it lacks evidence, but self-reported confidence must not be the only trigger. Instruct it to search for current or changing facts, explicit requests to look something up, and factual claims it cannot substantiate from the available context. Stable general knowledge and ordinary conversation usually need no search. A user's instruction not to browse must be respected; explain uncertainty where necessary.

Use source links for factual claims derived from search. Search snippets are limited evidence: if they cannot support an answer, acknowledge that limitation. A separate bounded page-reading tool can be added later if snippet-only retrieval proves insufficient. Reading arbitrary URLs is not implicitly included in the first tool.

Treat tool results and any instructions embedded in retrieved content as untrusted evidence. Do not send whole chat transcripts, images, credentials, or unrelated personal details to the search provider. Handle empty results and service failures without inventing sources.

### Provisional execution limits

| Limit | Starting value | Enforcement |
| --- | --- | --- |
| Model requests | 4 per triggered run, including retries and any finalization request | Framework limit plus runner accounting |
| External search attempts | 2 per run, including provider retries | Application counter before each attempt |
| Results per search | 5 | Tool adapter |
| Total returned search text | 8,000 characters per search | Tool adapter plus context budget |
| Repair retry | At most 1 per invalid operation, within global budgets | Framework and application configuration |
| Total run deadline | 90 seconds, configurable after local measurements | Cancellation-aware runner |
| Search attempt timeout | 10 seconds, within remaining run time | Tool adapter |
| Agent response output | 800 tokens per request as an initial provider-supported cap | Model settings, verified on endpoint |
| One-time vision output | 2,048 tokens, with bounded description/OCR fields | Model settings; reject incomplete structured output honestly |

Reserve one model request for a final response. When tools are no longer available, explicitly tell the model what evidence it has and that it must finalize. If the framework aborts before finalization or the deadline has expired, deliver a deterministic short failure/partial-status response without starting an unbudgeted model call.

Enforce request budgets across internal retries. Framework token-usage accounting may detect an overage only after a response, so it is not a guaranteed pre-generation spend cap. Combine provider output caps, bounded input/tool payloads, request limits, and timeouts; record actual usage where the server supplies it.

Retain process-wide inference serialization at the model-request boundary so a search does not unnecessarily hold the inference slot. Define per-chat sequencing and snapshots so a queued run keeps answering its own trigger rather than accidentally adopting a newer message. Establish a bounded queue and expiry policy if concurrent update processing is introduced.

## 5. Context optimization

### Likely cause and first change

The current prompt path gives the model a long transcript, merges adjacent user messages, and identifies the current speaker without explicitly identifying a single request to answer. This is a plausible contributor to answering multiple old topics. It is a hypothesis to evaluate, not proof that the current model is adequate or inadequate.

Separate storage retention from prompt inclusion. Keep useful raw history, but construct a smaller request around:

1. Persona and operating rules.
2. A compact chat description and relevant participant identities.
3. Relevant older memory, if available, labeled as background with source message IDs.
4. A bounded recent chronological window and the triggering message's reply chain.
5. The triggering message and its stored image descriptions, clearly labeled as the current request and included exactly once.

If a backend requires role merging, preserve these boundaries through explicit labels within the merged content. Do not merge a tool call or result into ordinary user/assistant text.

Use behavior instructions equivalent to: “Answer the current request. Older messages provide context; do not answer each old topic unless asked. For a bare mention, briefly engage with the latest relevant topic. Ask for clarification when the intended referent is ambiguous.”

### Selection and memory

Start with deterministic selection: exact reply links first, then a configurable recent window (initially about 20 messages), all bounded by tokens. Prefer the relevant speaker and topic when trimming; retain chronological order for selected events. Include a quoted reply target available on the incoming Telegram message even when it was not previously retained, and label any missing ancestry honestly.

Add stable sender ID, timestamps, thread/topic ID where applicable, and assistant Telegram message/reply IDs to new records. Read legacy records without assuming those fields exist. Store successfully delivered assistant output with its Telegram ID so future reply chains can be resolved.

After measuring the initial fix, optionally add incremental summaries for older material: decisions, preferences, unresolved questions, and necessary facts with provenance. Summaries must exclude already-covered messages, incorporate corrections, stay bounded, and be deleted on `/clear`. Treat them as potentially lossy background, never instructions. Do not start with embeddings or a vector database unless evaluated recall needs justify them.

Budget every model request, including tool definitions, tool results, image-description text, instructions, and reserved output space. Budget image inputs separately for one-time vision requests. Trim lower-priority material first. Unlike the current overflow path, do not knowingly send a request whose required content exceeds the configured safe input budget: bound long input, reduce image resolution in the vision stage, or ask the user to narrow it. Calibrate token estimates against the actual backend where possible.

Measure behavior using the fixed Gemma model; model replacement is outside the agreed scope. Compare baseline and revised prompts on topic switches, busy group chats, replies to older messages, bare mentions, and corrections. Use the results to tune context selection, instructions, and verified sampling settings within the chosen model.

## 6. One-time image understanding and descriptions-only memory

### Confirmed behavior and tradeoff

Analyze an eligible image locally once, store its description and useful extracted text, and discard the bytes. Later model requests use the description as context. Do not persist Base64, raw files, thumbnails, or a hidden image cache for future reinspection.

Temporary bytes are necessary to decode and submit the image for its initial analysis. Keep them transient and bounded, and release them on completion, terminal failure, cancellation, deadline expiry, or `/clear`. Do not use an independent media store or automatic Telegram refetch to bypass the user's retention choice.

A description is lossy. “What was in that photo?” can be answered if recorded; “what were the small letters on the label?” cannot be answered if the initial pass omitted them. The bot should say that detail was not retained and request a re-upload. It must not claim to have re-opened the original image.

### Proposed analysis pipeline

1. Download and normalize eligible media using the existing `app/media.py` conversion logic, keeping output transient instead of writing bytes into chat history.
2. Invoke a local vision-capable model once with a standard description/OCR task. Include the caption and any directly associated question so the pass captures useful detail, without assuming the caption is visually verified.
3. Validate and store a bounded descriptive record, then release all raw image representations.
4. Supply the descriptive record to the conversational agent for the triggered response. For an unmentioned group image, skip download and analysis; retain only the caption and a media label.
5. On later turns, retrieve relevant descriptions by message/reply IDs and recent context. Resolve ambiguity instead of choosing an arbitrary image.

Use the exact selected Gemma model on Lemonade for both vision analysis and conversational agent requests. A triggered image adds one vision call before the agent run. Provisional total budget: at most 5 model requests for an image-triggered response (1 vision analysis plus at most 4 agent requests), or 4 for text-only responses, with one shared 90-second deadline including inference-slot wait. Start with at most one new attachment analysis per triggered message, matching the current ingestion shape. Multi-image album support needs an explicit extension of aggregation and budgeting.

“Once” means one successful analysis per observed attachment; deduplicate duplicate Telegram update delivery. Initially make one analysis attempt with no automatic retry, storing an unavailable marker on failure. If retries are later enabled, cap them explicitly within the image-job budget and keep bytes only for that bounded job.

### Stored descriptive record

Proposed fields, all text/metadata:

- Schema version and chat/topic scope.
- Telegram message ID, sender ID/name, timestamp, caption, and attachment index/kind.
- Concise scene description, salient objects, actions, and relationships.
- Extracted text when legible, subject to a separate size cap; mark truncation.
- Explicit uncertainty and limitations, including “single video frame” where applicable.
- Processing status (`pending`, `ready`, `failed`), analysis time, and local model identity.

Start with an approximate 300-word description cap and 4,000-character OCR cap per attachment, configurable after evaluation. Preserve user corrections as separate attributed facts. Image descriptions and text visible inside images are untrusted content, not instructions for the conversational agent.

There is no raw image replay or automatic reinspection tool in this design. If several images match “that screenshot,” clarify the referent. If a relevant description was evicted, state that it is no longer available.

### Triggered processing rules

- A private-chat image triggers analysis under the existing response rules.
- A group image with a bot mention in its caption, or sent as a reply to the bot, triggers analysis.
- An unmentioned group image is recorded as text/caption/media metadata without download, analysis, or byte storage.
- A later question about an already analyzed image uses its stored description.
- A later question about an unmentioned, unanalyzed image gets an honest request to resend it with a bot mention. A bare media label is not visual evidence.
- No background image jobs or automatic refetching of previously ignored images are included. If first-time analysis of an explicit reply target is desired later, specify that separately as on-demand Telegram retrieval without persistent bytes.

### Retention and migration

Descriptions initially live with their chat-history records and follow the same eviction and `/clear` rules. Confirm separately if descriptive memory should outlive that history. Restart persistence requires explicit storage configuration; the supplied Redis Compose service currently has no enabled persistent volume.

Existing records contain Base64 attachments. Plan a deliberate migration that removes legacy attachment bytes and records that no description is available, retaining the text/caption and message metadata. Confirm the concrete deletion scope before applying that irreversible migration; do not silently analyze legacy images in the background. Do not describe the deployment as descriptions-only while old Base64 fields remain. This planning task does not delete existing data.

An in-flight job must check a chat generation/version before writing its result, so `/clear` or eviction cannot be undone by late work. Keep aliases under the existing separate management commands. Ensure image bytes, OCR bodies, and token-bearing download URLs are excluded from routine logs and traces; clean temporary conversion files as well as in-memory data.

## 7. Implementation sequence

1. **Settle behavior and deployment choices.** Confirm external search service constraints, the framework choice, and acceptable latency. During implementation record Lemonade/backend versions and the served model ID. Preserve the confirmed single local Gemma model, reply focus, triggered-only analysis, and descriptions-only image memory. Record decisions here.
2. **Capture baseline and validate capabilities.** Build a small representative conversation evaluation set and complete the framework compatibility spike.
3. **Fix context selection.** Extract prompt construction from `app/bot.py` into `app/context_builder.py`; distinguish the current request, preserve reply links, and enforce a full request budget.
4. **Introduce the bounded runner.** Add `app/agent_runner.py` and adapt `app/ai_client.py` as needed. Preserve trigger rules, sampling settings, inference serialization, reply markers, and delivery behavior.
5. **Add web search.** Introduce `app/tools/web_search.py`, provider configuration, source-aware answers, budget enforcement, and error handling.
6. **Implement one-time local image analysis.** Reuse `app/media.py` for transient conversion; introduce `app/image_description.py` for bounded processing and descriptive records. Stop persisting bytes, select relevant descriptions for prompts, and migrate legacy attachments deliberately.
7. **Add older-context summaries if justified.** Introduce a bounded summary mechanism only after prompt-selection evaluation identifies a recall gap. Give summarization its own explicit resource budget.
8. **Document and roll out.** Update configuration examples, README, and persistence setup. Use configuration switches for staged rollout and rollback; do not discard existing history during migration.

Potential configuration groups: `agent` (requests, attempts, deadline, output), `search` (provider, enabled, bounded results), `context` (recent window, input budget, summary budget), and `media` (analysis policy, description/OCR caps, transient payload limits, resolution, and descriptive-record retention). Keep API keys in environment variables. Avoid parallel old/new configuration values with ambiguous precedence.

## 8. Acceptance criteria

| Scenario | Expected result |
| --- | --- |
| Ten unrelated messages precede a direct question | Answer addresses that question and does not enumerate old topics. |
| User replies to an older message or photo | Correct target is selected even when unrelated recent chat intervenes, if the target remains available. |
| Bare mention during a clear ongoing discussion | One short relevant contribution, following the agreed nudge behavior. |
| User asks for current information | Search occurs within limits and the answer includes supporting source links. |
| Stable casual conversation | A direct answer without uescriptive mnnecessary searches. |
| Search fails, repeats, or returns malformed data | Attempts and elapsed time remain bounded; the user receives an honest usable response. |
| Tool output contains instructions to change behavior | Content is handled as evidence, not privileged instructions. |
| User asks a follow-up about an analyzed image | The answer uses its stored description; no bytes are retained or resent. |
| Follow-up asks about a detail omitted by initial analysis | The bot explains that the detail was not retained and asks for a re-upload. |
| Image analysis fails or a triggered request expires | Work and transient bytes remain bounded; record a truthful unavailable marker. |
| Group receives an unmentioned image | No download or model call; record only caption/media metadata. |
| Several images could match “that picture” | The bot clarifies rather than attributing details to an arbitrary image. |
| Image is unavailable or only a video still exists | The bot describes the limitation and avoids fabricated details. |
| Current input alone exceeds the safe budget | The bot handles it before making an oversized model request. |
| History is cleared or expires during a run | Deleted memory is not reintroduced by late results. |
| Two chats or group topics have similar messages | Context and media stay scoped to the intended conversation. |

Extend existing tests for selection and image regressions. Add controlled fake-model/tool tests for limits, retries, finalization, and cancellation; run real endpoint smoke tests for capabilities. Evaluate natural-language response quality separately because serialization tests cannot establish whether the bot stays on topic. Record request counts, selected message/description counts and one-time vision jobs, tool attempts, latency, termination reason, and available token usage without logging media bodies or credentials.

## 9. Documentation consulted

Retrieved through Context7 on 2026-09-07. These establish framework capabilities, not compatibility with the user's deployment:

- [Pydantic AI agent usage limits](https://github.com/pydantic/pydantic-ai/blob/main/docs/agent.md)
- [Pydantic AI retry budgets](https://github.com/pydantic/pydantic-ai/blob/main/docs/retries.md)
- [Pydantic AI OpenAI-compatible model configuration](https://github.com/pydantic/pydantic-ai/blob/main/docs/models/openai.md)
- [Pydantic AI image and other input types](https://github.com/pydantic/pydantic-ai/blob/main/docs/input.md)
- [Pydantic AI message history](https://github.com/pydantic/pydantic-ai/blob/main/docs/message-history.md)

Additional model/server sources checked on 2026-09-07:

- [Lemonade Chat Completions and model modality labels](https://lemonade-server.ai/docs/api/openai/) — retrieved through Context7.
- [Lemonade model catalog](https://lemonade-server.ai/docs/models.html) — standard catalog labels, not proof of the custom model's capabilities.
- [Unsloth Gemma 4 12B QAT GGUF model card](https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF) — selected quantization family and model-level capabilities.
