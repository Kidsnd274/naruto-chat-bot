"""A fake Telegram Bot API at the HTTP layer, so tests can run the real
TelegramBot (startup, polling, handlers, sending) without the network."""

import asyncio
import inspect
import json
import time

from telegram.request import BaseRequest

BOT_USER = {"id": 42, "is_bot": True, "first_name": "Naruto", "username": "naruto_bot"}


class FakeTelegram(BaseRequest):
    def __init__(self, *, can_read_all_group_messages: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.can_read_all = can_read_all_group_messages
        self.chat_member = {"status": "member", "user": BOT_USER}
        self._updates: list[dict] = []
        self._update_id = 0
        self._message_id = 5000
        self.fail_ephemeral = False

    # ------------------------------------------------------ BaseRequest API

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 1.0

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if endpoint != "getUpdates":
            self.calls.append((endpoint, params))
        handler = getattr(self, f"_{endpoint}", None)
        if handler is None:
            result = True
        else:
            result = await handler(params) if inspect.iscoroutinefunction(handler) else handler(params)
        if isinstance(result, tuple):  # (error_code, description)
            code, description = result
            return code, json.dumps({"ok": False, "error_code": code,
                                     "description": description}).encode()
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # ------------------------------------------------------------ endpoints

    def _getMe(self, params):
        return {**BOT_USER, "can_join_groups": True,
                "can_read_all_group_messages": self.can_read_all,
                "supports_inline_queries": False}

    async def _getUpdates(self, params):
        offset = params.get("offset") or 0
        for _ in range(10):
            pending = [u for u in self._updates if u["update_id"] >= offset]
            if pending:
                return pending
            await asyncio.sleep(0.01)
        return []

    def _sendMessage(self, params):
        if self.fail_ephemeral and "ephemeral_message_parameters" in params:
            return 400, "Bad Request: ephemeral messages are not allowed here"
        self._message_id += 1
        chat_id = int(params["chat_id"])
        chat = ({"id": chat_id, "type": "group", "title": "BBQ crew"} if chat_id < 0
                else {"id": chat_id, "type": "private", "first_name": "Owner"})
        return {"message_id": self._message_id, "date": int(time.time()), "chat": chat,
                "from": BOT_USER, "text": params.get("text", "")}

    def _getChatMember(self, params):
        return self.chat_member

    def _editMessageText(self, params):
        return True

    def _sendRichMessage(self, params):
        self._message_id += 1
        return {"message_id": self._message_id, "date": int(time.time()),
                "chat": {"id": int(params["chat_id"]), "type": "group", "title": "BBQ crew"},
                "from": BOT_USER}

    def _sendPoll(self, params):
        self._message_id += 1
        options = params["options"]
        if isinstance(options, str):
            options = json.loads(options)
        texts = [o["text"] if isinstance(o, dict) else o for o in options]
        return {"message_id": self._message_id, "date": int(time.time()),
                "chat": {"id": int(params["chat_id"]), "type": "group", "title": "BBQ crew"},
                "from": BOT_USER,
                "poll": {"id": f"poll-{self._message_id}", "question": params["question"],
                         "options": [{"text": t, "voter_count": 0, "persistent_id": f"o{i}"}
                                     for i, t in enumerate(texts)],
                         "total_voter_count": 0, "is_closed": False,
                         "is_anonymous": params.get("is_anonymous", True), "type": "regular",
                         "allows_multiple_answers": params.get("allows_multiple_answers", False),
                         "allows_revoting": True, "members_only": False}}

    # -------------------------------------------------------------- helpers

    def push(self, **update) -> int:
        self._update_id += 1
        self._updates.append({"update_id": self._update_id, **update})
        return self._update_id

    def push_message(self, text, *, chat_id=-4001, chat_type="group", title="BBQ crew",
                     user=None, message_id=None, command=False, **extra) -> int:
        self._message_id += 1
        user = user or {"id": 7, "is_bot": False, "first_name": "Alice", "username": "alice"}
        chat = ({"id": chat_id, "type": chat_type, "title": title} if chat_id < 0
                else {"id": chat_id, "type": "private", "first_name": user["first_name"]})
        message = {"message_id": message_id or self._message_id, "date": int(time.time()),
                   "chat": chat, "from": user, "text": text, **extra}
        if command:
            message["entities"] = [{"type": "bot_command", "offset": 0,
                                    "length": len(text.split()[0])}]
        return self.push(message=message)

    def sent(self, endpoint: str = "sendMessage") -> list[dict]:
        return [params for name, params in self.calls if name == endpoint]

    async def wait_for(self, endpoint: str, count: int = 1, timeout: float = 3.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.sent(endpoint)
            if len(found) >= count:
                return found
            await asyncio.sleep(0.01)
        raise AssertionError(f"{endpoint} called {len(self.sent(endpoint))} times, "
                             f"expected {count}; calls: {[c[0] for c in self.calls]}")

    async def settle(self, seconds: float = 0.2) -> None:
        await asyncio.sleep(seconds)
