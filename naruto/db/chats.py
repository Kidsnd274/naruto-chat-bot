"""Chats the bot has seen, their approval status and chat-ID aliases."""

from dataclasses import dataclass
import logging
import sqlite3

from naruto.db.database import Database
from naruto.db.migrations import CHAT_SCOPED_TABLES

logger = logging.getLogger(__name__)

PENDING = "pending"
ENABLED = "enabled"
DISABLED = "disabled"
STATUSES = (PENDING, ENABLED, DISABLED)


def infer_chat_type(chat_id: int) -> str:
    """Supergroup IDs start with -100; basic groups are other negatives."""
    if chat_id > 0:
        return "private"
    if str(chat_id).startswith("-100"):
        return "supergroup"
    return "group"


@dataclass
class Chat:
    chat_id: int
    title: str
    type: str
    status: str
    membership: str
    added_by_user_id: int | None
    added_by_name: str | None
    can_pin: bool | None
    can_delete: bool | None
    rights_checked_at: int | None
    owner_notified_at: int | None
    created_at: int
    updated_at: int
    status_changed_at: int | None
    last_activity_at: int | None
    recording_since: int | None = None  # when live recording started (imports stop here)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Chat":
        data = dict(row)
        for key in ("can_pin", "can_delete"):
            if data[key] is not None:
                data[key] = bool(data[key])
        # Columns added by later migrations have defaults here.
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @property
    def enabled(self) -> bool:
        return self.status == ENABLED

    @property
    def display_title(self) -> str:
        return self.title or f"Chat {self.chat_id}"

    def missing_rights(self) -> list[str]:
        """Rights the bot should have but does not (unknown counts as missing)."""
        missing = []
        if not self.can_pin:
            missing.append("pin messages")
        return missing


@dataclass
class ChatSummary:
    chat: Chat
    live_messages: int
    imported_messages: int
    last_message_at: int | None
    aliases: list[int]


class ChatRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- reads

    def resolve(self, chat_id: int) -> int:
        """Map an old chat ID to its current one; other IDs map to themselves."""
        current = chat_id
        for _ in range(8):  # alias chains are short; guard against cycles
            target = self.db.scalar(
                "SELECT chat_id FROM chat_id_aliases WHERE alias_chat_id = ?",
                (current,),
            )
            if target is None or target == current:
                return current
            current = target
        return current

    def get(self, chat_id: int, *, resolve: bool = True) -> Chat | None:
        if resolve:
            chat_id = self.resolve(chat_id)
        row = self.db.query_one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
        return Chat.from_row(row) if row else None

    def aliases_for(self, chat_id: int) -> list[int]:
        rows = self.db.query(
            "SELECT alias_chat_id FROM chat_id_aliases WHERE chat_id = ? "
            "ORDER BY created_at",
            (chat_id,),
        )
        return [row[0] for row in rows]

    def list_all(self) -> list[Chat]:
        rows = self.db.query(
            "SELECT * FROM chats ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'enabled' THEN 1 ELSE 2 END, "
            "COALESCE(last_activity_at, created_at) DESC"
        )
        return [Chat.from_row(row) for row in rows]

    def list_by_status(self, status: str) -> list[Chat]:
        rows = self.db.query(
            "SELECT * FROM chats WHERE status = ? ORDER BY created_at", (status,)
        )
        return [Chat.from_row(row) for row in rows]

    def summaries(self) -> list[ChatSummary]:
        counts = {
            row["chat_id"]: row
            for row in self.db.query(
                "SELECT chat_id, "
                "SUM(source = 'live') AS live, SUM(source = 'import') AS imported, "
                "MAX(date) AS last_date FROM messages GROUP BY chat_id"
            )
        }
        aliases: dict[int, list[int]] = {}
        for row in self.db.query("SELECT alias_chat_id, chat_id FROM chat_id_aliases"):
            aliases.setdefault(row["chat_id"], []).append(row["alias_chat_id"])
        result = []
        for chat in self.list_all():
            row = counts.get(chat.chat_id)
            result.append(ChatSummary(
                chat=chat,
                live_messages=int(row["live"] or 0) if row else 0,
                imported_messages=int(row["imported"] or 0) if row else 0,
                last_message_at=row["last_date"] if row else None,
                aliases=aliases.get(chat.chat_id, []),
            ))
        return result

    def find_by_title(self, title: str) -> list[Chat]:
        rows = self.db.query(
            "SELECT * FROM chats WHERE title = ? COLLATE NOCASE", (title.strip(),)
        )
        return [Chat.from_row(row) for row in rows]

    # --------------------------------------------------------------- writes

    def upsert_seen(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        chat_type: str | None = None,
    ) -> tuple[Chat, bool]:
        """Record that the bot saw this chat. New chats start as pending.

        Returns ``(chat, created)``. Titles and types are refreshed when given.
        """
        chat_id = self.resolve(chat_id)
        ts = self.db.now()
        with self.db.transaction():
            existing = self.get(chat_id, resolve=False)
            if existing is None:
                self.db.execute(
                    "INSERT INTO chats (chat_id, title, type, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (chat_id, title or "", chat_type or infer_chat_type(chat_id), ts, ts),
                )
                created = True
            else:
                created = False
                updates = {}
                if title and title != existing.title:
                    updates["title"] = title
                if chat_type and chat_type != existing.type:
                    updates["type"] = chat_type
                if updates:
                    self._update(chat_id, **updates)
            return self.get(chat_id, resolve=False), created

    def _update(self, chat_id: int, **fields) -> None:
        fields["updated_at"] = self.db.now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(
            f"UPDATE chats SET {assignments} WHERE chat_id = ?",
            (*fields.values(), chat_id),
        )

    def set_status(self, chat_id: int, status: str) -> Chat | None:
        if status not in STATUSES:
            raise ValueError(f"unknown chat status {status!r}")
        chat_id = self.resolve(chat_id)
        chat = self.get(chat_id, resolve=False)
        if chat is None:
            return None
        if chat.status != status:
            ts = self.db.now()
            fields: dict = {"status": status, "status_changed_at": ts}
            if status == ENABLED and chat.recording_since is None:
                fields["recording_since"] = ts  # the recorder starts now
            self._update(chat_id, **fields)
            logger.info("Chat %s status %s -> %s", chat_id, chat.status, status)
        return self.get(chat_id, resolve=False)

    def set_recording_since(self, chat_id: int, ts: int | None) -> None:
        """The owner's correction of when live recording began."""
        self._update(self.resolve(chat_id), recording_since=ts)

    def set_membership(
        self,
        chat_id: int,
        membership: str,
        *,
        added_by_user_id: int | None = None,
        added_by_name: str | None = None,
    ) -> None:
        fields: dict = {"membership": membership}
        if added_by_user_id is not None:
            fields["added_by_user_id"] = added_by_user_id
            fields["added_by_name"] = added_by_name
        self._update(self.resolve(chat_id), **fields)

    def set_rights(self, chat_id: int, *, can_pin: bool | None, can_delete: bool | None) -> None:
        self._update(
            self.resolve(chat_id),
            can_pin=None if can_pin is None else int(can_pin),
            can_delete=None if can_delete is None else int(can_delete),
            rights_checked_at=self.db.now(),
        )

    def mark_owner_notified(self, chat_id: int) -> None:
        self._update(self.resolve(chat_id), owner_notified_at=self.db.now())

    def touch_activity(self, chat_id: int, ts: int | None = None) -> None:
        self.db.execute(
            "UPDATE chats SET last_activity_at = ? WHERE chat_id = ?",
            (ts or self.db.now(), self.resolve(chat_id)),
        )

    def seed_enabled(self, chat_id: int) -> bool:
        """Enable a chat from the one-time config.json seed. Returns True if
        the chat was created or changed."""
        chat, created = self.upsert_seen(chat_id)
        if chat.status == ENABLED:
            return created
        self.set_status(chat.chat_id, ENABLED)
        return True

    # ------------------------------------------------------- group upgrades

    def migrate(self, old_chat_id: int, new_chat_id: int) -> Chat | None:
        """Move a chat to a new ID after a basic group became a supergroup.

        Messages, roster and aliases move to the new ID, and the old ID stays
        as an alias. If both IDs are already known (the bot saw the new
        supergroup first), they are merged and the old chat's explicit
        approval decision wins over a pending new chat.
        """
        if old_chat_id == new_chat_id:
            return self.get(new_chat_id)
        ts = self.db.now()
        with self.db.transaction():
            old = self.get(old_chat_id, resolve=False)
            new = self.get(new_chat_id, resolve=False)
            if old is None:
                # Nothing recorded under the old ID; just remember the link.
                self._add_alias(old_chat_id, new_chat_id, ts)
                return new
            if new is None:
                self.db.execute(
                    "UPDATE chats SET chat_id = ?, type = 'supergroup', updated_at = ? "
                    "WHERE chat_id = ?",
                    (new_chat_id, ts, old_chat_id),
                )
            else:
                status = new.status
                if new.status == PENDING and old.status != PENDING:
                    status = old.status
                self._update(
                    new_chat_id,
                    status=status,
                    title=new.title or old.title,
                    type="supergroup",
                    added_by_user_id=new.added_by_user_id or old.added_by_user_id,
                    added_by_name=new.added_by_name or old.added_by_name,
                    created_at=min(old.created_at, new.created_at),
                    recording_since=min((ts for ts in (old.recording_since,
                                                       new.recording_since) if ts is not None),
                                        default=None),
                )
                self.db.execute("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,))
            for table in CHAT_SCOPED_TABLES:
                self.db.execute(
                    f"UPDATE OR IGNORE {table} SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                )
                # Rows that collided with an existing row for the new ID
                # (roster entries seen in both chats) are duplicates.
                self.db.execute(f"DELETE FROM {table} WHERE chat_id = ?", (old_chat_id,))
            self.db.execute(
                "UPDATE chat_id_aliases SET chat_id = ? WHERE chat_id = ?",
                (new_chat_id, old_chat_id),
            )
            self._add_alias(old_chat_id, new_chat_id, ts)
        logger.info("Migrated chat %s to %s", old_chat_id, new_chat_id)
        return self.get(new_chat_id, resolve=False)

    def _add_alias(self, old_chat_id: int, new_chat_id: int, ts: int) -> None:
        self.db.execute(
            "INSERT INTO chat_id_aliases (alias_chat_id, chat_id, reason, created_at) "
            "VALUES (?, ?, 'migrated', ?) "
            "ON CONFLICT(alias_chat_id) DO UPDATE SET chat_id = excluded.chat_id",
            (old_chat_id, new_chat_id, ts),
        )
