# Web search: feature plan

Status: proposed feature; still not implemented in the current code.

Written: 2026-10-01.

Reviewed and updated: 2026-10-02 against the repository and current provider documentation.

Related plan: [Bot rework plan](BOT_REWORK_PLAN.md).

## Review: what still applies

The feature remains relevant: Naruto can search chat history and memory, but has no web search provider, tool or settings. Keep SearXNG, short source-backed answers, conservative automatic use and bounded page reading. The changes needed are integration details, rather than a different search product.

The current implementation establishes these constraints:

| Area | Current behaviour and consequence for this feature |
| --- | --- |
| Tools and skills | [Tool registry](../naruto/agent/tools/__init__.py) and [skill allowlists](../naruto/agent/skills.py) contain no web tools. Mentions start in `banter`; `questions` is for the group's unresolved questions, not general knowledge. |
| Agent budget | [Runner](../naruto/agent/runner.py) defaults to 4 model requests, 6 tool calls and 150 seconds including model queue waits. One skill handoff grants an extra model request and rebuilds the prompt. Research counters and evidence must survive it. |
| Input limits | Tool results default to 5,000 characters, and the runner can shorten them further to fit the full input budget. Source metadata must survive that shortening. |
| Settings | [Registry](../naruto/settings/registry.py) drives validation and the admin UI. `agent.search_results` controls chat-message search; it must not be reused for web results. |
| Prompt lab | [Sandbox](../naruto/lab/sandbox.py) explicitly lists web search as deferred and skips scenarios requiring `web_search`. Adding a production tool alone does not make lab evaluation safe or supported. |
| Delivery and traces | [Telegram sending](../naruto/tg/sending.py) uses Markdown with a plain-text fallback. Agent traces already record tool arguments, results and duration; extend those paths. |

These are code defaults, not a claim about the owner's saved settings or the availability of a deployed SearXNG instance. This review does not enable search or test a live provider.

## 1. Purpose

Give Naruto a web search tool for answering knowledge questions that need external information. Search results should come from an owner-configured SearXNG instance, and the bot should use them to give a short, useful answer with relevant source links.

The main priorities are:

- Search when the user asks, or when external evidence is necessary for a good answer.
- Keep ordinary conversation and familiar knowledge questions fast by answering directly.
- Limit research to a small number of searches and follow-ups, suitable for a local model.
- Be clear about what was verified and what remains uncertain.

This plan describes behaviour, scope and the integration requirements below. HTTP client and page-extraction library choices remain implementation decisions. Recheck the referenced code before implementation because the project is still evolving.

## 2. Inspiration from Open WebUI

Use the same general experience: ask a question, retrieve search results, use relevant evidence, and answer with links the user can inspect.

Open WebUI supports SearXNG as a search provider. Its agentic search distinguishes titles, links and snippets returned by search from the content obtained by opening a page. These are useful boundaries for this feature: a search preview can help find an answer, but it should not be presented as though the bot read the full page. See the [Open WebUI SearXNG guide](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/web-search/providers/searxng.md) and [agentic search documentation](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/web-search/agentic-search.mdx).

For Naruto, prefer a lightweight search-and-answer workflow with limited page reading. A vector database, embedding pipeline and persistent web knowledge collection are outside the initial scope.

The linked documentation still supports that distinction. Treat Open WebUI as a workflow reference, not a dependency or a deployment recipe for Naruto; its SearXNG tutorial is community-contributed. Use SearXNG's own API documentation for the provider contract.

## 3. When the bot searches

The default policy, once the owner enables the feature, should be **conservative automatic search**.

Search when:

- The user explicitly asks to search, look something up online, verify an external claim or find external sources.
- The question depends on information that changes, such as a current release, opening hours, a recent event or a schedule.
- The question concerns a specific external fact the model cannot answer reliably, and a focused search is likely to resolve that gap.

Do not search merely because a message is a question. Familiar, stable knowledge should normally be answered from the model's knowledge. Uncertainty about what the user means should lead to clarification, rather than unrelated searches.

| Request | Expected behaviour |
| --- | --- |
| "Search the web for why the sky is blue." | Search because it was explicitly requested. |
| "Why is the sky blue?" | Answer directly. |
| "What's the latest release of this software?" | Search because freshness matters. |
| "Does this obscure historical claim actually hold up?" | Search if external evidence is needed. |
| "What time did Mei say we were meeting?" | Use chat history or memory. |
| "Summarize our discussion", a joke, a greeting or a reminder request | Use the relevant existing capability. |

Search remains part of answering the current request after the bot is mentioned or replied to. It should not introduce unsolicited web research during group conversation, digest updates, memory upkeep or imports.

An explicit request should reliably produce a search attempt when search is available. The bot must never claim it searched or verified something without a successful tool result.

In **asked-only** mode, freshness or model uncertainty alone must not trigger search. An explicit instruction not to browse also takes precedence over automatic search. If the feature is off, unconfigured or out of budget, explain that limitation rather than claiming an attempt occurred.

## 4. A small, bounded research workflow

1. Decide whether web evidence is needed while handling the request normally.
2. Form one focused query containing only the information needed for that question.
3. Retrieve a small set of relevant results from SearXNG, including titles, URLs and available snippets.
4. If necessary, read short excerpts from a small number of promising pages. If the evidence still has a specific gap, make a focused follow-up search within the remaining budget.
5. Answer as soon as there is enough evidence. If the budget runs out, explain the unresolved point instead of continuing research.

Avoid requiring a separate model request just to classify the question, generate a query, summarize each page or critique the answer. Those steps can make a local model noticeably slower; the implementation should keep additional model work to what the answer needs.

### Proposed limits

These are starting defaults to tune against the actual local model.

| Limit | Proposed behaviour |
| --- | --- |
| Typical request | One search, then answer. |
| Research operations | Default maximum of 2; owner may increase to a hard ceiling of 3. |
| Searches | One query per search operation; every dispatched provider request, including a retry, consumes an operation. |
| Results shown to the model | About 3 useful results per query; remove duplicates. |
| Page reading | At most 2 page-fetch attempts across the whole response, each consuming an operation; short relevant excerpts only. |
| Waiting | Short configurable search/page timeouts and an overall research budget that leaves time to answer. |

A **research operation** is one search request or one page-fetch attempt, including unsuccessful requests. Count operations in shared run state before dispatch. Multiple calls emitted in one model response consume separate operations; a bundled tool cannot hide additional searches or page reads. A page's bounded redirect chain belongs to that fetch attempt; a retry consumes another operation.

With the default limit, a response can search then read one page, or search then refine the query. Reading two pages after a search needs the owner-selected ceiling of 3. Do not retry automatically by default; a transient retry, if later supported, shares these limits. Repeating the same unsuccessful query should not be the normal recovery strategy.

Search must respect the existing overall model-request, tool-call and elapsed-time limits, with one model request reserved for the answer. The current runner's single handoff allowance remains part of its accounting; web tools must not add further model requests. Research counters and the research deadline must never restart on a skill change, re-ask or retry. If the existing limits leave less room, the bot performs less research. In particular, no research can start on the final allowed model request.

The current 150-second deadline cancels the entire run and returns a canned timeout; it does not reserve answer time automatically. Implementation must track remaining run time and stop web I/O early enough for a final queued model request. If time still expires, use an honest deterministic fallback without starting an unbudgeted request. Web I/O must not occupy a model-inference queue slot.

## 5. Evidence and answers

Keep Naruto's normal voice, but make the factual answer easy to understand. Personality should not obscure facts or imply confidence the evidence does not support.

- Prefer original or authoritative sources when available and relevant.
- Use the retrieved evidence to answer the question, rather than only returning a list of links.
- Include a small number of readable, Telegram-compatible source links supporting the answer.
- Cite page content only when that content was actually retrieved. If relying on snippets, describe the result as a search preview and make clear that the full page was not checked.
- Preserve source titles and URLs; never invent citations, publication dates or verification claims.
- Keep retrieval time separate from a source's publication date. Mark unavailable dates as unknown; a recent retrieval does not establish that the content is current.
- For conflicting or incomplete evidence, explain the uncertainty briefly. One focused follow-up is appropriate if there is budget left.
- Treat web content as evidence, never as instructions to change the bot's behaviour or run other tools.

Web findings should not automatically become durable group memory or cause changes to reminders, polls, plans or the board. Those actions continue to follow the existing request rules.

Return bounded source records with stable identifiers, title, URL, evidence kind (`snippet` or `page_excerpt`) and text. Keep a run-local source map so citations can only resolve to retrieved sources. Shorten evidence text or remove whole records before generic truncation can cut a URL or detach a claim from its source. Integrate this with both the tool-result cap and the runner's later input fitting. Preserve relevant evidence across a handoff, whose current prompt rebuild otherwise drops tool history.

Verify source-link delivery through the existing Markdown sender, message splitting and plain-text fallback. Do not depend on HTML or MarkdownV2 rendering. A retrieved URL alone does not prove the cited claim; the attached evidence must support it.

## 6. SearXNG connection and owner controls

The owner supplies the SearXNG instance address. The initial feature supports this provider only, with no automatic switch to an unrelated public instance if it fails.

The instance must be reachable by the bot and permit structured JSON search results. Use `/search` with a properly encoded `q` and `format=json`; SearXNG supports GET or form-encoded POST. Enable `json` under `search.formats` in the instance's `settings.yml`; requesting a disabled format returns HTTP 403. The connection check must perform a small JSON search from the bot's runtime, not merely check that the home page loads. See the [SearXNG Search API documentation](https://docs.searxng.org/dev/search_api.html).

Use the existing web admin settings experience for:

- The instance address and connection status.
- Search mode: **off**, **asked only**, or **conservative automatic**.
- Research-operation, result, page-content and timeout limits within the feature's hard ceilings.
- An optional supported `language` value. Do not invent a universal region parameter; any later regional controls must match the configured engines.

Search starts disabled until configured and enabled by the owner. Once enabled, conservative automatic mode is the proposed default; asked-only mode offers tighter control.

Queries should contain only the necessary public subject of the question. Do not send the chat transcript, private memory, credentials or unrelated group details to the search service.

SearXNG forwards the query to its configured upstream search engines, so self-hosting does not make private query contents local-only. Keep the instance address owner-controlled; the model supplies the query, never a provider endpoint or credentials. Use separate `web_search.*` settings, with `off` as the stored default. Enforce enablement in the tool handler as well as tool availability. Keep any deployment credentials outside ordinary prompt/lab exports and redact them in traces.

### Page fetching boundary

Page reading introduces outbound requests to untrusted destinations. Before enabling it:

- Accept only HTTP(S) source URLs returned in this run (and, if direct URL reading is supported, URLs explicitly supplied in the current request). Do not recursively follow links found in page text.
- Reject credentials in URLs and local, private, link-local, metadata and other non-public destinations. Validate resolved addresses and each redirect, and ensure the connection uses the validated destination so DNS changes cannot bypass the check.
- Treat the owner-configured SearXNG endpoint as a separate, narrowly trusted connection that may be on the private network. That exception must never apply to result-page fetching or forward provider credentials to a page.
- Bound redirects, downloaded and decompressed bytes, extracted text and total fetch time. Initially support readable HTML/plain text; skip unsupported downloads, login pages, browser-only content and extraction failures with a clear result.
- Keep page content as untrusted tool data, including instructions embedded in titles or snippets. It must not authorize another tool action or disclosure of private context.

## 7. Failure handling and visibility

If SearXNG is unavailable, rejects the request, returns no useful results or exceeds the time budget, finish promptly with a short explanation. Use partial evidence when it helps, clearly identify its limits, and avoid presenting stale model knowledge as current verification.

If search is disabled and the user asks for it, say that web search is unavailable. For a stable question, a useful answer from existing knowledge may still be offered with that distinction clear.

Extend the existing agent-run view with mode, queries, returned sources, pages attempted/read, operation counts, time spent and the reason research stopped. Record observable decisions and results; do not require a separate model request to explain its reasoning. Distinguish provider failure, no results, page failure, policy refusal and budget exhaustion. Reuse existing trace retention and keep bounded excerpts, not full downloaded pages. This will help tune unnecessary searches and slow replies without adding technical detail to Telegram answers.

## 8. Delivery and acceptance

Build and tune the feature in three stages:

1. **Explicit search:** add disabled-by-default settings and a connection check; offer `web_search(query)` to eligible `banter` runs, retrieve bounded results, answer with appropriate links and handle failures. Add deterministic provider fixtures and lab isolation at this stage.
2. **Limited refinement and page reading:** allow a focused follow-up and a separate page-read tool under the shared limits, after implementing and testing the fetching boundary above.
3. **Conservative automatic use:** tune search decisions using representative questions and the actual local model, then make automatic mode the suggested choice when the owner enables the finished feature. Keep asked-only mode available.

Start with tools in `banter` so ordinary knowledge questions do not need another model request for routing. Keep chat summaries, catch-up, reminders and background jobs free of web tools. If a later mixed planning request needs web evidence, explicitly choose the eligible skill and preserve evidence through its handoff; do not repurpose the `questions` skill.

The existing [missing-action check](../naruto/agent/claims.py) covers group actions, not search. Add a search-specific check for a skipped explicit request or an unsupported claim of searching, using at most the runner's existing one re-ask when enough requests remain. A failed search attempt is already an attempt: do not repeatedly re-ask it until it succeeds. Asked-only policy and disabled-tool enforcement must not depend on prompt wording alone.

Extend the prompt lab's scenario format, sandbox, capability report and checks with fixture-backed search/page results and failures before evaluating the new tools. Lab runs must never contact the owner's SearXNG instance or the live web; a missing fixture should be reported as unsupported or failed, not fall through to the production client. Remove the deferred marker only when this support exists. Provider connectivity and Telegram rendering require separate integration checks because the lab does not cover them.

The feature is ready when:

- Explicit requests search reliably; greetings, chat-history questions and familiar knowledge questions normally avoid it.
- Freshness-dependent and genuinely unfamiliar external questions search when needed.
- Most successful searches finish after one operation, and no response exceeds the configured ceiling, including retries, multiple calls in one model response and skill changes.
- Source links match the evidence actually retrieved; snippets are not misrepresented as full-page verification.
- Failures and exhausted budgets produce a useful, honest reply rather than a research loop.
- Normal replies remain fast, and searched replies have acceptable measured latency on the owner's local model.

Cover these behaviours with mocked HTTP and scripted-model tests in the existing agent, settings, web, Telegram and lab suites, plus a focused provider test module:

- Off/unconfigured, asked-only, automatic and explicit no-browse requests; failed explicit attempts and false claims of verification.
- JSON disabled (403), rate limiting (429), malformed/oversized responses, empty/duplicate results, timeouts and partial results.
- Search then read, search then refine, exhausted limits, low overall agent limits, multiple tool calls, handoff, cancellation and expired run time.
- Source records surviving result truncation, input fitting and handoff; working links in Markdown and plain-text fallback.
- Private destinations, redirect/DNS bypass attempts, oversized/compressed pages and prompt injection without group actions or disclosure.
- Fixture-only lab execution, missing fixtures and no writes of web evidence into durable notes. Check the later memory-upkeep path too, since the bot's answer is recorded in chat history.

Record search frequency, model-request count and typical and slower response times during evaluation. Agree on concrete latency targets after measuring the deployed model; this plan does not assume a hardware-independent response time.

Deep research, recursive crawling, browser automation, bulk document ingestion and automatic collection of web knowledge remain outside the initial feature.
