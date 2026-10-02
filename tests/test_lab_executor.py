"""The prompt lab's runs, candidates, scenarios and the executor: budget,
the bot's queue, cancelling, restarts, waiting for the owner and drift."""

import asyncio
import json

import pytest

from fakes import ScriptedLLM, tool_call
from naruto.db.lab import LabRepository
from naruto.db.migrations import MIGRATIONS
from naruto.lab import config
from naruto.lab.service import LabError, LabService
from naruto.llm import ChatResult
from naruto.model_queue import FOREGROUND, RequestInfo

SCENARIO = {
    "id": "hello", "timezone": "UTC", "time": "2026-10-03T18:00:00+00:00",
    "members": [{"id": 7, "name": "Alice", "username": "alice"}],
    "turns": [{"from": "Alice", "from_id": 7, "text": "@naruto_bot hi",
               "expect": {"contains_any": ["hi"]}}],
}


@pytest.fixture
def scripts():
    """Answers per attempt: scripts.append([...]) before running."""
    return []


@pytest.fixture
def lab(services, tmp_path, scripts):
    services.settings.set("model.name", "test-model", actor="test")

    def factory(settings, attempt):
        script = scripts.pop(0) if scripts else ["Oi, hi!"]
        return ScriptedLLM(*script)

    service = LabService(services, tmp_path / "lab", llm_factory=factory)
    services.lab = service
    yield service


def start(lab, **spec):
    return lab.start_run({"objective": "Make Naruto answer the current question", **spec},
                         created_by="test")


async def finish(lab, batch):
    assert await lab.executor.wait(batch.id, 10)
    return lab.repo.attempts(batch_id=batch.id)


# ------------------------------------------------------------------- runs

def test_a_run_freezes_the_baseline_and_its_model(lab, services):
    services.settings.set("persona.prompt", "Persona v1", actor="owner")
    run = start(lab, budget={"attempts": 10}, protected=["Naruto's voice"])
    assert run.baseline["persona.prompt"] == "Persona v1"
    assert (run.model_endpoint, run.model_name) == ("http://localhost:8080/v1", "test-model")
    assert run.spec["budget"] == {"attempts": 10, "model_requests": 300, "hours": 4}
    assert run.code_fingerprint == config.code_fingerprint()
    assert run.schema_version == len(MIGRATIONS)
    services.settings.set("persona.prompt", "Persona v2", actor="owner")
    assert lab.get_run(run.id).baseline["persona.prompt"] == "Persona v1"
    assert lab.run_state(run)["state"] == "idle"


@pytest.mark.parametrize("spec,message", [
    ({"objective": ""}, "needs an objective"),
    ({"scope": {"keys": ["retention.logs_days"]}}, "can't be tuned"),
    ({"scope": {"keys": ["nothing.*"]}}, "doesn't name any setting"),
    ({"scope": {"skills": ["dance"]}}, "Unknown skills"),
    ({"budget": {"attempts": 501}}, "budget.attempts"),
    ({"model": {"endpoint": "https://api.example.com/v1"}}, "only tests the configured"),
    ({"model": {"server": "cloud"}}, "Unknown model server"),
    ({"colour": "red"}, "Unknown run fields"),
])
def test_invalid_runs(lab, spec, message):
    with pytest.raises(LabError, match=message):
        lab.start_run({"objective": "x", **spec}, created_by="test")


def test_other_local_servers_need_the_owners_list(lab, services):
    services.settings.set("lab.model_servers", {"gufo-27b": "http://localhost:8081/v1"},
                          actor="owner")
    run = start(lab, model={"server": "gufo-27b", "name": "qwen3.8-27b"})
    assert (run.model_endpoint, run.model_name) == ("http://localhost:8081/v1", "qwen3.8-27b")
    assert lab.effective_settings(run, None)["model.endpoint_url"] == "http://localhost:8081/v1"


def test_token_permissions_bound_the_run(lab):
    token = lab.repo.add_token("agent", "h", chats=[-5], may_activate=False)
    with pytest.raises(LabError, match="may not activate"):
        lab.start_run({"objective": "x", "activation": {"policy": "agent_may_activate"}},
                      created_by="agent", token=token)
    with pytest.raises(LabError, match="may not read chats"):
        lab.start_run({"objective": "x", "data": {"chats": [-6]}}, created_by="agent",
                      token=token)
    assert lab.start_run({"objective": "x", "data": {"chats": [-5]}}, created_by="agent",
                         token=token).spec["data"] == {"chats": [-5]}


# ------------------------------------------------------------- candidates

def test_candidates_change_only_tunable_settings_in_scope(lab):
    run = start(lab, scope={"keys": ["skills.banter.*", "model.temperature"]})
    with pytest.raises(LabError, match="outside this run's scope"):
        lab.add_candidate(run.id, name="x", changes={"persona.prompt": "p"})
    with pytest.raises(LabError, match="part of the run's conditions"):
        lab.add_candidate(run.id, name="x", changes={"model.name": "other"})
    with pytest.raises(LabError, match="model.temperature: Must be at most 2"):
        lab.add_candidate(run.id, name="x", changes={"model.temperature": 5})
    with pytest.raises(LabError, match="parent already has"):
        lab.add_candidate(run.id, name="x",
                          changes={"skills.banter.reasoning": run.baseline[
                              "skills.banter.reasoning"]})
    first = lab.add_candidate(run.id, name="Cheekier banter",
                              changes={"skills.banter.instructions": "Be cheeky."},
                              hypothesis="More teasing reads as more Naruto.")
    second = lab.add_candidate(run.id, name="cooler", parent=first.id,
                               changes={"model.temperature": 0.5})
    assert first.label == "c1-cheekier-banter" and second.label == "c2-cooler"
    values = lab.effective_settings(run, second)
    assert values["skills.banter.instructions"] == "Be cheeky."
    assert values["model.temperature"] == 0.5
    view = lab.candidate_view(run, second)
    assert set(view["vs_parent"]) == {"model.temperature"}
    assert set(view["vs_baseline"]) == {"model.temperature", "skills.banter.instructions"}
    assert "digest updates" in view["background_effects"][0]
    assert lab.get_candidate(run, "c1") == first
    assert lab.get_candidate(run, "baseline") is None


def test_configuration_files_round_trip(lab):
    run = start(lab)
    keys = lab.tunable(run)
    files = config.to_files(run.baseline, keys)
    assert {"persona.md", "rules.md", "banter.md", "settings.json"} <= set(files)
    assert config.from_files(files, run.baseline, keys) == {}
    edited = dict(files, **{"banter.md": "Be cheeky.\n"})
    settings = json.loads(files["settings.json"])
    settings["model.temperature"] = 0.4
    edited["settings.json"] = json.dumps(settings)
    assert config.from_files(edited, run.baseline, keys) == {
        "skills.banter.instructions": "Be cheeky.", "model.temperature": 0.4}
    with pytest.raises(Exception, match="isn't a configuration file"):
        config.from_files({"notes.md": "x"}, run.baseline, keys)


# -------------------------------------------------------------- scenarios

def test_scenarios_are_versioned_with_reasons(lab):
    run = start(lab)
    first, created = lab.add_scenario(SCENARIO, created_by="test")
    assert created and first.version == 1
    again, created = lab.add_scenario(SCENARIO, created_by="test")
    assert not created and again.id == first.id
    changed = {**SCENARIO, "description": "now with a description"}
    with pytest.raises(LabError, match="give a reason"):
        lab.add_scenario(changed, created_by="test")
    second, _ = lab.add_scenario(changed, created_by="test", reason="clearer", run_id=run.id)
    assert second.version == 2 and lab.get_scenario("hello").id == second.id
    assert lab.repo.events(run.id, "scenario_changed")[0].detail["reason"] == "clearer"
    with pytest.raises(LabError, match="image"):
        lab.add_scenario({**SCENARIO, "id": "img", "messages": [
            {"from": "Alice", "media": "photo", "image": "x.png"}]}, created_by="test")


def test_sets_log_removals(lab):
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    lab.add_set(run.id, "regressions", "regression", ["hello"])
    with pytest.raises(LabError, match="needs a reason"):
        lab.change_set(run.id, "regressions", remove=[{"slug": "hello"}])
    lab.change_set(run.id, "regressions", remove=[{"slug": "hello", "reason": "flaky"}])
    view = lab.set_view(lab.get_set(run, "regressions"))
    assert view["scenarios"] == [] and view["removed"] == [{"slug": "hello", "reason": "flaky"}]
    assert lab.repo.events(run.id, "set_removed")[0].detail["purpose"] == "regression"


# --------------------------------------------------------------- attempts

async def test_a_suite_runs_every_candidate_in_order(lab, services):
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    candidate = lab.add_candidate(run.id, name="c", changes={"persona.prompt": "Test persona."})
    batch = lab.submit(run.id, scenarios=["hello"], candidates=[None, candidate.id], repeat=2,
                       actor="test")
    attempts = await finish(lab, batch)
    assert [(a.candidate_id, a.repeat) for a in attempts] == [
        (None, 1), (candidate.id, 1), (None, 2), (candidate.id, 2)]
    assert all(a.status == "done" and a.outcome == "pass" for a in attempts)
    assert lab.get_batch(batch.id).status == "done"
    detail = lab.attempt_detail(attempts[1], prompts=True, viewer="test")
    assert detail["turns"][0]["prompt"][0]["content"].startswith("Test persona.")
    assert detail["conditions"]["scenario"] == {"id": 1, "slug": "hello", "version": 1}
    assert detail["conditions"]["settings_hash"] == config.settings_hash(
        lab.effective_settings(run, candidate))
    assert lab.repo.attempt(attempts[1].id).first_viewed_at is not None
    assert lab.run_state(lab.get_run(run.id))["used"]["attempts"] == 4


async def test_lab_requests_wait_behind_replies_in_the_bots_queue(services, tmp_path,
                                                                 monkeypatch):
    services.settings.set("model.name", "test-model", actor="test")
    calls = []

    async def fake_completion(client, kwargs, *, stream=False, tools_offered=False):
        calls.append(kwargs)
        return ChatResult(text="Oi, hi!", reasoning=None, model="served-model", latency_ms=5,
                          usage={"prompt_tokens": 10}, finish_reason="stop")

    monkeypatch.setattr("naruto.llm.request_completion", fake_completion)
    lab = LabService(services, tmp_path / "lab")
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    queue = services.llm.queue
    reply = await queue.acquire(RequestInfo(task="reply", chat_id=-1), FOREGROUND)
    batch = lab.submit(run.id, scenarios=["hello"], actor="test")
    await asyncio.sleep(0.05)
    assert not calls  # one slot, held by a reply
    queue.release(reply, "done")
    attempts = await finish(lab, batch)
    assert attempts[0].outcome == "pass" and calls[0]["model"] == "test-model"
    rows = [r for r in services.requests.recent() if r.task == "lab"]
    assert rows and rows[0].priority == "background"
    assert rows[0].lab_attempt_id == attempts[0].id
    assert attempts[0].conditions["models_reported"] == ["served-model"]
    assert lab.get_run(run.id).model_reported == "served-model"


async def test_the_budget_stops_a_batch(lab, scripts):
    run = start(lab, budget={"attempts": 3, "model_requests": 1})
    lab.add_scenario(SCENARIO, created_by="test")
    with pytest.raises(LabError, match="the run has 3 left"):
        lab.submit(run.id, scenarios=["hello"], repeat=4, actor="test")
    batch = lab.submit(run.id, scenarios=["hello"], repeat=2, actor="test")
    first, second = await finish(lab, batch)
    assert first.outcome == "pass"
    assert second.status == "skipped" and "model requests used" in second.reason
    assert lab.get_batch(batch.id).status == "budget_exhausted"
    assert lab.repo.events(run.id, "budget_exhausted")
    with pytest.raises(LabError, match="budget is used up"):
        lab.submit(run.id, scenarios=["hello"], actor="test")


class BlockingLLM(ScriptedLLM):
    def __init__(self, gate: asyncio.Event):
        super().__init__("Oi!")
        self.gate = gate

    async def chat(self, *args, **kwargs):
        await self.gate.wait()
        return await super().chat(*args, **kwargs)


async def test_cancelling_stops_running_and_waiting_attempts(services, tmp_path):
    gate = asyncio.Event()
    lab = LabService(services, tmp_path / "lab", llm_factory=lambda s, a: BlockingLLM(gate))
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    batch = lab.submit(run.id, scenarios=["hello"], repeat=2, actor="test")
    await asyncio.sleep(0.05)
    assert lab.executor.cancel_batch(batch.id) == 2
    await lab.executor.wait(batch.id, 5)
    attempts = lab.repo.attempts(batch_id=batch.id)
    assert [(a.status, a.outcome) for a in attempts] == [("cancelled", "cancelled")] * 2
    assert lab.get_batch(batch.id).status == "cancelled"


async def test_a_restart_interrupts_and_resume_finishes(services, tmp_path):
    gate = asyncio.Event()
    lab = LabService(services, tmp_path / "lab", llm_factory=lambda s, a: BlockingLLM(gate))
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    batch = lab.submit(run.id, scenarios=["hello"], repeat=2, actor="test")
    await asyncio.sleep(0.05)
    await lab.shutdown()
    statuses = [a.status for a in lab.repo.attempts(batch_id=batch.id)]
    assert statuses[0] == "interrupted"

    restarted = LabService(services, tmp_path / "lab",
                           llm_factory=lambda s, a: ScriptedLLM("Oi, hi!"))
    restarted.recover()
    assert {a.status for a in restarted.repo.attempts(batch_id=batch.id)} == {"interrupted"}
    assert restarted.executor.resume(batch.id) == 2
    attempts = await finish(restarted, batch)
    assert [a.outcome for a in attempts] == ["pass", "pass"]


async def test_waiting_for_the_owner_holds_new_experiments(lab):
    run = start(lab, interactive={"enabled": True})
    scenario, _ = lab.add_scenario(SCENARIO, created_by="test")
    lab.repo.add_comparison(run_id=run.id, scenario_id=scenario.id, turn=1,
                            mapping={"A": 1, "B": 2}, presentation="...")
    assert lab.run_state(lab.get_run(run.id))["state"] == "waiting_for_owner"
    with pytest.raises(LabError, match="Waiting for the owner's choice"):
        lab.submit(run.id, scenarios=["hello"], actor="agent")
    assert lab.repo.events(run.id, "refused_while_waiting")
    batch = lab.submit(run.id, scenarios=["hello"], actor="agent",
                       owner_request="The owner asked for one more example.")
    await finish(lab, batch)
    assert lab.repo.events(run.id, "owner_request")[0].detail["text"].startswith("The owner")


async def test_drift_is_reported(lab, monkeypatch):
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    lab.note_conditions(run.id, 1, {"models_reported": ["model-a"],
                                    "code_fingerprint": run.code_fingerprint})
    lab.note_conditions(run.id, 2, {"models_reported": ["model-b"],
                                    "code_fingerprint": "different"})
    warnings = lab.get_run(run.id).warnings
    assert any("reported model-b instead of model-a" in w for w in warnings)
    assert any("code changed" in w for w in warnings)


async def test_a_conversation_continues_from_a_saved_state(lab, scripts):
    run = start(lab)
    lab.add_scenario({**SCENARIO, "id": "remind", "turns": [
        {"from": "Alice", "from_id": 7, "text": "@naruto_bot remind us in 1 hour to buy ice"}]},
        created_by="test")
    lab.add_scenario({"id": "follow-up", "continues": True, "members": SCENARIO["members"],
                      "turns": [
        {"from": "Alice", "from_id": 7, "reply_to_answer": True, "text": "what's pending?",
         "expect": {"contains_any": ["ice"]}}]}, created_by="test")
    scripts.append([[tool_call("set_reminder", {"when": "in 1 hour", "text": "buy ice"})],
                    "[REPLY] Reminder set!"])
    first = (await finish(lab, lab.submit(run.id, scenarios=["remind"], actor="test")))[0]
    with pytest.raises(LabError, match="pass continue_from"):
        lab.submit(run.id, scenarios=["follow-up"], actor="test")
    assert first.state_path and first.outcome == "unjudged"
    candidate = lab.add_candidate(run.id, name="c", changes={"persona.prompt": "Test persona."})
    scripts.append(["The ice reminder is pending."])
    batch = lab.submit(run.id, scenarios=["follow-up"], candidates=[candidate.id],
                       continue_from=first.id, actor="test")
    second = (await finish(lab, batch))[0]
    assert second.outcome == "pass", second.reason
    turn = lab.attempt_detail(second, prompts=True)["turns"][0]
    context, current = turn["prompt"][1]["content"], turn["prompt"][2]["content"]
    assert "Reminder set!" in context and "buy ice" in current  # the pending reminder
    assert turn["prompt"][0]["content"].startswith("Test persona.")
    assert "replying to [" in current


async def test_nothing_reaches_the_live_bot(lab, services):
    services.chats.upsert_seen(-42, title="Real group")
    before = {table: services.db.scalar(f"SELECT COUNT(*) FROM {table}")
              for table in ("messages", "memory_notes", "reminders", "boards", "agent_runs")}
    run = start(lab)
    lab.add_scenario({**SCENARIO, "turns": [
        {"from": "Alice", "from_id": 7, "text": "@naruto_bot remember Alice loves ramen"}]},
        created_by="test")
    lab_scripts = [[tool_call("remember", {"fact": "Alice loves ramen"})], "Got it!"]
    lab.executor.llm_factory = lambda settings, attempt: ScriptedLLM(*lab_scripts)
    attempt = (await finish(lab, lab.submit(run.id, scenarios=["hello"], actor="test")))[0]
    assert attempt.status == "done"
    after = {table: services.db.scalar(f"SELECT COUNT(*) FROM {table}") for table in before}
    assert after == before and services.telegram is None


async def test_old_finished_runs_are_cleaned_up(lab, services):
    run = start(lab)
    lab.add_scenario(SCENARIO, created_by="test")
    attempt = (await finish(lab, lab.submit(run.id, scenarios=["hello"], actor="test")))[0]
    state = attempt.state_path
    lab.stop_run(run.id, reason="no_improvement", actor="test")
    assert lab.cleanup(90) == 0
    services.db.execute("UPDATE lab_runs SET finished_at = 0")
    assert lab.cleanup(90) == 1
    assert lab.repo.run(run.id) is None and not lab.repo.attempts(run_id=run.id)
    assert not __import__("pathlib").Path(state).exists()
    assert isinstance(lab.repo, LabRepository)
