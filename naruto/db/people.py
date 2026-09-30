"""People and their Telegram accounts, shared by every chat.

Telegram user IDs are the same in every chat, so each account belongs to
exactly one person. The owner can give a person a display name and aliases
once, merge two accounts into one person, or split them again.
"""

from dataclasses import dataclass, field
import sqlite3

from naruto.db.database import Database, now_ts


@dataclass
class Account:
    user_id: int
    person_id: int
    telegram_name: str | None
    username: str | None
    export_name: str | None
    is_bot: bool
    first_seen_at: int
    last_seen_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Account":
        data = {name: row[name] for name in cls.__dataclass_fields__}
        data["is_bot"] = bool(data["is_bot"])
        return cls(**data)

    @property
    def label(self) -> str:
        return self.telegram_name or self.export_name or f"User {self.user_id}"

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else "no @username"


@dataclass
class Person:
    id: int
    name: str | None
    created_at: int
    updated_at: int
    accounts: list[Account] = field(default_factory=list)  # most recently seen first
    aliases: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        """The chosen name, else the most recently seen Telegram name."""
        if self.name:
            return self.name
        for account in self.accounts:
            if account.telegram_name:
                return account.telegram_name
        for account in self.accounts:
            if account.export_name:
                return account.export_name
        return f"Person {self.id}"

    @property
    def username(self) -> str | None:
        return next((a.username for a in self.accounts if a.username), None)

    @property
    def is_bot(self) -> bool:
        return bool(self.accounts) and all(a.is_bot for a in self.accounts)

    @property
    def last_seen_at(self) -> int:
        return max((a.last_seen_at for a in self.accounts), default=self.updated_at)


@dataclass
class PersonSummary:
    person: Person
    chat_count: int
    message_count: int


class PeopleRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- accounts

    def _ensure_account(self, user_id: int, seen_at: int) -> int:
        """Return the account's person, creating both if new."""
        person_id = self.person_id_for(user_id)
        if person_id is not None:
            return person_id
        ts = now_ts()
        with self.db.transaction():
            person_id = self.db.execute(
                "INSERT INTO people (name, created_at, updated_at) VALUES (NULL, ?, ?)",
                (ts, ts)).lastrowid
            self.db.execute(
                "INSERT INTO accounts (user_id, person_id, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, person_id, seen_at, seen_at))
        return person_id

    def touch_live(self, user_id: int, telegram_name: str | None, username: str | None, *,
                   is_bot: bool = False, seen_at: int | None = None) -> int:
        """Record what Telegram currently calls this account."""
        seen_at = seen_at or now_ts()
        person_id = self._ensure_account(user_id, seen_at)
        self.db.execute(
            "UPDATE accounts SET telegram_name = COALESCE(?, telegram_name), username = ?, "
            "is_bot = ?, first_seen_at = MIN(first_seen_at, ?), "
            "last_seen_at = MAX(last_seen_at, ?) WHERE user_id = ?",
            (telegram_name or None, username, int(is_bot), seen_at, seen_at, user_id))
        return person_id

    def touch_imported(self, user_id: int, export_name: str | None, seen_at: int) -> int:
        """Record the name an export used for this account."""
        person_id = self._ensure_account(user_id, seen_at)
        self.db.execute(
            "UPDATE accounts SET export_name = COALESCE(?, export_name), "
            "first_seen_at = MIN(first_seen_at, ?) WHERE user_id = ?",
            (export_name or None, seen_at, user_id))
        return person_id

    def person_id_for(self, user_id: int) -> int | None:
        return self.db.scalar("SELECT person_id FROM accounts WHERE user_id = ?", (user_id,))

    def account(self, user_id: int) -> Account | None:
        row = self.db.query_one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
        return Account.from_row(row) if row else None

    # ---------------------------------------------------------------- reads

    def _load(self, person_ids: list[int]) -> dict[int, Person]:
        if not person_ids:
            return {}
        placeholders = ", ".join("?" for _ in person_ids)
        people = {row["id"]: Person(row["id"], row["name"], row["created_at"], row["updated_at"])
                  for row in self.db.query(
                      f"SELECT * FROM people WHERE id IN ({placeholders})", person_ids)}
        for row in self.db.query(
                f"SELECT * FROM accounts WHERE person_id IN ({placeholders}) "
                "ORDER BY last_seen_at DESC", person_ids):
            if row["person_id"] in people:
                people[row["person_id"]].accounts.append(Account.from_row(row))
        for row in self.db.query(
                f"SELECT person_id, alias FROM person_aliases WHERE person_id IN ({placeholders}) "
                "ORDER BY created_at, alias", person_ids):
            if row["person_id"] in people:
                people[row["person_id"]].aliases.append(row["alias"])
        return people

    def get(self, person_id: int) -> Person | None:
        return self._load([person_id]).get(person_id)

    def for_user(self, user_id: int) -> Person | None:
        person_id = self.person_id_for(user_id)
        return self.get(person_id) if person_id is not None else None

    def for_users(self, user_ids) -> dict[int, Person]:
        """user_id -> Person for the known accounts among ``user_ids``."""
        user_ids = [u for u in set(user_ids) if u is not None]
        if not user_ids:
            return {}
        placeholders = ", ".join("?" for _ in user_ids)
        links = {row["user_id"]: row["person_id"] for row in self.db.query(
            f"SELECT user_id, person_id FROM accounts WHERE user_id IN ({placeholders})",
            user_ids)}
        people = self._load(list(set(links.values())))
        return {user_id: people[person_id] for user_id, person_id in links.items()
                if person_id in people}

    def display_names(self, user_ids) -> dict[int, str]:
        return {user_id: person.display_name
                for user_id, person in self.for_users(user_ids).items()}

    def find_by_username(self, username: str) -> Account | None:
        target = username.lstrip("@").strip()
        if not target:
            return None
        row = self.db.query_one(
            "SELECT * FROM accounts WHERE username = ? COLLATE NOCASE "
            "ORDER BY last_seen_at DESC LIMIT 1", (target,))
        return Account.from_row(row) if row else None

    def all(self, query: str | None = None) -> list[PersonSummary]:
        """Everyone, most recently seen first, optionally filtered by any
        name, @username or alias."""
        people = self._load([row[0] for row in self.db.query("SELECT id FROM people")])
        chats: dict[int, set] = {}
        for row in self.db.query(
                "SELECT a.person_id, m.chat_id FROM members m "
                "JOIN accounts a ON a.user_id = m.user_id"):
            chats.setdefault(row["person_id"], set()).add(row["chat_id"])
        counts: dict[int, int] = {}
        for row in self.db.query(
                "SELECT a.person_id, COUNT(*) AS n FROM messages x "
                "JOIN accounts a ON a.user_id = x.sender_id GROUP BY a.person_id"):
            counts[row["person_id"]] = row["n"]
        needle = (query or "").strip().lower().lstrip("@")
        result = []
        for person in people.values():
            if needle:
                haystack = [person.display_name, person.name or "", *person.aliases]
                for account in person.accounts:
                    haystack += [account.telegram_name or "", account.export_name or "",
                                 account.username or "", str(account.user_id)]
                if not any(needle in value.lower() for value in haystack):
                    continue
            result.append(PersonSummary(person, len(chats.get(person.id, ())),
                                        counts.get(person.id, 0)))
        result.sort(key=lambda s: (s.person.is_bot, -s.person.last_seen_at))
        return result

    def chats_of(self, person_id: int) -> list[tuple[int, int]]:
        """(chat_id, messages from this person) for every chat they're in."""
        rows = self.db.query(
            "SELECT m.chat_id, (SELECT COUNT(*) FROM messages x WHERE x.chat_id = m.chat_id "
            "  AND x.sender_id IN (SELECT user_id FROM accounts WHERE person_id = ?)) AS n "
            "FROM members m JOIN accounts a ON a.user_id = m.user_id "
            "WHERE a.person_id = ? GROUP BY m.chat_id ORDER BY n DESC",
            (person_id, person_id))
        return [(row["chat_id"], row["n"]) for row in rows]

    # --------------------------------------------------------------- writes

    def _touch_person(self, person_id: int) -> None:
        self.db.execute("UPDATE people SET updated_at = ? WHERE id = ?", (now_ts(), person_id))

    def set_name(self, person_id: int, name: str | None) -> None:
        name = (name or "").strip() or None
        self.db.execute("UPDATE people SET name = ?, updated_at = ? WHERE id = ?",
                        (name, now_ts(), person_id))

    def add_alias(self, person_id: int, alias: str) -> bool:
        alias = alias.strip()
        if not alias or self.get(person_id) is None:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO person_aliases (person_id, alias, created_at) VALUES (?, ?, ?)",
            (person_id, alias, now_ts()))
        return True

    def remove_alias(self, person_id: int, alias: str) -> bool:
        return self.db.execute(
            "DELETE FROM person_aliases WHERE person_id = ? AND alias = ?",
            (person_id, alias.strip())).rowcount > 0

    def clear_aliases(self, person_ids) -> int:
        person_ids = list(person_ids)
        if not person_ids:
            return 0
        placeholders = ", ".join("?" for _ in person_ids)
        return self.db.execute(
            f"DELETE FROM person_aliases WHERE person_id IN ({placeholders})",
            person_ids).rowcount

    def merge(self, source_id: int, target_id: int) -> Person:
        """Fold ``source`` into ``target``: accounts and aliases move over; the
        target keeps its name, or takes the source's if it has none."""
        if source_id == target_id:
            raise ValueError("Can't merge a person into themselves.")
        source, target = self.get(source_id), self.get(target_id)
        if source is None or target is None:
            raise ValueError("Unknown person.")
        with self.db.transaction():
            self.db.execute("UPDATE accounts SET person_id = ? WHERE person_id = ?",
                            (target_id, source_id))
            self.db.execute(
                "INSERT OR IGNORE INTO person_aliases (person_id, alias, created_at) "
                "SELECT ?, alias, created_at FROM person_aliases WHERE person_id = ?",
                (target_id, source_id))
            if not target.name and source.name:
                self.db.execute("UPDATE people SET name = ? WHERE id = ?", (source.name, target_id))
            self.db.execute("DELETE FROM people WHERE id = ?", (source_id,))
            self._touch_person(target_id)
        return self.get(target_id)

    def move_account(self, user_id: int, target_id: int) -> Person:
        """Attach one account to another person. A person left with no
        accounts is merged into the target."""
        old_id = self.person_id_for(user_id)
        if old_id is None or self.get(target_id) is None:
            raise ValueError("Unknown account or person.")
        if old_id == target_id:
            return self.get(target_id)
        remaining = self.db.scalar(
            "SELECT COUNT(*) FROM accounts WHERE person_id = ? AND user_id != ?", (old_id, user_id))
        if not remaining:
            return self.merge(old_id, target_id)
        with self.db.transaction():
            self.db.execute("UPDATE accounts SET person_id = ? WHERE user_id = ?",
                            (target_id, user_id))
            self._touch_person(target_id)
            self._touch_person(old_id)
        return self.get(target_id)

    def split(self, user_id: int) -> Person:
        """Give an account its own new person (no name, no aliases)."""
        old_id = self.person_id_for(user_id)
        if old_id is None:
            raise ValueError("Unknown account.")
        others = self.db.scalar(
            "SELECT COUNT(*) FROM accounts WHERE person_id = ? AND user_id != ?", (old_id, user_id))
        if not others:
            raise ValueError("That account is already a person of its own.")
        ts = now_ts()
        with self.db.transaction():
            new_id = self.db.execute(
                "INSERT INTO people (name, created_at, updated_at) VALUES (NULL, ?, ?)",
                (ts, ts)).lastrowid
            self.db.execute("UPDATE accounts SET person_id = ? WHERE user_id = ?", (new_id, user_id))
            self._touch_person(old_id)
        return self.get(new_id)
