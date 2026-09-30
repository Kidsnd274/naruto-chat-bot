"""Builders for real python-telegram-bot objects plus a fake Bot that records
what the code under test sends."""

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)
from telegram.error import BadRequest

BOT_ID = 42
BOT_USERNAME = "naruto_bot"
OWNER_ID = 1000
GROUP_ID = -4001
T0 = 1_780_000_000  # a fixed Unix time


def user(user_id=7, name="Alice", username="alice", is_bot=False) -> User:
    return User(id=user_id, first_name=name, is_bot=is_bot, username=username)


ALICE = user()
BOB = user(8, "Bob", None)
OWNER = user(OWNER_ID, "Owner", "owner")
BOT_USER = user(BOT_ID, "Naruto", BOT_USERNAME, is_bot=True)


def group(chat_id=GROUP_ID, title="BBQ crew", chat_type="group") -> Chat:
    return Chat(id=chat_id, type=chat_type, title=title)


def at(offset_seconds: int = 0) -> datetime:
    return datetime.fromtimestamp(T0 + offset_seconds, timezone.utc)


def message(
    message_id: int,
    text: str | None = "hi",
    *,
    sender: User | None = ALICE,
    chat: Chat | None = None,
    offset: int = 0,
    reply_to: Message | None = None,
    command: bool = False,
    bot=None,
    **kwargs,
) -> Message:
    entities = None
    if command and text:
        entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text.split()[0]))]
    msg = Message(
        message_id=message_id,
        date=at(offset),
        chat=chat or group(),
        from_user=sender,
        text=text,
        entities=entities,
        reply_to_message=reply_to,
        **kwargs,
    )
    if bot is not None:
        msg.set_bot(bot)
    return msg


def photo_message(message_id: int, caption: str | None = None, **kwargs) -> Message:
    photo = [PhotoSize("small-id", "small-u", 90, 60), PhotoSize("big-id", "big-u", 1280, 853)]
    return message(message_id, None, photo=photo, caption=caption, **kwargs)


def update(msg: Message | None = None, *, update_id: int = 1, edited: Message | None = None,
           **kwargs) -> Update:
    return Update(update_id=update_id, message=msg, edited_message=edited, **kwargs)


def member_update(new_status: str = "member", old_status: str = "left", *,
                  chat: Chat | None = None, by: User = ALICE, can_pin: bool = False) -> Update:
    def member(status):
        if status == "administrator":
            return ChatMemberAdministrator(
                BOT_USER, can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                can_delete_messages=True, can_manage_video_chats=False,
                can_restrict_members=False, can_promote_members=False,
                can_change_info=False, can_invite_users=False,
                can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
                can_pin_messages=can_pin)
        if status == "member":
            return ChatMemberMember(BOT_USER)
        return ChatMemberLeft(BOT_USER)

    change = ChatMemberUpdated(chat or group(), by, at(), member(old_status), member(new_status))
    return Update(update_id=5, my_chat_member=change)


class FakeBot:
    """Stands in for telegram.Bot. Sent messages come back as real Message
    objects so the recorder can store them."""

    def __init__(self):
        self.id = BOT_ID
        self.username = BOT_USERNAME
        self.sent: list[dict] = []
        self.left: list[int] = []
        self.actions: list[tuple] = []
        self.fail_markdown = False
        self.fail_ephemeral = False
        self.member = None
        self._next_id = 900

    async def send_message(self, chat_id, text, parse_mode=None, reply_parameters=None,
                           reply_markup=None, api_kwargs=None, **kwargs):
        if parse_mode == "Markdown" and self.fail_markdown:
            raise BadRequest("Can't parse entities")
        if api_kwargs and "ephemeral_message_parameters" in api_kwargs and self.fail_ephemeral:
            raise BadRequest("Ephemeral messages are not available")
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                          "reply_parameters": reply_parameters, "reply_markup": reply_markup,
                          "api_kwargs": api_kwargs})
        self._next_id += 1
        chat = group(chat_id) if chat_id < 0 else Chat(chat_id, "private")
        return Message(message_id=self._next_id, date=at(60), chat=chat,
                       from_user=BOT_USER, text=text)

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append((chat_id, action))

    async def leave_chat(self, chat_id, **kwargs):
        self.left.append(chat_id)
        return True

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        return self.member or ChatMemberMember(BOT_USER)


def context(bot: FakeBot, args: list[str] | None = None):
    return SimpleNamespace(bot=bot, args=args or [])
