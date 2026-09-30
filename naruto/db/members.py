"""Per-chat roster and /alias nicknames."""

from dataclasses import dataclass, field

from naruto.db.database import Database, now_ts


@dataclass
class Member:
    chat_id: int
    user_id: int
    display_name: str
    username: str | None
    is_bot: bool
    source: str
    first_seen_at: int
    last_seen_at: int
    aliases: list[str] = field(default_factory=list)

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else "no @username"


class MemberRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_live(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        username: str | None,
        *,
        is_bot: bool = False,
        seen_at: int | None = None,
    ) -> None:
        """Last-seen name and username win; aliases are kept."""
        ts = seen_at or now_ts()
        self.db.execute(
            "INSERT INTO members (chat_id, user_id, display_name, username, is_bot, "
            "source, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, 'live', ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "display_name = excluded.display_name, username = excluded.username, "
            "is_bot = excluded.is_bot, source = 'live', "
            "last_seen_at = MAX(members.last_seen_at, excluded.last_seen_at)",
            (chat_id, user_id, display_name, username, int(is_bot), ts, ts),
        )

    def upsert_imported(self, chat_id: int, user_id: int, display_name: str, seen_at: int) -> None:
        """Add a member known only from an export. Names seen live win,
        because exports show the exporting account's contact names."""
        self.db.execute(
            "INSERT INTO members (chat_id, user_id, display_name, username, is_bot, "
            "source, first_seen_at, last_seen_at) VALUES (?, ?, ?, NULL, 0, 'import', ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "display_name = CASE WHEN members.source = 'import' "
            "  THEN excluded.display_name ELSE members.display_name END, "
            "first_seen_at = MIN(members.first_seen_at, excluded.first_seen_at)",
            (chat_id, user_id, display_name, seen_at, seen_at),
        )

    def get(self, chat_id: int, user_id: int) -> Member | None:
        row = self.db.query_one(
            "SELECT * FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        if row is None:
            return None
        member = Member(**dict(row))
        member.is_bot = bool(member.is_bot)
        member.aliases = self.aliases(chat_id, user_id)
        return member

    def list(self, chat_id: int, *, include_bots: bool = True) -> list[Member]:
        alias_rows = self.db.query(
            "SELECT user_id, alias FROM member_aliases WHERE chat_id = ? "
            "ORDER BY created_at, alias",
            (chat_id,),
        )
        aliases: dict[int, list[str]] = {}
        for row in alias_rows:
            aliases.setdefault(row["user_id"], []).append(row["alias"])
        sql = "SELECT * FROM members WHERE chat_id = ?"
        if not include_bots:
            sql += " AND is_bot = 0"
        sql += " ORDER BY last_seen_at DESC, display_name"
        members = []
        for row in self.db.query(sql, (chat_id,)):
            member = Member(**dict(row))
            member.is_bot = bool(member.is_bot)
            member.aliases = aliases.get(member.user_id, [])
            members.append(member)
        return members

    def find_user_id_by_username(self, chat_id: int, username: str) -> int | None:
        target = username.lstrip("@").strip()
        if not target:
            return None
        return self.db.scalar(
            "SELECT user_id FROM members WHERE chat_id = ? AND username = ? COLLATE NOCASE",
            (chat_id, target),
        )

    # -------------------------------------------------------------- aliases

    def aliases(self, chat_id: int, user_id: int) -> list[str]:
        rows = self.db.query(
            "SELECT alias FROM member_aliases WHERE chat_id = ? AND user_id = ? "
            "ORDER BY created_at, alias",
            (chat_id, user_id),
        )
        return [row[0] for row in rows]

    def add_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        """Returns False if the user is not in the roster."""
        alias = alias.strip()
        if not alias or self.get(chat_id, user_id) is None:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO member_aliases (chat_id, user_id, alias, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, user_id, alias, now_ts()),
        )
        return True

    def remove_alias(self, chat_id: int, user_id: int, alias: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM member_aliases WHERE chat_id = ? AND user_id = ? AND alias = ?",
            (chat_id, user_id, alias.strip()),
        )
        return cursor.rowcount > 0

    def clear_aliases(self, chat_id: int) -> int:
        cursor = self.db.execute("DELETE FROM member_aliases WHERE chat_id = ?", (chat_id,))
        return cursor.rowcount
