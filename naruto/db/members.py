"""Per-chat roster: who has been seen in which chat. Names and aliases
belong to the person (see naruto.db.people) and are shared by every chat."""

from dataclasses import dataclass, field

from naruto.db.database import Database, now_ts
from naruto.db.people import PeopleRepository, Person


@dataclass
class Member:
    chat_id: int
    user_id: int
    person_id: int
    display_name: str
    telegram_name: str | None
    username: str | None
    export_name: str | None
    is_bot: bool
    source: str
    first_seen_at: int
    last_seen_at: int
    aliases: list[str] = field(default_factory=list)

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else "no @username"


def _member(row, person: Person) -> Member:
    account = next(a for a in person.accounts if a.user_id == row["user_id"])
    return Member(
        chat_id=row["chat_id"], user_id=row["user_id"], person_id=person.id,
        display_name=person.display_name, telegram_name=account.telegram_name,
        username=account.username, export_name=account.export_name, is_bot=account.is_bot,
        source=row["source"], first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"], aliases=list(person.aliases),
    )


class MemberRepository:
    def __init__(self, db: Database, people: PeopleRepository):
        self.db = db
        self.people = people

    def _roster(self, chat_id: int, user_id: int, source: str, seen_at: int) -> None:
        self.db.execute(
            "INSERT INTO members (chat_id, user_id, source, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "source = CASE WHEN members.source = 'live' THEN 'live' ELSE excluded.source END, "
            "first_seen_at = MIN(members.first_seen_at, excluded.first_seen_at), "
            "last_seen_at = MAX(members.last_seen_at, excluded.last_seen_at)",
            (chat_id, user_id, source, seen_at, seen_at))

    def upsert_live(self, chat_id: int, user_id: int, display_name: str, username: str | None,
                    *, is_bot: bool = False, seen_at: int | None = None) -> None:
        seen_at = seen_at or now_ts()
        self.people.touch_live(user_id, display_name, username, is_bot=is_bot, seen_at=seen_at)
        self._roster(chat_id, user_id, "live", seen_at)

    def upsert_imported(self, chat_id: int, user_id: int, display_name: str, seen_at: int) -> None:
        self.people.touch_imported(user_id, display_name, seen_at)
        self._roster(chat_id, user_id, "import", seen_at)

    def get(self, chat_id: int, user_id: int) -> Member | None:
        row = self.db.query_one(
            "SELECT * FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        if row is None:
            return None
        person = self.people.for_user(user_id)
        return _member(row, person) if person else None

    def list(self, chat_id: int, *, include_bots: bool = True) -> list[Member]:
        rows = self.db.query(
            "SELECT * FROM members WHERE chat_id = ? ORDER BY last_seen_at DESC", (chat_id,))
        people = self.people.for_users([row["user_id"] for row in rows])
        members = [_member(row, people[row["user_id"]]) for row in rows if row["user_id"] in people]
        if not include_bots:
            members = [m for m in members if not m.is_bot]
        return members

    def find_user_id_by_username(self, chat_id: int, username: str) -> int | None:
        target = username.lstrip("@").strip()
        if not target:
            return None
        return self.db.scalar(
            "SELECT m.user_id FROM members m JOIN accounts a ON a.user_id = m.user_id "
            "WHERE m.chat_id = ? AND a.username = ? COLLATE NOCASE", (chat_id, target))

    # ------------------------------------------------ aliases (per person)

    def aliases(self, chat_id: int, user_id: int) -> list[str]:
        member = self.get(chat_id, user_id)
        return member.aliases if member else []

    def add_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        """Returns False if the user is not in this chat's roster."""
        member = self.get(chat_id, user_id)
        if member is None:
            return False
        return self.people.add_alias(member.person_id, alias)

    def remove_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        member = self.get(chat_id, user_id)
        return bool(member) and self.people.remove_alias(member.person_id, alias)

    def clear_aliases(self, chat_id: int) -> int:
        """Clear the aliases of everyone in this chat (they apply everywhere)."""
        return self.people.clear_aliases({m.person_id for m in self.list(chat_id)})
