"""docs/LAB.md's walkthrough, run as written: every command of §14 against
the web admin's lab API (in process) and the scripted stand-in model. Also:
every lab command in the docs and the skill is one the client accepts, and
the two copies of the skill are the same."""

from pathlib import Path
import re
import shlex
import shutil

import pytest
from fastapi.testclient import TestClient

from lab_doc_model import answer as doc_answer
from naruto.lab import client as cli
from naruto.lab.service import LabService
from naruto.llm import ChatResult, make_tool_call
from naruto.web import auth
from naruto.web.app import create_app
from test_lab_api import AppClient

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "LAB.md"
SKILLS = [ROOT / ".agents" / "skills" / "naruto-lab" / "SKILL.md",
          ROOT / ".claude" / "skills" / "naruto-lab" / "SKILL.md"]
WALKTHROUGH = ("## 14. ", "## 15. ")
PREFIX = "python3 -m naruto.lab"


class DocModel:
    """lab_doc_model as a model client."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kwargs):
        self.calls += 1
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        current = messages[-1]["content"]
        current = current if isinstance(current, str) else " ".join(
            part.get("text", "") for part in current if isinstance(part, dict))
        text, call = doc_answer(system, current, bool(tools))
        calls = [make_tool_call("call-1", call["name"], __import__("json").dumps(
            call["arguments"]), 0)] if call else []
        return ChatResult(text=text, reasoning=None, model="doc-model", latency_ms=20,
                          usage={"prompt_tokens": 900, "completion_tokens": 30},
                          finish_reason="tool_calls" if calls else "stop", tool_calls=calls)


def section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def shell_blocks(text: str) -> list[str]:
    return re.findall(r"```sh\n(.*?)```", text, re.DOTALL)


def lab_commands(text: str) -> list[str]:
    """Every `python3 -m naruto.lab ...` line (continuations joined)."""
    joined = re.sub(r"\\\n\s*", " ", text)
    commands = []
    for line in joined.splitlines():
        line = line.strip().lstrip("$ ")
        if line.startswith(PREFIX):
            commands.append(line.split("  #")[0].split(" # ")[0].strip())
    return commands


def run_walkthrough(text: str, home: Path, execute) -> list[tuple[str, int, str]]:
    """A tiny interpreter for the walkthrough's shell: mkdir -p, cp -r,
    cat >/>> heredocs and lab commands. ``execute(argv) -> (code, out)``."""
    def path(word: str) -> Path:
        return Path(word.replace("~", str(home), 1)) if word.startswith("~") else Path(word)

    results = []
    for block in shell_blocks(text):
        lines = block.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].strip()
            index += 1
            if not line or line.startswith("#"):
                continue
            heredoc = re.fullmatch(r"cat (>>?) (\S+) <<'EOF'", line)
            if heredoc:
                body = []
                while lines[index].strip() != "EOF":
                    body.append(lines[index])
                    index += 1
                index += 1
                target = path(heredoc.group(2))
                content = "\n".join(body) + "\n"
                if heredoc.group(1) == ">>":
                    content = target.read_text() + content
                target.write_text(content)
                continue
            words = shlex.split(line.split("  #")[0])
            if words[:2] == ["mkdir", "-p"]:
                path(words[2]).mkdir(parents=True, exist_ok=True)
            elif words[:2] == ["cp", "-r"]:
                shutil.copytree(path(words[2]), path(words[3]))
            elif line.startswith(PREFIX):
                expected = re.search(r"# exits (\d+)", line)
                argv = [str(path(w)) if w.startswith("~") else w for w in words[3:]]
                code, out = execute(argv)
                wanted = int(expected.group(1)) if expected else 0
                assert code == wanted, f"{line}\nexited {code}, expected {wanted}:\n{out}"
                results.append((line, code, out))
            else:
                pytest.fail(f"The walkthrough uses a command the test can't run: {line}")
    return results


async def test_the_walkthrough_runs_as_written(services, tmp_path, monkeypatch, capsys):
    services.settings.set("model.name", "doc-model", actor="test")
    services.lab = LabService(services, tmp_path / "lab",
                              llm_factory=lambda settings, attempt: DocModel())
    secret = services.lab.create_token("claude-code")[1]
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    walkthrough = section(DOCS.read_text(), *WALKTHROUGH)
    with TestClient(create_app(services, session_secret="s"), follow_redirects=False) as http:
        def execute(argv):
            code = cli.main(argv, client=AppClient(http, secret))
            out, err = capsys.readouterr()
            return code, out + err

        results = run_walkthrough(walkthrough, tmp_path, execute)
    lab = services.lab
    assert len(results) >= 35
    first, second = lab.repo.run(1), lab.repo.run(2)
    assert first.status == "finished" and first.recommendation["action"] == "activate"
    tuning = lab.compare(1, set_ref="tuning")["summary"]
    assert tuning["baseline"]["pass_rate"] == 0 and tuning["c1-current-request-only"]["pass_rate"] == 1
    protect = lab.compare(1, set_ref="protect")
    assert protect["regressions"] == [] and protect["summary"]["c1-current-request-only"][
        "pass_rate"] == 1
    held_out = lab.compare(1, set_ref="held-out")["summary"]
    assert held_out["c1-current-request-only"]["pass_rate"] == 1
    assert [c.status for c in lab.repo.comparisons(2)] == ["answered", "answered"]
    assert lab.repo.preferences(2).body["interpretations"][0]["status"] == "assumption"
    assert second.status == "active"
    folder = tmp_path / "naruto-lab" / "run-2-find-a-banter-tone-the-owner-likes"
    for name in ("comparisons.md", "preferences.md", "report.md", "replies/tone-karaoke.md",
                 "candidates/c2-cheeky-short/CHANGES.md"):
        assert (folder / name).exists(), name
    assert "Keep teasing to one line." in (folder / "candidates/c2-cheeky-short/banter.md"
                                           ).read_text()


@pytest.mark.parametrize("path", [DOCS, *SKILLS], ids=lambda p: p.parent.name or p.name)
def test_every_documented_command_is_one_the_client_accepts(path):
    parser = cli.build_parser()
    commands = lab_commands(path.read_text())
    assert commands
    for command in commands:
        argv = shlex.split(command)[3:]
        argv = [word for word in argv if word not in ("…", "...")]
        if any(w in ("COMMAND", "RUN", "BATCH", "ID", "ATTEMPT", "S", "SLUG", "N", "cN")
               or "…" in w or "<" in w or "|" in w for w in argv):
            continue  # a template, not a runnable line
        try:
            parser.parse_args(argv)
        except SystemExit:
            pytest.fail(f"{path.name}: the client doesn't accept: {command}")


def test_the_skill_is_the_same_for_every_agent():
    first, second = (path.read_text() for path in SKILLS)
    assert first == second
    assert first.startswith("---\nname: naruto-lab\ndescription: ")
