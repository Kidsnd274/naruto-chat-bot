"""Evaluation reports: results.json (everything) and report.md (summary,
per-case table and the answers to review by hand)."""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from statistics import median

from naruto.evaluation.cases import Case
from naruto.evaluation.runner import Attempt, ModelTarget


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def _seconds(ms: int | float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} s"


def summarize(attempts: list[Attempt], model: str) -> dict:
    mine = [a for a in attempts if a.model == model]
    checked = [a for a in mine if a.status in ("pass", "fail")]
    passed = [a for a in checked if a.status == "pass"]
    ttft = [a.ttft_ms for a in mine if a.ttft_ms is not None]
    total = [a.total_ms for a in mine if a.total_ms is not None]
    completion = [a.usage.get("completion_tokens") for a in mine
                  if a.usage and a.usage.get("completion_tokens") is not None]
    return {
        "model": model,
        "attempts": len(mine),
        "checked": len(checked),
        "passed": len(passed),
        "pass_rate": (len(passed) / len(checked)) if checked else None,
        "manual": sum(a.status == "manual" for a in mine),
        "errors": sum(a.status == "error" for a in mine),
        "skipped": sum(a.status == "skipped" for a in mine),
        "ttft_median_ms": median(ttft) if ttft else None,
        "ttft_p90_ms": _percentile(ttft, 0.9),
        "total_median_ms": median(total) if total else None,
        "total_p90_ms": _percentile(total, 0.9),
        "prompt_tokens_mean": (sum(a.prompt_tokens for a in mine) / len(mine)) if mine else None,
        "completion_tokens_mean": (sum(completion) / len(completion)) if completion else None,
    }


def _cell(attempts: list[Attempt]) -> str:
    if not attempts:
        return "—"
    statuses = [a.status for a in attempts]
    if len(statuses) == 1:
        return statuses[0]
    checked = [s for s in statuses if s in ("pass", "fail")]
    if checked:
        return f"{checked.count('pass')}/{len(checked)} pass"
    return ", ".join(sorted(set(statuses)))


def render_markdown(attempts: list[Attempt], cases: list[Case], targets: list[ModelTarget]) -> str:
    models = [t.label for t in targets]
    lines = [f"# Evaluation report", "",
             f"{datetime.now().astimezone():%Y-%m-%d %H:%M %Z} · {len(cases)} cases · "
             f"{', '.join(f'`{m}`' for m in models)}", "", "## Summary", "",
             "| Model | Auto-checked pass rate | Manual review | Errors | TTFT median / p90 | "
             "Total median / p90 | Prompt tokens (est.) | Output tokens |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for model in models:
        s = summarize(attempts, model)
        rate = "—" if s["pass_rate"] is None else f"{s['pass_rate']:.0%} ({s['passed']}/{s['checked']})"
        output = "—" if s["completion_tokens_mean"] is None else f"{s['completion_tokens_mean']:.0f}"
        prompt = "—" if s["prompt_tokens_mean"] is None else f"{s['prompt_tokens_mean']:.0f}"
        lines.append(
            f"| `{model}` | {rate} | {s['manual']} | {s['errors']} | "
            f"{_seconds(s['ttft_median_ms'])} / {_seconds(s['ttft_p90_ms'])} | "
            f"{_seconds(s['total_median_ms'])} / {_seconds(s['total_p90_ms'])} | {prompt} | {output} |")
    lines += ["", "## Cases", "", "| Case | Category | " + " | ".join(f"`{m}`" for m in models) + " |",
              "| --- | --- | " + " | ".join("---" for _ in models) + " |"]
    for case in cases:
        cells = [_cell([a for a in attempts if a.case_id == case.id and a.model == m]) for m in models]
        lines.append(f"| [{case.id}](#{case.id}) | {case.category} | " + " | ".join(cells) + " |")
    lines += ["", "## Answers", ""]
    for case in cases:
        lines += [f"### {case.id}", ""]
        if case.description:
            lines += [case.description, ""]
        lines += [f"**Trigger** ({case.trigger.sender}): {case.trigger.text}", ""]
        if case.expect.get("manual"):
            lines += [f"**Judge by hand:** {case.expect['manual']}", ""]
        for attempt in [a for a in attempts if a.case_id == case.id]:
            timing = f"TTFT {_seconds(attempt.ttft_ms)}, total {_seconds(attempt.total_ms)}"
            lines.append(f"- `{attempt.model}` #{attempt.attempt} — **{attempt.status}** ({timing})")
            if attempt.error:
                lines.append(f"  - error: {attempt.error}")
            for call in attempt.tool_calls:
                arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
                failed = " (failed)" if call.get("error") else ""
                lines.append(f"  - tool `{call['name']}` {arguments[:300]}{failed}")
            for check in attempt.checks:
                if not check.passed:
                    lines.append(f"  - failed {check.name}: {check.detail}")
            if attempt.text:
                quoted = attempt.text.replace("\n", "\n  > ")
                lines.append(f"  > {'[REPLY] ' if attempt.threaded else ''}{quoted}")
        lines.append("")
    return "\n".join(lines)


def write_report(attempts: list[Attempt], cases: list[Case], targets: list[ModelTarget],
                 out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    report_path = out_dir / "report.md"
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "models": [{"name": t.name, "endpoint": t.endpoint} for t in targets],
        "summary": [summarize(attempts, t.label) for t in targets],
        "attempts": [asdict(a) for a in attempts],
    }
    results_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_markdown(attempts, cases, targets), encoding="utf-8")
    return results_path, report_path
