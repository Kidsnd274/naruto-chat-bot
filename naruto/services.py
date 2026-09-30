"""Shared objects used by the Telegram handlers, the web admin and the
background jobs. Everything runs in one process on one asyncio loop."""

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from naruto.bootstrap import Bootstrap
from naruto.db.board import BoardRepository
from naruto.db.chats import ChatRepository
from naruto.db.database import Database
from naruto.db.logs import LogRepository
from naruto.db.members import MemberRepository
from naruto.db.messages import MessageRepository
from naruto.db.people import PeopleRepository
from naruto.db.plans import PlanRepository
from naruto.db.runs import AgentRunRepository
from naruto.llm import LLMClient
from naruto.settings.seed import SeedData
from naruto.settings.service import SettingsService

if TYPE_CHECKING:
    from naruto.importer.service import ImportService
    from naruto.tg.access import ChatAccess


@dataclass
class BotIdentity:
    id: int
    username: str
    name: str


@dataclass
class RuntimeStatus:
    """What the dashboard shows. Updated by the bot and the health check."""
    started_at: float = field(default_factory=time.time)
    bot: BotIdentity | None = None
    can_read_all_group_messages: bool | None = None
    telegram_connected: bool = False
    telegram_error: str | None = None
    last_update_at: float | None = None
    llm_reachable: bool | None = None
    llm_models: list[str] = field(default_factory=list)
    llm_error: str | None = None
    llm_checked_at: float | None = None


@dataclass
class Services:
    bootstrap: Bootstrap
    db: Database
    settings: SettingsService
    chats: ChatRepository
    people: PeopleRepository
    members: MemberRepository
    messages: MessageRepository
    logs: LogRepository
    runs: AgentRunRepository
    boards: BoardRepository
    plans: PlanRepository
    llm: LLMClient
    seed: SeedData = field(default_factory=SeedData)
    status: RuntimeStatus = field(default_factory=RuntimeStatus)
    access: "ChatAccess | None" = None  # set once the Telegram bot exists
    telegram: Any = None  # the telegram.Bot, set once it exists
    imports: "ImportService | None" = None  # set by main (needs an upload directory)

    @classmethod
    def create(cls, bootstrap: Bootstrap, db: Database, seed: SeedData | None = None) -> "Services":
        settings = SettingsService(db)
        people = PeopleRepository(db)
        return cls(
            bootstrap=bootstrap,
            db=db,
            settings=settings,
            chats=ChatRepository(db),
            people=people,
            members=MemberRepository(db, people),
            messages=MessageRepository(db),
            logs=LogRepository(db),
            runs=AgentRunRepository(db),
            boards=BoardRepository(db),
            plans=PlanRepository(db),
            llm=LLMClient(settings, bootstrap.openai_api_key),
            seed=seed or SeedData(),
        )

    def timezone(self) -> tzinfo:
        name = self.settings["general.timezone"]
        if name:
            return ZoneInfo(name)
        return datetime.now().astimezone().tzinfo

    def is_owner(self, user_id: int | None) -> bool:
        owner = self.bootstrap.owner_user_id
        return owner is not None and user_id == owner
