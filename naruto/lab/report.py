"""Comparing configurations and the run report.

The comparison favours specific findings over one score: per scenario and
configuration, how many attempts passed their checks (k of n, flaky when
some did and some didn't), which checks failed, regressions and
improvements against the baseline, coverage by outcome class (so a
configuration can't look better by having fewer cases evaluated), the
skills that ran, timing and tokens, and judgments kept apart by kind (AI,
owner). Attempts that ran under different conditions are listed rather than
mixed in silently.
"""

from datetime import datetime, timezone
import json
from statistics import median

from naruto.lab import checks, config, preferences
from naruto.lab.sandbox import NOT_SIMULATED

CHECKED = (checks.PASS, checks.FAIL, checks.ACTION_FAILED)
FINISHED = ("done", "skipped", "cancelled")
VALIDATION_REUSE_LIMIT = 2  # candidates chosen between on a validation set


def _p90(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]


def _stats(values: list[int]) -> dict:
    return {"median": median(values) if values else None, "p90": _p90(values),
            "count": len(values)}


def _rate(passed: int, checked: int) -> str:
    return f"{passed}/{checked}" if checked else "—"


def _select(lab, run, *, configs=None, set_ref=None, scenarios=None):
    attempts = [a for a in lab.repo.attempts(run_id=run.id) if a.status in FINISHED]
    if configs:
        wanted = {c.id if c else None for c in (lab.get_candidate(run, ref) for ref in configs)}
        attempts = [a for a in attempts if a.candidate_id in wanted]
    slugs = None
    if set_ref is not None:
        lab_set = lab.get_set(run, set_ref)
        slugs = {item.slug for item in lab.repo.set_items(lab_set.id)}
    if scenarios:
        slugs = (slugs or set()) | {lab.get_scenario(ref).slug for ref in scenarios}
    records = {}
    selected = []
    for attempt in attempts:
        record = records.setdefault(attempt.scenario_id, lab.repo.scenario(attempt.scenario_id))
        if record is None or (slugs is not None and record.slug not in slugs):
            continue
        selected.append((attempt, record))
    return selected


def _judgment_summary(judgments, current_version: int | None) -> tuple[dict, int]:
    summary: dict = {}
    stale = 0
    for judgment in judgments:
        if current_version is not None and judgment.rubric_version < current_version:
            stale += 1
            continue
        entry = summary.setdefault(judgment.criterion, {}).setdefault(
            judgment.kind, {"pass": 0, "fail": 0, "scores": []})
        if judgment.verdict == "score" and judgment.score is not None:
            entry["scores"].append(judgment.score)
        elif judgment.verdict in ("pass", "fail"):
            entry[judgment.verdict] += 1
    for criterion in summary.values():
        for entry in criterion.values():
            scores = entry.pop("scores")
            entry["mean_score"] = round(sum(scores) / len(scores), 2) if scores else None
            entry["scored"] = len(scores)
    return summary, stale


def hidden_attempts(lab, run) -> set[int]:
    """Attempts in comparisons waiting for the owner: their replies stay out
    of reports and exports until the owner answers."""
    return {int(a) for c in lab.repo.comparisons(run.id, status="pending")
            for a in c.mapping.values()}


def compare(lab, run, *, configs=None, set_ref=None, scenarios=None) -> dict:
    selected = _select(lab, run, configs=configs, set_ref=set_ref, scenarios=scenarios)
    hidden = hidden_attempts(lab, run)
    rubric = lab.repo.rubric(run.id)
    version = rubric.version if rubric else None
    judgments_by_attempt: dict[int, list] = {}
    for judgment in lab.repo.judgments(run_id=run.id):
        judgments_by_attempt.setdefault(judgment.attempt_id, []).append(judgment)
    purposes: dict[str, list[str]] = {}
    for lab_set in lab.repo.sets(run.id):
        for item in lab.repo.set_items(lab_set.id):
            purposes.setdefault(item.slug, []).append(lab_set.purpose)

    labels: list[str] = []
    groups: dict[tuple, dict[str, list]] = {}
    for attempt, record in selected:
        label = lab.label(run, attempt.candidate_id)
        if label not in labels:
            labels.append(label)
        key = (record.slug, record.version, attempt.continue_from)
        groups.setdefault(key, {}).setdefault(label, []).append(attempt)
    labels.sort(key=lambda label: (label != "baseline", label))

    summary = {label: {"attempts": 0, "outcomes": {}, "checked": 0, "passed": 0,
                       "scenarios": 0, "skills": {}, "failed_checks": {}, "model_ms": [],
                       "wait_ms": [], "first_try_model_ms": [], "later_tries_model_ms": [],
                       "prompt_tokens": [], "completion_tokens": [], "judgments": [],
                       "focused": 0}
               for label in labels}
    rows = []
    for (slug, version_number, continues), by_label in sorted(groups.items(),
                                                              key=lambda item: item[0][:2]):
        row = {"slug": slug, "version": version_number, "continues": continues,
               "purposes": sorted(set(purposes.get(slug, []))), "results": {}}
        for label, attempts in by_label.items():
            total = summary[label]
            total["scenarios"] += 1
            outcomes: dict[str, int] = {}
            failed: dict[str, int] = {}
            for position, attempt in enumerate(sorted(attempts, key=lambda a: a.repeat)):
                outcome = attempt.outcome or attempt.status
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                total["outcomes"][outcome] = total["outcomes"].get(outcome, 0) + 1
                total["attempts"] += 1
                if (attempt.conditions or {}).get("focused"):
                    total["focused"] += 1
                if attempt.model_ms:
                    total["model_ms"].append(attempt.model_ms)
                    (total["first_try_model_ms"] if position == 0
                     else total["later_tries_model_ms"]).append(attempt.model_ms)
                total["wait_ms"].append(attempt.wait_ms)
                usage = (attempt.result or {}).get("usage") or {}
                for key in ("prompt_tokens", "completion_tokens"):
                    if isinstance(usage.get(key), (int, float)):
                        total[key].append(usage[key])
                for turn in attempt.turns:
                    skill = turn.get("final_skill")
                    if skill:
                        total["skills"][skill] = total["skills"].get(skill, 0) + 1
                    for check in turn.get("checks") or []:
                        if not check["passed"]:
                            failed[check["name"]] = failed.get(check["name"], 0) + 1
                            total["failed_checks"][check["name"]] = \
                                total["failed_checks"].get(check["name"], 0) + 1
                total["judgments"] += judgments_by_attempt.get(attempt.id, [])
            checked = sum(outcomes.get(o, 0) for o in CHECKED)
            passed = outcomes.get(checks.PASS, 0)
            total["checked"] += checked
            total["passed"] += passed
            judged, stale = _judgment_summary(
                [j for a in attempts for j in judgments_by_attempt.get(a.id, [])], version)
            shown = [a for a in attempts if a.id not in hidden]
            first = min(shown, key=lambda a: (a.repeat, a.id)) if shown else None
            example = ({"attempt": first.id, "outcome": first.outcome or first.status,
                        "answers": [t.get("answer") or "" for t in first.turns]} if first else
                       {"attempt": None, "outcome": "hidden",
                        "answers": ["(in an A/B comparison waiting for the owner)"]})
            row["results"][label] = {
                "example": example,
                "attempts": [a.id for a in attempts], "outcomes": outcomes,
                "checked": checked, "passed": passed, "pass": _rate(passed, checked),
                "flaky": 0 < passed < checked, "failed_checks": failed,
                "judgments": judged, "stale_judgments": stale}
        rows.append(row)

    reference = "baseline" if "baseline" in labels else (labels[0] if labels else None)
    regressions, improvements, gaps = [], [], []
    for row in rows:
        base = row["results"].get(reference)
        for label, result in row["results"].items():
            if label == reference or base is None:
                continue
            if not result["checked"] or not base["checked"]:
                if result["checked"] != base["checked"]:
                    gaps.append({"slug": row["slug"], "version": row["version"],
                                 "config": label, "reference": reference,
                                 "note": "only one of them has checked attempts"})
                continue
            base_rate = base["passed"] / base["checked"]
            rate = result["passed"] / result["checked"]
            entry = {"slug": row["slug"], "version": row["version"], "config": label,
                     reference: base["pass"], "candidate": result["pass"],
                     "failed_checks": result["failed_checks"]}
            if rate < base_rate:
                regressions.append(entry)
            elif rate > base_rate:
                improvements.append(entry)

    for label, total in summary.items():
        for key in ("model_ms", "wait_ms", "first_try_model_ms", "later_tries_model_ms"):
            total[key] = _stats(total[key])
        for key in ("prompt_tokens", "completion_tokens"):
            values = total[key]
            total[key] = {"mean": round(sum(values) / len(values)) if values else None,
                          "reported": len(values)}
        total["pass_rate"] = (round(total["passed"] / total["checked"], 3)
                              if total["checked"] else None)
        total["judgments"], total["stale_judgments"] = _judgment_summary(total["judgments"],
                                                                         version)

    return {"configs": labels, "reference": reference, "summary": summary, "scenarios": rows,
            "regressions": regressions, "improvements": improvements,
            "coverage_gaps": gaps,
            "mismatched_conditions": _mismatches(run, [a for a, _ in selected]),
            "rubric_version": version}


def _mismatches(run, attempts) -> list[dict]:
    result = []
    for attempt in attempts:
        conditions = attempt.conditions or {}
        differs = []
        reported = conditions.get("models_reported") or []
        if run.model_reported and any(m != run.model_reported for m in reported):
            differs.append(f"model {', '.join(reported)}")
        if conditions.get("code_fingerprint") not in (None, run.code_fingerprint):
            differs.append("code changed")
        if differs:
            result.append({"attempt": attempt.id, "differs": differs})
    return result


def validation_status(lab, run) -> dict:
    """Whether the validation sets still give independent evidence."""
    sets = [s for s in lab.repo.sets(run.id) if s.purpose == "validation"]
    if not sets:
        return {"sets": [], "note": "No validation set: improvements are only shown on the "
                                    "scenarios used for tuning."}
    slugs = {item.slug for s in sets for item in lab.repo.set_items(s.id)}
    attempts = [a for a in lab.repo.attempts(run_id=run.id)
                if a.status == "done" and lab.repo.scenario(a.scenario_id).slug in slugs]
    compared = sorted({lab.label(run, a.candidate_id) for a in attempts} - {"baseline"})
    first_seen = min((a.first_viewed_at for a in attempts if a.first_viewed_at), default=None)
    after_seen = [c.label for c in lab.repo.candidates(run.id)
                  if first_seen is not None and c.created_at > first_seen]
    moved = [e.detail for e in lab.repo.events(run.id, "set_removed")
             if e.detail.get("purpose") == "validation"]
    reasons = []
    if len(compared) > VALIDATION_REUSE_LIMIT:
        reasons.append(f"{len(compared)} candidates were compared on it")
    if after_seen:
        reasons.append(f"{', '.join(after_seen)} were made after validation results were read")
    if moved:
        reasons.append(f"{len(moved)} scenarios were taken out of it")
    return {"sets": [s.name for s in sets], "scenarios": sorted(slugs),
            "candidates_compared": compared, "made_after_seeing_results": after_seen,
            "removed": moved, "independent": not reasons,
            "note": ("Still independent evidence." if not reasons else
                     "No longer independent evidence: " + "; ".join(reasons) + ".")}


# ------------------------------------------------------------------ report

def _when(ts) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes") if ts else None


def build_report(lab, run) -> dict:
    state = lab.run_state(run)
    candidates = [lab.candidate_view(run, c) for c in lab.repo.candidates(run.id)]
    rubric = lab.repo.rubric(run.id)
    simulated = sorted({call["method"] for a in lab.repo.attempts(run_id=run.id)
                        for t in a.turns for call in t.get("telegram") or []})
    events = lab.repo.events(run.id)
    finished = run.status == "finished"
    validated = finished and run.stop_reason not in ("cancelled", "blocked")
    report = {
        "run": {"id": run.id, "objective": run.objective,
                "protected": run.spec.get("protected", []), "scope": run.spec.get("scope"),
                "status": state["state"], "stop_reason": run.stop_reason,
                "started": _when(run.created_at), "finished": _when(run.finished_at),
                "activation_policy": run.spec.get("activation", {}).get("policy"),
                "evaluator": run.spec.get("evaluator"),
                "data_access": run.spec.get("data")},
        "conditions": {"endpoint": config.redact_endpoint(run.model_endpoint),
                       "model": run.model_name or "(first listed)",
                       "model_reported": run.model_reported,
                       "code_fingerprint": run.code_fingerprint,
                       "schema_version": run.schema_version, "warnings": run.warnings},
        "validity": ("" if validated else
                     "This run didn't finish normally: nothing in it was validated."),
        "candidates": candidates,
        "rubric": ({"version": rubric.version, "status": rubric.status,
                    "criteria": rubric.criteria, "confirmation": rubric.confirmation,
                    "history": [{"version": r.version, "status": r.status, "reason": r.reason}
                                for r in lab.repo.rubrics(run.id)]} if rubric else None),
        "comparison": compare(lab, run),
        "by_set": {s.name: {"purpose": s.purpose, **compare(lab, run, set_ref=s.id)}
                   for s in lab.repo.sets(run.id)},
        "validation": validation_status(lab, run),
        "notes": [{"kind": n.kind, "text": n.text, "evidence": n.evidence, "by": n.created_by}
                  for n in lab.repo.notes(run.id)],
        "events": [{"kind": e.kind, "at": _when(e.created_at), **e.detail} for e in events
                   if e.kind != "drift"],
        "coverage": {"simulated_telegram_actions": simulated,
                     "not_simulated": NOT_SIMULATED,
                     "live_verification": "A pass here is a pass in a sandbox: it doesn't show "
                                          "that Telegram delivery or the production model "
                                          "server work. Check changes in a test group."},
        "resources": {"budget": run.budget, "used": state["used"],
                      "cost": "not applicable (local model); the agent's own usage isn't "
                              "measured"},
        "recommendation": run.recommendation, "summary": run.summary,
    }
    report.update(preferences.report_section(lab, run))
    report["activations"] = [
        {"id": a.id, "candidate": lab.label(run, a.candidate_id), "mode": a.mode,
         "at": _when(a.created_at), "authorized_by": a.authorized_by,
         "reverted": _when(a.reverted_at), "keys": sorted(a.applied)}
        for a in lab.repo.activations(run_id=run.id)]
    return report


def _md_table(rows: list[list[str]], header: list[str]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in row) + " |"
              for row in rows]
    return lines


def _ms(stats: dict) -> str:
    if not stats or stats.get("median") is None:
        return "—"
    return f"{stats['median'] / 1000:.1f} s / {(stats['p90'] or 0) / 1000:.1f} s"


def render_markdown(report: dict, lab=None, run=None) -> str:
    run_info = report["run"]
    lines = [f"# Report: run {run_info['id']}", "", run_info["objective"], ""]
    if report["validity"]:
        lines += [f"**{report['validity']}**", ""]
    lines += [f"- Status: {run_info['status']}" + (f" ({run_info['stop_reason']})"
                                                   if run_info["stop_reason"] else ""),
              f"- Model: `{report['conditions']['model']}` at "
              f"`{report['conditions']['endpoint']}`"
              + (f", reported as `{report['conditions']['model_reported']}`"
                 if report["conditions"]["model_reported"] else ""),
              f"- Activation policy: {run_info['activation_policy']}",
              f"- Evaluator: {(run_info['evaluator'] or {}).get('name')}"
              + (" (external)" if (run_info["evaluator"] or {}).get("external") else ""), ""]
    if run_info["protected"]:
        lines += ["Must keep: " + "; ".join(run_info["protected"]), ""]
    for warning in report["conditions"]["warnings"]:
        lines.append(f"> ⚠ {warning}")
    if report["conditions"]["warnings"]:
        lines.append("")

    recommendation = report.get("recommendation")
    lines += ["## Recommendation", ""]
    if recommendation:
        target = f" {recommendation.get('candidate')}" if recommendation.get("candidate") else ""
        lines += [f"**{recommendation.get('action', '').replace('_', ' ')}{target}**", ""]
        if recommendation.get("text"):
            lines += [recommendation["text"], ""]
    else:
        lines += ["None yet.", ""]
    if report.get("summary"):
        lines += [report["summary"], ""]

    lines += ["## Configurations", ""]
    lines += _md_table([[c["label"], c["parent"] or "—", ", ".join(c["vs_baseline"]) or "—",
                         c["hypothesis"] or "—"] for c in report["candidates"]],
                       ["Candidate", "Built on", "Differs from the baseline in", "Hypothesis"])
    lines.append("")
    for candidate in report["candidates"]:
        for effect in candidate["background_effects"]:
            lines.append(f"- {candidate['label']}: {effect}")
    lines.append("")

    comparison = report["comparison"]
    lines += ["## Results", "",
              "Deterministic checks only; judgments are below. Pass = attempts whose checks "
              "all passed, of the attempts that could be checked.", ""]
    outcome_names = list(checks.OUTCOMES)
    rows = []
    for label in comparison["configs"]:
        s = comparison["summary"][label]
        rows.append([label, _rate(s["passed"], s["checked"]),
                     ", ".join(f"{o} {s['outcomes'][o]}" for o in outcome_names
                               if s["outcomes"].get(o)),
                     s["scenarios"], _ms(s["model_ms"]), _ms(s["wait_ms"]),
                     s["prompt_tokens"]["mean"] or "—"])
    lines += _md_table(rows, ["Configuration", "Pass", "Outcomes", "Scenarios",
                              "Model time median / p90", "Queue wait", "Prompt tokens"])
    lines.append("")
    if comparison["regressions"]:
        lines += ["### Regressions", ""]
        lines += [f"- **{r['slug']}** (v{r['version']}): {r['config']} {r['candidate']} vs "
                  f"{comparison['reference']} {r[comparison['reference']]}; failed "
                  f"{', '.join(r['failed_checks']) or '—'}" for r in comparison["regressions"]]
        lines.append("")
    if comparison["improvements"]:
        lines += ["### Improvements", ""]
        lines += [f"- **{r['slug']}** (v{r['version']}): {r['config']} {r['candidate']} vs "
                  f"{comparison['reference']} {r[comparison['reference']]}"
                  for r in comparison["improvements"]]
        lines.append("")
    if comparison["coverage_gaps"]:
        lines += ["### Not comparable", ""]
        lines += [f"- {g['slug']}: {g['config']} vs {g['reference']}: {g['note']}"
                  for g in comparison["coverage_gaps"]]
        lines.append("")
    lines += ["### By scenario", ""]
    rows = []
    for row in comparison["scenarios"]:
        cells = []
        for label in comparison["configs"]:
            result = row["results"].get(label)
            if result is None:
                cells.append("—")
                continue
            cell = result["pass"]
            other = {o: n for o, n in result["outcomes"].items() if o not in CHECKED}
            if other:
                cell += " (" + ", ".join(f"{o} {n}" for o, n in other.items()) + ")"
            if result["flaky"]:
                cell += " ⚠ varies"
            cells.append(cell)
        name = f"{row['slug']} v{row['version']}" + (" (continued)" if row["continues"] else "")
        rows.append([name, ", ".join(row["purposes"]) or "—", *cells])
    lines += _md_table(rows, ["Scenario", "Sets", *comparison["configs"]])
    lines.append("")
    if comparison["mismatched_conditions"]:
        lines += ["Attempts under different conditions (not a clean comparison): "
                  + "; ".join(f"attempt {m['attempt']}: {', '.join(m['differs'])}"
                              for m in comparison["mismatched_conditions"]), ""]

    lines += ["### Representative replies", "",
              "The first try of each configuration, so tone can be compared directly.", ""]
    for row in comparison["scenarios"][:15]:
        lines += [f"**{row['slug']}**", ""]
        for label in comparison["configs"]:
            result = row["results"].get(label)
            if result is None:
                continue
            example = result["example"]
            answers = [a for a in example["answers"] if a] or ["(sent nothing)"]
            quoted = " / ".join(" ".join(a.split())[:300] for a in answers)
            lines.append(f"- {label} (attempt {example['attempt']}, {example['outcome']}): "
                         f"{quoted}")
        lines.append("")

    lines += ["## Judgments", ""]
    rubric = report.get("rubric")
    if rubric:
        lines += [f"Rubric v{rubric['version']} ({rubric['status']}"
                  + (f": {rubric['confirmation']}" if rubric.get("confirmation") else "") + ")",
                  ""]
        lines += [f"- **{c.get('id')}**: {c.get('description', '')}" for c in rubric["criteria"]]
        lines.append("")
    judged_rows = []
    for label in comparison["configs"]:
        s = comparison["summary"][label]
        for criterion, kinds in s["judgments"].items():
            for kind, entry in kinds.items():
                result = f"{entry['pass']} pass, {entry['fail']} fail"
                if entry["mean_score"] is not None:
                    result += f", mean score {entry['mean_score']} ({entry['scored']})"
                judged_rows.append([label, criterion, "AI" if kind == "ai" else "owner", result])
        if s["stale_judgments"]:
            judged_rows.append([label, "—", "—", f"{s['stale_judgments']} under an older "
                                                 "rubric (judge again)"])
    lines += (_md_table(judged_rows, ["Configuration", "Criterion", "By", "Result"])
              if judged_rows else ["No judgments yet."])
    lines.append("")

    validation = report["validation"]
    lines += ["## Validation", "", validation["note"], ""]
    for name, section in report["by_set"].items():
        lines += [f"- Set **{name}** ({section['purpose']}): " + ", ".join(
            f"{label} {_rate(s['passed'], s['checked'])}"
            for label, s in section["summary"].items())]
    lines.append("")

    if report.get("interactive"):
        lines += report["interactive"]["markdown"]

    if report["notes"]:
        lines += ["## Agent's notes", ""]
        lines += [f"- *{n['kind']}*: {n['text']}" for n in report["notes"]]
        lines.append("")
    if report["events"]:
        lines += ["## What changed during the run", ""]
        for event in report["events"]:
            detail = {k: v for k, v in event.items() if k not in ("kind", "at")}
            lines.append(f"- {event['at']} {event['kind'].replace('_', ' ')}: "
                         f"{json.dumps(detail, ensure_ascii=False)[:300]}")
        lines.append("")
    if report["activations"]:
        lines += ["## Activations", ""]
        lines += [f"- {a['at']}: {a['candidate']} ({a['mode']}; {', '.join(a['keys'])}), "
                  f"authorized by {a['authorized_by']}"
                  + (f", reverted {a['reverted']}" if a["reverted"] else "")
                  for a in report["activations"]]
        lines.append("")

    coverage = report["coverage"]
    lines += ["## Coverage and live checks", "",
              "Simulated Telegram actions: " + (", ".join(coverage["simulated_telegram_actions"])
                                                or "none"), "",
              coverage["live_verification"], "", "Not simulated:", ""]
    lines += [f"- {item}" for item in coverage["not_simulated"]]
    used, budget = report["resources"]["used"], report["resources"]["budget"]
    lines += ["", "## Resources", "",
              f"{used['attempts']} of {budget.get('attempts')} attempts, "
              f"{used['model_requests']} of {budget.get('model_requests')} model requests, "
              f"{used['model_ms'] / 60000:.1f} min of model time. "
              f"Cost: {report['resources']['cost']}.", ""]
    return "\n".join(lines)
