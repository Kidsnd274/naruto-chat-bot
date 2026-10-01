"""The prompt lab's sandbox: scenarios, the production reply path on a
scenario's clock, conversations with state, commands, simulated Telegram
outcomes and outcome classes."""

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fakes import ScriptedLLM, tool_call
from naruto.lab.checks import check_tool_calls, combine, run_checks
from naruto.lab.sandbox import BOT_ID, CHAT_ID, Sandbox, run_scenario
from naruto.lab.scenario import ScenarioError, load_scenarios, parse_scenario
from naruto.llm import LLMError
from naruto.settings.registry import REGISTRY
from naruto.tg.skill_commands import command_request

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = FIXTURES / "lab_scenarios.json"
BASE = {key: setting.default for key, setting in REGISTRY.items()}
SG = ZoneInfo("Asia/Singapore")
MEMBERS = [{"id": 7, "name": "Alice", "username": "alice"}, {"id": 8, "name": "Bob"},
           {"id": 9, "name": "Wei", "aliases": ["Always Late"]}]


def scenario(**fields):
    raw = {"id": "s", "timezone": "Asia/Singapore", "members": MEMBERS,
           "time": "2026-10-03T18:00:00+08:00", **fields}
    return parse_scenario(raw, FIXTURES)


async def run(s, *script, settings=None):
    llm = ScriptedLLM(*script)
    result = await run_scenario(s, {**BASE, **(settings or {})}, llm_factory=lambda _: llm)
    return result, llm


def current_request(run_trace) -> str:
    content = run_trace["prompt"][2]["content"]
    return content if isinstance(content, str) else content[0]["text"]


# ---------------------------------------------------------------- scenarios

def test_old_evaluation_cases_still_load():
    scenarios = {s.id: s for s in load_scenarios(SCENARIOS)}
    assert len(scenarios) == 6
    focus = scenarios["focus-direct-question"]
    assert focus.chat_title == "BBQ crew" and focus.timezone == "Asia/Singapore"
    assert len(focus.members) == 3 and focus.has_auto_checks and len(focus.turns) == 1
    assert focus.messages[0].date == 1790416800  # 2026-09-26 18:00 +08
    vision = scenarios["vision-describe"]
    assert vision.turns[0].message.image == FIXTURES / "images" / "orange.png"
    assert vision.turns[0].message.media == "photo"
    assert scenarios["character-serious-moment"].messages[1].from_bot
    assert scenarios["search-reply-to-older-message"].settings["context.recent_window"] == 2


def test_dates_ids_and_follow_up_turns():
    s = scenario(
        messages=[{"from": "Alice", "from_id": 7, "text": "a"},
                  {"from": "Bob", "from_id": 8, "text": "b"}],
        turns=[{"from": "Wei", "from_id": 9, "text": "@naruto_bot hi"},
               {"after": "2h", "messages": [{"from": "Bob", "from_id": 8, "text": "c"}],
                "from": "Alice", "from_id": 7, "reply_to_answer": True, "text": "and?"},
               {"from": "Bob", "from_id": 8, "command": "/summary today"}])
    t0 = int(datetime(2026, 10, 3, 18, 0, tzinfo=SG).timestamp())
    assert [m.date for m in s.messages] == [t0 - 120, t0 - 60]
    assert [t.date for t in s.turns] == [t0, t0 + 7200, t0 + 7260]
    assert s.turns[1].before[0].date == t0 + 7140  # a minute before the turn
    assert [m.id for m in s.all_messages] == [1, 2, 3, 4, 5, 6]
    assert s.turns[2].command == "summary" and s.turns[2].args == ["today"]
    assert s.turns[2].message.text == "/summary today"


@pytest.mark.parametrize("raw,message", [
    ({"turns": [{"from": "A", "text": "@naruto_bot x"}]}, "needs an 'id'"),
    ({"id": "a"}, r"'turns' \(or 'trigger'\) is required"),
    ({"id": "a", "category": "vibes", "trigger": {"from": "A", "text": "@naruto_bot"}},
     "unknown category"),
    ({"id": "a", "trigger": {"text": "@naruto_bot"}}, "'from' is required"),
    ({"id": "a", "trigger": {"from": "A", "reply_to": 9}}, "reply_to 9"),
    ({"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"}, "expect": {"vibes": 1}},
     "unknown expectations"),
    ({"id": "a", "trigger": {"from": "A", "date": "someday"}}, "invalid date"),
    ({"id": "a", "messages": [{"id": 1, "from": "A"}], "trigger": {"id": 1, "from": "B"}},
     "unique"),
    ({"id": "a", "trigger": {"from": "A", "text": "hello all"}}, "isn't addressed to the bot"),
    ({"id": "a", "turns": [{"from": "A", "command": "/dance"}]}, "command must be one of"),
    ({"id": "a", "turns": [{"from": "A", "text": "@naruto_bot", "after": "5m"}]},
     "'after' is for later turns"),
    ({"id": "a", "turns": [{"from": "A", "text": "@naruto_bot", "reply_to_answer": True}]},
     "no earlier answer"),
    ({"id": "a", "turns": [{"from": "A", "text": "@naruto_bot", "date": 2000},
                           {"from": "A", "text": "@naruto_bot", "date": 1000}]}, "earlier than"),
    ({"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"},
      "simulate": {"pin_message": "error"}}, "Telegram methods"),
    ({"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"}, "skill": "dance"},
     "unknown skill"),
    ({"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"},
      "expect": {"state": {"vibes": []}}}, "expect.state takes"),
    ({"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"}, "colour": "red"},
     "unknown fields"),
])
def test_invalid_scenarios(raw, message):
    with pytest.raises(ScenarioError, match=message):
        parse_scenario(raw, FIXTURES)


def test_load_scenarios_file_errors(tmp_path):
    with pytest.raises(ScenarioError, match="Could not read"):
        load_scenarios(tmp_path / "none.json")
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(ScenarioError, match="non-empty"):
        load_scenarios(empty)
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps([{"id": "a", "trigger": {"from": "A", "text": "@naruto_bot"}}] * 2))
    with pytest.raises(ScenarioError, match="unique"):
        load_scenarios(dup)


# ------------------------------------------------------------------- checks

def test_answer_checks():
    expect = {"contains_any": ["Saturday", "sat"], "contains_all": ["6pm"],
              "not_contains": ["ramen"], "regex": r"\bpit \d+", "min_chars": 5,
              "max_chars": 60, "reply_threaded": True}
    results = {r.name: r for r in run_checks(expect, "Saturday at 6pm, pit 42!", threaded=True)}
    assert all(r.passed for r in results.values())
    failing = {r.name: r for r in run_checks(expect, "Ramen time" + "!" * 60, threaded=False)}
    assert {n for n, r in failing.items() if not r.passed} == {
        "contains_any", "contains_all", "not_contains", "regex", "max_chars", "reply_threaded"}
    assert run_checks({"judge": "judge me"}, "x", False) == []


def test_tool_call_checks_tell_a_failed_action_from_a_wrong_one():
    calls = [{"name": "create_poll", "arguments": {"options": ["Saturday", "Sunday"]},
              "error": False}]
    assert check_tool_calls([{"name": "create_poll", "arguments": {"options": "saturday"}}],
                            calls)[0].passed
    assert not check_tool_calls([], calls)[0].passed
    wrong = check_tool_calls(["pin_message"], calls)[0]
    assert not wrong.passed and wrong.kind == "check"
    failed = check_tool_calls(["pin_message"], [{"name": "pin_message", "arguments": {},
                                                 "error": True, "result": "Error: refused"}])[0]
    assert not failed.passed and failed.kind == "action" and "refused" in failed.detail
    assert combine(["pass", "action_failed", "fail"]) == "action_failed"
    assert combine(["pass", "skipped"]) == "skipped"
    assert combine(["pass", "pass"]) == "pass"
    assert combine(["unjudged", "pass"]) == "pass" and combine(["unjudged"]) == "unjudged"


# ------------------------------------------------------------------ running

async def test_example_scenarios_score():
    scenarios = {s.id: s for s in load_scenarios(SCENARIOS)}
    answers = {
        "focus-direct-question": ["[REPLY] Oi! Warm up first and trust your shoes."],
        "summarize-plan": ["Alright: Saturday 6pm at East Coast pit 42. Still open: who buys "
                           "the meat?"],
        "search-reply-to-older-message": ["Her flight lands at 7:40am, so leave by 7!"],
        "character-serious-moment": ["Hey, that sounds tough. I'm here for you."],
        "vision-describe": ["Looks blue to me!"],
        "tool-create-poll": [[tool_call("create_poll", {"question": "BBQ day?",
                                                        "options": ["Saturday", "Sunday"]})],
                             "Poll's up, vote!"],
    }
    outcomes = {}
    for scenario_id, script in answers.items():
        result, llm = await run(scenarios[scenario_id], *script)
        outcomes[scenario_id] = result.outcome
        assert result.model_requests == len(llm.calls)
    assert outcomes == {"focus-direct-question": "pass", "summarize-plan": "pass",
                        "search-reply-to-older-message": "pass",
                        "character-serious-moment": "pass", "vision-describe": "fail",
                        "tool-create-poll": "pass"}


async def test_the_prompt_is_built_by_the_production_builder_on_the_scenario_clock():
    s = load_scenarios(SCENARIOS)
    reply = {x.id: x for x in s}["search-reply-to-older-message"]
    result, llm = await run(reply, "Lands at 7:40.")
    prompt = result.turns[0].run["prompt"]
    context, current = prompt[1]["content"], prompt[2]["content"]
    assert result.turns[0].run["window_size"] == 2  # the scenario's setting
    assert "My flight lands at 7:40am" not in context
    assert "It replies to this earlier message" in current and "7:40am" in current
    assert "@naruto_bot" not in current
    assert "- Wei (@weiwei), also called Always Late" in context
    assert "Now: Fri 29 May 2026, 04:30 (UTC+08:00)" in current  # a minute after the chat
    assert llm.calls[0]["info"].task == "reply"  # LabLLM turns this into task lab

    again, _ = await run(reply, "Lands at 7:40.")
    assert again.turns[0].run["prompt"] == prompt  # equivalent state, identical request


async def test_the_bot_answer_is_delivered_and_stored_like_a_live_reply():
    s = scenario(turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot hi"},
                        {"from": "Alice", "from_id": 7, "reply_to_answer": True, "text": "lol",
                         "expect": {"contains_any": ["heh"], "reply_threaded": False}}])
    result, llm = await run(s, "[REPLY] Oi Bob!", "Heh.")
    first, second = result.turns
    assert first.answer == "Oi Bob!" and first.threaded and first.delivery == "group"
    assert first.telegram[0]["method"] == "send_message"
    assert first.telegram[0]["reply_to"] == 1  # threaded to Bob's message
    context = llm.calls[1]["messages"][1]["content"]
    assert "Naruto (you)" in context and "Oi Bob!" in context
    current = llm.calls[1]["messages"][2]["content"]
    assert "replying to [" in current  # Alice replied to the bot's answer
    assert second.outcome == "pass" and result.outcome == "pass"


async def test_tool_state_carries_over_and_reminders_come_due_between_turns():
    s = scenario(turns=[
        {"from": "Bob", "from_id": 8, "text": "@naruto_bot remind us in 1 hour to bring the grill",
         "expect": {"tool_calls": ["set_reminder"],
                    "state": {"reminders": [{"text": "grill", "due": "2026-10-03 19:00"}]}}},
        {"after": "2h", "from": "Alice", "from_id": 7, "text": "@naruto_bot what's pending?",
         "expect": {"state": {"reminders": []}}},
    ])
    result, llm = await run(s, [tool_call("set_reminder", {"when": "in 1 hour",
                                                           "text": "bring the grill"})],
                            "Reminder set for 7pm!", "Nothing pending.")
    first, second = result.turns
    assert first.outcome == "pass", first.checks
    assert first.state_changes["reminders"]["added"][0]["text"] == "bring the grill"
    assert second.outcome == "pass"
    second_context = llm.calls[2]["messages"][1]["content"]
    assert "⏰ Reminder: bring the grill" in second_context  # delivered at 19:00
    assert "(19:00)" in second_context
    assert "Pending reminders" not in llm.calls[2]["messages"][2]["content"]


async def test_seeded_state_shows_in_the_prompt_and_state_checks():
    s = scenario(state={
        "digest": {"text": "The group is planning a BBQ.", "updated": "2026-10-03T12:00:00+08:00"},
        "notes": [{"text": "Wei is always late", "about": 9, "category": "running joke"},
                  "The group does a BBQ every National Day"],
        "board": {"plans": ["BBQ Sat 6pm", {"text": "Book pit 42", "done": True}],
                  "questions": ["Who brings the grill?"]},
        "reminders": [{"due": "2026-10-04 17:00", "text": "bring the grill", "by": 8}],
        "plans": [{"title": "BBQ", "items": ["Sat 6pm", "East Coast"]}],
        "history_summaries": [{"from": "2025-08-01", "to": "2025-08-31",
                               "text": "Everyone went to Bali."}],
    }, turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot status?",
               "expect": {"state": {"board": {"plans": ["BBQ"], "decided": []},
                                    "notes": ["always late"],
                                    "plans": [{"title": "BBQ", "status": "proposed"}],
                                    "pins": 0}}}])
    result, llm = await run(s, "All good.")
    request = llm.calls[0]["messages"]
    context, current = request[1]["content"], request[2]["content"]
    assert "Wei is always late" in context and "The group is planning a BBQ." in context
    assert "updated Sat 03 Oct, 12:00" in context
    assert "Summaries of earlier history: 1–31 Aug 2025 (1 periods)" in context
    assert "☐ BBQ Sat 6pm" in current and "Who brings the grill?" in current
    assert "bring the grill" in current and "plan 1: BBQ" in current
    assert result.outcome == "pass", result.turns[0].checks


async def test_commands_take_the_telegram_handlers_path():
    s = scenario(
        messages=[{"from": "Bob", "from_id": 8, "text": "earlier"}],
        turns=[{"from": "Bob", "from_id": 8, "command": "/summary today",
                "expect": {"skill": "summarize"}},
               {"from": "Alice", "from_id": 7, "command": "/remind"},
               {"from": "Bob", "from_id": 8, "command": "/catchup"}])
    result, llm = await run(s, "Here's today.", "You missed nothing.")
    summary, usage, catchup = result.turns
    tz = SG
    expected = command_request("summary", ["today"], tz=tz, now=s.turns[0].date)
    assert (summary.skill, summary.note, summary.since) == (
        "summarize", expected.note, expected.since)
    assert summary.threaded and summary.outcome == "pass"
    assert "They used /summary: summarize today's messages." in current_request(summary.run)
    assert usage.delivery == "usage" and usage.answer.startswith("Usage: /remind")
    assert len(llm.calls) == 2  # the usage reply needs no model
    assert catchup.delivery == "ephemeral" and catchup.skill == "catchup"
    assert catchup.since == s.turns[0].date + 1  # Bob last spoke with /summary
    sandbox_texts = [c.get("text") for c in catchup.telegram]
    assert "You missed nothing." not in sandbox_texts  # ephemeral: not posted, not stored


async def test_hand_over_and_the_skill_check():
    s = scenario(turns=[{"from": "Bob", "from_id": 8,
                         "text": "@naruto_bot what did we talk about today?",
                         "expect": {"skill": "summarize", "max_model_requests": 2}}])
    result, _ = await run(s, [tool_call("use_skill", {"skill": "summarize"})], "We talked BBQ.")
    turn = result.turns[0]
    assert turn.skill == "banter" and turn.final_skill == "summarize"
    assert turn.outcome == "pass" and turn.model_requests == 2


async def test_simulated_failures_become_action_failures():
    s = scenario(simulate={"pin_chat_message": "rights_error"},
                 messages=[{"from": "Alice", "from_id": 7, "text": "Address: 1 East Coast Rd"}],
                 turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot pin the address",
                         "expect": {"tool_calls": [{"name": "pin_message"}]}}])
    result, _ = await run(s, [tool_call("pin_message", {"message_id": 1})],
                          "Can't pin it, I need admin rights.")
    turn = result.turns[0]
    assert turn.outcome == "action_failed"
    assert turn.telegram[0] == {"method": "pin_chat_message", "failed": "rights_error",
                                "message_id": 1}
    assert "Not enough rights" in turn.tool_calls[0]["result"]


async def test_unsupported_features_are_skipped_not_passed():
    missing = scenario(requires=["web_search"],
                       turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot search it"}])
    result, llm = await run(missing, "x")
    assert result.outcome == "skipped" and "web_search" in result.reason and not llm.calls

    no_image = scenario(messages=[{"from": "Alice", "from_id": 7, "media": "photo"}],
                        turns=[{"from": "Bob", "from_id": 8,
                                "text": "@naruto_bot what's in Alice's photo?",
                                "expect": {"contains_any": ["dog"]}}])
    result, _ = await run(no_image, [tool_call("describe_image", {"message_id": 1})],
                          "I can't see it.")
    assert result.outcome == "skipped" and "doesn't provide" in result.reason


async def test_model_server_errors_are_infrastructure_errors():
    s = scenario(turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot hi",
                         "expect": {"contains_any": ["hi"]}},
                        {"from": "Bob", "from_id": 8, "text": "@naruto_bot again"}])
    result, _ = await run(s, LLMError("Connection refused", transient=True))
    assert result.outcome == "error" and "Connection refused" in result.reason
    assert len(result.turns) == 1  # the conversation stops


async def test_a_stuck_model_fails_rather_than_errors():
    s = scenario(turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot hi"}],
                 settings={"agent.max_model_requests": 1})
    result, _ = await run(s, [tool_call("search_chat", {"query": "x"})])
    turn = result.turns[0]
    assert turn.outcome == "fail" and turn.checks[0]["name"] == "answered"


async def test_focused_and_quiet_turns():
    focused = scenario(skill="plan", turns=[{"from": "Bob", "from_id": 8, "text": "plan it"}])
    result, llm = await run(focused, "Plan: BBQ.")
    assert result.focused and result.turns[0].kind == "focused"
    assert "consolidate the plan" in llm.calls[0]["messages"][0]["content"]  # plan's instructions

    quiet = scenario(turns=[{"from": "Bob", "from_id": 8, "text": "anyone hungry?",
                             "expect": {"answers": False}}])
    result, llm = await run(quiet, "x")
    assert result.turns[0].kind == "not_addressed" and result.outcome == "pass"
    assert not llm.calls


async def test_the_configuration_under_test_reaches_the_request():
    s = scenario(turns=[{"from": "Bob", "from_id": 8, "text": "@naruto_bot hi"}])
    sandbox = Sandbox(s, {**BASE, "persona.prompt": "You are a test persona.",
                          "skills.banter.reasoning": False},
                      llm_factory=lambda settings: ScriptedLLM("Hi."))
    try:
        await sandbox.run()
        llm = sandbox.services.llm
        assert llm.calls[0]["messages"][0]["content"].startswith("You are a test persona.")
        assert llm.calls[0]["reasoning"] is False
        assert sandbox.services.messages.count(CHAT_ID) == 2
        assert sandbox.services.messages.latest(CHAT_ID, 1)[0].sender_id == BOT_ID
    finally:
        sandbox.close()
