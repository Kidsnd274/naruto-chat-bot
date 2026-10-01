"""Activating a lab candidate in the live settings, and reverting it:
permissions, conflicts, drift, model switches, the Lab page and the CLI."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from fakes import ScriptedLLM
from naruto.lab import activation
from naruto.lab.service import LabError, LabService
from naruto.settings.registry import SettingError
from naruto.web import auth
from naruto.web.app import create_app
from test_lab_api import run_cli

SCENARIO = {"id": "hi", "timezone": "UTC", "members": [{"id": 7, "name": "Alice"}],
            "turns": [{"from": "Alice", "from_id": 7, "text": "@naruto_bot hi",
                       "expect": {"contains_any": ["hi"]}}]}


@pytest.fixture
def lab(services, tmp_path):
    services.settings.set("model.name", "test-model", actor="test")
    services.lab = LabService(services, tmp_path / "lab",
                              llm_factory=lambda settings, attempt: ScriptedLLM("Oi, hi!"))
    return services.lab


async def evaluated(lab, *, policy="recommend", changes=None, **spec):
    run = lab.start_run({"objective": "Warmer", "activation": {"policy": policy}, **spec},
                        created_by="test")
    lab.add_scenario(SCENARIO, created_by="test")
    candidate = lab.add_candidate(run.id, name="warm", changes=changes or {
        "persona.prompt": "Warm persona.", "model.temperature": 0.4})
    batch = lab.submit(run.id, scenarios=["hi"], candidates=[None, candidate.id], actor="test")
    assert await lab.executor.wait(batch.id, 10)
    return run, candidate


def test_set_many_is_all_or_nothing(services):
    seen = []
    services.settings.on_change(lambda key, value: seen.append(key))
    with pytest.raises(SettingError):
        services.settings.set_many({"persona.prompt": "P", "model.temperature": 9}, actor="t")
    assert services.settings.is_default("persona.prompt") and not seen
    changed = services.settings.set_many({"persona.prompt": "P", "model.temperature": 0.5,
                                          "context.recent_window": 40}, actor="t")
    assert changed == ["persona.prompt", "model.temperature"]  # 40 is the default already
    assert services.settings["persona.prompt"] == "P" and seen == changed
    assert [h.key for h in services.settings.history(limit=2)] == ["model.temperature",
                                                                    "persona.prompt"]


async def test_activate_and_revert(lab, services):
    run, candidate = await evaluated(lab)
    plan = activation.plan(lab, run, candidate)
    assert plan["apply"] == {"persona.prompt": "Warm persona.", "model.temperature": 0.4}
    assert plan["background_effects"] == ["model.temperature"] and not plan["conflicts"]
    assert "+Warm persona." in plan["diffs"]["persona.prompt"]
    with pytest.raises(LabError, match="who authorized"):
        activation.activate(lab, run.id, "c1", authorized_by="", actor="owner")
    done = activation.activate(lab, run.id, "c1", authorized_by="owner (web admin)",
                               actor="owner (web admin)")
    assert services.settings["persona.prompt"] == "Warm persona."
    assert services.settings["model.temperature"] == 0.4
    assert services.settings.last_changed("persona.prompt").changed_by.startswith(
        f"lab run {run.id} c1-warm (authorized by owner")
    assert done.previous["persona.prompt"].startswith("You are Naruto")
    assert done.evidence["summary"]["pass_rate"] == 1 and done.evidence["attempts"] == 1
    with pytest.raises(LabError, match="already match"):
        activation.activate(lab, run.id, "c1", authorized_by="owner", actor="owner")
    activation.revert(lab, done.id, actor="owner")
    assert services.settings["persona.prompt"].startswith("You are Naruto")
    assert services.settings["model.temperature"] is None
    with pytest.raises(LabError, match="already reverted"):
        activation.revert(lab, done.id, actor="owner")


async def test_agents_need_both_permissions(lab):
    run, _ = await evaluated(lab)
    no_permission = lab.repo.add_token("a", "h1", chats=[], may_activate=False)
    with pytest.raises(LabError, match="may not activate"):
        activation.activate(lab, run.id, "c1", authorized_by="owner said so",
                            token=no_permission, actor="agent:a")
    allowed = lab.repo.add_token("b", "h2", chats=[], may_activate=True)
    with pytest.raises(LabError, match="activation policy is recommend"):
        activation.activate(lab, run.id, "c1", authorized_by="owner said so", token=allowed,
                            actor="agent:b")
    run2, _ = await evaluated(lab, policy="agent_may_activate")
    unevaluated = lab.add_candidate(run2.id, name="new", changes={"persona.prompt": "New."})
    with pytest.raises(LabError, match="hasn't been evaluated"):
        activation.activate(lab, run2.id, unevaluated.id, authorized_by="owner said so",
                            token=allowed, actor="agent:b")
    assert activation.activate(lab, run2.id, "c1", authorized_by="owner said 'ship c1'",
                               token=allowed, actor="agent:b").authorized_by.endswith("c1'")


async def test_conflicts_and_drift_are_surfaced(lab, services):
    run, _ = await evaluated(lab)
    services.settings.set("persona.prompt", "The persona agent's rewrite.", actor="owner")
    with pytest.raises(LabError, match="changed by someone else") as refused:
        activation.activate(lab, run.id, "c1", authorized_by="owner", actor="owner")
    assert refused.value.details["conflicts"][0]["changed_by"] == "owner"
    assert services.settings["persona.prompt"] == "The persona agent's rewrite."
    assert services.settings["model.temperature"] is None  # nothing applied

    run2, _ = await evaluated(lab, changes={"skills.banter.instructions": "Be warm."})
    services.settings.set("prompt.rules", "New rules.", actor="owner")
    with pytest.raises(LabError, match="acknowledge_drift") as refused:
        activation.activate(lab, run2.id, "c1", authorized_by="owner", actor="owner")
    assert refused.value.details["drift"][0]["key"] == "prompt.rules"
    done = activation.activate(lab, run2.id, "c1", authorized_by="owner",
                               acknowledge_drift="the rules change is unrelated", actor="owner")
    assert done.drift[0]["acknowledged"] == "the rules change is unrelated"

    services.settings.set("skills.banter.instructions", "Edited again.", actor="owner")
    with pytest.raises(LabError, match="changed again") as refused:
        activation.revert(lab, done.id, actor="owner")
    assert refused.value.details["changed"][0]["live"] == "Edited again."


async def test_tuning_for_another_model_switches_it_and_full_mode_goes_back(lab, services):
    services.settings.set("lab.model_servers", {"gufo": "http://localhost:8081/v1"},
                          actor="owner")
    first, _ = await evaluated(lab)
    other, _ = await evaluated(lab, model={"server": "gufo", "name": "qwen3.8-27b"})
    plan = activation.plan(lab, other, lab.get_candidate(other, "c1"))
    assert plan["model_switch"] and plan["apply"]["model.name"] == "qwen3.8-27b"
    activation.activate(lab, other.id, "c1", authorized_by="owner", actor="owner")
    assert services.settings["model.endpoint_url"] == "http://localhost:8081/v1"
    assert services.settings["persona.prompt"] == "Warm persona."
    back = activation.activate(lab, first.id, "baseline", mode="full", authorized_by="owner",
                               actor="owner")
    assert services.settings["model.endpoint_url"] == "http://localhost:8080/v1"
    assert services.settings["model.name"] == "test-model"
    assert services.settings["persona.prompt"].startswith("You are Naruto")
    assert back.mode == "full"
    with pytest.raises(LabError, match="use mode full"):
        activation.activate(lab, first.id, "baseline", authorized_by="owner", actor="owner")


async def test_chats_with_their_own_settings_are_named(lab, services):
    services.chats.upsert_seen(-5, title="Quiet group")
    services.settings.set_for_chat(-5, "persona.prompt", "Their own persona.", actor="owner")
    run, candidate = await evaluated(lab)
    assert activation.plan(lab, run, candidate)["per_chat_overrides"] == {
        "persona.prompt": [-5]}


async def test_the_lab_page_and_the_cli_activate_and_revert(lab, services, monkeypatch,
                                                            capsys):
    run, _ = await evaluated(lab, policy="agent_may_activate")
    secret = lab.create_token("claude-code", may_activate=True)[1]
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    with TestClient(create_app(services, session_secret="s"), follow_redirects=False) as http:
        code, out, _ = run_cli(http, secret, capsys, "activate", str(run.id), "c1", "--preview")
        assert code == 0 and set(json.loads(out)["apply"]) == {"persona.prompt",
                                                                "model.temperature"}
        assert services.settings["model.temperature"] is None
        code, _, err = run_cli(http, secret, capsys, "activate", str(run.id), "c1")
        assert code == 2 and "--authorized-by" in err
        code, out, _ = run_cli(http, secret, capsys, "activate", str(run.id), "c1",
                               "--authorized-by", "the owner, in the terminal: 'ship it'")
        activation_id = json.loads(out)["id"]
        assert code == 0 and services.settings["model.temperature"] == 0.4
        code, out, _ = run_cli(http, secret, capsys, "revert", str(activation_id))
        assert code == 0 and services.settings["model.temperature"] is None

        token = re.search(r'name="csrf_token" value="([^"]+)"', http.get("/login").text).group(1)
        http.post("/login", data={"csrf_token": token, "password": "correct horse", "next": "/"})
        page = http.get(f"/lab/runs/{run.id}/activate/c1")
        assert page.status_code == 200 and "+Warm persona." in page.text
        assert "also change digest updates" in page.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        http.post(f"/lab/runs/{run.id}/activate/c1", data={"csrf_token": csrf, "mode": "changes"})
        assert services.settings["persona.prompt"] == "Warm persona."
        latest = lab.repo.activations()[0]
        assert latest.authorized_by == "owner (web admin)"
        assert "apply its full configuration again" in http.get("/lab").text
        http.post(f"/lab/activations/{latest.id}/revert", data={"csrf_token": csrf})
        assert services.settings["persona.prompt"].startswith("You are Naruto")
