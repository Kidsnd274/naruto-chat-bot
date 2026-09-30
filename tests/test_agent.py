"""The agent loop, its tools, the board, plan proposals and polls."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from telegram import Poll, PollAnswer, PollOption

import fakes
from fakes import ALICE, BOB, GROUP_ID, FakeBot, ScriptedLLM, context, message, tool_call, update
from naruto.agent.runner import (
    DEADLINE_TEXT,
    FAILURE_TEXT,
    LAST_CALL_NOTE,
    STUCK_TEXT,
    AgentRunner,
    RunRequest,
)
from naruto.db.messages import IMPORT, NewMessage
from naruto.llm import LLMClient, LLMError, request_completion, split_inline_tool_calls
from naruto.tg.access import ChatAccess
from naruto.tg.board import BoardPublisher, render_html, render_markdown
from naruto.tg.plans import PlanButtons
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


async def run_tools(services, bot, chat, *calls, trigger_text="@naruto_bot go"):
    trigger = store(services, 999, trigger_text, offset=10_000)
    llm = ScriptedLLM(list(calls), "Done.")
    runner = AgentRunner(services, bot, llm=llm)
    outcome = await runner.run(RunRequest(chat=chat, trigger=trigger, bot=services.status.bot))
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
        tool_call("get_earlier_messages", {"before_message_id": rows[2].id, "count": 5}, "b"))
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
    assert "### 🗓 Plans" in markdown and "- ☑ BBQ Sat 6pm" in markdown
    assert "- ☐ Book the pit \\(Sam\\)" not in markdown and "Book the pit (Sam)" in markdown
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
    services.boards.set_section(GROUP_ID, "decided", ["Split costs <evenly>"], actor="t")
    note = await BoardPublisher(services).publish(bot, chat)
    assert note.startswith("Sent the board, but couldn't pin it")
    sent = bot.sent[-1]
    assert sent["parse_mode"] == "HTML" and "• Split costs &lt;evenly&gt;" in sent["text"]
    board = services.boards.get(GROUP_ID)
    assert board.format == "html" and not board.pinned

    services.boards.set_section(GROUP_ID, "decided", ["Split costs"], actor="t")
    assert await BoardPublisher(services).publish(bot, chat) == "Updated the pinned board."
    assert bot.edits[-1]["parse_mode"] == "HTML"


def test_board_rendering_and_prompt(services, chat):
    board = services.boards.set_section(GROUP_ID, "plans", [{"text": "BBQ", "done": True}, "Pit"],
                                        actor="t")
    assert render_html(board, "1 Oct").splitlines()[2:] == ["<b>🗓 Plans</b>", "☑ BBQ", "☐ Pit"]
    empty = services.boards.get(-1)
    assert "Nothing on it yet" in render_markdown(empty, "1 Oct")
    assert board.as_text() == "🗓 Plans (plans)\n  ☑ BBQ\n  ☐ Pit"


async def test_board_and_open_plans_are_background(services, wired, bot, chat):
    services.boards.set_section(GROUP_ID, "questions", ["Who brings the grill?"], actor="t")
    services.plans.create(GROUP_ID, "BBQ", ["Sat 6pm"], run_id=None, proposed_for_user_id=7)
    services.llm = ScriptedLLM("Oi!")
    await say(wired, bot, message(6, "@naruto_bot status?"))
    context_block = services.llm.calls[0]["messages"][1]["content"]
    assert "## Background\nPinned board:\n❓ Open questions (questions)\n  • Who brings the grill?" \
        in context_block
    assert "- plan 1: BBQ: Sat 6pm" in context_block


# ----------------------------------------------------------------- plans

async def test_plan_is_proposed_and_confirmed_onto_the_board(services, wired, bot, chat):
    services.llm = ScriptedLLM(
        [tool_call("propose_plan", {"title": "BBQ", "items": ["Sat 6pm", "East Coast"]})],
        "Plan's up!")
    await say(wired, bot, message(6, "@naruto_bot lock it in"))
    proposal = bot.sent[-2]
    assert proposal["parse_mode"] == "HTML" and "📋 <b>Plan: BBQ</b>" in proposal["text"]
    buttons = proposal["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["plan:confirm:1", "plan:change:1"]
    assert bot.sent[-1]["text"] == "Plan's up!"
    plan = services.plans.get(1)
    stored = services.messages.get_live(GROUP_ID, plan.message_id)
    assert stored is not None and stored.from_bot

    buttons_handler = PlanButtons(services, BoardPublisher(services))
    answers = []
    query = SimpleNamespace(data="plan:confirm:1", from_user=BOB,
                            answer=lambda text=None, **kw: _record(answers, text),
                            edit_message_text=lambda text, **kw: _record(answers, text))
    await buttons_handler.on_callback(SimpleNamespace(callback_query=query), context(bot))
    assert services.plans.get(1).status == "confirmed"
    assert services.plans.get(1).decided_by_name == "Bob"
    assert [(i.text, i.done) for i in services.boards.get(GROUP_ID).items("plans")] == [
        ("BBQ: Sat 6pm; East Coast", True)]
    assert "Confirmed" in answers[0] and "✅ Confirmed by Bob" in answers[1]
    assert "✅ Confirmed by Bob" in services.messages.get_live(GROUP_ID, plan.message_id).text
    assert bot.api_calls[-1][0] == "sendRichMessage"

    await buttons_handler.on_callback(SimpleNamespace(callback_query=query), context(bot))
    assert answers[-1] == "This plan was already confirmed or replaced."


async def test_a_new_proposal_replaces_an_open_one_with_the_same_title(services, bot, chat):
    _, results = await run_tools(services, bot, chat,
                                 tool_call("propose_plan", {"title": "BBQ", "items": ["Sat"]}, "a"),
                                 tool_call("propose_plan", {"title": "bbq", "items": ["Sun"]}, "b"))
    assert "It replaces plan 1." in results[1]
    assert [p.status for p in services.plans.for_chat(GROUP_ID)] == ["proposed", "cancelled"]
    assert "Replaced by a newer plan" in bot.edits[-1]["text"]


async def test_no_tools_are_offered_when_the_limit_is_zero(services, wired, bot, chat):
    services.settings.set("agent.max_tool_calls", 0, actor="t")
    services.llm = ScriptedLLM("Hi!")
    await say(wired, bot, message(6, "@naruto_bot hi"))
    assert services.llm.calls[0]["tools"] is None


async def _record(bucket, text):
    bucket.append(text)
    return True


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
