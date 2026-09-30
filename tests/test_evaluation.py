"""Evaluation harness: case loading, prompt building, checks, running
against a fake streaming model, reports and case extraction."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naruto.evaluation import __main__ as cli
from naruto.evaluation.cases import CaseError, load_cases, parse_case
from naruto.evaluation.checks import run_checks
from naruto.evaluation.extract import ExtractError, extract_case
from naruto.evaluation.report import render_markdown, write_report
from naruto.evaluation.runner import ModelTarget, build_prompt, evaluate, load_settings_db, parse_model

FIXTURES = Path(__file__).parent / "fixtures"
CASES = FIXTURES / "eval_cases.json"
EXPORT = FIXTURES / "export_basic_group.json"


# ------------------------------------------------------------------- cases

def test_example_cases_load_with_defaults():
    cases = {case.id: case for case in load_cases(CASES)}
    assert len(cases) == 6
    focus = cases["focus-direct-question"]
    assert focus.chat_title == "BBQ crew" and focus.timezone == "Asia/Singapore"
    assert len(focus.members) == 3 and focus.has_auto_checks
    assert focus.messages[0].date == 1790416800  # 2026-09-26 18:00 +08
    assert cases["vision-describe"].trigger.image == FIXTURES / "images" / "orange.png"
    assert cases["vision-describe"].trigger.media == "photo"
    assert cases["character-serious-moment"].messages[1].from_bot
    assert cases["tool-create-poll"].has_auto_checks


@pytest.mark.parametrize("raw,message", [
    ({"trigger": {"from": "A", "text": "x"}}, "needs an 'id'"),
    ({"id": "a"}, "'trigger' is required"),
    ({"id": "a", "category": "vibes", "trigger": {"from": "A"}}, "unknown category"),
    ({"id": "a", "trigger": {"text": "x"}}, "'from' is required"),
    ({"id": "a", "trigger": {"from": "A", "reply_to": 9}}, "reply_to 9"),
    ({"id": "a", "trigger": {"from": "A"}, "expect": {"sounds_nice": True}}, "unknown expectations"),
    ({"id": "a", "trigger": {"from": "A", "date": "someday"}}, "invalid date"),
    ({"id": "a", "messages": [{"id": 1, "from": "A"}], "trigger": {"id": 1, "from": "B"}}, "unique"),
])
def test_invalid_cases(raw, message):
    with pytest.raises(CaseError, match=message):
        parse_case(raw, FIXTURES)


def test_load_cases_file_errors(tmp_path):
    missing = tmp_path / "none.json"
    with pytest.raises(CaseError, match="Could not read"):
        load_cases(missing)
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(CaseError, match="non-empty"):
        load_cases(empty)
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps([{"id": "a", "trigger": {"from": "A"}}] * 2))
    with pytest.raises(CaseError, match="unique"):
        load_cases(dup)


# ------------------------------------------------------------------ prompt

def test_prompt_is_built_by_the_production_builder():
    cases = {case.id: case for case in load_cases(CASES)}
    prompt, services = build_prompt(cases["search-reply-to-older-message"], {})
    context, current = prompt.messages[1]["content"], prompt.messages[2]["content"]
    assert prompt.window_size == 2  # case setting override
    assert "My flight lands at 7:40am" not in context
    assert "It replies to this earlier message" in current and "7:40am" in current
    assert "@naruto_bot" not in current
    assert "- Wei (@weiwei), also called Always Late" in context

    prompt, _ = build_prompt(cases["character-serious-moment"], {})
    assert "Naruto (you)" in prompt.messages[1]["content"]
    prompt, _ = build_prompt(cases["vision-describe"], {})
    assert prompt.messages[2]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_base_settings_apply_and_invalid_ones_fail():
    case = load_cases(CASES)[0]
    prompt, _ = build_prompt(case, {"persona.prompt": "You are a test persona.", "unknown": 1})
    assert prompt.messages[0]["content"].startswith("You are a test persona.")
    with pytest.raises(ValueError, match="context.recent_window"):
        build_prompt(case, {"context.recent_window": -1})


def test_settings_db_is_read(tmp_path):
    from naruto.db import open_database
    from naruto.settings.service import SettingsService

    path = tmp_path / "bot.db"
    database = open_database(str(path))
    SettingsService(database).set("model.temperature", 0.3, actor="t")
    database.close()
    assert load_settings_db(str(path)) == {"model.temperature": 0.3}


def test_parse_model():
    assert parse_model("qwen", "http://d/v1") == ModelTarget("qwen", "http://d/v1")
    assert parse_model("qwen@http://x:8080/v1", "http://d/v1") == ModelTarget("qwen", "http://x:8080/v1")


# ------------------------------------------------------------------ checks

def test_checks():
    expect = {"contains_any": ["Saturday", "sat"], "contains_all": ["6pm"], "not_contains": ["ramen"],
              "regex": r"\bpit \d+", "min_chars": 5, "max_chars": 60, "reply_threaded": True}
    results = {r.name: r for r in run_checks(expect, "Saturday at 6pm, pit 42!", threaded=True)}
    assert all(r.passed for r in results.values())
    failing = {r.name: r for r in run_checks(expect, "Ramen time" + "!" * 60, threaded=False)}
    assert {n for n, r in failing.items() if not r.passed} == {
        "contains_any", "contains_all", "not_contains", "regex", "max_chars", "reply_threaded"}
    assert run_checks({"manual": "judge me"}, "x", False) == []


# ------------------------------------------------------------------ runner

class FakeStreamClient:
    """Streams a scripted answer per case, detected from the prompt."""

    def __init__(self, answers, fail=False):
        self.answers = answers
        self.fail = fail
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise ConnectionError("server down")
        current = kwargs["messages"][-1]["content"]
        text = current if isinstance(current, str) else current[0]["text"]
        answer = next((a for key, a in self.answers.items() if key in text), "Heh.")
        return self._stream(answer)

    async def _stream(self, answer):
        def chunk(content=None, reasoning=None, finish=None, tool_calls=None):
            delta = SimpleNamespace(content=content, reasoning_content=reasoning, model_extra={},
                                    tool_calls=tool_calls)
            return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)
        yield chunk(reasoning="thinking")
        if isinstance(answer, tuple):  # (tool name, arguments): streamed in two pieces
            name, arguments = answer
            raw = json.dumps(arguments)
            half = len(raw) // 2
            yield chunk(tool_calls=[SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(
                name=name, arguments=raw[:half]))])
            yield chunk(tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(
                name=None, arguments=raw[half:]))])
            yield chunk(finish="tool_calls")
        else:
            for piece in answer.split(" "):
                yield chunk(content=piece + " ")
            yield chunk(finish="stop")
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(
            model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 12}))


ANSWERS = {
    "bouldering": "[REPLY] Oi! Warm up first and trust your shoes.",
    "decide so far": "Alright: Saturday 6pm at East Coast pit 42. Still open: who buys the meat?",
    "pick her up": "Her flight lands at 7:40am, so leave by 7!",
    "really rough": "Hey, that sounds tough. I'm here, and we'll get food this weekend.",
    "what colour": "Looks blue to me!",
    "make a poll": ("create_poll", {"question": "BBQ day?", "options": ["Saturday", "Sunday"]}),
    "poll is up": "Poll's up, vote!",
}


async def test_evaluate_scores_each_case():
    cases = load_cases(CASES)
    client = FakeStreamClient(ANSWERS)
    attempts = await evaluate(cases, [ModelTarget("fake", "http://x/v1")],
                              client_factory=lambda target: client, keep_prompts=True)
    by_case = {a.case_id: a for a in attempts}
    assert by_case["focus-direct-question"].status == "pass"
    assert by_case["focus-direct-question"].threaded is True
    assert by_case["focus-direct-question"].text == "Oi! Warm up first and trust your shoes."
    assert by_case["summarize-plan"].status == "pass"
    assert by_case["search-reply-to-older-message"].status == "pass"
    assert by_case["character-serious-moment"].status == "pass"
    assert by_case["vision-describe"].status == "fail"
    poll = by_case["tool-create-poll"]
    assert poll.status == "pass" and poll.text == "Poll's up, vote!"
    assert poll.tool_calls == [{"name": "create_poll", "error": False, "arguments": {
        "question": "BBQ day?", "options": ["Saturday", "Sunday"]}}]
    assert poll.model_requests == 2
    first = by_case["focus-direct-question"]
    assert first.ttft_ms is not None and first.total_ms >= first.ttft_ms
    assert first.reasoning == "thinking" and first.usage["completion_tokens"] == 12
    assert first.prompt[0]["role"] == "system"
    request = client.requests[0]
    assert request["stream"] is True and request["model"] == "fake"
    assert request["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert request["extra_body"]["reasoning_effort"] == "low"
    assert "create_poll" in [tool["function"]["name"] for tool in request["tools"]]


async def test_evaluate_records_errors_and_repeats():
    cases = load_cases(CASES)[:1]
    attempts = await evaluate(cases, [ModelTarget("a", "x"), ModelTarget("b", "y")],
                              client_factory=lambda t: FakeStreamClient({}, fail=True), repeat=2)
    assert len(attempts) == 4
    assert all(a.status == "error" and "server down" in a.error for a in attempts)


async def test_report_files(tmp_path):
    cases = load_cases(CASES)
    targets = [ModelTarget("fake-a", "x"), ModelTarget("fake-b", "y")]
    attempts = await evaluate(cases, targets, client_factory=lambda t: FakeStreamClient(ANSWERS),
                              repeat=2)
    results, report = write_report(attempts, cases, targets, tmp_path / "out")
    data = json.loads(results.read_text())
    assert [s["model"] for s in data["summary"]] == ["fake-a", "fake-b"]
    assert data["summary"][0]["pass_rate"] == 5 / 6  # 5 of 6 auto-checked cases
    markdown = report.read_text()
    assert "| `fake-a` | 83% (10/12)" in markdown
    assert "| [vision-describe](#vision-describe) | vision | 0/2 pass | 0/2 pass |" in markdown
    assert "failed contains_any" in markdown
    assert '- tool `create_poll` {"question": "BBQ day?"' in markdown
    assert "**Judge by hand:** Warm and supportive" in markdown
    assert render_markdown([], cases, targets).startswith("# Evaluation report")


# ----------------------------------------------------------------- extract

def test_extract_case_from_export():
    case = extract_case(EXPORT, 1000009, before=4, case_id="weather", category="focus")
    assert case["id"] == "weather" and case["chat"]["title"] == "BBQ crew"
    assert [m["id"] for m in case["messages"]] == [1000005, 1000006, 1000007, 1000008]
    assert case["messages"][0].get("reply_to") is None  # target outside the window
    assert case["messages"][2]["media"] == "sticker"
    assert case["trigger"]["text"] == "@naruto_bot Sunny on Saturday"
    assert {m["id"] for m in case["members"]} == {7, 8, 9}
    parse_case(case, FIXTURES)  # the skeleton is a valid case
    with pytest.raises(ExtractError):
        extract_case(EXPORT, 42)


# --------------------------------------------------------------------- cli

def test_cli_run_and_extract(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_client_factory",
                        lambda api_key, timeout: (lambda target: FakeStreamClient(ANSWERS)))
    code = cli.main(["run", str(CASES), "--model", "fake@http://x/v1", "--out", str(tmp_path / "r"),
                     "--only", "focus-direct-question,summarize-plan"])
    output = capsys.readouterr().out
    assert code == 0 and "fake: auto-checked pass rate 100%" in output
    assert (tmp_path / "r" / "report.md").exists()

    out = tmp_path / "case.json"
    assert cli.main(["extract", "--export", str(EXPORT), "--trigger", "1000006",
                     "--out", str(out)]) == 0
    assert json.loads(out.read_text())["trigger"]["text"].endswith("the pit last time")
    assert cli.main(["extract", "--export", str(EXPORT), "--trigger", "1", "--out", str(out)]) == 2

    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert cli.main(["run", str(bad), "--model", "x", "--out", str(tmp_path / "z")]) == 2


def test_cli_warns_when_writing_inside_the_repo(capsys):
    cli._warn_if_in_repo(cli.REPO_ROOT / "eval-out")
    assert "inside the repository" in capsys.readouterr().err
    cli._warn_if_in_repo(Path("/tmp/somewhere-else"))
    assert capsys.readouterr().err == ""
