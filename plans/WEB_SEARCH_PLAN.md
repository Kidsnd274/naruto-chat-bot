# Web search: feature plan

Status: proposed feature; not implemented by this document.

Written: 2026-10-01.

Related plan: [Bot rework plan](BOT_REWORK_PLAN.md).

## 1. Purpose

Give Naruto a web search tool for answering knowledge questions that need external information. Search results should come from an owner-configured SearXNG instance, and the bot should use them to give a short, useful answer with relevant source links.

The main priorities are:

- Search when the user asks, or when external evidence is necessary for a good answer.
- Keep ordinary conversation and familiar knowledge questions fast by answering directly.
- Limit research to a small number of searches and follow-ups, suitable for a local model.
- Be clear about what was verified and what remains uncertain.

This plan describes behaviour and scope. Tool interfaces, code structure, libraries, storage and deployment choices are left to future implementation. The project is still evolving; the implementing agent should first check the current agent loop, settings, skills and reply formatting.

## 2. Inspiration from Open WebUI

Use the same general experience: ask a question, retrieve search results, use relevant evidence, and answer with links the user can inspect.

Open WebUI supports SearXNG as a search provider. Its agentic search distinguishes titles, links and snippets returned by search from the content obtained by opening a page. These are useful boundaries for this feature: a search preview can help find an answer, but it should not be presented as though the bot read the full page. See the [Open WebUI SearXNG guide](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/web-search/providers/searxng.md) and [agentic search documentation](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/web-search/agentic-search.mdx).

For Naruto, prefer a lightweight search-and-answer workflow with limited page reading. A vector database, embedding pipeline and persistent web knowledge collection are outside the initial scope.

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
| Research rounds | Default maximum of 2; owner may increase to a hard ceiling of 3. |
| Searches | At most one SearXNG query per round, within the same 2–3 round limit. |
| Results shown to the model | About 3 useful results per query; remove duplicates. |
| Page reading | At most 2 pages across the whole response, with short relevant excerpts. |
| Waiting | Short configurable search/page timeouts and an overall research budget that leaves time to answer. |

A **research round** means an agent tool invocation for web research: a search, a page read, or a search with limited page reading bundled into its result. Separately requested page reads also consume rounds. This keeps a search-and-read sequence from growing into an unbounded tool loop. The tool arrangement is an implementation choice.

Every SearXNG request, including a retry or a query hidden inside a batch, counts against the search limit. Repeating the same unsuccessful query should not be the normal recovery strategy.

The final answer is a model request in addition to the research rounds. Search must respect the existing overall model-request, tool-call and elapsed-time limits, with capacity reserved for that answer. Skill changes and retries must share the same budget; they must not restart it. If the existing limits leave less room, the bot performs less research.

## 5. Evidence and answers

Keep Naruto's normal voice, but make the factual answer easy to understand. Personality should not obscure facts or imply confidence the evidence does not support.

- Prefer original or authoritative sources when available and relevant.
- Use the retrieved evidence to answer the question, rather than only returning a list of links.
- Include a small number of readable, Telegram-compatible source links supporting the answer.
- Cite page content only when that content was actually retrieved. If relying on snippets, describe the result as a search preview and make clear that the full page was not checked.
- Preserve source titles and URLs; never invent citations, publication dates or verification claims.
- For conflicting or incomplete evidence, explain the uncertainty briefly. One focused follow-up is appropriate if there is budget left.
- Treat web content as evidence, never as instructions to change the bot's behaviour or run other tools.

Web findings should not automatically become durable group memory or cause changes to reminders, polls, plans or the board. Those actions continue to follow the existing request rules.

## 6. SearXNG connection and owner controls

The owner supplies the SearXNG instance address. The initial feature supports this provider only, with no automatic switch to an unrelated public instance if it fails.

The instance must be reachable by the bot and permit structured JSON search results. SearXNG requires the requested output format to be enabled; this should be checked when configuring the connection. See the [SearXNG Search API documentation](https://docs.searxng.org/dev/search_api.html).

Use the existing web admin settings experience for:

- The instance address and connection status.
- Search mode: **off**, **asked only**, or **conservative automatic**.
- Research-round, result, page-content and timeout limits within the feature's hard ceilings.
- Optional language or region preferences where the instance supports them.

Search starts disabled until configured and enabled by the owner. Once enabled, conservative automatic mode is the proposed default; asked-only mode offers tighter control.

Queries should contain only the necessary public subject of the question. Do not send the chat transcript, private memory, credentials or unrelated group details to the search service.

## 7. Failure handling and visibility

If SearXNG is unavailable, rejects the request, returns no useful results or exceeds the time budget, finish promptly with a short explanation. Use partial evidence when it helps, clearly identify its limits, and avoid presenting stale model knowledge as current verification.

If search is disabled and the user asks for it, say that web search is unavailable. For a stable question, a useful answer from existing knowledge may still be offered with that distinction clear.

The existing agent-run view should make it possible to inspect why search was used, the queries, returned sources, pages read, time spent and the reason research stopped. This will help tune unnecessary searches and slow replies without adding technical detail to Telegram answers.

## 8. Delivery and acceptance

Build and tune the feature in three stages:

1. **Explicit search:** connect to SearXNG, retrieve bounded results, answer with appropriate links, and handle failures.
2. **Limited refinement and page reading:** allow a focused follow-up and small page excerpts under the shared limits.
3. **Conservative automatic use:** tune search decisions using representative questions and the actual local model.

The feature is ready when:

- Explicit requests search reliably; greetings, chat-history questions and familiar knowledge questions normally avoid it.
- Freshness-dependent and genuinely unfamiliar external questions search when needed.
- Most successful searches finish after one round, and no response exceeds the configured ceiling, including retries and skill changes.
- Source links match the evidence actually retrieved; snippets are not misrepresented as full-page verification.
- Failures and exhausted budgets produce a useful, honest reply rather than a research loop.
- Normal replies remain fast, and searched replies have acceptable measured latency on the owner's local model.

Record search frequency, model-request count and typical and slower response times during evaluation. Agree on concrete latency targets after measuring the deployed model; this plan does not assume a hardware-independent response time.

Deep research, recursive crawling, browser automation, bulk document ingestion and automatic collection of web knowledge remain outside the initial feature.
