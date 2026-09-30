"""Automatic checks on a model answer. Anything subjective (staying in
character, choosing the right topic) is left to the ``manual`` note."""

from dataclasses import dataclass
import json
import re


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def _lower(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(v).lower() for v in values or []]


def run_checks(expect: dict, text: str, threaded: bool) -> list[CheckResult]:
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
    arguments (case-insensitive). An empty list means no tool may be called."""
    made = [call["name"] for call in calls]
    if not expected:
        return [CheckResult("tool_calls", not calls,
                            f"called {made}" if calls else "no tools called")]
    results = []
    for want in expected:
        if isinstance(want, str):
            want = {"name": want}
        name = want.get("name")
        matching = [call for call in calls if call["name"] == name and not call.get("error")]
        wanted_args = want.get("arguments") or {}
        ok = [call for call in matching if all(
            str(value).lower() in json.dumps(call.get("arguments", {}).get(key, ""),
                                             ensure_ascii=False).lower()
            for key, value in wanted_args.items())]
        detail = f"called {made}" if not ok else f"{name} called"
        results.append(CheckResult(f"tool:{name}", bool(ok), detail))
    return results
