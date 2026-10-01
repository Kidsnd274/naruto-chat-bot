"""A run as a folder of readable files (``lab export``).

    run-3-make-naruto-answer/
      README.md          objective, status, model, budget; what's where
      baseline/          the live configuration when the run started
        persona.md  rules.md  banter.md  ...  settings.json
      candidates/
        c1-cheekier/     the complete configuration, with the changes
          CHANGES.md     hypothesis, rationale and diffs
      replies/
        <scenario>.md    each configuration's replies, scenario by scenario
      comparisons.md     A/B rounds and the owner's choices (stage 5)
      preferences.md     the preference summary (stage 5)
      report.md          the report (stage 4)

The server renders the files; the CLI writes them. Replies that are part of
an A/B comparison the owner hasn't answered yet are left out, so the folder
can be read without spoiling the choice.
"""

from datetime import datetime, timezone
import json

from naruto.lab import config, report

FOLDER_PREFIX = "run"


def _when(ts: int | float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (text or "").splitlines()) or "> "


def folder_name(run) -> str:
    return f"{FOLDER_PREFIX}-{run.id}-{run.slug}"


def hidden_attempts(lab, run) -> set[int]:
    """Attempts in comparisons waiting for the owner."""
    hidden = set()
    for comparison in lab.repo.comparisons(run.id, status="pending"):
        hidden.update(int(a) for a in comparison.mapping.values())
    return hidden


def configuration_files(lab, run, candidate) -> dict[str, str]:
    keys = lab.tunable(run)
    files = config.to_files(lab.effective_settings(run, candidate), keys)
    if candidate is not None:
        files["CHANGES.md"] = changes_md(lab, run, candidate)
    return files


def changes_md(lab, run, candidate) -> str:
    view = lab.candidate_view(run, candidate)
    lines = [f"# {view['label']}", "", f"Built on: {view['parent']}", ""]
    if view["hypothesis"]:
        lines += ["## Hypothesis", "", view["hypothesis"], ""]
    if view["rationale"]:
        lines += ["## Why", "", view["rationale"], ""]
    for title, diffs in (("Changes from its parent", view["vs_parent"]),
                         ("Changes from the baseline", view["vs_baseline"])):
        lines += [f"## {title}", ""]
        if not diffs:
            lines += ["None.", ""]
            continue
        for key, change in diffs.items():
            name = config.file_name(key) or key
            lines += ["```diff", config.text_diff(change["from"], change["to"], name).rstrip(),
                      "```", ""]
    if view["background_effects"]:
        lines += ["## Also affects", "", *[f"- {e}" for e in view["background_effects"]], ""]
    return "\n".join(lines)


def replies_md(lab, run, slug: str, attempts: list, hidden: set[int]) -> str:
    record = lab.repo.latest_scenario(slug)
    body = record.body if record else {}
    lines = [f"# {slug}", ""]
    if body.get("description"):
        lines += [body["description"], ""]
    origin = body.get("origin", "synthetic")
    lines += [f"Origin: {origin}" + (" (made up for testing)" if origin == "synthetic" else ""),
              ""]
    turns = body.get("turns") or [{**(body.get("trigger") or {}), "expect": body.get("expect")}]
    by_config: dict[str, list] = {}
    for attempt in attempts:
        by_config.setdefault(lab.label(run, attempt.candidate_id), []).append(attempt)
    ordered = sorted(by_config, key=lambda label: (label != "baseline", label))
    for index, raw_turn in enumerate(turns, start=1):
        who = raw_turn.get("from", "?")
        said = raw_turn.get("text") or raw_turn.get("command") or ""
        lines += [f"## Turn {index}: {who}", "", _quote(said), ""]
        judge = (raw_turn.get("expect") or {}).get("judge")
        if isinstance(judge, dict):
            judge = "; ".join(f"{k}: {v}" for k, v in judge.items())
        if judge:
            lines += [f"*What to look for:* {judge}", ""]
        for label in ordered:
            changed = ""
            candidate = next((c for c in lab.repo.candidates(run.id) if c.label == label), None)
            if candidate is not None:
                changed = f" (changes {', '.join(sorted(candidate.changes))})"
            lines += [f"### {label}{changed}", ""]
            for attempt in by_config[label]:
                version = record and attempt.scenario_id != record.id
                tag = f"attempt {attempt.id}, try {attempt.repeat}"
                if version:
                    tag += ", an older version of the scenario"
                if attempt.id in hidden:
                    lines += [f"- {tag}: in an A/B comparison waiting for the owner; shown once "
                              "they answer.", ""]
                    continue
                turn = next((t for t in attempt.turns if t.get("index") == index), None)
                if turn is None:
                    lines += [f"- {tag}: {attempt.outcome or attempt.status} "
                              f"({attempt.reason or 'this turn did not run'})", ""]
                    continue
                failed = [f"{c['name']} ({c['detail']})" for c in turn.get("checks") or []
                          if not c["passed"]]
                summary = f"- {tag}: **{turn.get('outcome')}**"
                if failed:
                    summary += f"; failed {', '.join(failed)}"
                if turn.get("reason") or turn.get("error"):
                    summary += f"; {turn.get('reason') or turn.get('error')}"
                lines.append(summary)
                for step in (turn.get("run") or {}).get("steps") or []:
                    if step.get("type") == "tool":
                        arguments = json.dumps(step.get("arguments") or {}, ensure_ascii=False)
                        state = " (failed)" if step.get("error") else ""
                        lines.append(f"  - tool `{step['name']}` {arguments[:300]}{state}")
                answer = turn.get("answer") or ""
                if turn.get("delivery") == "none" and not answer:
                    lines += ["", "  *(sent nothing)*"]
                else:
                    prefix = "[threaded reply] " if turn.get("threaded") else ""
                    lines += ["", "  " + _quote(prefix + answer).replace("\n", "\n  ")]
                lines.append("")
    return "\n".join(lines)


def readme_md(lab, run) -> str:
    state = lab.run_state(run)
    lines = [f"# Run {run.id}: {run.objective.splitlines()[0][:100]}", "",
             run.objective, "",
             f"- Status: {state['state']}" + (f" ({run.stop_reason})" if run.stop_reason else ""),
             f"- Model: `{run.model_name or '(first listed)'}` at "
             f"`{config.redact_endpoint(run.model_endpoint)}`",
             f"- Started: {_when(run.created_at)}",
             f"- Budget used: {state['used']['attempts']} of {run.budget.get('attempts')} "
             f"attempts, {state['used']['model_requests']} of "
             f"{run.budget.get('model_requests')} model requests",
             f"- Activation: {run.spec.get('activation', {}).get('policy')}", ""]
    if run.spec.get("protected"):
        lines += ["Keep: " + "; ".join(run.spec["protected"]), ""]
    if run.warnings:
        lines += ["## Warnings", "", *[f"- {w}" for w in run.warnings], ""]
    lines += ["## Configurations", "",
              "Each folder holds the complete configuration (every prompt file and "
              "settings.json), so any one can be read on its own.", "",
              "| Folder | Built on | Changes | Hypothesis |", "| --- | --- | --- | --- |",
              "| `baseline/` | — | — | the live configuration when the run started |"]
    for candidate in lab.repo.candidates(run.id):
        parent = lab.repo.candidate(candidate.parent_id) if candidate.parent_id else None
        lines.append(f"| `candidates/{candidate.label}/` | "
                     f"{parent.label if parent else 'baseline'} | "
                     f"{', '.join(sorted(candidate.changes))} | "
                     f"{candidate.hypothesis.replace('|', '/')[:120]} |")
    lines += ["", "Replies are in `replies/`, one file per scenario.", ""]
    return "\n".join(lines)


def export_files(lab, run) -> dict[str, str]:
    """Every file of the run's folder, by relative path."""
    files = {"README.md": readme_md(lab, run)}
    for name, text in configuration_files(lab, run, None).items():
        files[f"baseline/{name}"] = text
    for candidate in lab.repo.candidates(run.id):
        for name, text in configuration_files(lab, run, candidate).items():
            files[f"candidates/{candidate.label}/{name}"] = text
    hidden = hidden_attempts(lab, run)
    by_slug: dict[str, list] = {}
    for attempt in lab.repo.attempts(run_id=run.id):
        record = lab.repo.scenario(attempt.scenario_id)
        if record is not None:
            by_slug.setdefault(record.slug, []).append(attempt)
    for slug, attempts in sorted(by_slug.items()):
        files[f"replies/{slug}.md"] = replies_md(lab, run, slug, attempts, hidden)
    if by_slug:
        files["report.md"] = report.render_markdown(report.build_report(lab, run))
    return files
