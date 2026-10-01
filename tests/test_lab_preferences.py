"""Interactive A/B rounds: neutral comparisons of real replies, the
owner's choices (never inferred), corrections, waiting, and the preference
summary, through the service, the web admin and the CLI."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from fakes import ScriptedLLM
from naruto.lab import preferences
from naruto.lab.export import export_files
from naruto.lab.service import LabError, LabService
from naruto.web import auth
from naruto.web.app import create_app
from test_lab_api import AppClient, run_cli

SITUATION = {
    "id": "teasing", "timezone": "UTC", "time": "2026-10-03T18:00:00+00:00",
    "description": "Wei teases Naruto about bowling.",
    "members": [{"id": 9, "name": "Wei"}],
    "messages": [{"from": "Wei", "from_id": 9, "text": "you bowled a 40 lol"}],
    "turns": [{"from": "Wei", "from_id": 9, "text": "@naruto_bot admit I'm better"}],
}
ANSWERS = {"baseline": "Heh, rematch Saturday. Believe it!", "c1": "Pfft. Luck. Rematch?"}


@pytest.fixture
def lab(services, tmp_path):
    services.settings.set("model.name", "test-model", actor="test")

    def factory(settings, attempt):
        return ScriptedLLM(ANSWERS["c1" if attempt.candidate_id else "baseline"])

    services.lab = LabService(services, tmp_path / "lab", llm_factory=factory)
    return services.lab


async def two_replies(lab, *, interactive=None):
    run = lab.start_run({"objective": "Find a tone the owner likes",
                         "interactive": interactive or {"enabled": True}}, created_by="test")
    lab.add_scenario(SITUATION, created_by="test")
    c1 = lab.add_candidate(run.id, name="cheekier", changes={"persona.prompt": "Cheekier."})
    batch = lab.submit(run.id, scenarios=["teasing"], candidates=[None, c1.id], actor="agent")
    assert await lab.executor.wait(batch.id, 10)
    return run, lab.repo.attempts(batch_id=batch.id)


async def test_a_round_is_neutral_waits_for_the_owner_and_keeps_corrections(lab):
    run, attempts = await two_replies(lab)
    comparison = preferences.create(lab, run.id, [a.id for a in attempts], actor="agent")
    shown = preferences.view(lab, comparison)
    assert sorted(comparison.mapping.values()) == sorted(a.id for a in attempts)
    assert "mapping" not in shown and shown["labels"] == ["A", "B"]
    text = shown["presentation"]
    assert "made up for testing" in text and "Wei: you bowled a 40 lol" in text
    assert ANSWERS["baseline"] in text and ANSWERS["c1"] in text
    assert "baseline" not in text and "cheekier" not in text
    assert lab.run_state(lab.get_run(run.id))["state"] == "waiting_for_owner"
    with pytest.raises(LabError, match="Waiting for the owner"):
        lab.submit(run.id, scenarios=["teasing"], actor="agent")

    files = export_files(lab, run)
    assert ANSWERS["c1"] not in files["replies/teasing.md"]
    assert "waiting for the owner" in files["replies/teasing.md"]
    assert ANSWERS["c1"] not in files["report.md"]
    assert "Waiting for the owner" in files["comparisons.md"]

    with pytest.raises(LabError, match="one of"):
        preferences.answer(lab, comparison.id, "C", comment=None, channel="owner")
    with pytest.raises(LabError, match="say what to combine"):
        preferences.answer(lab, comparison.id, "combination", comment="", channel="owner")
    preferences.answer(lab, comparison.id, "A", comment="funnier", channel="owner via agent")
    answered = preferences.view(lab, preferences.get(lab, comparison.id))
    assert answered["status"] == "answered" and answered["choice"]["comment"] == "funnier"
    assert set(answered["mapping"]) == {"A", "B"}
    assert lab.run_state(lab.get_run(run.id))["state"] == "idle"
    with pytest.raises(LabError, match="correct it"):
        preferences.answer(lab, comparison.id, "B", comment=None, channel="owner")
    preferences.correct(lab, comparison.id, "combination", comment="A's humour, B's brevity",
                        channel="owner via agent")
    history = preferences.view(lab, preferences.get(lab, comparison.id))["history"]
    assert [c["choice"] for c in history] == ["A", "combination"]
    assert history[1]["supersedes"] == history[0]["id"]

    files = export_files(lab, run)
    assert ANSWERS["c1"] in files["replies/teasing.md"]
    assert "(corrected later)" in files["comparisons.md"]
    winner = answered["mapping"]["A"]["configuration"]
    assert winner in files["comparisons.md"]
    section = lab.report(run.id)["interactive"]
    assert section["comparisons"][0]["corrections"] == 1


async def test_revealing_before_the_answer_is_logged(lab):
    run, attempts = await two_replies(lab)
    comparison = preferences.create(lab, run.id, [a.id for a in attempts], actor="agent")
    revealed = preferences.view(lab, comparison, reveal=True, actor="agent")
    assert revealed["revealed_before_answer"] and "mapping" in revealed
    assert lab.repo.events(run.id, "revealed_before_answer")


async def test_limits_withdrawal_and_same_situation(lab):
    run, attempts = await two_replies(lab)
    ids = [a.id for a in attempts]
    first = preferences.create(lab, run.id, ids, actor="agent")
    with pytest.raises(LabError, match="already wait"):
        preferences.create(lab, run.id, ids, actor="agent")
    with pytest.raises(LabError, match="Say why"):
        preferences.withdraw(lab, first.id, reason="", actor="agent")
    preferences.withdraw(lab, first.id, reason="the owner wants another situation",
                         actor="agent")
    assert preferences.get(lab, first.id).status == "withdrawn"
    with pytest.raises(LabError, match="2 to 4"):
        preferences.create(lab, run.id, ids[:1], actor="agent")
    other_slug = dict(SITUATION, id="other")
    lab.add_scenario(other_slug, created_by="test")
    batch = lab.submit(run.id, scenarios=["other"], actor="agent")
    await lab.executor.wait(batch.id, 10)
    other = lab.repo.attempts(batch_id=batch.id)[0]
    with pytest.raises(LabError, match="same situation"):
        preferences.create(lab, run.id, [ids[0], other.id], actor="agent")


async def test_later_turns_compare_only_the_same_conversation(lab):
    run = lab.start_run({"objective": "x"}, created_by="test")
    lab.add_scenario({**SITUATION, "turns": SITUATION["turns"] + [
        {"from": "Wei", "from_id": 9, "reply_to_answer": True, "text": "ok but seriously?"}]},
        created_by="test")
    lab.add_scenario({"id": "follow", "continues": True, "members": SITUATION["members"],
                      "turns": [{"from": "Wei", "from_id": 9, "reply_to_answer": True,
                                 "text": "ok but seriously?"}]}, created_by="test")
    c1 = lab.add_candidate(run.id, name="calm", changes={"persona.prompt": "Calm."})
    batch = lab.submit(run.id, scenarios=["teasing"], candidates=[None, c1.id], actor="agent")
    await lab.executor.wait(batch.id, 10)
    base, cand = lab.repo.attempts(batch_id=batch.id)
    with pytest.raises(LabError, match="continue_from"):
        preferences.create(lab, run.id, [base.id, cand.id], turn=2, actor="agent")
    batch = lab.submit(run.id, scenarios=["follow"], candidates=[None, c1.id],
                       continue_from=base.id, actor="agent")
    await lab.executor.wait(batch.id, 10)
    follow = [a.id for a in lab.repo.attempts(batch_id=batch.id)]
    comparison = preferences.create(lab, run.id, follow, actor="agent")
    assert f"continuing the conversation of attempt {base.id}" in comparison.presentation
    assert ANSWERS["baseline"] in comparison.presentation  # the shared earlier reply


async def test_the_preference_summary(lab):
    run, _ = await two_replies(lab)
    with pytest.raises(LabError, match="status is one of"):
        preferences.set_summary(lab, run.id, {"interpretations": [
            {"text": "likes teasing", "status": "certain"}]}, edited_by="agent")
    preferences.set_summary(lab, run.id, {
        "owner_statements": [{"text": "A's humour, B's brevity", "comparison": 1}],
        "interpretations": [{"text": "Prefers short teasing replies", "evidence": [1]}],
        "context": ["Not when someone is upset"]}, edited_by="agent")
    preferences.add_owner_correction(lab, run.id, "Teasing is fine, never mean",
                                     edited_by="owner (web admin)")
    prefs = lab.repo.preferences(run.id)
    assert prefs.version == 2 and prefs.body["interpretations"][0]["status"] == "assumption"
    text = preferences.summary_md(lab, run)
    assert "“A's humour, B's brevity”" in text and "[assumption] Prefers short" in text
    assert "Teasing is fine, never mean" in text and "a correction" in text


async def test_the_owner_answers_on_the_lab_page(lab, services, monkeypatch):
    run, attempts = await two_replies(lab)
    comparison = preferences.create(lab, run.id, [a.id for a in attempts], actor="agent")
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    with TestClient(create_app(services, session_secret="s"), follow_redirects=False) as http:
        token = re.search(r'name="csrf_token" value="([^"]+)"', http.get("/login").text).group(1)
        http.post("/login", data={"csrf_token": token, "password": "correct horse", "next": "/"})
        assert f"Lab run #{run.id} is waiting for your choice" in http.get("/").text
        page = http.get(f"/lab/runs/{run.id}")
        assert page.status_code == 200 and ANSWERS["c1"] in page.text
        assert 'value="A"' in page.text and 'value="both_bad"' in page.text
        hidden = http.get(f"/lab/attempts/{attempts[1].id}")
        assert ANSWERS["c1"] not in hidden.text and "hidden so they don't sway you" in hidden.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        http.post(f"/lab/comparisons/{comparison.id}/answer",
                  data={"csrf_token": csrf, "choice": "B", "comment": "shorter"})
        choice = preferences.view(lab, preferences.get(lab, comparison.id))["choice"]
        assert choice["choice"] == "B" and choice["channel"] == "owner (web admin)"
        shown = http.get(f"/lab/attempts/{attempts[1].id}")
        assert ANSWERS["c1"] in shown.text
        http.post(f"/lab/runs/{run.id}/preferences",
                  data={"csrf_token": csrf, "correction": "Shorter is better"})
        assert "Shorter is better" in http.get(f"/lab/runs/{run.id}").text


async def test_the_cli_relays_the_presentation_verbatim(lab, services, monkeypatch, capsys):
    run, attempts = await two_replies(lab)
    secret = lab.create_token("codex")[1]
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    with TestClient(create_app(services, session_secret="s"), follow_redirects=False) as http:
        code, out, _ = run_cli(http, secret, capsys, "--text", "ask", str(run.id),
                               "--attempt", str(attempts[0].id), "--attempt", str(attempts[1].id))
        assert code == 0
        comparison_id = int(re.search(r"Comparison (\d+)", out).group(1))
        stored = preferences.get(lab, comparison_id).presentation
        assert out.strip() == stored.strip()
        code, out, _ = run_cli(http, secret, capsys, "answer", str(comparison_id), "--choice",
                               "no_preference", "--comment", "both fine")
        assert code == 0 and json.loads(out)["choice"]["channel"] == "owner via agent:codex"
        code, out, err = run_cli(http, secret, capsys, "answer", str(comparison_id),
                                 "--choice", "A")
        assert code == 3 and "correct it" in err
