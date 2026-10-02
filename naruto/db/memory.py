"""Group memory: short durable facts per chat, kept after messages expire.

Every change is recorded in memory_note_history (who, when, what), and the
owner can lock a note so neither the bot nor members change or delete it.
"""

from dataclasses import dataclass
import json
import re
import sqlite3

from naruto.db.database import Database

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("person", "Person"),
    ("preference", "Preference"),
    ("date", "Date"),
    ("decision", "Decision"),
    ("recurring_plan", "Recurring plan"),
    ("group_fact", "Group fact"),
    ("running_joke", "Running joke"),
)
CATEGORY_KEYS = tuple(key for key, _ in CATEGORIES)
MAX_NOTE_CHARS = 400

# Who created a note.
BOT = "bot"
MEMBER = "member"
OWNER = "owner"
IMPORT = "import"


_UNCHANGED = object()


class NoteLocked(ValueError):
    pass


@dataclass
class Note:
    id: int
    chat_id: int
    content: str
    category: str
    person_id: int | None
    source_row_ids: list[int]
    created_by: str
    created_by_user_id: int | None
    locked: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Note":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["locked"] = bool(data["locked"])
        data["source_row_ids"] = json.loads(data["source_row_ids"] or "[]")
        return cls(**data)

    @property
    def category_label(self) -> str:
        return dict(CATEGORIES).get(self.category, self.category)


@dataclass
class NoteChange:
    id: int
    note_id: int
    action: str
    content: str | None
    category: str | None
    person_id: int | None
    changed_at: int
    changed_by: str


def clean_content(text: str) -> str:
    return " ".join((text or "").split())[:MAX_NOTE_CHARS]


def normalize_category(value: str | None) -> str:
    value = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return value if value in CATEGORY_KEYS else "group_fact"


class NoteRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- reads

    def get(self, note_id: int) -> Note | None:
        row = self.db.query_one("SELECT * FROM memory_notes WHERE id = ?", (note_id,))
        return Note.from_row(row) if row else None

    def count(self, chat_id: int) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM memory_notes WHERE chat_id = ?",
                                  (chat_id,)) or 0)

    def for_chat(self, chat_id: int, *, person_id: int | None = None,
                 category: str | None = None, query: str | None = None,
                 limit: int | None = None) -> list[Note]:
        sql = "SELECT * FROM memory_notes WHERE chat_id = ?"
        params: list = [chat_id]
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if category:
            sql += " AND category = ?"
            params.append(category)
        for word in re.findall(r"\w+", query or "")[:8]:
            sql += " AND content LIKE ? ESCAPE '\\'"
            params.append("%" + word.replace("\\", "\\\\").replace("%", "\\%")
                          .replace("_", "\\_") + "%")
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [Note.from_row(row) for row in self.db.query(sql, params)]

    def history(self, note_id: int) -> list[NoteChange]:
        rows = self.db.query(
            "SELECT * FROM memory_note_history WHERE note_id = ? ORDER BY id DESC", (note_id,))
        return [NoteChange(row["id"], row["note_id"], row["action"], row["content"],
                           row["category"], row["person_id"], row["changed_at"],
                           row["changed_by"]) for row in rows]

    def counts_by_chat(self) -> dict[int, int]:
        return {row[0]: row[1] for row in self.db.query(
            "SELECT chat_id, COUNT(*) FROM memory_notes GROUP BY chat_id")}

    # --------------------------------------------------------------- writes

    def _log(self, note: Note, action: str, actor: str) -> None:
        self.db.execute(
            "INSERT INTO memory_note_history (note_id, chat_id, action, content, category, "
            "person_id, changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (note.id, note.chat_id, action, note.content, note.category, note.person_id,
             self.db.now(), actor))

    def add(self, chat_id: int, content: str, *, category: str | None = None,
            person_id: int | None = None, source_row_ids: list[int] | None = None,
            created_by: str, created_by_user_id: int | None = None, actor: str) -> Note:
        content = clean_content(content)
        if not content:
            raise ValueError("A note needs some text.")
        ts = self.db.now()
        with self.db.transaction():
            note_id = self.db.execute(
                "INSERT INTO memory_notes (chat_id, content, category, person_id, source_row_ids, "
                "created_by, created_by_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, content, normalize_category(category), person_id,
                 json.dumps(source_row_ids or []), created_by, created_by_user_id, ts, ts)
            ).lastrowid
            note = self.get(note_id)
            self._log(note, "created", actor)
        return note

    def update(self, note_id: int, *, content: str | None = None, category: str | None = None,
               person_id=_UNCHANGED, source_row_ids: list[int] | None = None,
               actor: str, force: bool = False) -> Note:
        """Change a note. Locked notes only change with ``force`` (the owner)."""
        note = self.get(note_id)
        if note is None:
            raise ValueError(f"There is no note {note_id}.")
        if note.locked and not force:
            raise NoteLocked(f"Note {note_id} is locked by the owner.")
        fields: dict = {}
        if content is not None and clean_content(content) != note.content:
            fields["content"] = clean_content(content)
            if not fields["content"]:
                raise ValueError("A note needs some text.")
        if category is not None and normalize_category(category) != note.category:
            fields["category"] = normalize_category(category)
        if person_id is not _UNCHANGED and person_id != note.person_id:
            fields["person_id"] = person_id
        if source_row_ids:
            merged = list(dict.fromkeys(note.source_row_ids + list(source_row_ids)))
            fields["source_row_ids"] = json.dumps(merged[-20:])
        if not fields:
            return note
        fields["updated_at"] = self.db.now()
        with self.db.transaction():
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self.db.execute(f"UPDATE memory_notes SET {assignments} WHERE id = ?",
                            (*fields.values(), note_id))
            note = self.get(note_id)
            if {"content", "category", "person_id"} & set(fields):
                self._log(note, "updated", actor)
        return note

    def delete(self, note_id: int, *, actor: str, force: bool = False) -> Note:
        note = self.get(note_id)
        if note is None:
            raise ValueError(f"There is no note {note_id}.")
        if note.locked and not force:
            raise NoteLocked(f"Note {note_id} is locked by the owner.")
        with self.db.transaction():
            self._log(note, "deleted", actor)
            self.db.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
        return note

    def set_locked(self, note_id: int, locked: bool, *, actor: str) -> Note:
        note = self.get(note_id)
        if note is None:
            raise ValueError(f"There is no note {note_id}.")
        if note.locked != locked:
            with self.db.transaction():
                self.db.execute("UPDATE memory_notes SET locked = ?, updated_at = ? WHERE id = ?",
                                (int(locked), self.db.now(), note_id))
                note = self.get(note_id)
                self._log(note, "locked" if locked else "unlocked", actor)
        return note

    def delete_for_chat(self, chat_id: int) -> int:
        with self.db.transaction():
            deleted = self.db.execute("DELETE FROM memory_notes WHERE chat_id = ?",
                                      (chat_id,)).rowcount
            self.db.execute("DELETE FROM memory_note_history WHERE chat_id = ?", (chat_id,))
        return deleted
