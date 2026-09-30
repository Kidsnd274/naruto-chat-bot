"""Automatic checks on a model answer. Anything subjective (staying in
character, choosing the right topic) is left to the ``manual`` note."""

from dataclasses import dataclass
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
