"""The pinned board: plans, decisions and open questions the group sees.

One board per chat, stored as JSON sections. Publishing it to Telegram (one
pinned message, edited in place) is done by naruto.tg.board.
"""

from dataclasses import dataclass, field
import json
import sqlite3

from naruto.db.database import Database, now_ts

# (key, heading). The order is the order on the board.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("plans", "🗓 Plans"),
    ("decided", "✅ Decided"),
    ("questions", "❓ Open questions"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)
MAX_ITEMS_PER_SECTION = 25
MAX_ITEM_CHARS = 300
# The board is one Telegram message (at most 4,096 characters). Items may use
# this much in total, which leaves room for headings, marks and escaping.
MAX_BOARD_CHARS = 3000
ITEM_OVERHEAD_CHARS = 4  # "- ☐ " and the line break


class BoardFull(ValueError):
    """A change would make the board too big; the message says what to do."""


@dataclass
class BoardItem:
    text: str
    done: bool = False

    def as_dict(self) -> dict:
        return {"text": self.text, "done": self.done}


@dataclass
class Board:
    chat_id: int
    sections: dict[str, list[BoardItem]] = field(default_factory=dict)
    message_id: int | None = None
    message_chat_id: int | None = None
    format: str | None = None
    pinned: bool = False
    updated_at: int | None = None
    updated_by: str | None = None
    published_at: int | None = None
    publish_error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Board":
        raw = json.loads(row["sections"] or "{}")
        sections = {key: [BoardItem(str(item.get("text", "")), bool(item.get("done")))
                          for item in raw.get(key, []) if isinstance(item, dict)]
                    for key in SECTION_KEYS}
        return cls(chat_id=row["chat_id"], sections=sections, message_id=row["message_id"],
                   message_chat_id=row["message_chat_id"], format=row["format"],
                   pinned=bool(row["pinned"]), updated_at=row["updated_at"],
                   updated_by=row["updated_by"], published_at=row["published_at"],
                   publish_error=row["publish_error"])

    def items(self, section: str) -> list[BoardItem]:
        return self.sections.get(section, [])

    @property
    def is_empty(self) -> bool:
        return not any(self.sections.get(key) for key in SECTION_KEYS)

    def as_text(self) -> str:
        """Plain text for the model's prompt."""
        lines = []
        for key, heading in SECTIONS:
            items = self.items(key)
            if not items:
                continue
            lines.append(f"{heading} ({key})")
            for item in items:
                mark = "☑" if item.done else "☐" if key == "plans" else "•"
                lines.append(f"  {mark} {item.text}")
        return "\n".join(lines)


def normalize_items(items) -> list[BoardItem]:
    """Accept [{"text", "done"}] or plain strings; drop empties and
    duplicates, cap sizes."""
    result: list[BoardItem] = []
    seen = set()
    for item in items or []:
        if isinstance(item, str):
            text, done = item, False
        elif isinstance(item, dict):
            text, done = str(item.get("text") or ""), bool(item.get("done"))
        else:
            continue
        text = " ".join(text.split())[:MAX_ITEM_CHARS]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(BoardItem(text, done))
    return result


def board_chars(sections: dict[str, list[BoardItem]]) -> int:
    """What the items take up on the board, for MAX_BOARD_CHARS."""
    return sum(len(item.text) + ITEM_OVERHEAD_CHARS
               for key in SECTION_KEYS for item in sections.get(key, []))


def check_size(sections: dict[str, list[BoardItem]]) -> None:
    for key, heading in SECTIONS:
        count = len(sections.get(key, []))
        if count > MAX_ITEMS_PER_SECTION:
            raise BoardFull(f"{heading} can hold {MAX_ITEMS_PER_SECTION} items ({count} given). "
                            "Remove or combine some first.")
    used = board_chars(sections)
    if used > MAX_BOARD_CHARS:
        raise BoardFull(f"The board would be too long for one Telegram message ({used} of "
                        f"{MAX_BOARD_CHARS} characters). Shorten or remove items first.")


def parse_lines(text: str) -> list[BoardItem]:
    """One item per line; a leading "[x]" marks it done (web admin form)."""
    items = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•").strip()
        done = False
        lowered = line.lower()
        if lowered.startswith("[x]"):
            done, line = True, line[3:].strip()
        elif lowered.startswith("[ ]"):
            line = line[3:].strip()
        if line:
            items.append({"text": line, "done": done})
    return normalize_items(items)


def format_lines(items: list[BoardItem]) -> str:
    return "\n".join(f"{'[x] ' if item.done else ''}{item.text}" for item in items)


class BoardRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, chat_id: int) -> Board:
        row = self.db.query_one("SELECT * FROM boards WHERE chat_id = ?", (chat_id,))
        if row is None:
            return Board(chat_id=chat_id, sections={key: [] for key in SECTION_KEYS})
        return Board.from_row(row)

    def exists(self, chat_id: int) -> bool:
        return self.db.scalar("SELECT 1 FROM boards WHERE chat_id = ?", (chat_id,)) is not None

    def set_section(self, chat_id: int, section: str, items, *, actor: str) -> Board:
        """Replace one section. Raises BoardFull (and changes nothing) if
        the board would no longer fit in one message."""
        return self.set_sections(chat_id, {section: items}, actor=actor)

    def set_sections(self, chat_id: int, changes: dict[str, list], *, actor: str) -> Board:
        """Replace several sections at once, checking the size of the result."""
        board = self.get(chat_id)
        for section, items in changes.items():
            if section not in SECTION_KEYS:
                raise ValueError(f"unknown board section {section!r}")
            board.sections[section] = normalize_items(
                [item.as_dict() if isinstance(item, BoardItem) else item for item in items])
        check_size(board.sections)
        self._save_sections(board, actor)
        return self.get(chat_id)

    def add_item(self, chat_id: int, section: str, text: str, *, done: bool = False,
                 actor: str) -> Board:
        """Add (or move to the end) one item. Raises BoardFull rather than
        dropping it when the section or the board is full."""
        board = self.get(chat_id)
        items = [item.as_dict() for item in board.items(section)]
        items = [item for item in items if item["text"].lower() != text.strip().lower()]
        items.append({"text": text, "done": done})
        return self.set_section(chat_id, section, items, actor=actor)

    def _save_sections(self, board: Board, actor: str) -> None:
        payload = json.dumps({key: [item.as_dict() for item in board.items(key)]
                              for key in SECTION_KEYS}, ensure_ascii=False)
        ts = now_ts()
        self.db.execute(
            "INSERT INTO boards (chat_id, sections, updated_at, updated_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET sections = excluded.sections, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (board.chat_id, payload, ts, actor))

    def set_message(self, chat_id: int, *, message_id: int | None, message_chat_id: int | None,
                    format: str | None, pinned: bool) -> None:
        self.db.execute(
            "UPDATE boards SET message_id = ?, message_chat_id = ?, format = ?, pinned = ?, "
            "published_at = ?, publish_error = NULL WHERE chat_id = ?",
            (message_id, message_chat_id, format, int(pinned), now_ts(), chat_id))

    def set_publish_error(self, chat_id: int, error: str | None) -> None:
        self.db.execute("UPDATE boards SET publish_error = ? WHERE chat_id = ?",
                        (error, chat_id))

    def clear(self, chat_id: int) -> bool:
        return self.db.execute("DELETE FROM boards WHERE chat_id = ?", (chat_id,)).rowcount > 0
