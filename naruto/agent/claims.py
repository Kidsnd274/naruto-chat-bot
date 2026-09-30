"""Catching answers that skip the tool a request needs.

Local models sometimes answer "Reminder set!" without calling set_reminder,
especially once the chat shows earlier answers like that: the transcript has
the bot's words, not its tool calls, so a false claim teaches the next one.
When the current request clearly asks for an action, or the answer says it
was done, and the run never did it, the runner asks once more with a note
saying so (see AgentRunner).

The patterns are deliberately narrow. A false alarm costs one extra model
request (the model answers again); a miss leaves the answer as it was.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ActionCheck:
    tools: tuple[str, ...]  # any successful call to one of these does the action
    what: str  # for the note: "no tool was called to <what>"
    request: re.Pattern  # the request asks for the action
    claim: re.Pattern  # the answer says it was done


def _pattern(text: str) -> re.Pattern:
    return re.compile(text, re.IGNORECASE)


CHECKS: tuple[ActionCheck, ...] = (
    ActionCheck(
        ("set_reminder", "cancel_reminder"), "set or cancel a reminder (set_reminder)",
        _pattern(r"\bremind\s+(?:me|us|everyone|all|him|her|them|@\w+|\w+)\s+"
                 r"(?:to|about|that|of|in|at|on|tomorrow|tonight|later|every)\b"
                 r"|\b(?:set|make|add|create|schedule|cancel|delete|remove)\s+"
                 r"(?:a|an|the|my|our|that|this|up\s+a)?\s*reminder"),
        _pattern(r"\breminder(?:'s|\s+is)?\s+(?:set|scheduled|on|added|cancelled|canceled)\b"
                 r"|\b(?:set|setting|scheduled|added)\s+(?:a|the|your|that|this|up\s+a)?\s*"
                 r"reminder\b|\b(?:i'?ll|i\s+will|gonna)\s+remind\b|\bset\s+for\s+\d"),
    ),
    ActionCheck(
        ("remember", "forget"), "save or delete a memory note (remember / forget)",
        _pattern(r"\bremember\s+(?:that|this|:)|\bforget\s+(?:that|this|about\s+(?:that|it))\b"
                 r"|\bdon'?t\s+forget\s+(?:that|this)\b"),
        _pattern(r"\b(?:i'?ll|i\s+will|gonna)\s+(?:remember|forget)\b"
                 r"|\b(?:saved|forgotten|forgot\s+it|wiped)\b"
                 r"|\b(?:added|saved|put)\s+(?:it|that|this)\s+(?:to|in)\s+(?:my|the)\s+memory\b"
                 r"|\block(?:ed)?\s+(?:it|that)\s+in\b"),
    ),
    ActionCheck(
        ("create_poll",), "create a poll (create_poll)",
        _pattern(r"\b(?:make|create|start|do|run|set\s+up|put\s+up|open|new|another)\b"
                 r"[^.?!\n]{0,20}\bpoll\b|\bpoll\s+(?:for|on|about|between|us|everyone)\b"
                 r"|\b(?:start|take|have|do|run|hold)\s+a\s+vote\b"),
        _pattern(r"\bpoll(?:'s|\s+is)\s+(?:up|live|out|open|ready)\b"
                 r"|\b(?:made|created|started|set\s+up|put\s+up|posted|opened)\s+"
                 r"(?:a|the|your|that|this)\s+poll\b"),
    ),
    ActionCheck(
        ("update_board",), "change the board (update_board)",
        _pattern(r"\b(?:add|put|move|mark|remove|take|update|clear|tick|cross|create|start"
                 r"|make)\b[^.?!\n]{0,40}\bboard\b"),
        _pattern(r"\b(?:added|put|moved|marked|removed|took|updated|cleared|ticked|crossed)\b"
                 r"[^.?!\n]{0,40}\bboard\b|\bboard(?:'s|\s+is)\s+(?:updated|up|started)\b"),
    ),
    ActionCheck(
        ("pin_message", "unpin_message"), "pin or unpin a message (pin_message)",
        _pattern(r"\b(?:un)?pin\s+(?:this|that|it|the|my|his|her|their|message|msg|\[?\d+\]?)\b"),
        _pattern(r"\b(?:un)?pinned\s+(?:it|that|this|the|your)\b"),
    ),
)


def asks_to_remember(request_text: str) -> bool:
    """The request asks the bot to remember or forget something."""
    return bool(CHECKS[1].request.search(request_text))


def missing_actions(request_text: str, answer: str, offered: list[str] | tuple[str, ...],
                    done: set[str]) -> list[ActionCheck]:
    """Checks whose action the request asks for or the answer claims, while
    the run offered its tools but made no successful call to them. A
    clarifying question ("what time?") only counts if it also claims."""
    asking = answer.rstrip(" *_!.").endswith("?")
    found = []
    for check in CHECKS:
        if not set(check.tools) & set(offered) or set(check.tools) & done:
            continue
        if check.claim.search(answer) or (check.request.search(request_text) and not asking):
            found.append(check)
    return found


def check_note(checks: list[ActionCheck]) -> str:
    whats = "; ".join(check.what for check in checks)
    return (f"[Check: nothing was done in the group in this response. No tool was called to "
            f"{whats}. If the current request asks for that, call the tool now. If details are "
            f"missing, or it shouldn't be done, answer again without saying it was done.]")
