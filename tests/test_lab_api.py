"""The lab's JSON API, its tokens and the Lab page, and the CLI client
driven through the app (no network)."""

import json
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from fakes import ScriptedLLM, tool_call
from naruto.lab import client as cli
from naruto.lab.service import LabService
from naruto.web import auth
from naruto.web.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = {
    "id": "hello", "timezone": "UTC", "time": "2026-10-03T18:00:00+00:00",
    "members": [{"id": 7, "name": "Alice", "username": "alice"}],
    "turns": [{"from": "Alice", "from_id": 7, "text": "@naruto_bot hi",
               "expect": {"contains_any": ["hi"]}}],
}
RUN = {"objective": "Answer the current question", "budget": {"attempts": 20}}


@pytest.fixture
def scripts():
    return []


@pytest.fixture
def lab(services, tmp_path, scripts):
    services.settings.set("model.name", "test-model", actor="test")

    def factory(settings, attempt):
        return ScriptedLLM(*(scripts.pop(0) if scripts else ["Oi, hi!"]))

    services.lab = LabService(services, tmp_path / "lab", llm_factory=factory)
    return services.lab


@pytest.fixture
def http(services, lab, monkeypatch):
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    app = create_app(services, session_secret="test-secret")
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def secret(lab):
    return lab.create_token("claude-code", chats=[-5])[1]


def api(http, secret, method, path, body=None, **params):
    return http.request(method, f"/api/lab/v1{path}", json=body, params=params,
                        headers={"Authorization": f"Bearer {secret}"})


class AppClient(cli.Client):
    """The CLI's client, sending through the test app."""

    def __init__(self, http, secret):
        super().__init__("http://testserver", secret)
        self.http = http

    def _send(self, method, url, data, headers):
        response = self.http.request(method, url, content=data, headers=headers)
        return response.status_code, response.content


def run_cli(http, secret, capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv), client=AppClient(http, secret))
    out, err = capsys.readouterr()
    return code, out, err


# ------------------------------------------------------------------ tokens

def test_the_api_needs_a_valid_token(http, lab, secret):
    assert http.get("/api/lab/v1/capabilities").status_code == 401
    bad = api(http, "nlab_wrong", "GET", "/capabilities")
    assert bad.status_code == 401 and bad.json()["error"] == "unauthorized"
    assert api(http, secret, "GET", "/capabilities").status_code == 200
    token = lab.repo.tokens()[0]
    assert token.last_used_at is not None and token.token_hash != secret
    lab.revoke_token(token.id)
    assert api(http, secret, "GET", "/capabilities").status_code == 401
    unknown = api(http, secret, "GET", "/nothing")
    assert unknown.headers["content-type"].startswith("application/json")


def test_the_lab_page_creates_tokens_once(http, lab, services):
    services.chats.upsert_seen(-5, title="BBQ crew")
    token = re.search(r'name="csrf_token" value="([^"]+)"', http.get("/login").text).group(1)
    http.post("/login", data={"csrf_token": token, "password": "correct horse", "next": "/"})
    page = http.get("/lab")
    assert page.status_code == 200 and "API tokens" in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    created = http.post("/lab/tokens", data={"csrf_token": csrf, "name": "codex",
                                             "chats": "-5", "may_activate": "on"})
    secret = re.search(r"(nlab_[\w-]+)", created.text).group(1)
    assert "isn't shown again" in created.text
    stored = lab.repo.tokens()[0]
    assert stored.name == "codex" and stored.chats == [-5] and stored.may_activate
    assert secret not in http.get("/lab").text
    assert api(http, secret, "GET", "/runs").status_code == 200
    assert http.post(f"/lab/tokens/{stored.id}/revoke",
                     data={"csrf_token": csrf}).status_code == 303
    assert api(http, secret, "GET", "/runs").status_code == 401


# ------------------------------------------------------------- discovery

def test_capabilities_describe_this_version(http, secret):
    data = api(http, secret, "GET", "/capabilities").json()
    keys = {row["key"]: row for row in data["settings"]}
    assert keys["persona.prompt"]["tunable"] == "always"
    assert keys["persona.prompt"]["file"] == "persona.md"
    assert keys["context.recent_window"]["tunable"] == "when the run's scope names it"
    assert keys["model.temperature"]["also_used_by_background_tasks"]
    assert "model.endpoint_url" not in keys and "retention.logs_days" not in keys
    tools = {tool["name"]: tool["sandbox"] for tool in data["tools"]}
    assert tools["create_poll"].startswith("simulated") and "image" in tools["describe_image"]
    assert data["scenario"]["example"]["turns"][1]["reply_to_answer"]
    assert any("Web search" in item for item in data["deferred"])
    assert data["outcomes"]["action_failed"]
    active = api(http, secret, "GET", "/config/active").json()
    assert active["settings"]["persona.prompt"].startswith("You are Naruto")


# -------------------------------------------------------------------- flow

def test_a_run_from_start_to_finish(http, secret, scripts):
    run = api(http, secret, "POST", "/runs", RUN)
    assert run.status_code == 201
    run = run.json()
    assert run["state"]["state"] == "idle" and run["created_by"] == "agent:claude-code"
    rid = run["id"]
    assert api(http, secret, "POST", "/scenarios", {"scenario": SCENARIO}).json()["version"] == 1
    candidate = api(http, secret, "POST", f"/runs/{rid}/candidates",
                    {"name": "warmer", "changes": {"skills.banter.instructions": "Be warm."},
                     "hypothesis": "Warmer replies"})
    assert candidate.status_code == 201 and candidate.json()["label"] == "c1-warmer"
    bad = api(http, secret, "POST", f"/runs/{rid}/candidates",
              {"name": "x", "changes": {"retention.logs_days": 3}})
    assert bad.status_code == 400 and "outside this run's scope" in bad.json()["message"]

    batch = api(http, secret, "POST", f"/runs/{rid}/attempts",
                {"scenario": "hello", "candidates": ["baseline", "c1"], "wait": 10})
    assert batch.status_code == 202
    batch = batch.json()
    assert batch["status"] == "done" and batch["counts"] == {"pass": 2}
    assert [a["candidate"] for a in batch["attempts"]] == ["baseline", "c1-warmer"]
    assert batch["attempts"][0]["answers"] == ["Oi, hi!"]
    attempt = api(http, secret, "GET", f"/attempts/{batch['attempts'][1]['id']}",
                  prompts="true").json()
    assert attempt["turns"][0]["prompt"][0]["content"].count("Be warm.") == 1
    assert attempt["conditions"]["model"] == "test-model"

    export = api(http, secret, "GET", f"/runs/{rid}/export").json()
    assert export["folder"] == f"run-{rid}-answer-the-current-question"
    files = export["files"]
    assert files["candidates/c1-warmer/banter.md"] == "Be warm.\n"
    assert "Be warm." in files["candidates/c1-warmer/CHANGES.md"]
    assert "### baseline" in files["replies/hello.md"]
    assert "Oi, hi!" in files["replies/hello.md"]

    finished = api(http, secret, "POST", f"/runs/{rid}/finish",
                   {"reason": "no_improvement",
                    "recommendation": {"action": "keep_baseline"}}).json()
    assert finished["status"] == "finished" and finished["stop_reason"] == "no_improvement"
    refused = api(http, secret, "POST", f"/runs/{rid}/attempts", {"scenario": "hello"})
    assert refused.status_code == 409


def test_chat_scope_guards_real_chat_scenarios(http, lab, secret):
    record = lab.repo.add_scenario("from-chat", {**SCENARIO, "id": "from-chat",
                                                 "origin": "history"},
                                   origin="history", focused=False, chat_id=-6, reason=None,
                                   created_by="owner")
    assert api(http, secret, "GET", f"/scenarios/{record.id}").status_code == 403
    listed = api(http, secret, "GET", "/scenarios").json()["scenarios"]
    assert "from-chat" not in [s["slug"] for s in listed]
    allowed = lab.repo.add_scenario("from-chat-5", {**SCENARIO, "id": "from-chat-5",
                                                    "origin": "history"},
                                    origin="history", focused=False, chat_id=-5, reason=None,
                                    created_by="owner")
    assert api(http, secret, "GET", f"/scenarios/{allowed.id}").status_code == 200


# --------------------------------------------------------------------- cli

def test_the_cli_drives_a_run_and_exports_it(http, secret, capsys, tmp_path, scripts):
    spec = tmp_path / "run.json"
    spec.write_text(json.dumps(RUN))
    code, out, _ = run_cli(http, secret, capsys, "run", "start", "--file", str(spec))
    assert code == 0
    rid = json.loads(out)["id"]

    scenario_file = tmp_path / "scenarios.json"
    vision = {"id": "orange", "timezone": "UTC", "members": SCENARIO["members"],
              "turns": [{"from": "Alice", "from_id": 7, "text": "@naruto_bot colour?",
                         "image": "orange.png", "expect": {"contains_any": ["orange"]}}]}
    (tmp_path / "orange.png").write_bytes((FIXTURES / "images" / "orange.png").read_bytes())
    scenario_file.write_text(json.dumps({"scenarios": [SCENARIO, vision]}))
    code, out, _ = run_cli(http, secret, capsys, "scenario", "add", "--file", str(scenario_file))
    assert code == 0 and [s["slug"] for s in json.loads(out)] == ["hello", "orange"]

    code, out, _ = run_cli(http, secret, capsys, "export", str(rid), "--out", str(tmp_path / "x"))
    folder = tmp_path / "x" / f"run-{rid}-answer-the-current-question"
    baseline = folder / "baseline"
    assert code == 0 and (baseline / "persona.md").exists()

    edited = tmp_path / "edited"
    edited.mkdir()
    for item in baseline.iterdir():
        (edited / item.name).write_text(item.read_text())
    (edited / "banter.md").write_text("Be cheeky, but kind.\n")
    code, out, _ = run_cli(http, secret, capsys, "candidate", "add", str(rid), "--name",
                           "cheeky", "--from-dir", str(edited), "--hypothesis", "cheekier")
    assert code == 0 and json.loads(out)["changes"] == {
        "skills.banter.instructions": "Be cheeky, but kind."}

    scripts += [["Oi, hi!"], ["Heh, hi!"], ["It's orange!"]]
    code, out, _ = run_cli(http, secret, capsys, "--text", "try", str(rid), "--scenario", "hello",
                           "--candidate", "baseline", "--candidate", "c1", "--wait", "10",
                           "--quiet")
    assert code == 0 and "baseline  try 1: pass" in out and "c1-cheeky  try 1: pass" in out
    assert "turn 1: Heh, hi!" in out
    code, out, _ = run_cli(http, secret, capsys, "try", str(rid), "--scenario", "orange",
                           "--wait", "10", "--quiet")
    assert json.loads(out)["attempts"][0]["outcome"] == "pass"

    code, out, _ = run_cli(http, secret, capsys, "export", str(rid), "--out", str(tmp_path / "x"))
    assert (folder / "candidates" / "c1-cheeky" / "banter.md").read_text() == \
        "Be cheeky, but kind.\n"
    assert "Heh, hi!" in (folder / "replies" / "hello.md").read_text()
    assert json.loads((folder / cli.MANIFEST).read_text())

    code, _, err = run_cli(http, secret, capsys, "run", "show", "999")
    assert code == 2 and "There is no run 999" in err
    code, _, err = run_cli(http, secret, capsys, "candidate", "add", str(rid), "--name", "x",
                           "--from-dir", str(baseline))
    assert code == 2 and "same as the parent" in err


def test_cli_errors_and_the_repository_warning(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("NARUTO_LAB_TOKEN", raising=False)
    code = cli.main(["--token-file", str(tmp_path / "none"), "capabilities"])
    assert code == 2 and "No token" in capsys.readouterr().err
    monkeypatch.setenv("NARUTO_LAB_TOKEN", "nlab_x")
    code = cli.main(["--url", "http://127.0.0.1:9", "capabilities"])
    assert code == 4 and "Can't reach the bot" in capsys.readouterr().err
    assert cli._inside_repo(cli.REPO_ROOT / "out") and not cli._inside_repo(tmp_path)
    target = cli.write_export("run-1-x", {"a/b.md": "1", "c.md": "2"}, tmp_path)
    cli.write_export("run-1-x", {"c.md": "3"}, tmp_path)
    assert not (target / "a").exists() and (target / "c.md").read_text() == "3"
    with pytest.raises(cli.ClientError, match="outside"):
        cli.write_export("run-1-x", {"../evil.md": "x"}, tmp_path)
