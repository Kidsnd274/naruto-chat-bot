"""Shared helpers for group memory: parsing the model's JSON, applying its
note changes, and showing notes to the model."""

import json
import logging
import re

from naruto.db.memory import CATEGORY_KEYS, Note, NoteLocked, clean_content
from naruto.db.members import match_members
from naruto.services import Services

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class MemoryOutputError(ValueError):
    """The model's answer was not the JSON object it was asked for."""


def parse_json_object(text: str) -> dict:
    """The first JSON object in the answer: fenced, bare, or with prose
    around it."""
    text = (text or "").strip()
    candidates = [match.strip() for match in _FENCE.findall(text)] + [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise MemoryOutputError("The answer was not a JSON object.")


def resolve_about(services: Services, chat_id: int, about, bot_id: int | None) -> int | None:
    """A person's name (as the model wrote it) -> person_id, if it is exactly
    one person in this chat."""
    if not about or not isinstance(about, str) or about.strip().lower() in ("null", "none"):
        return None
    members = [m for m in services.members.list(chat_id) if m.user_id != bot_id]
    people = {m.person_id for m in match_members(members, about)}
    return people.pop() if len(people) == 1 else None


def apply_note_actions(services: Services, chat_id: int, actions, *, created_by: str,
                       actor: str, bot_id: int | None, allow_add: bool = True,
                       known_row_ids: set[int] | None = None) -> dict:
    """Apply the add / update actions the model proposed. Deletions are
    never automatic; locked notes and notes of other chats are skipped."""
    notes = services.notes
    counts = {"added": 0, "updated": 0, "skipped": 0}
    if not isinstance(actions, list):
        return counts
    existing = {note.content.lower() for note in notes.for_chat(chat_id)}
    limit = services.settings["memory.max_notes_per_chat"]
    for action in actions:
        if not isinstance(action, dict):
            counts["skipped"] += 1
            continue
        kind = str(action.get("action") or "add").lower()
        content = clean_content(str(action.get("content") or ""))
        category = action.get("category")
        if category not in CATEGORY_KEYS:
            category = None
        sources = [int(s) for s in action.get("sources") or []
                   if isinstance(s, int) or (isinstance(s, str) and s.isdigit())]
        if known_row_ids is not None:
            sources = [s for s in sources if s in known_row_ids]
        try:
            if kind == "add":
                if not allow_add or not content or content.lower() in existing \
                        or notes.count(chat_id) >= limit:
                    counts["skipped"] += 1
                    continue
                notes.add(chat_id, content, category=category,
                          person_id=resolve_about(services, chat_id, action.get("about"), bot_id),
                          source_row_ids=sources, created_by=created_by, actor=actor)
                existing.add(content.lower())
                counts["added"] += 1
            elif kind == "update":
                note_id = int(str(action.get("id", "")).lstrip("n") or 0)
                note = notes.get(note_id)
                if note is None or note.chat_id != chat_id:
                    counts["skipped"] += 1
                    continue
                changes: dict = {"content": content or None, "category": category,
                                 "source_row_ids": sources}
                if "about" in action:
                    changes["person_id"] = resolve_about(services, chat_id, action.get("about"),
                                                         bot_id)
                notes.update(note_id, actor=actor, **changes)
                counts["updated"] += 1
            else:
                counts["skipped"] += 1
        except (NoteLocked, ValueError) as exc:
            logger.debug("Skipped a note change: %s", exc)
            counts["skipped"] += 1
    return counts


def note_lines(services: Services, notes: list[Note]) -> list[str]:
    """``[n12] Alice: Vegetarian (preference)`` lines for prompts."""
    names = services.people.names_by_person(n.person_id for n in notes)
    lines = []
    for note in notes:
        who = names.get(note.person_id)
        prefix = f"{who}: " if who else ""
        locked = ", locked" if note.locked else ""
        lines.append(f"[n{note.id}] {prefix}{note.content} ({note.category_label.lower()}{locked})")
    return lines


def notes_for_prompt(services: Services, chat_id: int, people_ids: set[int],
                     limit: int) -> list[Note]:
    """Notes about the people in the conversation and general notes first,
    then the rest; kept in note order so the prompt prefix stays stable."""
    if limit <= 0:
        return []
    notes = services.notes.for_chat(chat_id)
    if len(notes) <= limit:
        return notes
    first = [n for n in notes if n.person_id is None or n.person_id in people_ids]
    first_ids = {n.id for n in first}
    rest = [n for n in notes if n.id not in first_ids]
    rest.sort(key=lambda n: n.updated_at, reverse=True)
    chosen = {n.id for n in (first + rest)[:limit]}
    return [n for n in notes if n.id in chosen]
