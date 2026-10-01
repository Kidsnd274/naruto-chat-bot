"""Deterministic checks on one turn of a scenario, and outcome classes.

Anything subjective (tone, relevance, staying in character) is left to
judgments: ``expect.judge`` says what a judge should look for.

Outcome classes keep different kinds of failure apart, so a candidate can't
look better just because fewer cases were evaluated:

- ``pass`` / ``fail``: every check passed / a check failed (a wrong answer);
- ``action_failed``: a tool the turn needed was called but failed;
- ``error``: the model server, a time-out, the queue or a crash;
- ``skipped``: not run (an unsupported feature, the budget);
- ``cancelled``: stopped by the owner or the agent;
- ``unjudged``: ran, but has no checks (waiting for judgments).
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
import re

PASS = "pass"
FAIL = "fail"
ACTION_FAILED = "action_failed"
ERROR = "error"
SKIPPED = "skipped"
CANCELLED = "cancelled"
UNJUDGED = "unjudged"
OUTCOMES = (PASS, FAIL, ACTION_FAILED, ERROR, SKIPPED, CANCELLED, UNJUDGED)
# When the turns of one attempt differ, the attempt takes the first of these.
PRECEDENCE = (ERROR, CANCELLED, ACTION_FAILED, FAIL, SKIPPED, UNJUDGED, PASS)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    kind: str = "check"  # check | action (a needed tool failed) | skipped

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail,
                "kind": self.kind}


def _lower(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(v).lower() for v in values or []]


def run_checks(expect: dict, text: str, threaded: bool) -> list[CheckResult]:
    """Checks on the answer text and how it was sent."""
    results = []
    lowered = text.lower()
    if "contains_any" in expect:
        options = _lower(expect["contains_any"])
        found = [o for o in options if o in lowered]
        results.append(CheckResult("contains_any", bool(found),
                                   f"found {found}" if found else f"none of {options}"))
    if "contains_all" in expect:
        missing = [o for o in _lower(expect["contains_all"]) if o not in lowered]
        results.append(CheckResult("contains_all", not missing,
                                   f"missing {missing}" if missing else "all present"))
    if "not_contains" in expect:
        present = [o for o in _lower(expect["not_contains"]) if o in lowered]
        results.append(CheckResult("not_contains", not present,
                                   f"contains {present}" if present else "clean"))
    if expect.get("regex"):
        matched = re.search(expect["regex"], text, re.IGNORECASE | re.DOTALL) is not None
        results.append(CheckResult("regex", matched, expect["regex"]))
    if "min_chars" in expect:
        results.append(CheckResult("min_chars", len(text) >= int(expect["min_chars"]),
                                   f"{len(text)} characters"))
    if "max_chars" in expect:
        results.append(CheckResult("max_chars", len(text) <= int(expect["max_chars"]),
                                   f"{len(text)} characters"))
    if expect.get("reply_threaded") is not None:
        wanted = bool(expect["reply_threaded"])
        results.append(CheckResult("reply_threaded", threaded == wanted,
                                   f"threaded={threaded}"))
    return results


def check_tool_calls(expected: list, calls: list[dict]) -> list[CheckResult]:
    """``expected`` lists tools that must be called, each ``{"name": ...}``
    with optional ``"arguments"`` whose values must appear in the call's
    arguments (case-insensitive). An empty list means no tool may be called.
    A tool that was called but failed is an action failure, not a wrong
    answer."""
    made = [call["name"] for call in calls]
    if not expected:
        return [CheckResult("tool_calls", not calls,
                            f"called {made}" if calls else "no tools called")]
    results = []
    for want in expected:
        if isinstance(want, str):
            want = {"name": want}
        name = want.get("name")
        wanted_args = want.get("arguments") or {}

        def matches(call) -> bool:
            return call["name"] == name and all(
                str(value).lower() in json.dumps(call.get("arguments", {}).get(key, ""),
                                                 ensure_ascii=False).lower()
                for key, value in wanted_args.items())

        ok = [call for call in calls if matches(call) and not call.get("error")]
        failed = [call for call in calls if matches(call) and call.get("error")]
        if ok:
            results.append(CheckResult(f"tool:{name}", True, f"{name} called"))
        elif failed:
            results.append(CheckResult(f"tool:{name}", False,
                                       f"{name} was called but failed: "
                                       f"{str(failed[-1].get('result', ''))[:200]}", "action"))
        else:
            results.append(CheckResult(f"tool:{name}", False, f"called {made}"))
    return results


def check_forbidden_tools(forbidden: list, calls: list[dict]) -> CheckResult:
    used = sorted({call["name"] for call in calls} & set(forbidden))
    return CheckResult("forbidden_tools", not used,
                       f"called {used}" if used else "none of them called")


# ------------------------------------------------------------ state checks

def _matches(item: dict, want, text_key: str) -> bool:
    if isinstance(want, str):
        want = {text_key: want}
    for key, value in want.items():
        have = item.get(key)
        if have is None:
            return False
        if isinstance(have, list):
            have = " ".join(map(str, have))
        if str(value).lower() not in str(have).lower():
            return False
    return True


def _list_check(name: str, wanted: list, items: list[dict], text_key: str) -> CheckResult:
    if not wanted:
        return CheckResult(name, not items, f"{len(items)} found" if items else "none")
    missing = [w for w in wanted if not any(_matches(item, w, text_key) for item in items)]
    return CheckResult(name, not missing,
                       f"missing {missing}" if missing else f"all {len(wanted)} found")


def check_state(expect_state: dict, state: dict, tz) -> list[CheckResult]:
    """``state`` is the sandbox after the turn (see Sandbox.state()).
    Lists say what must exist (text matches are case-insensitive
    substrings); an empty list says there must be none."""
    results = []
    if "reminders" in expect_state:
        items = []
        for reminder in state.get("reminders", []):
            due = datetime.fromtimestamp(reminder["due_at"], tz).strftime("%Y-%m-%d %H:%M")
            items.append({"text": reminder["text"], "due": due})
        results.append(_list_check("state:reminders", expect_state["reminders"], items, "text"))
    if "board" in expect_state:
        board = state.get("board", {})
        for section, wanted in (expect_state["board"] or {}).items():
            items = [{"text": item["text"], "done": item["done"]}
                     for item in board.get(section, [])]
            results.append(_list_check(f"state:board.{section}", wanted, items, "text"))
    if "notes" in expect_state:
        items = [{"text": note["text"], "category": note.get("category") or ""}
                 for note in state.get("notes", [])]
        results.append(_list_check("state:notes", expect_state["notes"], items, "text"))
    if "polls" in expect_state:
        results.append(_list_check("state:polls", expect_state["polls"],
                                   state.get("polls", []), "question"))
    if "plans" in expect_state:
        results.append(_list_check("state:plans", expect_state["plans"],
                                   state.get("plans", []), "title"))
    if "pins" in expect_state:
        count = len(state.get("pins", []))
        results.append(CheckResult("state:pins", count == int(expect_state["pins"]),
                                   f"{count} pinned"))
    return results


# ---------------------------------------------------------------- outcomes

@dataclass
class TurnVerdict:
    outcome: str
    checks: list[CheckResult] = field(default_factory=list)
    reason: str | None = None


def turn_outcome(checks: list[CheckResult], *, error: str | None = None,
                 skipped: str | None = None) -> TurnVerdict:
    if error:
        return TurnVerdict(ERROR, checks, error)
    if skipped:
        return TurnVerdict(SKIPPED, checks, skipped)
    failing = [c for c in checks if not c.passed and c.kind != "skipped"]
    if any(c.kind == "action" for c in failing):
        return TurnVerdict(ACTION_FAILED, checks)
    if failing:
        return TurnVerdict(FAIL, checks)
    if any(c.kind != "skipped" for c in checks):
        return TurnVerdict(PASS, checks)
    if checks:
        return TurnVerdict(SKIPPED, checks, "every check needs something unsupported")
    return TurnVerdict(UNJUDGED, checks)


def combine(outcomes: list[str]) -> str:
    """One outcome for an attempt from its turns' outcomes. Turns without
    checks (setting up a conversation, or waiting for judgments) don't hide
    the outcome of the turns that have them."""
    checked = [o for o in outcomes if o != UNJUDGED] or outcomes
    for outcome in PRECEDENCE:
        if outcome in checked:
            return outcome
    return UNJUDGED
