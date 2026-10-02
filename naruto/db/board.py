"""The pinned board: the group's plans and open questions.

One board per chat, stored as JSON sections plus a title. A plan has details
(when, where, what was decided about it) and is done once confirmed; a
question can be for one person. Publishing it to Telegram (one pinned
message, edited in place) is done by naruto.tg.board.
"""

from dataclasses import dataclass, field
import json
import sqlite3

from naruto.db.database import Database

# (key, heading). The order is the order on the board.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("plans", "🗓 Plans"),
    ("questions", "❓ Open questions"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)
MAX_ITEMS_PER_SECTION = 25
MAX_DETAILS_PER_PLAN = 25
MAX_ITEM_CHARS = 300
MAX_TITLE_CHARS = 80
DERIVED_TITLE_CHARS = 60
# The board is one Telegram message (at most 4,096 characters). Items may use
# this much in total, which leaves room for headings, marks and escaping.
MAX_BOARD_CHARS = 3000
ITEM_OVERHEAD_CHARS = 4  # "- ☐ " and the line break
_KEEP = object()


class BoardFull(ValueError):
    """A change would make the board too big; the message says what to do."""


@dataclass
class BoardItem:
    text: str  # a plan's name, or a question
    done: bool = False  # plans: confirmed
    details: list[str] = field(default_factory=list)  # plans only
    for_name: str | None = None  # questions only: who it's for
    for_user_id: int | None = None  # ... when they're a member of the chat

    def as_dict(self) -> dict:
        data: dict = {"text": self.text, "done": self.done}
        if self.details:
            data["details"] = list(self.details)
        if self.for_name:
            data["for_name"] = self.for_name
        if self.for_user_id:
            data["for_user_id"] = self.for_user_id
        return data


@dataclass
class Board:
    chat_id: int
    sections: dict[str, list[BoardItem]] = field(default_factory=dict)
    title: str | None = None  # None: built from the plans (display_title)
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
        raw = fold_decided(json.loads(row["sections"] or "{}"))
        sections = {key: normalize_items(raw.get(key) or []) for key in SECTION_KEYS}
        return cls(chat_id=row["chat_id"], sections=sections, title=row["title"],
                   message_id=row["message_id"], message_chat_id=row["message_chat_id"],
                   format=row["format"], pinned=bool(row["pinned"]),
                   updated_at=row["updated_at"], updated_by=row["updated_by"],
                   published_at=row["published_at"], publish_error=row["publish_error"])

    def items(self, section: str) -> list[BoardItem]:
        return self.sections.get(section, [])

    @property
    def is_empty(self) -> bool:
        return not any(self.sections.get(key) for key in SECTION_KEYS)

    @property
    def display_title(self) -> str:
        """The title, or one built from the plan names."""
        if self.title:
            return self.title
        names = " · ".join(item.text for item in self.items("plans"))
        if len(names) > DERIVED_TITLE_CHARS:
            names = names[:DERIVED_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ·,;") + "…"
        return names or ("Open questions" if self.items("questions") else "Board")

    def as_text(self) -> str:
        """Plain text for the model's prompt."""
        lines = [f"Title: {self.title}"] if self.title else []
        for key, heading in SECTIONS:
            items = self.items(key)
            if not items:
                continue
            lines.append(f"{heading} ({key})")
            for item in items:
                if key == "plans":
                    lines.append(f"  {'☑' if item.done else '☐'} {item.text}")
                    lines.extend(f"     - {detail}" for detail in item.details)
                else:
                    who = f" (for {item.for_name})" if item.for_name else ""
                    lines.append(f"  • {item.text}{who}")
        return "\n".join(lines)


def fold_decided(raw: dict) -> dict:
    """Boards from before plans had details kept decisions in a section of
    their own; they become one confirmed plan called "Decided"."""
    decided = [item.get("text") if isinstance(item, dict) else item
               for item in raw.get("decided") or []]
    decided = [str(text) for text in decided if text]
    raw = {key: value for key, value in raw.items() if key != "decided"}
    if decided:
        raw["plans"] = [*(raw.get("plans") or []),
                        {"text": "Decided", "done": True, "details": decided}]
    return raw


def _clean(text, limit: int = MAX_ITEM_CHARS) -> str:
    return " ".join(str(text or "").split())[:limit]


def clean_title(title) -> str | None:
    """No pin emoji (the board adds it), one line, capped."""
    return _clean(str(title or "").replace("📌", ""), MAX_TITLE_CHARS) or None


def normalize_items(items) -> list[BoardItem]:
    """Accept item dicts (see BoardItem.as_dict) or plain strings; drop
    empties and duplicates, cap sizes."""
    result: list[BoardItem] = []
    seen = set()
    for item in items or []:
        if isinstance(item, BoardItem):
            item = item.as_dict()
        if isinstance(item, str):
            item = {"text": item}
        elif not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        details = []
        for detail in item.get("details") or []:
            detail = _clean(detail)
            if detail and detail.lower() not in (d.lower() for d in details):
                details.append(detail)
        for_user_id = item.get("for_user_id")
        result.append(BoardItem(text, bool(item.get("done")), details,
                                for_name=_clean(item.get("for_name"), 100) or None,
                                for_user_id=int(for_user_id) if for_user_id else None))
    return result


def board_chars(sections: dict[str, list[BoardItem]], title: str | None = None) -> int:
    """What the title and items take up on the board, for MAX_BOARD_CHARS."""
    return len(title or "") + sum(
        len(item.text) + len(item.for_name or "") + ITEM_OVERHEAD_CHARS
        + sum(len(detail) + ITEM_OVERHEAD_CHARS for detail in item.details)
        for key in SECTION_KEYS for item in sections.get(key, []))


def check_size(sections: dict[str, list[BoardItem]], title: str | None = None) -> None:
    for key, heading in SECTIONS:
        count = len(sections.get(key, []))
        if count > MAX_ITEMS_PER_SECTION:
            raise BoardFull(f"{heading} can hold {MAX_ITEMS_PER_SECTION} items ({count} given). "
                            "Remove or combine some first.")
    for plan in sections.get("plans", []):
        if len(plan.details) > MAX_DETAILS_PER_PLAN:
            raise BoardFull(f"A plan can have {MAX_DETAILS_PER_PLAN} details ({plan.text!r} has "
                            f"{len(plan.details)}). Remove or combine some first.")
    used = board_chars(sections, title)
    if used > MAX_BOARD_CHARS:
        raise BoardFull(f"The board would be too long for one Telegram message ({used} of "
                        f"{MAX_BOARD_CHARS} characters). Shorten or remove items first.")


def _plan_names(board: Board) -> set[str]:
    return {item.text.lower() for item in board.items("plans")}


# ------------------------------------------------------- web admin form

def parse_lines(text: str, section: str) -> list[BoardItem]:
    """One item per line. Plans: "[x]" marks one confirmed, and an indented
    line is a detail of the plan above. Questions: "@Name: question" is for
    that person (format_lines writes the same)."""
    items: list[dict] = []
    for raw in (text or "").splitlines():
        indented = raw[:1].isspace()
        line = raw.strip().lstrip("-•").strip()
        if not line:
            continue
        if section == "plans" and indented and items:
            items[-1]["details"].append(line)
            continue
        item: dict = {"text": line, "done": False, "details": []}
        lowered = line.lower()
        if lowered.startswith("[x]"):
            item["done"], item["text"] = True, line[3:].strip()
        elif lowered.startswith("[ ]"):
            item["text"] = line[3:].strip()
        if section == "questions" and line.startswith("@") and ":" in line:
            name, _, question = line[1:].partition(":")
            item["for_name"], item["text"] = name.strip(), question.strip()
        items.append(item)
    return normalize_items(items)


def format_lines(items: list[BoardItem], section: str) -> str:
    lines = []
    for item in items:
        if section == "questions" and item.for_name:
            lines.append(f"@{item.for_name}: {item.text}")
            continue
        lines.append(f"{'[x] ' if item.done else ''}{item.text}")
        lines.extend(f"  - {detail}" for detail in item.details)
    return "\n".join(lines)


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

    def set_section(self, chat_id: int, section: str, items, *, actor: str,
                    title=_KEEP) -> Board:
        """Replace one section. Raises BoardFull (and changes nothing) if
        the board would no longer fit in one message."""
        return self.set_sections(chat_id, {section: items}, actor=actor, title=title)

    def set_sections(self, chat_id: int, changes: dict[str, list], *, actor: str,
                     title=_KEEP) -> Board:
        """Replace several sections at once, checking the size of the result.
        A title written for other plans is dropped when the plans change
        without a new one (the board then builds one from the plan names)."""
        board = self.get(chat_id)
        names = _plan_names(board)
        for section, items in changes.items():
            if section not in SECTION_KEYS:
                raise ValueError(f"unknown board section {section!r}")
            board.sections[section] = normalize_items(items)
        if title is not _KEEP:
            board.title = clean_title(title)
        elif _plan_names(board) != names:
            board.title = None
        check_size(board.sections, board.title)
        self._save_sections(board, actor)
        return self.get(chat_id)

    def add_item(self, chat_id: int, section: str, text: str, *, done: bool = False,
                 details: list[str] | None = None, actor: str) -> Board:
        """Add (or move to the end) one item. Raises BoardFull rather than
        dropping it when the section or the board is full."""
        board = self.get(chat_id)
        items = [item.as_dict() for item in board.items(section)]
        items = [item for item in items if item["text"].lower() != text.strip().lower()]
        items.append({"text": text, "done": done, "details": details or []})
        return self.set_section(chat_id, section, items, actor=actor)

    def _save_sections(self, board: Board, actor: str) -> None:
        payload = json.dumps({key: [item.as_dict() for item in board.items(key)]
                              for key in SECTION_KEYS}, ensure_ascii=False)
        ts = self.db.now()
        self.db.execute(
            "INSERT INTO boards (chat_id, sections, title, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "sections = excluded.sections, title = excluded.title, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (board.chat_id, payload, board.title, ts, actor))

    def set_message(self, chat_id: int, *, message_id: int | None, message_chat_id: int | None,
                    format: str | None, pinned: bool) -> None:
        self.db.execute(
            "UPDATE boards SET message_id = ?, message_chat_id = ?, format = ?, pinned = ?, "
            "published_at = ?, publish_error = NULL WHERE chat_id = ?",
            (message_id, message_chat_id, format, int(pinned), self.db.now(), chat_id))

    def set_publish_error(self, chat_id: int, error: str | None) -> None:
        self.db.execute("UPDATE boards SET publish_error = ? WHERE chat_id = ?",
                        (error, chat_id))

    def clear(self, chat_id: int) -> bool:
        return self.db.execute("DELETE FROM boards WHERE chat_id = ?", (chat_id,)).rowcount > 0
