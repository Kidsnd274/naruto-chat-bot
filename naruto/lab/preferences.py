"""Interactive A/B rounds: the owner chooses between real replies.

The server decides which attempt is A and which is B (at random, kept in
the comparison) and writes the presentation: the situation and the replies
exactly as the bot gave them, so the agent can relay it without rewording.
While a comparison waits, the run waits too: no new experiments start
unless the owner asked for something (recorded). Choices keep the exact
replies and configurations they were about; a correction is a new choice
that supersedes the old one, which is kept. The preference summary keeps
the owner's own words apart from the agent's interpretation.
"""

import secrets

from naruto.lab.errors import LabError

LABELS = ("A", "B", "C", "D")
CHOICES = ("A", "B", "C", "D", "both_good", "both_bad", "no_preference", "skip", "combination")
STATUSES = ("assumption", "confirmed", "corrected")
SITUATION_LINES = 12
PROMPT = ("Which do you prefer? Answer A or B, both good, both bad, no preference or skip, or "
          "describe a mix (\"A's humour, B's brevity\"). A comment helps but isn't needed.")


def _turn(attempt, index: int) -> dict | None:
    return next((t for t in attempt.turns if t.get("index") == index), None)


def _situation(lab, record, attempt, index: int) -> list[str]:
    """What the people in the chat had said when the bot answered: the
    scenario's messages and turns, with the bot's earlier replies from this
    attempt (equal across the compared attempts)."""
    body = record.body
    lines = []
    if attempt.continue_from:
        previous = lab.repo.attempt(attempt.continue_from)
        if previous is not None:
            lines.append(f"(continuing the conversation of attempt {previous.id})")
            for turn in previous.turns[-3:]:
                lines.append(f"{turn['trigger']['from']}: {turn['trigger']['text']}")
                if turn.get("answer"):
                    lines.append(f"{body.get('bot', {}).get('name', 'Naruto')}: {turn['answer']}")
    bot_name = (body.get("bot") or {}).get("name", "Naruto")
    for message in body.get("messages") or []:
        lines.append(f"{bot_name if message.get('bot') else message.get('from', '?')}: "
                     f"{message.get('text') or '[' + str(message.get('media', 'media')) + ']'}")
    raw_turns = body.get("turns") or [body.get("trigger") or {}]
    for number, raw in enumerate(raw_turns[:index], start=1):
        for message in raw.get("messages") or []:
            lines.append(f"{message.get('from', '?')}: {message.get('text', '')}")
        said = raw.get("text") or raw.get("command") or ""
        lines.append(f"{raw.get('from', '?')}: {said}")
        if number < index:
            turn = _turn(attempt, number)
            if turn is not None and turn.get("answer"):
                lines.append(f"{bot_name}: {turn['answer']}")
    if len(lines) > SITUATION_LINES:
        lines = ["…", *lines[-SITUATION_LINES:]]
    return lines


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in (text or "(sent nothing)").splitlines())


def presentation(lab, comparison_id: int, record, attempts: dict, index: int) -> str:
    body = record.body
    first = next(iter(attempts.values()))
    origin = body.get("origin", "synthetic")
    heading = f"Comparison {comparison_id}"
    lines = [heading, ""]
    if body.get("description"):
        lines.append(f"The situation{' (made up for testing)' if origin == 'synthetic' else ''}:"
                     f" {body['description']}")
    elif origin == "synthetic":
        lines.append("The situation (made up for testing):")
    lines += ["", "The chat:", *[f"  {line}" for line in _situation(lab, record, first, index)],
              ""]
    for label, attempt in attempts.items():
        turn = _turn(attempt, index) or {}
        answer = turn.get("answer") or ""
        if turn.get("delivery") == "ephemeral":
            answer += "\n(sent privately to them)"
        lines += [f"Reply {label}:", _indent(answer), ""]
    lines.append(PROMPT)
    return "\n".join(lines)


def create(lab, run_id: int, attempt_ids: list[int], *, turn: int = 1, actor: str):
    run = lab.active_run(run_id)
    if not isinstance(attempt_ids, list) or not 2 <= len(set(attempt_ids)) <= len(LABELS):
        raise LabError(f"Compare 2 to {len(LABELS)} different attempts.")
    attempts = [lab.get_attempt(int(a), run.id) for a in dict.fromkeys(attempt_ids)]
    for attempt in attempts:
        if attempt.status != "done" or _turn(attempt, turn) is None:
            raise LabError(f"Attempt {attempt.id} has no turn {turn} to compare.", "conflict")
    first = attempts[0]
    if any(a.scenario_id != first.scenario_id or a.continue_from != first.continue_from
           for a in attempts):
        raise LabError("Compared replies must answer the same situation: the same scenario "
                       "version (and the same earlier conversation, if continued).")
    if turn > 1:
        for number in range(1, turn):
            answers = {(_turn(a, number) or {}).get("answer") for a in attempts}
            if len(answers) > 1:
                raise LabError(f"Before turn {turn} the conversations differ (turn {number}'s "
                               "replies aren't the same), so it isn't the same situation. "
                               "Continue one conversation under each configuration "
                               "(continue_from) and compare those.")
    models = {tuple((a.conditions or {}).get("models_reported") or ()) for a in attempts}
    code = {(a.conditions or {}).get("code_fingerprint") for a in attempts}
    if len(models) > 1 or len(code) > 1:
        raise LabError("These attempts ran under different conditions (another model, or the "
                       "bot's code changed), so they aren't comparable.", "conflict")
    pending = lab.repo.comparisons(run.id, status="pending")
    limit = run.interactive.get("comparisons_at_a_time", 1)
    if len(pending) >= limit:
        raise LabError(f"{len(pending)} comparison(s) already wait for the owner (at most "
                       f"{limit} at a time).", "conflict", pending=[c.id for c in pending])
    in_use = {int(a) for c in pending for a in c.mapping.values()}
    if in_use & {a.id for a in attempts}:
        raise LabError("One of these attempts is already in a comparison that waits.",
                       "conflict")
    shuffled = list(attempts)
    secrets.SystemRandom().shuffle(shuffled)
    labelled = dict(zip(LABELS, shuffled))
    record = lab.repo.scenario(first.scenario_id)
    comparison = lab.repo.add_comparison(
        run_id=run.id, scenario_id=first.scenario_id, turn=turn,
        mapping={label: a.id for label, a in labelled.items()}, presentation="")
    text = presentation(lab, comparison.id, record, labelled, turn)
    lab.repo.update_comparison(comparison.id, presentation=text)
    lab.repo.add_event(run.id, "comparison", comparison=comparison.id, by=actor,
                       attempts=sorted(a.id for a in attempts))
    return lab.repo.comparison(comparison.id)


def get(lab, comparison_id: int):
    comparison = lab.repo.comparison(comparison_id)
    if comparison is None:
        raise LabError(f"There is no comparison {comparison_id}.", "not_found")
    return comparison


def view(lab, comparison, *, reveal: bool = False, actor: str | None = None) -> dict:
    """The comparison as the owner sees it. Which configuration gave which
    reply is shown once answered, or when asked (logged before an answer)."""
    run = lab.get_run(comparison.run_id)
    choices = lab.repo.choices(comparison.id)
    current = choices[-1] if choices else None
    data = {"id": comparison.id, "run": run.id, "turn": comparison.turn,
            "status": comparison.status, "presentation": comparison.presentation,
            "labels": sorted(comparison.mapping),
            "choice": None if current is None else {
                "choice": current.choice, "comment": current.comment,
                "channel": current.channel, "id": current.id},
            "history": [{"id": c.id, "choice": c.choice, "comment": c.comment,
                         "channel": c.channel, "supersedes": c.supersedes,
                         "at": c.created_at} for c in choices],
            "revealed_before_answer": bool(comparison.revealed_before_answer)}
    if reveal and comparison.status == "pending" and not comparison.revealed_before_answer:
        lab.repo.update_comparison(comparison.id, revealed_before_answer=1)
        lab.repo.add_event(run.id, "revealed_before_answer", comparison=comparison.id,
                           by=actor)
        data["revealed_before_answer"] = True
    if reveal or comparison.status != "pending":
        data["mapping"] = {label: {"attempt": int(attempt_id),
                                   "configuration": _configuration(lab, run, attempt_id)}
                           for label, attempt_id in comparison.mapping.items()}
    return data


def _configuration(lab, run, attempt_id) -> str:
    attempt = lab.repo.attempt(int(attempt_id))
    return lab.label(run, attempt.candidate_id) if attempt else "an attempt that was deleted"


def _check_choice(comparison, choice: str, comment: str | None) -> None:
    allowed = [c for c in CHOICES if c not in LABELS or c in comparison.mapping]
    if choice not in allowed:
        raise LabError(f"The choice is one of: {', '.join(allowed)}.")
    if choice == "combination" and not (comment or "").strip():
        raise LabError("For a mix, say what to combine in the comment (\"A's humour, B's "
                       "brevity\").")


def answer(lab, comparison_id: int, choice: str, *, comment: str | None, channel: str):
    """The owner's choice. Never inferred: only what they said."""
    comparison = get(lab, comparison_id)
    if comparison.status == "answered":
        raise LabError("This comparison is answered; to change the choice, correct it.",
                       "conflict")
    if comparison.status == "withdrawn":
        raise LabError("This comparison was withdrawn.", "conflict")
    _check_choice(comparison, choice, comment)
    lab.repo.add_choice(comparison.id, choice, comment=(comment or "").strip() or None,
                        channel=channel, supersedes=None)
    lab.repo.update_comparison(comparison.id, status="answered",
                               answered_at=lab.services.db.now())
    return get(lab, comparison.id)


def correct(lab, comparison_id: int, choice: str, *, comment: str | None, channel: str):
    comparison = get(lab, comparison_id)
    choices = lab.repo.choices(comparison.id)
    if comparison.status != "answered" or not choices:
        raise LabError("Only an answered comparison can be corrected.", "conflict")
    _check_choice(comparison, choice, comment)
    lab.repo.add_choice(comparison.id, choice, comment=(comment or "").strip() or None,
                        channel=channel, supersedes=choices[-1].id)
    lab.repo.add_event(comparison.run_id, "choice_corrected", comparison=comparison.id,
                       choice=choice, by=channel)
    return get(lab, comparison.id)


def withdraw(lab, comparison_id: int, *, reason: str, actor: str):
    comparison = get(lab, comparison_id)
    if comparison.status != "pending":
        raise LabError("Only a waiting comparison can be withdrawn.", "conflict")
    if not (reason or "").strip():
        raise LabError("Say why it's withdrawn (e.g. the owner asked for another situation).")
    lab.repo.update_comparison(comparison.id, status="withdrawn")
    lab.repo.add_event(comparison.run_id, "comparison_withdrawn", comparison=comparison.id,
                       reason=reason, by=actor)
    return get(lab, comparison.id)


# ------------------------------------------------------------ preferences

def set_summary(lab, run_id: int, body: dict, *, edited_by: str):
    """The preference summary, versioned. ``owner_statements``: the owner's
    own words (quotes); ``interpretations``: the agent's reading, each an
    assumption until the owner confirms or corrects it; ``context``: how a
    preference depends on the situation."""
    run = lab.get_run(run_id)
    if not isinstance(body, dict):
        raise LabError("The summary is a JSON object.")
    statements = []
    for item in body.get("owner_statements") or []:
        item = {"text": item} if isinstance(item, str) else dict(item)
        if not str(item.get("text", "")).strip():
            raise LabError("Each owner statement needs text: their words.")
        statements.append({"text": item["text"].strip(), "comparison": item.get("comparison")})
    interpretations = []
    for item in body.get("interpretations") or []:
        item = {"text": item} if isinstance(item, str) else dict(item)
        status = item.get("status", "assumption")
        if status not in STATUSES:
            raise LabError(f"An interpretation's status is one of: {', '.join(STATUSES)}.")
        interpretations.append({"text": str(item.get("text", "")).strip(), "status": status,
                                "evidence": list(item.get("evidence") or [])})
    context = [str(c).strip() for c in body.get("context") or [] if str(c).strip()]
    return lab.repo.add_preferences(run.id, {"owner_statements": statements,
                                             "interpretations": interpretations,
                                             "context": context}, edited_by=edited_by)


def add_owner_correction(lab, run_id: int, text: str, *, edited_by: str):
    """The owner corrects the summary in their own words (web admin)."""
    current = lab.repo.preferences(run_id)
    body = dict(current.body) if current else {"owner_statements": [], "interpretations": [],
                                                "context": []}
    body["owner_statements"] = [*body.get("owner_statements", []),
                                {"text": text.strip(), "comparison": None, "correction": True}]
    return lab.repo.add_preferences(run_id, body, edited_by=edited_by)


def summary_md(lab, run) -> str | None:
    prefs = lab.repo.preferences(run.id)
    if prefs is None:
        return None
    body = prefs.body
    lines = [f"# What the owner prefers (v{prefs.version}, by {prefs.edited_by})", "",
             "## In the owner's words", ""]
    lines += [f"- “{s['text']}”" + (f" (comparison {s['comparison']})" if s.get("comparison")
                                     else "") + (" — a correction" if s.get("correction") else "")
              for s in body.get("owner_statements", [])] or ["- (none yet)"]
    lines += ["", "## The agent's reading", ""]
    lines += [f"- [{i['status']}] {i['text']}" + (f" (evidence: {', '.join(map(str, i['evidence']))})"
                                                  if i.get("evidence") else "")
              for i in body.get("interpretations", [])] or ["- (none yet)"]
    if body.get("context"):
        lines += ["", "## Depends on the situation", "", *[f"- {c}" for c in body["context"]]]
    return "\n".join(lines) + "\n"


def comparisons_md(lab, run) -> str | None:
    comparisons = lab.repo.comparisons(run.id)
    if not comparisons:
        return None
    lines = [f"# A/B comparisons, run {run.id}", ""]
    for comparison in comparisons:
        data = view(lab, comparison)
        lines += [f"## Comparison {comparison.id} ({comparison.status})", "", "```text",
                  comparison.presentation, "```", ""]
        if comparison.status == "pending":
            lines += ["Waiting for the owner. Which configuration gave which reply is shown "
                      "once they answer.", ""]
            continue
        for label, info in data.get("mapping", {}).items():
            lines.append(f"- {label} = {info['configuration']} (attempt {info['attempt']})")
        for choice in data["history"]:
            superseded = " (corrected later)" if choice["id"] != (data["choice"] or {}).get(
                "id") else ""
            lines.append(f"- Choice: **{choice['choice']}**"
                         + (f", “{choice['comment']}”" if choice["comment"] else "")
                         + f" via {choice['channel']}{superseded}")
        if data["revealed_before_answer"]:
            lines.append("- The configurations were revealed before the answer.")
        lines.append("")
    return "\n".join(lines)


def report_section(lab, run) -> dict:
    comparisons = lab.repo.comparisons(run.id)
    prefs = lab.repo.preferences(run.id)
    if not comparisons and prefs is None:
        return {}
    items = []
    for comparison in comparisons:
        data = view(lab, comparison)
        items.append({"id": comparison.id, "status": comparison.status,
                      "scenario": lab.repo.scenario(comparison.scenario_id).slug,
                      "choice": data["choice"], "mapping": data.get("mapping"),
                      "corrections": max(len(data["history"]) - 1, 0),
                      "revealed_before_answer": data["revealed_before_answer"]})
    wins: dict[str, int] = {}
    for item in items:
        choice = (item["choice"] or {}).get("choice")
        if item["mapping"] and choice in item["mapping"]:
            configuration = item["mapping"][choice]["configuration"]
            wins[configuration] = wins.get(configuration, 0) + 1
    markdown = ["## Owner's choices", "",
                "Choices are tuning evidence: \"the owner preferred this reply\", not \"this "
                "configuration reliably gives the preferred tone\". Fresh situations show the "
                "latter.", ""]
    for item in items:
        choice = item["choice"] or {}
        chosen = choice.get("choice", "—")
        config_name = ((item["mapping"] or {}).get(chosen) or {}).get("configuration")
        markdown.append(f"- Comparison {item['id']} ({item['scenario']}, {item['status']}): "
                        + (f"{chosen}" + (f" = {config_name}" if config_name else "")
                           + (f", “{choice['comment']}”" if choice.get("comment") else "")
                           if item["choice"] else "no answer yet")
                        + (f" (corrected {item['corrections']}×)" if item["corrections"] else ""))
    if wins:
        markdown += ["", "Preferred: " + ", ".join(f"{k} {v}×" for k, v in wins.items())]
    if prefs is not None:
        markdown += ["", summary_md(lab, run).replace("# ", "### ", 1)]
    markdown.append("")
    return {"interactive": {"comparisons": items, "preferred": wins,
                            "preferences": prefs.body if prefs else None,
                            "markdown": markdown}}
