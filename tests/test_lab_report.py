"""Judging, comparing and reporting in the prompt lab, and keeping a
discovered failure (or a real conversation) as a scenario."""

import pytest

from fakes import ScriptedLLM, tool_call
from naruto.db.messages import LIVE, NewMessage
from naruto.lab.report import render_markdown
from naruto.lab.service import LabError, LabService

MEMBERS = [{"id": 7, "name": "Alice", "username": "alice"}, {"id": 8, "name": "Bob"}]


def scenario(slug, text, expect, **extra):
    return {"id": slug, "timezone": "UTC", "time": "2026-10-03T18:00:00+00:00",
            "members": MEMBERS, "turns": [{"from": "Alice", "from_id": 7,
                                           "text": f"@naruto_bot {text}", "expect": expect}],
            **extra}


class ByPrompt(ScriptedLLM):
    """Answers by what the current request and system prompt contain."""

    def __init__(self, rules):
        super().__init__()
        self.rules = rules

    async def chat(self, messages, **kwargs):
        system = messages[0]["content"]
        current = messages[-1]["content"]
        current = current if isinstance(current, str) else current[0]["text"]
        self.calls.append({"messages": messages})
        for (persona_has, request_has), answer in self.rules.items():
            if persona_has in system and request_has in current:
                text = answer
                break
        else:
            text = "Heh."
        from naruto.llm import ChatResult
        return ChatResult(text=text, reasoning=None, model="fake", latency_ms=10,
                          usage={"prompt_tokens": 100, "completion_tokens": 5},
                          finish_reason="stop")


@pytest.fixture
def lab(services, tmp_path):
    services.settings.set("model.name", "test-model", actor="test")
    services.lab = LabService(services, tmp_path / "lab")
    return services.lab


def use(lab, llm_or_factory):
    lab.executor.llm_factory = (llm_or_factory if callable(llm_or_factory)
                                and not hasattr(llm_or_factory, "chat")
                                else (lambda settings, attempt: llm_or_factory))


async def finish(lab, batch):
    assert await lab.executor.wait(batch.id, 10)
    return lab.repo.attempts(batch_id=batch.id)


# ------------------------------------------------------------------ judging

async def test_rubrics_and_judgments(lab):
    run = lab.start_run({"objective": "Warmer replies"}, created_by="test")
    lab.add_scenario(scenario("hi", "hi", {}), created_by="test")
    use(lab, ScriptedLLM("Oi Alice, good to see you!"))
    attempt = (await finish(lab, lab.submit(run.id, scenarios=["hi"], actor="test")))[0]
    assert attempt.outcome == "unjudged"
    with pytest.raises(LabError, match="set one first"):
        lab.add_judgment(attempt.id, turn=1, kind="ai", criterion="warmth", verdict="pass",
                         evidence=["Oi Alice"], judge="claude")
    with pytest.raises(LabError, match="Only the owner confirms"):
        lab.set_rubric(run.id, [{"id": "warmth", "description": "Friendly"}],
                       status="confirmed")
    rubric = lab.set_rubric(run.id, [{"id": "Warmth", "description": "Friendly, not gushing"},
                                     {"id": "voice", "description": "Sounds like Naruto"}])
    assert [c["id"] for c in rubric.criteria] == ["warmth", "voice"]
    with pytest.raises(LabError, match="isn't in rubric v1"):
        lab.add_judgment(attempt.id, turn=1, kind="ai", criterion="humour", verdict="pass",
                         evidence=["Oi"], judge="claude")
    with pytest.raises(LabError, match="must quote its evidence"):
        lab.add_judgment(attempt.id, turn=1, kind="ai", criterion="warmth", verdict="pass",
                         judge="claude")
    with pytest.raises(LabError, match="aren't in that turn"):
        lab.add_judgment(attempt.id, turn=1, kind="ai", criterion="warmth", verdict="pass",
                         evidence=["Hello there friend"], judge="claude")
    lab.add_judgment(attempt.id, turn=1, kind="ai", criterion="warmth", verdict="pass",
                     evidence=["oi  alice, good"], judge="claude")
    lab.add_judgment(attempt.id, turn=1, kind="owner", criterion="voice", verdict="score",
                     score=4, judge="owner via claude")
    summary = lab.compare(run.id)["summary"]["baseline"]
    assert summary["judgments"]["warmth"]["ai"]["pass"] == 1
    assert summary["judgments"]["voice"]["owner"]["mean_score"] == 4
    with pytest.raises(LabError, match="needs a reason"):
        lab.set_rubric(run.id, [{"id": "warmth", "description": "Kind"}])
    lab.set_rubric(run.id, [{"id": "warmth", "description": "Kind"}], status="confirmed",
                   confirmation="Owner: yes, kind is what I mean", reason="owner's wording")
    assert lab.compare(run.id)["summary"]["baseline"]["stale_judgments"] == 2
    assert lab.repo.events(run.id, "rubric_changed")


# ---------------------------------------------------------------- comparing

async def test_compare_finds_regressions_flaky_results_and_gaps(lab):
    run = lab.start_run({"objective": "Answer the current question"}, created_by="test")
    lab.add_scenario(scenario("tips", "bouldering tips?", {"contains_any": ["warm"]}),
                     created_by="test")
    lab.add_scenario(scenario("bbq", "what time is the bbq?", {"contains_any": ["6"]}),
                     created_by="test")
    lab.add_scenario(scenario("web", "search it", {}, requires=["web_search"]),
                     created_by="test")
    c1 = lab.add_candidate(run.id, name="terse", changes={"persona.prompt": "TERSE"})
    answers = {("You are Naruto", "bouldering"): "Warm up first!",
               ("You are Naruto", "bbq"): "6pm, believe it!",
               ("TERSE", "bouldering"): "Climb.",
               ("TERSE", "bbq"): "6pm."}
    flaky = {"count": 0}

    def factory(settings, attempt):
        rules = dict(answers)
        if attempt.candidate_id == c1.id and attempt.scenario_id == 1:
            flaky["count"] += 1
            if flaky["count"] == 2:
                rules[("TERSE", "bouldering")] = "Warm up."
        return ByPrompt(rules)

    use(lab, factory)
    await finish(lab, lab.submit(run.id, scenarios=["tips", "bbq", "web"],
                                 candidates=[None, c1.id], repeat=2, actor="test"))
    result = lab.compare(run.id)
    assert result["configs"] == ["baseline", "c1-terse"]
    by_slug = {row["slug"]: row for row in result["scenarios"]}
    assert by_slug["tips"]["results"]["baseline"]["pass"] == "2/2"
    tips = by_slug["tips"]["results"]["c1-terse"]
    assert tips["pass"] == "1/2" and tips["flaky"]
    assert tips["failed_checks"] == {"contains_any": 1}
    assert by_slug["web"]["results"]["baseline"]["outcomes"] == {"skipped": 2}
    assert [r["slug"] for r in result["regressions"]] == ["tips"]
    assert result["improvements"] == []
    summary = result["summary"]
    assert summary["baseline"]["pass_rate"] == 1 and summary["c1-terse"]["pass_rate"] == 0.75
    assert summary["baseline"]["outcomes"] == {"pass": 4, "skipped": 2}
    assert summary["baseline"]["skills"] == {"banter": 4}
    assert summary["baseline"]["prompt_tokens"]["mean"] == 100
    only_bbq = lab.compare(run.id, scenarios=["bbq"], configs=["c1"])
    assert only_bbq["configs"] == ["c1-terse"] and len(only_bbq["scenarios"]) == 1


async def test_a_validation_set_stops_being_independent(lab):
    run = lab.start_run({"objective": "x"}, created_by="test")
    lab.add_scenario(scenario("v1", "hi", {"contains_any": ["hi"]}), created_by="test")
    lab.add_set(run.id, "held-out", "validation", ["v1"])
    use(lab, ScriptedLLM("hi!"))
    attempt = (await finish(lab, lab.submit(run.id, set_ref="held-out", actor="test")))[0]
    report = lab.report(run.id)
    assert report["validation"]["independent"]
    lab.attempt_detail(attempt, viewer="agent")  # the agent reads the validation result
    lab.services.db.execute("UPDATE lab_attempts SET first_viewed_at = first_viewed_at - 10")
    lab.add_candidate(run.id, name="tuned", changes={"persona.prompt": "P"})
    status = lab.report(run.id)["validation"]
    assert not status["independent"] and "c1-tuned" in status["note"]


async def test_the_report(lab):
    run = lab.start_run({"objective": "Answer the current question",
                         "protected": ["Naruto's voice"]}, created_by="test")
    lab.add_scenario(scenario("bbq", "what time is the bbq?", {"contains_any": ["6"]}),
                     created_by="test")
    c1 = lab.add_candidate(run.id, name="cooler", changes={"model.temperature": 0.3},
                           hypothesis="Less rambling")
    use(lab, ScriptedLLM("6pm!"))
    await finish(lab, lab.submit(run.id, scenarios=["bbq"], candidates=[None, c1.id],
                                 actor="test"))
    lab.add_note(run.id, "defect", "describe_image can't run on imported photos", actor="agent")
    markdown = render_markdown(lab.report(run.id))
    assert "didn't finish normally" in markdown
    for heading in ("## Recommendation", "## Results", "### By scenario",
                    "### Representative replies", "## Judgments", "## Validation",
                    "## Coverage and live checks", "## Resources", "## Agent's notes"):
        assert heading in markdown
    assert "c1-cooler: model.temperature also change digest updates" in markdown
    assert "Must keep: Naruto's voice" in markdown
    lab.finish_run(run.id, reason="objective_met",
                   recommendation={"action": "activate", "candidate": "c1",
                                   "text": "Same results, shorter replies."},
                   summary="Done.", actor="agent")
    final = render_markdown(lab.report(run.id))
    assert "didn't finish normally" not in final and "**activate c1**" in final


# ---------------------------------------------------------- saved scenarios

async def test_a_failure_becomes_a_regression_scenario_with_the_same_prompt(lab):
    run = lab.start_run({"objective": "Remember reminders"}, created_by="test")
    lab.add_scenario({
        "id": "conversation", "timezone": "UTC", "time": "2026-10-03T18:00:00+00:00",
        "members": MEMBERS, "state": {"notes": [{"text": "Bob is vegetarian", "about": 8}]},
        "messages": [{"from": "Bob", "from_id": 8, "text": "bbq saturday?"}],
        "turns": [
            {"from": "Alice", "from_id": 7, "text": "@naruto_bot remind us in 1 hour to buy ice"},
            {"after": "10m", "messages": [{"from": "Bob", "from_id": 8, "text": "nice"}],
             "from": "Alice", "from_id": 7, "reply_to_answer": True,
             "text": "what's pending?", "expect": {"contains_any": ["ice"]}}]},
        created_by="test")
    script = [[tool_call("set_reminder", {"when": "in 1 hour", "text": "buy ice"})],
              "[REPLY] Reminder set!", "Nothing is pending."]
    use(lab, lambda settings, attempt: ScriptedLLM(*script))
    attempt = (await finish(lab, lab.submit(run.id, scenarios=["conversation"],
                                            actor="test")))[0]
    assert attempt.outcome == "fail"
    saved = await lab.scenario_from_attempt(attempt.id, turn=2, slug=None, expect=None,
                                            reason=None, description=None, actor="agent")
    body = saved.body
    assert saved.slug == f"conversation-turn2-a{attempt.id}"
    assert body["provenance"]["attempt"] == attempt.id
    assert [m.get("bot", False) for m in body["messages"]] == [False, False, True, False]
    assert body["messages"][2]["text"] == "Reminder set!"
    assert body["state"]["reminders"][0]["text"] == "buy ice"
    assert body["state"]["notes"] == [{"text": "Bob is vegetarian", "category": "group_fact",
                                      "about": 8}]
    assert body["turns"][0]["reply_to"] == body["messages"][2]["id"]
    assert body["turns"][0]["expect"] == {"contains_any": ["ice"]}

    use(lab, lambda settings, attempt: ScriptedLLM("The ice reminder."))
    again = (await finish(lab, lab.submit(run.id, scenarios=[saved.slug], actor="test")))[0]
    original_prompt = attempt.turns[1]["run"]["prompt"]
    assert again.turns[0]["run"]["prompt"] == original_prompt
    assert again.outcome == "pass"


async def test_a_real_conversation_becomes_a_scenario_for_allowed_tokens(lab, services):
    chat_id = -5
    services.chats.upsert_seen(chat_id, title="Real group", chat_type="group")
    services.chats.set_status(chat_id, "enabled")
    services.members.upsert_live(chat_id, 7, "Alice", "alice")
    rows = []
    for i, text in enumerate(["bbq sat?", "6pm works", "@naruto_bot when is it?"], 1):
        rows.append(services.messages.insert_live(NewMessage(
            chat_id=chat_id, origin_chat_id=chat_id, source=LIVE, message_id=i, sender_id=7,
            sender_name="Alice", sender_username="alice", date=1_790_000_000 + i * 60,
            text=text)))
    agent_run = services.runs.start(chat_id=chat_id, skill="banter", trigger_row_id=rows[-1].id)
    services.runs.update(agent_run, window_size=2, status="ok")
    services.notes.add(chat_id, "Alice hosts", created_by="owner", actor="owner")
    stranger = lab.repo.add_token("other", "h1", chats=[], may_activate=False)
    with pytest.raises(LabError, match="may not read"):
        lab.scenario_from_agent_run(agent_run, slug=None, expect=None, description=None,
                                    token=stranger, actor="agent:other")
    allowed = lab.repo.add_token("mine", "h2", chats=[chat_id], may_activate=False)
    saved = lab.scenario_from_agent_run(agent_run, slug="when-is-it",
                                        expect={"contains_any": ["6"]}, description=None,
                                        token=allowed, actor="agent:mine")
    assert saved.origin == "history" and saved.chat_id == chat_id
    body = saved.body
    assert [m["text"] for m in body["messages"]] == ["bbq sat?", "6pm works"]
    assert body["turns"][0]["text"] == "@naruto_bot when is it?"
    assert body["state"]["notes"][0]["text"] == "Alice hosts"
    assert "not as of the original run" in body["provenance"]["approximate"][0]
