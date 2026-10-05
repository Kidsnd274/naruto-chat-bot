"""The agent loop, its tools, the board, plan proposals and polls."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from telegram import Poll, PollAnswer, PollOption

import fakes
from fakes import ALICE, BOB, GROUP_ID, FakeBot, ScriptedLLM, context, message, tool_call, update
from naruto.agent.context import ImageInput
from naruto.agent.runner import (
    BUSY_TEXT,
    DEADLINE_TEXT,
    FAILURE_TEXT,
    IMAGES_REFUSED_NOTE,
    LAST_CALL_NOTE,
    STUCK_TEXT,
    AgentRunner,
    RunRequest,
)
from naruto.db.board import BoardFull
from naruto.db.messages import IMPORT, NewMessage
from naruto.llm import (
    LLMClient,
    LLMError,
    RequestNotRun,
    request_completion,
    split_inline_tool_calls,
)
from naruto.tg.access import ChatAccess
from naruto.tg.board import RULE, BoardPublisher, Updated, render_html, render_markdown
from naruto.tg.plans import OLD_CARD_ANSWER, on_old_card_button
from naruto.tg.polls import PollTracker
from naruto.tg.recorder import Recorder
from naruto.tg.responder import Responder


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def wired(services, bot):
    recorder = Recorder(services)
    services.access = ChatAccess(services, bot)
    services.telegram = bot
    return SimpleNamespace(recorder=recorder, responder=Responder(services, recorder))


@pytest.fixture
def chat(services):
    services.chats.upsert_seen(GROUP_ID, title="BBQ crew")
    services.chats.set_status(GROUP_ID, "enabled")
    return services.chats.get(GROUP_ID)


async def say(wired, bot, msg):
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.on_message(update(msg), context(bot))


def tool_results(llm: ScriptedLLM, request: int) -> list[str]:
    return [m["content"] for m in llm.calls[request]["messages"] if m["role"] == "tool"]


# ------------------------------------------------------------------- LLM

def api_response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, reasoning_content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=None, model="m")


class OneShotClient:
    def __init__(self, response):
        self.response = response
        self.kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


async def test_structured_tool_calls_are_parsed():
    calls = [SimpleNamespace(id="a", function=SimpleNamespace(name="search_chat",
                                                             arguments='{"query": "bbq"}')),
             SimpleNamespace(id="b", function=SimpleNamespace(name="pin_message",
                                                             arguments="{not json"))]
    result = await request_completion(OneShotClient(api_response("", calls)),
                                      {"model": "m", "messages": []}, tools_offered=True)
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("a", "search_chat", {"query": "bbq"}), ("b", "pin_message", {})]
    assert result.tool_calls[1].error.startswith("Arguments are not valid JSON")
    assert result.tool_calls[0].as_request_part() == {
        "id": "a", "type": "function",
        "function": {"name": "search_chat", "arguments": '{"query": "bbq"}'}}


async def test_inline_qwen_tool_calls_are_parsed_only_when_tools_were_offered():
    content = ('Let me look.\n<tool_call>\n{"name": "search_chat", "arguments": '
               '{"query": "flight"}}\n</tool_call>')
    result = await request_completion(OneShotClient(api_response(content)),
                                      {"model": "m", "messages": []}, tools_offered=True)
    assert result.text == "Let me look."
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("search_chat", {"query": "flight"})]
    plain = await request_completion(OneShotClient(api_response(content)),
                                     {"model": "m", "messages": []}, tools_offered=False)
    assert plain.tool_calls == [] and "<tool_call>" in plain.text
    assert split_inline_tool_calls("<tool_call>garbage</tool_call>") == (
        "<tool_call>garbage</tool_call>", [])


async def test_tools_are_sent_with_the_request(services):
    client = LLMClient(services.settings, "key")
    services.settings.set("model.name", "m", actor="t")
    fake = OneShotClient(api_response("hi"))
    client._get_client = lambda: fake
    await client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert fake.kwargs["tools"] == [{"type": "function"}]


async def test_background_requests_wait_for_replies(services):
    client = LLMClient(services.settings, "key")
    services.settings.set("model.name", "m", actor="t")
    order = []
    release = asyncio.Event()

    class Slow:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            order.append(kwargs["messages"][0]["content"])
            if kwargs["messages"][0]["content"] == "first":
                await release.wait()
            return api_response("ok")

    client._get_client = lambda: Slow()

    def ask(text, background=False):
        return asyncio.create_task(client.chat([{"role": "user", "content": text}],
                                               background=background))

    first = ask("first")
    await asyncio.sleep(0.01)
    background = ask("digest", background=True)
    await asyncio.sleep(0.01)
    reply = ask("reply")
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(first, background, reply)
    assert order == ["first", "reply", "digest"]
    assert client.waiting == 0 and client.in_flight == 0


async def test_a_multi_slot_server_takes_requests_side_by_side(services):
    client = LLMClient(services.settings, "key")
    services.settings.set("model.name", "m", actor="t")
    services.settings.set("model.parallel_requests", 2, actor="t")
    running, most, order = 0, 0, []
    release = asyncio.Event()

    class Server:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            nonlocal running, most
            running += 1
            most = max(most, running)
            order.append(kwargs["messages"][0]["content"])
            await release.wait()
            running -= 1
            return api_response("ok")

    client._get_client = lambda: Server()

    def ask(text, background=False):
        return asyncio.create_task(client.chat([{"role": "user", "content": text}],
                                               background=background))

    tasks = [ask("chat A"), ask("chat B")]
    await asyncio.sleep(0.01)
    tasks += [ask("digest", background=True), ask("chat C")]  # both slots busy
    await asyncio.sleep(0.01)
    assert order == ["chat A", "chat B"] and client.waiting == 2
    release.set()
    await asyncio.gather(*tasks)
    assert most == 2 and order[2:] == ["chat C", "digest"]  # the reply went first


# ---------------------------------------------------------------- runner

async def test_search_then_answer_is_traced(services, wired, bot, chat):
    services.llm = ScriptedLLM([tool_call("search_chat", {"query": "flight"})],
                               "[REPLY] She lands at 7:40!")
    await say(wired, bot, message(5, "my flight lands at 7:40 on friday", sender=BOB))
    await say(wired, bot, message(6, "@naruto_bot when does Bob land?"))

    assert bot.sent[-1]["text"] == "She lands at 7:40!"
    assert bot.sent[-1]["reply_parameters"].message_id == 6
    llm = services.llm
    assert "search_chat" in llm.tool_names(0) and "create_poll" in llm.tool_names(0)
    result = tool_results(llm, 1)[0]
    assert "matching 'flight'" in result and "Bob" in result and "7:40" in result
    assistant = llm.calls[1]["messages"][-2]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "search_chat"

    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.model_requests == 2 and run.tool_calls == 1
    assert [s["type"] for s in run.steps] == ["model", "tool", "model"]
    assert run.steps[1]["name"] == "search_chat" and run.steps[1]["error"] is False
    assert run.tool_names == ["search_chat"] and run.usage == {"prompt_tokens": 100,
                                                               "completion_tokens": 10}
    assert run.reply_message_ids == [901]  # FakeBot numbers its messages from 901


async def test_every_request_fits_the_input_budget(services, wired, bot, chat):
    from naruto.agent.text import estimate_request_tokens

    services.settings.set("context.input_token_budget", 4000, actor="t")
    services.settings.set("agent.tool_result_chars", 20000, actor="t")
    services.settings.set("agent.search_results", 40, actor="t")
    for i in range(40):
        await wired.recorder.on_message(update(message(
            10 + i, f"bbq plan {i}: " + "bring charcoal and more charcoal " * 8, sender=BOB,
            offset=i)), context(bot))
    services.llm = ScriptedLLM([tool_call("search_chat", {"query": "bbq"})],
                               [tool_call("search_chat", {"query": "charcoal"})],
                               "[REPLY] Lots of charcoal.")
    await say(wired, bot, message(60, "@naruto_bot what do we bring?", offset=100))

    assert bot.sent[-1]["text"] == "Lots of charcoal."
    sizes = [estimate_request_tokens(call["messages"], call["tools"], 2048)
             for call in services.llm.calls]
    assert len(sizes) == 3 and max(sizes) <= 4000
    run = services.runs.recent()[0][0]
    fits = [s for s in run.steps if s["type"] == "fit"]
    assert fits and fits[-1]["tokens"] <= 4000
    assert fits[-1]["dropped"] > 0 or fits[-1]["shortened"] > 0
    # The current request and the tool calls with their results are all still there.
    last = services.llm.calls[-1]["messages"]
    assert "what do we bring?" in last[2]["content"]
    assert [m["role"] for m in last[3:]] == ["assistant", "tool", "assistant", "tool"]


async def test_a_request_too_large_without_history_is_not_sent(services, wired, bot, chat):
    services.settings.set("context.input_token_budget", 1000, actor="t")
    services.settings.set("persona.prompt", "You are Naruto. " * 400, actor="t")
    services.llm = ScriptedLLM("[REPLY] never")
    await say(wired, bot, message(6, "@naruto_bot hi"))
    assert services.llm.calls == []
    assert bot.sent[-1]["text"] == FAILURE_TEXT
    run = services.runs.recent()[0][0]
    assert run.status == "error" and "Input token budget" in run.error


async def test_last_request_gets_no_more_tools(services, wired, bot, chat):
    services.settings.set("agent.max_model_requests", 2, actor="t")
    services.llm = ScriptedLLM([tool_call("search_chat", {"query": "a"})],
                               [tool_call("search_chat", {"query": "b"})])
    await say(wired, bot, message(6, "@naruto_bot dig deep"))
    assert tool_results(services.llm, 1)[0].endswith(LAST_CALL_NOTE)
    assert len(services.llm.calls) == 2
    assert bot.sent[-1]["text"] == STUCK_TEXT
    run = services.runs.recent()[0][0]
    assert run.status == "error" and run.tool_calls == 1


async def test_a_hand_over_does_not_use_up_a_request(services, wired, bot, chat):
    services.settings.set("agent.max_model_requests", 2, actor="t")
    services.llm = ScriptedLLM([tool_call("use_skill", {"skill": "plan"})],
                               [tool_call("search_chat", {"query": "friday"})],
                               "Friday it is.")
    await say(wired, bot, message(6, "@naruto_bot what's the plan for friday?"))
    assert len(services.llm.calls) == 3
    assert tool_results(services.llm, 2)[0].endswith(LAST_CALL_NOTE)
    assert bot.sent[-1]["text"] == "Friday it is."
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.skill == "plan" and run.model_requests == 3


async def test_a_post_on_the_last_request_still_runs(services, wired, bot, chat):
    services.settings.set("agent.max_model_requests", 2, actor="t")
    services.llm = ScriptedLLM(
        [tool_call("search_chat", {"query": "friday"}, "c1")],
        [tool_call("update_board", {"section": "plans", "items": [{"text": "Poker Fri"}]}, "c2"),
         tool_call("search_chat", {"query": "venue"}, "c3")])
    await say(wired, bot, message(6, "@naruto_bot sum up friday"))
    assert len(services.llm.calls) == 2
    assert [i.text for i in services.boards.get(GROUP_ID).items("plans")] == ["Poker Fri"]
    assert STUCK_TEXT not in [sent["text"] for sent in bot.sent]
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.tool_calls == 2  # the second search didn't run


async def test_a_failed_post_on_the_last_request_is_stuck(services, wired, bot, chat):
    services.settings.set("agent.max_model_requests", 1, actor="t")
    services.llm = ScriptedLLM(
        [tool_call("update_board", {"section": "gossip", "items": ["Poker Fri"]})])
    await say(wired, bot, message(6, "@naruto_bot sum up friday"))
    assert bot.sent[-1]["text"] == STUCK_TEXT
    run = services.runs.recent()[0][0]
    assert run.status == "error" and run.tool_steps[0]["error"]


async def test_tool_call_limit(services, wired, bot, chat):
    services.settings.set("agent.max_tool_calls", 1, actor="t")
    services.llm = ScriptedLLM([tool_call("search_chat", {"query": "a"}, "c1"),
                                tool_call("search_chat", {"query": "b"}, "c2")], "Done.")
    await say(wired, bot, message(6, "@naruto_bot look twice"))
    results = tool_results(services.llm, 1)
    assert results[1].startswith("Not run: the tool call limit")
    assert results[1].endswith(LAST_CALL_NOTE)
    assert bot.sent[-1]["text"] == "Done."


async def test_bad_calls_are_reported_to_the_model(services, wired, bot, chat):
    services.llm = ScriptedLLM([tool_call("launch_rockets", {}, "c1"),
                                tool_call("get_messages_around", "{oops", "c2"),
                                tool_call("get_messages_around", {"message_id": 999}, "c3"),
                                tool_call("search_chat", {}, "c4")], "Sorry!")
    await say(wired, bot, message(6, "@naruto_bot hm"))
    results = tool_results(services.llm, 1)
    assert results[0].startswith("Unknown tool 'launch_rockets'")
    assert results[1].startswith("Invalid arguments")
    assert results[2] == "Error: There is no message 999 in this chat."
    assert results[3] == "Give a query, a person or a date to search for."
    run = services.runs.recent()[0][0]
    assert [s.get("error") for s in run.tool_steps] == [True, True, True, False]


async def test_model_failure_and_deadline(services, wired, bot, chat):
    services.llm = ScriptedLLM(LLMError("APIConnectionError"))
    await say(wired, bot, message(6, "@naruto_bot hi"))
    assert bot.sent[-1]["text"] == FAILURE_TEXT and bot.sent[-1]["reply_parameters"] is None

    class Hanging(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            await asyncio.sleep(5)

    services.llm = Hanging()
    services.settings._values["agent.deadline_seconds"] = 0.05  # below the allowed minimum
    await say(wired, bot, message(7, "@naruto_bot hello?"))
    assert bot.sent[-1]["text"] == DEADLINE_TEXT
    run = services.runs.recent()[0][0]
    assert run.status == "error" and run.error == "Deadline reached"


async def test_requests_the_queue_never_ran(services, wired, bot, chat):
    services.llm = ScriptedLLM(RequestNotRun("cancelled", "Cancelled from the queue page."))
    await say(wired, bot, message(6, "@naruto_bot hi"))
    assert bot.sent == []  # the owner cancelled it: nothing is posted
    run = services.runs.recent()[0][0]
    assert run.status == "error" and run.error.startswith("Not sent to the model")

    services.llm = ScriptedLLM(RequestNotRun("busy", "Too many requests are waiting."))
    await say(wired, bot, message(7, "@naruto_bot hi again"))
    assert bot.sent[-1]["text"] == BUSY_TEXT


async def test_replies_say_what_they_are_for(services, wired, bot, chat):
    services.llm = ScriptedLLM("Oi!")
    await say(wired, bot, message(6, "@naruto_bot hi"))
    info = services.llm.calls[0]["info"]
    run = services.runs.recent()[0][0]
    assert (info.task, info.chat_id, info.run_id) == ("reply", GROUP_ID, run.id)
    assert info.still_wanted() is True
    services.chats.set_status(GROUP_ID, "disabled")
    assert info.still_wanted() is False


async def test_runs_in_one_chat_take_turns(services, wired, bot, chat):
    active = maximum = 0

    class Counting(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return await super().chat(messages, **kwargs)

    services.llm = Counting([tool_call("search_chat", {"query": "x"})], "ok")
    first, second = message(6, "@naruto_bot one"), message(7, "@naruto_bot two")
    for msg in (first, second):
        await wired.recorder.on_message(update(msg), context(bot))
    await asyncio.gather(wired.responder.on_message(update(first), context(bot)),
                         wired.responder.on_message(update(second), context(bot)))
    assert maximum == 1 and len(bot.sent) == 2


async def test_a_queued_reply_is_dropped_when_the_chat_is_disabled(services, wired, bot, chat):
    services.llm = ScriptedLLM("Oi!")
    msg = message(6, "@naruto_bot you there?")
    await wired.recorder.on_message(update(msg), context(bot))
    lock = wired.responder.chat_lock(GROUP_ID)
    await lock.acquire()  # another run is answering in this chat
    queued = asyncio.create_task(wired.responder.on_message(update(msg), context(bot)))
    await asyncio.sleep(0.01)
    services.chats.set_status(GROUP_ID, "disabled")
    lock.release()
    await queued
    assert services.llm.calls == [] and bot.sent == []


async def test_an_answer_is_not_sent_if_the_chat_was_disabled_meanwhile(services, wired, bot,
                                                                         chat):
    class DisablingLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            services.chats.set_status(GROUP_ID, "disabled")  # the owner, mid-run
            return await super().chat(messages, **kwargs)

    services.llm = DisablingLLM("Oi!")
    await say(wired, bot, message(6, "@naruto_bot you there?"))
    assert len(services.llm.calls) == 1 and bot.sent == []
    run = services.runs.recent()[0][0]
    assert run.error == "Not sent: the chat was disabled while it ran."


async def test_no_tool_runs_after_the_chat_is_disabled(services, wired, bot, chat):
    class DisablingLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            services.chats.set_status(GROUP_ID, "disabled")  # the owner, mid-request
            return await super().chat(messages, **kwargs)

    services.llm = DisablingLLM(
        [tool_call("create_poll", {"question": "BBQ day?", "options": ["Sat", "Sun"]})],
        "Vote!")
    await say(wired, bot, message(6, "@naruto_bot poll it"))
    assert bot.polls == [] and bot.sent == []
    assert len(services.llm.calls) == 1  # stopped before the tool, no second request
    run = services.runs.recent()[0][0]
    assert run.status == "error" and run.error == "Stopped: the chat was disabled while it ran."
    assert run.tool_calls == 0


# ----------------------------------------------------------- read tools

def store(services, message_id, text, *, sender=(7, "Alice"), offset=0, source="live"):
    user_id, name = sender
    services.members.upsert_live(GROUP_ID, user_id, name, name.lower())
    record = NewMessage(chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source=source,
                        message_id=message_id, sender_id=user_id, sender_name=name,
                        date=fakes.T0 + offset, text=text,
                        import_id=1 if source == IMPORT else None)
    if source == IMPORT:
        services.messages.insert_imported([record])
        row_id = services.db.scalar("SELECT id FROM messages WHERE source = 'import' "
                                    "AND message_id = ?", (message_id,))
        return services.messages.get(row_id)
    return services.messages.insert_live(record)


async def run_tools(services, bot, chat, *calls, trigger_text="@naruto_bot go", skill="banter"):
    trigger = store(services, 999, trigger_text, offset=10_000)
    llm = ScriptedLLM(list(calls), "Done.")
    runner = AgentRunner(services, bot, llm=llm)
    outcome = await runner.run(RunRequest(chat=chat, trigger=trigger, bot=services.status.bot,
                                          skill=skill))
    return outcome, tool_results(llm, 1)


async def test_search_by_person_and_date(services, bot, chat):
    store(services, 1, "bbq at east coast?", offset=0)
    store(services, 2, "bbq sounds good", sender=(8, "Bob"), offset=86400 * 3)
    store(services, 3, "who brings the bbq grill", sender=(8, "Bob"), offset=86400 * 5)
    _, results = await run_tools(
        services, bot, chat,
        tool_call("search_chat", {"query": "bbq", "from_person": "bob"}, "a"),
        tool_call("search_chat", {"query": "bbq", "since": "2026-06-01"}, "b"),
        tool_call("search_chat", {"from_person": "Zed"}, "c"),
        tool_call("search_chat", {"from_person": "me"}, "d"))
    assert "2 messages matching 'bbq' from Bob" in results[0]
    assert "east coast" not in results[0]
    assert "east coast" not in results[1] and "grill" in results[1]
    assert results[2] == "Error: Nobody in this chat is called 'Zed'."
    assert "east coast" in results[3] and "Bob" not in results[3]


async def test_messages_around_and_earlier(services, bot, chat):
    rows = [store(services, i, f"message {i}", offset=i * 60) for i in range(1, 8)]
    _, results = await run_tools(
        services, bot, chat,
        tool_call("get_messages_around", {"message_id": rows[3].id, "before": 1, "after": 1}, "a"),
        tool_call("get_earlier_messages", {"before_message_id": rows[2].id, "count": 5}, "b"),
        skill="summarize")
    assert [line.split("]")[0] for line in results[0].splitlines()] == [
        f"[{rows[2].id}", f"[{rows[3].id}", f"[{rows[4].id}"]
    assert [line.split(": ", 1)[1] for line in results[1].splitlines()[1:]] == [
        "message 1", "message 2"]


# ----------------------------------------------------------------- board

async def test_board_is_sent_pinned_then_edited_in_place(services, bot, chat):
    items = [{"text": "BBQ Sat 6pm", "done": True}, {"text": "Book the pit (Sam)"}]
    _, results = await run_tools(services, bot, chat,
                                 tool_call("update_board", {"section": "plans", "items": items}))
    assert "Sent and pinned the board." in results[0]
    endpoint, payload = bot.api_calls[0]
    assert endpoint == "sendRichMessage"
    markdown = payload["rich_message"]["markdown"]
    assert markdown.startswith("📌 **BBQ Sat 6pm · Book the pit (Sam)**")
    assert "==**🗓 Plans**==" in markdown and "✅ **BBQ Sat 6pm**" in markdown
    assert "⏳ **Book the pit (Sam)**" in markdown
    assert '<footer><sub>updated <tg-time unix="' in markdown
    board = services.boards.get(GROUP_ID)
    assert board.format == "rich" and board.pinned and bot.pins == [(GROUP_ID, board.message_id)]

    services.boards.set_section(GROUP_ID, "questions", ["Who brings the grill?"], actor="t")
    note = await BoardPublisher(services).publish(bot, chat)
    assert note == "Updated the pinned board."
    assert bot.api_calls[-1][0] == "editMessageText"
    assert "Who brings the grill?" in bot.api_calls[-1][1]["rich_message"]["markdown"]
    assert len(bot.pins) == 1


async def test_board_falls_back_to_html_and_reports_missing_pin_right(services, bot, chat):
    bot.fail_rich = True
    bot.fail_pin = True
    services.boards.set_section(GROUP_ID, "questions", ["Split costs <evenly>?"], actor="t")
    note = await BoardPublisher(services).publish(bot, chat)
    assert note.startswith("Sent the board, but couldn't pin it")
    sent = bot.sent[-1]
    assert sent["parse_mode"] == "HTML" and "• Split costs &lt;evenly&gt;?" in sent["text"]
    board = services.boards.get(GROUP_ID)
    assert board.format == "html" and not board.pinned

    services.boards.set_section(GROUP_ID, "questions", ["Split costs?"], actor="t")
    assert await BoardPublisher(services).publish(bot, chat) == "Updated the pinned board."
    assert bot.edits[-1]["parse_mode"] == "HTML"


UPDATED = Updated(1_790_000_000, "22 Sep, 18:13")
TIME = '<tg-time unix="1790000000" format="r">22 Sep, 18:13</tg-time>'


def test_board_rendering_and_prompt(services, chat):
    board = services.boards.set_section(
        GROUP_ID, "plans", [{"text": "BBQ", "done": True, "details": ["Sat 6pm"]}, "Pit"],
        actor="t")
    assert render_html(board, UPDATED).splitlines() == [
        "📌 <b>BBQ · Pit</b>", "", RULE, "<b>🗓 Plans</b>", "", "✅ <b>BBQ</b>",
        "\u2003◦ Sat 6pm", "", "⏳ <b>Pit</b>", "", f"<i>updated {TIME}</i>"]
    empty = services.boards.get(-1)
    assert render_markdown(empty, UPDATED).splitlines()[:3] == [
        "📌 **Board**", "", "Nothing on it yet. Ask me to add plans or open questions."]
    assert board.as_text() == "🗓 Plans (plans)\n  ☑ BBQ\n     - Sat 6pm\n  ☐ Pit"


def test_rich_board_layout(services, chat):
    services.boards.set_section(GROUP_ID, "plans", [
        {"text": "Fri · Dinner + poker", "details": ["Venue TBC", "$10 buy-in"]},
        {"text": "Sat · BBQ", "done": True}], actor="t", title="📌 Fri dinner + poker · Sat BBQ")
    board = services.boards.set_section(GROUP_ID, "questions", [
        {"text": "Driving or drinking?", "for_name": "Bob", "for_user_id": BOB.id},
        {"text": "Venue?", "for_name": "Somebody new"}, "Time?"], actor="t")
    assert render_markdown(board, UPDATED).split("\n\n") == [
        "📌 **Fri dinner + poker · Sat BBQ**",
        f"{RULE}<br>==**🗓 Plans**==",
        "⏳ **Fri · Dinner + poker**<br>\u2003◦ Venue TBC<br>\u2003◦ \\$10 buy-in",
        "✅ **Sat · BBQ**",
        f"{RULE}<br>==**❓ Open questions**==",
        f"• [Bob](tg://user?id={BOB.id}): Driving or drinking?<br>• Somebody new: Venue?<br>"
        "• Time?",
        f"<footer><sub>updated {TIME}</sub></footer>"]
    assert board.as_text().splitlines()[0] == "Title: Fri dinner + poker · Sat BBQ"
    assert "  • Driving or drinking? (for Bob)" in board.as_text()


async def test_update_board_takes_a_title_and_plan_details(services, bot, chat):
    plans = [{"text": "Sat · BBQ", "done": True, "details": ["6pm", "Pit 42"]}]
    _, results = await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "plans", "title": "BBQ weekend", "items": plans}))
    board = services.boards.get(GROUP_ID)
    assert board.title == "BBQ weekend" and board.items("plans")[0].details == ["6pm", "Pit 42"]
    assert "pass title" not in results[0]
    _, results = await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "plans", "items": [*plans, {"text": "Sun · Brunch"}]}))
    assert services.boards.get(GROUP_ID).title is None and "pass title" in results[0]


async def test_a_question_for_someone_mentions_and_pings_them(services, bot, chat):
    store(services, 1, "i might drive", sender=(8, "Bob"), offset=0)
    questions = [{"text": "Driving or drinking?", "for": "bob"}, {"text": "Venue?"}]
    _, results = await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "questions", "items": questions}))
    asked = services.boards.get(GROUP_ID).items("questions")[0]
    assert (asked.for_name, asked.for_user_id) == ("Bob", 8)
    board = services.boards.get(GROUP_ID)
    ping = bot.sent[-1]
    assert ping["text"] == '❓ <a href="tg://user?id=8">Bob</a>: Driving or drinking?'
    assert ping["parse_mode"] == "HTML"
    assert ping["reply_parameters"].message_id == board.message_id
    assert "Sent Bob a message mentioning them" in results[0]

    # Already asked: no second ping. Someone unknown: no mention, and the model is told.
    sent = len(bot.sent)
    _, results = await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "questions",
                         "items": [*questions, {"text": "Cake?", "for": "Zed"}]}))
    assert len(bot.sent) == sent and "'Zed' matches nobody" in results[0]
    assert services.boards.get(GROUP_ID).items("questions")[2].for_name == "Zed"


async def test_question_pings_can_be_turned_off(services, bot, chat):
    services.settings.set("board.ping_questions", False, actor="t")
    store(services, 1, "hi", sender=(8, "Bob"), offset=0)
    await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "questions", "items": [{"text": "Drinks?", "for": "Bob"}]}))
    assert services.boards.get(GROUP_ID).items("questions")[0].for_user_id == 8
    assert bot.sent == []


async def test_the_board_comes_with_the_request(services, wired, bot, chat):
    """It changes whenever the bot acts, so it sits next to the current
    request rather than before the transcript (the server's prompt cache)."""
    services.boards.set_section(GROUP_ID, "questions", ["Who brings the grill?"], actor="t")
    services.plans.create(GROUP_ID, "BBQ", ["Sat 6pm"], run_id=None, proposed_for_user_id=7)
    services.llm = ScriptedLLM("Oi!")
    await say(wired, bot, message(6, "@naruto_bot status?"))
    _, context_block, current = (m["content"] for m in services.llm.calls[0]["messages"])
    assert current.startswith("## Board, plans and reminders\nWhat you keep for the group")
    assert "\n\nPinned board:\n❓ Open questions (questions)\n  • Who brings the grill?" \
        in current
    assert current.index("Pinned board") < current.index("## Current request")
    assert "Pinned board" not in context_block and "## Background" not in context_block
    assert "plan 1" not in current and "proposed" not in current  # old plan cards aren't shown


# ----------------------------------------------------------------- plans

async def test_plans_are_kept_on_the_board_not_posted_as_cards(services, wired, bot, chat):
    """A synthetic version of the chat in plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md
    §3: one board entry for the plan and at most one short reply, no card."""
    plan = {"section": "plans", "title": "Dinner at 8",
            "items": [{"text": "Dinner tonight", "done": False,
                       "details": ["8pm", "Sam fetches people"]}]}
    services.llm = ScriptedLLM([tool_call("update_board", plan)], "Board's updated.")
    text = "@naruto_bot can u summarize the plan. it's at 8pm and Sam will be fetching ppl"
    msg = message(6, text)
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.respond(bot, chat, msg, services.messages.get_live(GROUP_ID, 6),
                                  services.status.bot, skill="plan")

    assert "propose_plan" not in services.llm.tool_names(0)
    assert [s["text"] for s in bot.sent] == ["Board's updated."]  # one short reply
    assert all(s["reply_markup"] is None for s in bot.sent)  # no buttons anywhere
    assert [(i.text, i.done, i.details) for i in services.boards.get(GROUP_ID).items("plans")] \
        == [("Dinner tonight", False, ["8pm", "Sam fetches people"])]

    # Once the group says it's settled, the same entry is ticked, not added again.
    plan["items"][0]["done"] = True
    services.llm = ScriptedLLM([tool_call("update_board", plan)], "[NO REPLY]")
    await say(wired, bot, message(7, "@naruto_bot ok it's settled", offset=60))
    assert [(i.text, i.done) for i in services.boards.get(GROUP_ID).items("plans")] == \
        [("Dinner tonight", True)]
    assert len(bot.sent) == 1 and services.plans.for_chat(GROUP_ID) == []


def test_propose_plan_is_gone_from_every_skill_and_the_lab():
    from naruto.agent.skills import SKILLS
    from naruto.agent.tools import default_registry
    from naruto.lab.capabilities import SIMULATED
    assert all("propose_plan" not in skill.tools for skill in SKILLS.values())
    assert "propose_plan" not in default_registry().tools and "propose_plan" not in SIMULATED


async def test_an_old_plan_card_button_says_plans_are_on_the_board(services, bot, chat):
    plan = services.plans.create(GROUP_ID, "BBQ", ["Sat"], run_id=None, proposed_for_user_id=None)
    calls = []

    async def answer(text=None, **kwargs):
        calls.append(("answer", text))

    async def edit_markup(reply_markup="not given", **kwargs):
        calls.append(("buttons", reply_markup))

    async def edit_text(*args, **kwargs):
        calls.append(("text", args))

    query = SimpleNamespace(data=f"plan:confirm:{plan.id}", from_user=BOB, answer=answer,
                            edit_message_reply_markup=edit_markup, edit_message_text=edit_text)
    await on_old_card_button(SimpleNamespace(callback_query=query), context(bot))
    assert calls == [("answer", OLD_CARD_ANSWER), ("buttons", None)]  # the text stays
    assert OLD_CARD_ANSWER == "Plans are kept on the board now."
    assert services.plans.get(plan.id).status == "proposed"  # nothing is confirmed
    assert services.boards.get(GROUP_ID).is_empty and bot.api_calls == []


async def test_no_tools_are_offered_when_the_limit_is_zero(services, wired, bot, chat):
    services.settings.set("agent.max_tool_calls", 0, actor="t")
    services.llm = ScriptedLLM("Hi!")
    await say(wired, bot, message(6, "@naruto_bot hi"))
    assert services.llm.calls[0]["tools"] is None


async def test_the_board_stays_within_one_telegram_message(services, bot, chat):
    long_items = [f"{i:02d} " + "x" * 287 for i in range(20)]  # ~6,000 characters
    with pytest.raises(BoardFull):
        services.boards.set_section(GROUP_ID, "plans", long_items, actor="t")
    with pytest.raises(BoardFull):
        services.boards.set_section(GROUP_ID, "plans", [f"p{i}" for i in range(26)], actor="t")
    assert services.boards.get(GROUP_ID).is_empty  # nothing half-saved

    _, results = await run_tools(services, bot, chat, tool_call(
        "update_board", {"section": "questions", "items": [{"text": t} for t in long_items]}))
    assert results[0].startswith("Error: Not changed: The board would be too long")
    assert bot.api_calls == [] and bot.sent == []

    # A board saved before the limit existed is cut to fit when published.
    services.db.execute(
        "INSERT INTO boards (chat_id, sections, updated_at, updated_by) VALUES (?, ?, 0, 't')",
        (GROUP_ID, json.dumps({"plans": [{"text": t} for t in long_items]})))
    await BoardPublisher(services).publish(bot, chat)
    markdown = bot.api_calls[-1][1]["rich_message"]["markdown"]
    assert len(markdown) <= 4096 and "more (too long to show" in markdown
    bot.fail_rich = True
    await BoardPublisher(services).publish(bot, chat, fresh=True)
    assert len(bot.sent[-1]["text"]) <= 4096


# ------------------------------------------------------------------ polls

async def test_poll_is_created_recorded_and_counted(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        [tool_call("create_poll", {"question": "BBQ day?", "options": ["Sat", "Sun", "sat"]})],
        "Vote!")
    await say(wired, bot, message(6, "@naruto_bot poll it"))
    assert bot.polls[0]["options"] == ["Sat", "Sun"] and bot.polls[0]["is_anonymous"] is False
    stored = services.messages.search(GROUP_ID, None, limit=10)
    poll_row = next(m for m in stored if m.media_kind == "poll")
    assert poll_row.media_meta["options"] == ["Sat", "Sun"]

    tracker = PollTracker(services)
    poll_id = poll_row.media_meta["poll_id"]
    poll = Poll(id=poll_id, question="BBQ day?",
                options=[PollOption("Sat", 2, persistent_id="o0"),
                         PollOption("Sun", 1, persistent_id="o1")],
                total_voter_count=3, is_closed=False, is_anonymous=False, type=Poll.REGULAR,
                allows_multiple_answers=False, allows_revoting=True, members_only=False)
    await tracker.on_poll(SimpleNamespace(poll=poll), context(bot))
    await tracker.on_poll_answer(SimpleNamespace(poll_answer=PollAnswer(poll_id, [0], user=BOB,
                                                                          option_persistent_ids=["o0"])),
                                 context(bot))
    row = services.messages.get(poll_row.id)
    assert row.media_meta["counts"] == [2, 1] and row.media_meta["votes"] == {"8": [0]}

    services.llm = ScriptedLLM("Sat wins.")
    await say(wired, bot, message(7, "@naruto_bot results?", offset=120))
    transcript = services.llm.calls[0]["messages"][1]["content"]
    assert "[poll: BBQ day? — votes: Sat 2, Sun 1] (voted: Bob → Sat)" in transcript


async def test_poll_needs_two_distinct_options(services, bot, chat):
    _, results = await run_tools(services, bot, chat, tool_call(
        "create_poll", {"question": "Q?", "options": ["A", "a"]}))
    assert results[0] == "Error: A poll needs at least two different options."
    assert bot.polls == []


# ------------------------------------------------------------------- pins

async def test_pins_only_live_messages(services, bot, chat):
    live = store(services, 50, "the address is 12 Main St")
    imported = store(services, 60, "old news", source=IMPORT, offset=-86400)
    _, results = await run_tools(services, bot, chat,
                                 tool_call("pin_message", {"message_id": live.id}, "a"),
                                 tool_call("pin_message", {"message_id": imported.id}, "b"),
                                 tool_call("unpin_message", {"message_id": live.id}, "c"))
    assert results[0] == f"Pinned message {live.id}."
    assert "imported history" in results[1]
    assert bot.pins == [(GROUP_ID, 50)] and bot.unpins == [(GROUP_ID, 50)]


# ------------------------------------------------- deleting own messages

def bot_said(services, message_id, text, *, offset=0):
    """A message the bot sent, as record_sent stores it."""
    return services.messages.insert_live(NewMessage(
        chat_id=GROUP_ID, origin_chat_id=GROUP_ID, source="live", message_id=message_id,
        sender_id=fakes.BOT_ID, sender_name="Naruto", from_bot=True, date=fakes.T0 + offset,
        text=text))


@pytest.fixture
def soon_after(services):
    """The clock shortly after the stored messages (deleting needs < 48 hours)."""
    services.db._clock = lambda: fakes.T0 + 20_000


async def test_cleanup_request_deletes_the_bots_own_messages(services, wired, bot, chat,
                                                             soon_after):
    """The chat in plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md §3, step 5."""
    store(services, 10, "can u summarize the plan. it's at 8pm and Sam will be fetching ppl")
    long_reply = bot_said(services, 11, "The plan is up with Confirm/Change buttons…", offset=5)
    card = bot_said(services, 12, "📋 Plan: Dinner\n• 8pm\n• Sam fetches people", offset=6)
    services.llm = ScriptedLLM(
        [tool_call("delete_messages", {"message_ids": [long_reply.id, card.id]})], "[NO REPLY]")
    await say(wired, bot, message(20, "@naruto_bot delete ur unnecessary msgs", offset=60))

    assert bot.deleted == [(GROUP_ID, 11), (GROUP_ID, 12)]
    assert bot.sent == []  # no message about the cleanup
    result = tool_results(services.llm, 1)[0]
    assert result.startswith(f"Deleted [{long_reply.id}], [{card.id}].")
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.error is None

    # The next request no longer sees them.
    services.llm = ScriptedLLM("Sure.")
    await say(wired, bot, message(21, "@naruto_bot thanks", offset=120))
    transcript = services.llm.calls[0]["messages"][1]["content"]
    assert "Confirm/Change" not in transcript and "can u summarize" in transcript


async def test_deleting_anyone_elses_message_is_refused_and_says_why(services, bot, chat,
                                                                     soon_after):
    theirs = store(services, 30, "lol")
    mine = bot_said(services, 31, "Heh.")
    outcome, results = await run_tools(
        services, bot, chat,
        tool_call("delete_messages", {"message_ids": [mine.id, theirs.id]}, "a"),
        tool_call("delete_messages", {"message_ids": [999_999]}, "b"))
    assert results[0].startswith("Error: Nothing was deleted.")
    assert f"[{theirs.id}] can't be deleted: Alice sent it" in results[0]
    assert f"[{mine.id}]" not in results[0]  # not tried: the batch stopped
    assert "no such message in this chat" in results[1]
    assert bot.deleted == [] and outcome.actions == []


async def test_a_claimed_inability_to_delete_is_asked_again(services, wired, bot, chat,
                                                            soon_after):
    """Seen live: "I can't delete my own messages from here though" with no
    tool to try."""
    reply = bot_said(services, 11, "The plan is up…", offset=5)
    services.llm = ScriptedLLM(
        "Board's cleaned up! I can't delete my own messages from here though.",
        [tool_call("delete_messages", {"message_ids": [reply.id]})], "[NO REPLY]")
    await say(wired, bot, message(20, "@naruto_bot delete ur unnecessary msgs", offset=60))
    assert bot.deleted == [(GROUP_ID, 11)] and bot.sent == []
    run = services.runs.recent()[0][0]
    assert [s["type"] for s in run.steps] == ["model", "check", "model", "tool", "model"]
    assert "delete_messages" in run.steps[1]["note"]


async def test_delete_messages_is_offered_where_cleanup_requests_arrive(services):
    from naruto.agent.skills import SKILLS
    offered = {name for name, skill in SKILLS.items() if "delete_messages" in skill.tools}
    assert offered == {"banter", "plan", "decide", "questions"}


# ------------------------------------------------------------ migrations

def test_board_and_plans_move_with_a_group_upgrade(services, chat):
    services.boards.set_section(GROUP_ID, "plans", ["BBQ"], actor="t")
    services.boards.set_message(GROUP_ID, message_id=5, message_chat_id=GROUP_ID, format="rich",
                                pinned=True)
    services.plans.create(GROUP_ID, "BBQ", [], run_id=None, proposed_for_user_id=None)
    services.chats.migrate(GROUP_ID, -1004001)
    board = services.boards.get(-1004001)
    assert [i.text for i in board.items("plans")] == ["BBQ"]
    assert board.message_chat_id == GROUP_ID  # the old message: a new one gets sent
    assert services.plans.for_chat(-1004001)[0].title == "BBQ"
    assert json.loads(json.dumps(board.sections["plans"][0].as_dict())) == {"text": "BBQ",
                                                                             "done": False}


# ------------------------------------------------- internal JSON in replies

DIGEST_SHAPE = '```json\n{"digest": "", "notes": []}\n```'


async def test_internal_json_answer_is_asked_again_not_posted(services, wired, bot, chat):
    services.llm = ScriptedLLM(DIGEST_SHAPE, "[REPLY] Your first message was hi!")
    await say(wired, bot, message(6, "@naruto_bot check again"))
    assert bot.sent[-1]["text"] == "Your first message was hi!"
    assert len(services.llm.calls) == 2
    assert services.llm.calls[1]["messages"] == services.llm.calls[0]["messages"]  # identical
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.model_requests == 2
    assert run.steps[-1]["purpose"].startswith("asked again")


async def test_internal_json_twice_sends_the_stuck_line(services, wired, bot, chat):
    services.llm = ScriptedLLM(DIGEST_SHAPE)
    await say(wired, bot, message(6, "@naruto_bot check again"))
    assert bot.sent[-1]["text"] == STUCK_TEXT
    assert all('"digest"' not in sent["text"] for sent in bot.sent)
    assert services.messages.search(GROUP_ID, "digest") == []  # nothing stored either
    run = services.runs.recent()[0][0]
    assert run.status == "error" and "internal JSON" in run.error


async def test_text_after_internal_json_is_kept(services, wired, bot, chat):
    services.llm = ScriptedLLM('{"digest": "", "notes": []}\n\n[REPLY] My apologies, it was hi.')
    await say(wired, bot, message(6, "@naruto_bot check again"))
    assert bot.sent[-1]["text"] == "My apologies, it was hi."
    assert bot.sent[-1]["reply_parameters"].message_id == 6
    assert len(services.llm.calls) == 1


async def test_a_chat_without_background_never_reads_about_one(services, wired, bot, chat):
    services.llm = ScriptedLLM("Oi!")
    await say(wired, bot, message(6, "@naruto_bot hi"))
    request = json.dumps(services.llm.calls[0]["messages"]).lower()
    assert "digest" not in request and "background" not in request


# ------------------------------------------- answers that skip a needed tool

async def test_claimed_reminder_without_the_tool_is_asked_again(services, wired, bot, chat):
    """Seen live: "I've set a reminder" with no set_reminder call, and the
    next requests copied that answer from the transcript."""
    services.llm = ScriptedLLM(
        "I've set a reminder for you to dance in 1 minute!",
        [tool_call("set_reminder", {"when": "in 2 minutes", "text": "Dance!"})],
        "[REPLY] Reminder set for 2 minutes from now!")
    await say(wired, bot, message(6, "@naruto_bot remind me to dance in 2 minutes"))

    assert bot.sent[-1]["text"] == "Reminder set for 2 minutes from now!"
    assert [r.text for r in services.reminders.for_chat(GROUP_ID)] == ["Dance!"]
    second = services.llm.calls[1]["messages"]
    assert second[-2] == {"role": "assistant",
                          "content": "I've set a reminder for you to dance in 1 minute!"}
    assert second[-1]["role"] == "user" and "set_reminder" in second[-1]["content"]
    run = services.runs.recent()[0][0]
    assert [s["type"] for s in run.steps] == ["model", "check", "model", "tool", "model"]
    assert run.status == "ok"


async def test_no_second_try_when_the_tool_was_called(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        [tool_call("set_reminder", {"when": "in 2 minutes", "text": "Dance!"})],
        "Reminder set for 2 minutes from now!")
    await say(wired, bot, message(6, "@naruto_bot remind me to dance in 2 minutes"))
    assert len(services.llm.calls) == 2
    assert bot.sent[-1]["text"] == "Reminder set for 2 minutes from now!"


async def test_a_clarifying_question_is_not_asked_again(services, wired, bot, chat):
    services.llm = ScriptedLLM("Sure! What time tomorrow?")
    await say(wired, bot, message(6, "@naruto_bot remind me to call mum tomorrow"))
    assert len(services.llm.calls) == 1
    assert bot.sent[-1]["text"] == "Sure! What time tomorrow?"


async def test_the_check_runs_once_and_needs_requests_left(services, wired, bot, chat):
    services.llm = ScriptedLLM("Noted, I'll remember that!")
    await say(wired, bot, message(6, "@naruto_bot remember that I'm vegetarian"))
    assert len(services.llm.calls) == 2  # asked again once, then answered anyway
    assert bot.sent[-1]["text"] == "Noted, I'll remember that!"

    services.settings.set("agent.max_model_requests", 2, actor="t")
    services.llm = ScriptedLLM("Noted, I'll remember that!")
    await say(wired, bot, message(7, "@naruto_bot remember that I'm vegetarian", offset=10))
    assert len(services.llm.calls) == 1  # no room for a call and an answer


async def test_skills_without_the_tool_are_not_checked(services, wired, bot, chat):
    services.llm = ScriptedLLM("Here's the summary: Sam set a reminder for the pit booking.")
    await wired.responder.respond(bot, chat, message(6, "/summary"),
                                  store(services, 6, "/summary"), services.status.bot,
                                  skill="summarize")
    assert len(services.llm.calls) == 1


# ------------------------------------------------------- [NO REPLY] marker

async def test_no_reply_after_a_poll_sends_nothing_more(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        [tool_call("create_poll", {"question": "Sunday or Monday?",
                                   "options": ["Sunday", "Monday"]})],
        "[NO REPLY]")
    await say(wired, bot, message(6, "@naruto_bot make a poll, sunday or monday"))
    assert len(bot.polls) == 1
    assert bot.sent == []
    assert "[NO REPLY]" in tool_results(services.llm, 1)[0]
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.response is None


async def test_no_reply_marker_is_never_posted(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        [tool_call("create_poll", {"question": "Q?", "options": ["A", "B"]})],
        "**[NO REPLY]** Vote away!")
    await say(wired, bot, message(6, "@naruto_bot poll for A or B"))
    assert bot.sent[-1]["text"] == "Vote away!"


# ------------------------------------------------ servers without vision

async def test_refused_images_are_dropped_and_asked_again(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        LLMError("BadRequestError: this server does not accept images: the engine was "
                 "started without a vision tower"),
        "I can't see images right now, sorry!")
    request = RunRequest(chat=chat, trigger=store(services, 6, "@naruto_bot what's this?"),
                         bot=services.status.bot,
                         images=[ImageInput(row_id=1, mime_type="image/png", base64="AAAA")])
    outcome = await AgentRunner(services, bot).run(request)

    assert outcome.text == "I can't see images right now, sorry!"
    first, second = services.llm.calls
    assert isinstance(first["messages"][-1]["content"], list)
    assert second["messages"][-1]["content"].endswith(IMAGES_REFUSED_NOTE)
    run = services.runs.recent()[0][0]
    assert run.status == "ok" and run.steps[0]["purpose"].startswith("refused the images")


async def test_other_model_errors_are_not_retried(services, wired, bot, chat):
    services.llm = ScriptedLLM(LLMError("APIConnectionError"), "never")
    request = RunRequest(chat=chat, trigger=store(services, 6, "@naruto_bot what's this?"),
                         bot=services.status.bot,
                         images=[ImageInput(row_id=1, mime_type="image/png", base64="AAAA")])
    outcome = await AgentRunner(services, bot).run(request)
    assert outcome.text == FAILURE_TEXT and len(services.llm.calls) == 1


# ------------------------------------------------------- reasoning effort

async def test_reasoning_effort_is_sent_only_with_reasoning(services):
    client = LLMClient(services.settings, "key")
    services.settings.set("model.name", "m", actor="t")
    fake = OneShotClient(api_response("hi"))
    client._get_client = lambda: fake
    await client.chat([{"role": "user", "content": "x"}], reasoning=True)
    assert fake.kwargs["extra_body"] == {"reasoning_effort": "low",
                                         "chat_template_kwargs": {"enable_thinking": True}}
    await client.chat([{"role": "user", "content": "x"}], reasoning=False)
    assert fake.kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    services.settings.set("model.reasoning_effort", None, actor="t")  # the server decides
    await client.chat([{"role": "user", "content": "x"}], reasoning=True)
    assert "reasoning_effort" not in fake.kwargs["extra_body"]


# ------------------------------------------------------- progress messages

class SlowLLM(ScriptedLLM):
    """Takes a moment per request, like a model reading a long chat."""

    delay = 0.15

    async def chat(self, messages, **kwargs):
        await asyncio.sleep(self.delay)
        return await super().chat(messages, **kwargs)


@pytest.fixture
def quick_progress(services, monkeypatch):
    """The progress message after 0.05 s instead of whole seconds."""
    monkeypatch.setitem(services.settings._values, "behaviour.progress_after_seconds", 0.05)


async def summarize(wired, bot, services, text="/summary"):
    msg = message(6, text)
    await wired.recorder.on_message(update(msg), context(bot))
    trigger = store(services, 6, text)
    await wired.responder.respond(bot, services.chats.get(GROUP_ID), msg, trigger,
                                  services.status.bot, skill="summarize", force_reply=True)


async def test_a_slow_summary_posts_progress_then_the_answer_and_deletes_it(
        services, wired, bot, chat, quick_progress):
    services.llm = SlowLLM("*BBQ*: Saturday, 6pm.")
    await summarize(wired, bot, services)

    placeholder, answer = bot.sent
    assert placeholder["text"] == "📖 Reading back through the chat…"
    assert placeholder["reply_parameters"].message_id == 6
    assert placeholder["disable_notification"] is True  # the answer notifies, not the wait
    assert answer["text"] == "*BBQ*: Saturday, 6pm." and answer["disable_notification"] is None
    assert answer["reply_parameters"].message_id == 6
    assert bot.deleted == [(GROUP_ID, 901)] and bot.edits == []
    assert services.messages.get_live(GROUP_ID, 901) is None  # the wait isn't in the transcript
    assert services.messages.get_live(GROUP_ID, 902).text == "*BBQ*: Saturday, 6pm."
    assert services.runs.recent()[0][0].reply_message_ids == [902]


async def test_fast_answers_and_banter_get_no_progress_message(services, wired, bot, chat,
                                                               quick_progress):
    services.llm = ScriptedLLM("Saturday!")  # answers at once
    await summarize(wired, bot, services)
    services.llm = SlowLLM("Heh.")
    await say(wired, bot, message(7, "@naruto_bot hi", offset=10))
    assert [s["text"] for s in bot.sent] == ["Saturday!", "Heh."]
    assert bot.edits == [] and bot.deleted == []


async def test_a_hand_over_to_summarize_gets_one_too(services, wired, bot, chat, quick_progress):
    services.llm = SlowLLM([tool_call("use_skill", {"skill": "summarize"})], "Here's the gist.")
    await say(wired, bot, message(6, "@naruto_bot what did we talk about?"))
    assert [s["text"] for s in bot.sent] == ["📖 Reading back through the chat…",
                                             "Here's the gist."]
    assert bot.deleted == [(GROUP_ID, 901)]


async def test_a_result_without_text_just_deletes_the_progress_message(services, wired, bot,
                                                                       chat, quick_progress):
    services.llm = SlowLLM("[NO REPLY]")
    await summarize(wired, bot, services)
    assert bot.deleted == [(GROUP_ID, 901)] and len(bot.sent) == 1

    services.llm = SlowLLM([tool_call("create_poll", {"question": "BBQ day?",
                                                       "options": ["Sat", "Sun"]})], "")
    msg = message(7, "/plan", offset=10)
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.respond(bot, chat, msg, store(services, 7, "/plan", offset=10),
                                  services.status.bot, skill="plan", force_reply=True)
    assert bot.sent[-1]["text"] == "🗓 Pulling the plan together…"  # no text after the poll
    assert len(bot.polls) == 1 and bot.deleted[-1] == (GROUP_ID, 902)  # the poll is 903


async def test_a_failed_run_turns_the_progress_message_into_the_error(services, wired, bot,
                                                                      chat, quick_progress):
    services.llm = SlowLLM(LLMError("APIConnectionError"))
    await summarize(wired, bot, services)
    assert [s["text"] for s in bot.sent] == ["📖 Reading back through the chat…"]
    assert bot.edits[-1]["message_id"] == 901 and bot.edits[-1]["text"] == FAILURE_TEXT
    assert bot.deleted == []  # the error stays
    assert services.messages.get_live(GROUP_ID, 901).text == FAILURE_TEXT

    bot.fail_edit = True  # e.g. someone deleted it meanwhile: the error comes anew
    await summarize(wired, bot, services)
    assert bot.sent[-1]["text"] == FAILURE_TEXT and bot.deleted == [(GROUP_ID, 902)]


async def test_a_crashed_run_shows_the_error_on_the_progress_message(services, wired, bot, chat,
                                                                     quick_progress):
    services.llm = SlowLLM(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await summarize(wired, bot, services)
    assert bot.edits[-1]["text"] == FAILURE_TEXT and bot.deleted == []


async def test_an_answer_that_cant_be_sent_shows_on_the_progress_message(services, wired, bot,
                                                                         chat, quick_progress):
    from naruto.tg.responder import NOT_SENT_TEXT
    bot.fail_send_texts = {"Saturday, 6pm."}
    services.llm = SlowLLM("Saturday, 6pm.")
    await summarize(wired, bot, services)
    assert bot.edits[-1]["message_id"] == 901 and bot.edits[-1]["text"] == NOT_SENT_TEXT
    assert bot.deleted == []
    assert services.runs.recent()[0][0].error.startswith("Not sent: Forbidden")


async def test_the_progress_message_goes_when_the_chat_is_disabled_mid_run(
        services, wired, bot, chat, quick_progress):
    class DisablingLLM(SlowLLM):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            services.chats.set_status(GROUP_ID, "disabled")  # the owner, mid-run
            return result

    services.llm = DisablingLLM("Saturday, 6pm.")
    await summarize(wired, bot, services)
    assert [s["text"] for s in bot.sent] == ["📖 Reading back through the chat…"]
    assert bot.deleted == [(GROUP_ID, 901)]


async def test_a_placeholder_that_cant_be_deleted_is_left(services, wired, bot, chat,
                                                          quick_progress):
    from telegram.error import BadRequest
    bot.delete_errors = [BadRequest("Message can't be deleted")]
    services.llm = SlowLLM("Saturday, 6pm.")
    await summarize(wired, bot, services)
    assert [s["text"] for s in bot.sent] == ["📖 Reading back through the chat…",
                                             "Saturday, 6pm."]  # nothing more is posted
    assert bot.deleted == [] and bot.edits == []


async def test_the_progress_message_shows_the_stages_the_run_reaches(services, wired, bot, chat,
                                                                    quick_progress, monkeypatch):
    monkeypatch.setattr("naruto.tg.progress.STAGE_EDIT_SECONDS", 0.05)
    services.llm = SlowLLM([tool_call("get_earlier_messages", {})],
                           [tool_call("search_memory", {"query": "bbq"})], "*BBQ*: Saturday.")
    await summarize(wired, bot, services)
    # Reading what it found, then writing once the next tool wasn't a read.
    assert [e["text"] for e in bot.edits] == ["📖 Reading further back…", "✍️ Writing it up…"]
    assert all(e["message_id"] == 901 for e in bot.edits)
    assert bot.sent[-1]["text"] == "*BBQ*: Saturday." and bot.deleted == [(GROUP_ID, 901)]


async def test_stage_edits_are_spaced_out(services, wired, bot, chat, quick_progress):
    # The run is over long before another edit is allowed (5 s after the post).
    services.llm = SlowLLM([tool_call("get_earlier_messages", {})], "*BBQ*: Saturday.")
    await summarize(wired, bot, services)
    assert bot.edits == [] and bot.deleted == [(GROUP_ID, 901)]


async def test_answers_in_a_forum_topic_stay_in_it(services, wired, bot, chat, quick_progress):
    services.llm = SlowLLM("word " * 1500)  # two messages long
    msg = message(6, "/summary", message_thread_id=77, is_topic_message=True)
    await wired.recorder.on_message(update(msg), context(bot))
    await wired.responder.respond(bot, chat, msg, store(services, 6, "/summary"),
                                  services.status.bot, skill="summarize", force_reply=True)
    assert len(bot.sent) == 3  # the placeholder and two parts
    assert [s["message_thread_id"] for s in bot.sent] == [77, 77, 77]
    assert bot.sent[2]["reply_parameters"] is None  # only the first part is a reply


async def test_a_reply_chain_is_not_a_topic(services, wired, bot, chat):
    services.llm = ScriptedLLM("Saturday!")
    await say(wired, bot, message(6, "@naruto_bot when?", message_thread_id=3))
    assert bot.sent[-1]["message_thread_id"] is None


async def test_progress_messages_can_be_turned_off(services, wired, bot, chat, monkeypatch):
    monkeypatch.setitem(services.settings._values, "behaviour.progress_after_seconds", 0)
    services.llm = SlowLLM("Saturday, 6pm.")
    await summarize(wired, bot, services)
    assert [s["text"] for s in bot.sent] == ["Saturday, 6pm."]
