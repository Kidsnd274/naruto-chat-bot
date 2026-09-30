"""Web admin: authentication, CSRF, pages and actions."""

from pathlib import Path
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from naruto.db.messages import LIVE, NewMessage
from naruto.web import auth
from naruto.web.app import create_app

CHAT = -4001
PASSWORD = "correct horse"


@pytest.fixture
def app(services, monkeypatch):
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    return create_app(services, session_secret="test-secret")


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client) -> str:
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"csrf_token": token, "password": PASSWORD, "next": "/"})
    assert response.status_code == 303
    return csrf_from(client.get("/chats").text)


@pytest.fixture
def admin(client):
    token = login(client)
    return SimpleNamespace(client=client, token=token,
                           post=lambda url, data=None, **kw: client.post(
                               url, data={"csrf_token": token, **(data or {})}, **kw))


@pytest.fixture
def chat(services):
    services.chats.upsert_seen(CHAT, title="BBQ crew", chat_type="group")
    services.members.upsert_live(CHAT, 7, "Alice", "alice")
    for i, text in enumerate(["when is the bbq?", "saturday at east coast", "bring the grill"], 1):
        services.messages.insert_live(NewMessage(
            chat_id=CHAT, origin_chat_id=CHAT, source=LIVE, message_id=i, sender_id=7,
            sender_name="Alice", date=1_780_000_000 + i * 86400, text=text))
    return services.chats.get(CHAT)


# -------------------------------------------------------------------- auth

def test_pages_require_login(client):
    response = client.get("/settings")
    assert response.status_code == 303 and response.headers["location"] == "/login?next=/settings"
    assert client.get("/").headers["location"] == "/login"
    htmx = client.get("/partials/status", headers={"HX-Request": "true"})
    assert htmx.status_code == 204 and htmx.headers["HX-Redirect"] == "/login"


def test_healthz_is_public(client):
    assert client.get("/healthz").text == "ok"


def test_login_needs_csrf_and_the_right_password(client):
    assert client.post("/login", data={"password": PASSWORD}).status_code == 403
    token = csrf_from(client.get("/login").text)
    wrong = client.post("/login", data={"csrf_token": token, "password": "nope"})
    assert wrong.status_code == 401 and "Wrong password" in wrong.text
    ok = client.post("/login", data={"csrf_token": token, "password": PASSWORD,
                                     "next": "/logs?level=ERROR"})
    assert ok.status_code == 303 and ok.headers["location"] == "/logs?level=ERROR"
    cookie = ok.headers["set-cookie"].lower()
    assert "naruto_admin=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/logs").status_code == 200


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "\\\\evil", ""])
def test_login_never_redirects_off_site(client, target):
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"csrf_token": token, "password": PASSWORD,
                                           "next": target})
    assert response.headers["location"] == "/"


def test_login_rotates_the_session(client):
    before = csrf_from(client.get("/login").text)
    after = login(client)
    assert before != after


def test_logout(admin):
    assert admin.post("/logout").status_code == 303
    assert admin.client.get("/chats").status_code == 303


def test_empty_admin_password_never_logs_in(services, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY_SECONDS", 0)
    services.bootstrap = replace(services.bootstrap, admin_password="")
    client = TestClient(create_app(services, session_secret="s"), follow_redirects=False)
    token = csrf_from(client.get("/login").text)
    assert client.post("/login", data={"csrf_token": token, "password": ""}).status_code == 401


def test_state_changes_need_csrf(admin, chat):
    client = admin.client
    assert client.post(f"/chats/{CHAT}/enable").status_code == 403
    assert client.post(f"/chats/{CHAT}/enable", data={"csrf_token": "forged"}).status_code == 403
    by_header = client.post(f"/chats/{CHAT}/enable", headers={"X-CSRF-Token": admin.token})
    assert by_header.status_code == 303


def test_cross_origin_posts_are_refused(admin, chat):
    response = admin.post(f"/chats/{CHAT}/enable", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    same = admin.post(f"/chats/{CHAT}/enable", headers={"Origin": "http://testserver"})
    assert same.status_code == 303


def test_security_headers(admin):
    response = admin.client.get("/")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------- pages

def test_dashboard_shows_status_and_pending_chats(admin, chat, services):
    services.logs.insert_many([(1.0, 40, "naruto.tg", None, "Model request failed: boom")])
    html = admin.client.get("/").text
    assert "Waiting for approval" in html and "BBQ crew" in html
    assert "Model request failed: boom" in html
    assert "@naruto_bot" in html
    assert admin.client.get("/partials/status").status_code == 200


def test_model_check_updates_status(admin, services):
    async def models(timeout=10.0):
        return ["qwen3.8-27b"]

    services.llm.list_models = models
    html = admin.post("/model/check").text
    assert "Reachable" in html and "qwen3.8-27b" in html


def test_chats_page_and_actions(admin, chat, services):
    assert "BBQ crew" in admin.client.get("/chats").text
    admin.post(f"/chats/{CHAT}/enable")
    assert services.chats.get(CHAT).enabled
    response = admin.post(f"/chats/{CHAT}/disable", {"back": "detail"})
    assert response.headers["location"] == f"/chats/{CHAT}"
    assert services.chats.get(CHAT).status == "disabled"
    assert admin.post("/chats/999/enable").status_code == 404


def test_leave_needs_confirmation_and_the_bot(admin, chat, services):
    assert "Leave group" in admin.client.get(f"/chats/{CHAT}/leave").text
    assert admin.post(f"/chats/{CHAT}/leave").status_code == 503

    left = []

    class FakeAccess:
        async def leave(self, chat_id, actor):
            left.append((chat_id, actor))

    services.access = FakeAccess()
    assert admin.post(f"/chats/{CHAT}/leave").status_code == 303
    assert left == [(CHAT, "owner (web admin)")]


def test_chat_detail_browses_and_searches_messages(admin, chat):
    html = admin.client.get(f"/chats/{CHAT}").text
    assert "saturday at east coast" in html and "Alice" in html
    assert "3 messages" in html
    partial = admin.client.get(f"/chats/{CHAT}/messages", params={"q": "grill"}).text
    assert "bring the grill" in partial and "saturday" not in partial
    dated = admin.client.get(f"/chats/{CHAT}/messages", params={"since": "2026-05-31"}).text
    assert "bring the grill" in dated and "when is the bbq" not in dated
    assert admin.client.get(f"/chats/{CHAT}/messages", params={"since": "garbage"}).status_code == 400


def test_old_chat_id_redirects_to_current(admin, chat, services):
    services.chats.migrate(CHAT, -1004001)
    response = admin.client.get(f"/chats/{CHAT}")
    assert response.status_code == 303 and response.headers["location"] == "/chats/-1004001"


def test_aliases_can_be_added_and_removed(admin, chat, services):
    admin.post(f"/chats/{CHAT}/members/7/aliases", {"alias": "Ali"})
    assert services.members.aliases(CHAT, 7) == ["Ali"]
    assert "Ali" in admin.client.get(f"/chats/{CHAT}").text
    admin.post(f"/chats/{CHAT}/members/7/aliases/delete", {"alias": "Ali"})
    assert services.members.aliases(CHAT, 7) == []


def test_deleting_messages_takes_two_steps(admin, chat, services):
    preview = admin.post(f"/chats/{CHAT}/delete-messages",
                         {"scope": "older", "before": "2026-05-31", "source": ""})
    assert preview.status_code == 200 and "Delete 2 messages from before 2026-05-31?" in preview.text
    assert services.messages.count(CHAT) == 3

    done = admin.post(f"/chats/{CHAT}/delete-messages",
                      {"scope": "older", "before": "2026-05-31", "source": "", "confirm": "yes"})
    assert done.status_code == 303
    assert services.messages.count(CHAT) == 1
    assert admin.post(f"/chats/{CHAT}/delete-messages",
                      {"scope": "older", "before": ""}).status_code == 400

    admin.post(f"/chats/{CHAT}/delete-messages", {"scope": "all", "confirm": "yes"})
    assert services.messages.count(CHAT) == 0


# ---------------------------------------------------------------- settings

def test_settings_page_lists_sections_and_drift(admin, services):
    services.seed.settings["model.temperature"] = 0.7
    services.seed.sources["model.temperature"] = "config.json"
    html = admin.client.get("/settings").text
    for title in ("Model", "Persona and skills", "Retention", "Context"):
        assert title in html
    assert "Old configuration differs from the database" in html
    assert 'id="model.temperature"' in html


def test_save_valid_setting_via_htmx(admin, services):
    response = admin.post("/settings/context.recent_window", {"value": "25"},
                          headers={"HX-Request": "true"})
    assert response.status_code == 200 and "Saved" in response.text
    assert services.settings["context.recent_window"] == 25
    assert services.settings.last_changed("context.recent_window").changed_by == "owner (web admin)"


def test_invalid_setting_keeps_input_and_shows_error(admin, services):
    response = admin.post("/settings/context.recent_window", {"value": "-4"},
                          headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "Must be at least 1" in response.text and 'value="-4"' in response.text
    assert services.settings.is_default("context.recent_window")
    plain = admin.post("/settings/context.recent_window", {"value": "x"})
    assert plain.status_code == 400


def test_checkbox_off_reset_revert_and_history(admin, services):
    admin.post("/settings/media.enabled", {})  # unchecked checkbox sends nothing
    assert services.settings["media.enabled"] is False
    admin.post("/settings/media.enabled/revert")
    assert services.settings["media.enabled"] is True
    admin.post("/settings/model.temperature", {"value": "0.4"})
    admin.post("/settings/model.temperature/reset")
    assert services.settings["model.temperature"] is None
    history = admin.client.get("/settings/model.temperature/history").text
    assert "0.4" in history and "(default)" in history
    assert admin.post("/settings/nope.nothing", {"value": "1"}).status_code == 404


def test_persona_prompt_round_trips_multiline_text(admin, services):
    text = "You are Naruto.\n\nBe *loud* & kind <3"
    admin.post("/settings/persona.prompt", {"value": text.replace("\n", "\r\n")})
    assert services.settings["persona.prompt"] == text
    html = admin.client.get("/settings").text
    assert "Be *loud* &amp; kind &lt;3" in html  # escaped, not injected


# -------------------------------------------------------------------- logs

def test_logs_page_filters(admin, services):
    services.logs.insert_many([
        (1_780_000_000.0, 20, "naruto.tg.responder", CHAT, "Answering message 3"),
        (1_780_000_001.0, 40, "naruto.llm", None, "Model request failed"),
        (1_780_000_002.0, 10, "naruto.db", None, "debug detail"),
    ])
    html = admin.client.get("/logs").text
    assert "Answering message 3" in html and "debug detail" not in html
    errors = admin.client.get("/logs", params={"level": "ERROR"}).text
    assert "Model request failed" in errors and "Answering" not in errors
    by_chat = admin.client.get("/logs/rows", params={"chat": str(CHAT), "level": "DEBUG"}).text
    assert "Answering message 3" in by_chat and "Model request failed" not in by_chat
    live = admin.client.get("/logs", params={"live": "1"}).text
    assert 'hx-trigger="every 3s"' in live


# ------------------------------------------------------------------ import

FIXTURE = Path(__file__).parent / "fixtures" / "export_basic_group.json"


@pytest.fixture
def importer(services, tmp_path):
    from naruto.importer.service import ImportService

    services.settings.set("retention.imported_messages_days", 0, actor="t")
    services.settings.set("import.distill_memory", False, actor="t")
    services.imports = ImportService(services, tmp_path / "imports")
    return services.imports


def upload(admin, content: bytes, name="result.json"):
    return admin.client.post("/import", files={"file": (name, content, "application/json")},
                             headers={"X-CSRF-Token": admin.token, "HX-Request": "true"})


def test_import_upload_preview_and_run(admin, importer, services):
    assert "Upload a Telegram Desktop export" in admin.client.get("/import").text
    response = upload(admin, FIXTURE.read_bytes())
    assert response.status_code == 204
    location = response.headers["HX-Redirect"]
    record_id = int(location.rsplit("/", 1)[1])

    preview = admin.client.get(location).text
    assert "BBQ crew" in preview and "private_group" in preview
    assert "Alice Tan" in preview and "No known group matches" in preview
    assert "New pending group" in preview and "<strong>12</strong> messages in the export" in preview

    started = admin.post(f"/import/{record_id}/start", {"target": "new"})
    assert started.status_code == 303
    for _ in range(200):  # the job runs in a worker thread
        if importer.repo.get(record_id).status == "done":
            break
        time.sleep(0.02)
    result = admin.client.get(location).text
    assert "<strong>12</strong> messages imported" in result
    assert services.chats.get(-4001).status == "pending"
    assert "Imports" in admin.client.get("/chats/-4001").text
    progress = admin.client.get(f"/import/{record_id}/progress")
    assert progress.status_code == 204 and progress.headers["HX-Refresh"] == "true"


def test_import_estimate_follows_the_chosen_group(admin, importer, services):
    services.chats.upsert_seen(-4001, title="BBQ crew")
    services.chats.upsert_seen(-777, title="Other")
    record_id = int(upload(admin, FIXTURE.read_bytes()).headers["HX-Redirect"].rsplit("/", 1)[1])
    preview = admin.client.get(f"/import/{record_id}").text
    assert "Matched by chat ID" in preview
    estimate = admin.client.get(f"/import/{record_id}/estimate", params={"target": "-777"}).text
    assert "No overlap" in estimate


def test_import_upload_errors(admin, importer, services):
    bad = upload(admin, b"{broken")
    assert bad.status_code == 400 and "not valid JSON" in bad.text
    services.settings.set("import.max_upload_mb", 1, actor="t")
    big = upload(admin, b" " * (2 * 1024 * 1024))
    assert big.status_code == 413 and "1 MB limit" in big.text
    missing = admin.client.post("/import", data={"csrf_token": admin.token})
    assert missing.status_code == 400


def test_import_discard_and_invalid_start(admin, importer):
    record_id = int(upload(admin, FIXTURE.read_bytes()).headers["HX-Redirect"].rsplit("/", 1)[1])
    assert admin.post(f"/import/{record_id}/start", {"target": "abc"}).status_code == 400
    assert admin.post(f"/import/{record_id}/discard").status_code == 303
    assert importer.repo.get(record_id).status == "discarded"
    assert admin.post(f"/import/{record_id}/start", {"target": "new"}).status_code == 409
    assert admin.client.get("/import/999").status_code == 404


def test_import_page_without_service(admin, services):
    services.imports = None
    assert admin.client.get("/import").status_code == 503


# -------------------------------------------------------------------- runs

def test_runs_list_and_detail(admin, chat, services):
    run_id = services.runs.start(chat_id=CHAT, skill="banter", trigger_row_id=1,
                                 trigger_message_id=1, user_id=7)
    services.runs.update(run_id, status="ok", prompt=[
        {"role": "system", "content": "You are Naruto."},
        {"role": "user", "content": [{"type": "text", "text": "## Current request"},
                                     {"type": "image_url", "image_url": {"url": "<image/jpeg, 12 KB>"}}]},
    ], prompt_tokens=321, window_size=12, dropped=2, image_count=1, response="Saturday!",
        reasoning="thinking hard", latency_ms=1500, model="qwen", usage={"prompt_tokens": 300},
        reply_message_ids=[901])
    failed = services.runs.start(chat_id=CHAT, skill="banter")
    services.runs.update(failed, status="error", error="APITimeoutError")

    listing = admin.client.get("/runs").text
    assert "Saturday!" in listing and "APITimeoutError" in listing and "1.5 s" in listing
    only_errors = admin.client.get("/runs", params={"status": "error"}).text
    assert "Saturday!" not in only_errors
    detail = admin.client.get(f"/runs/{run_id}").text
    assert "You are Naruto." in detail and "## Current request" in detail
    assert "&lt;image/jpeg, 12 KB&gt;" in detail and "thinking hard" in detail
    assert "when is the bbq?" in detail  # the trigger message
    assert "Telegram message 901" in detail
    assert admin.client.get("/runs/999").status_code == 404


# ------------------------------------------------------------------ people

@pytest.fixture
def crowd(services, chat):
    services.members.upsert_live(CHAT, 8, "Bob", "bob")
    services.members.upsert_live(CHAT, 70, "Alice (work)", "alice_work")
    services.members.upsert_imported(CHAT, 7, "Ally (contact)", 1_780_000_000)
    return {uid: services.people.person_id_for(uid) for uid in (7, 8, 70)}


def test_people_list_and_search(admin, crowd):
    html = admin.client.get("/people").text
    assert "Alice" in html and "Bob" in html and "export: Ally (contact)" in html
    found = admin.client.get("/people", params={"q": "alice_work"}).text
    assert "Alice (work)" in found and ">Bob<" not in found
    assert "Nobody matches" in admin.client.get("/people", params={"q": "zzz"}).text


def test_rename_and_aliases(admin, crowd, services):
    alice = crowd[7]
    detail = admin.client.get(f"/people/{alice}").text
    assert "Use “Ally (contact)”" in detail and "BBQ crew" in detail
    admin.post(f"/people/{alice}/name", {"name": "Ally"})
    assert services.people.get(alice).display_name == "Ally"
    assert "Now shown as Ally." in admin.client.get(f"/people/{alice}").text
    admin.post(f"/people/{alice}/aliases", {"alias": "Al"})
    assert services.people.get(alice).aliases == ["Al"]
    admin.post(f"/people/{alice}/aliases/delete", {"alias": "Al"})
    admin.post(f"/people/{alice}/name", {"name": ""})
    assert services.people.get(alice).name is None
    # Names show up in the chat's message browser too.
    services.people.set_name(alice, "Ally")
    assert ">Ally</span>" in admin.client.get(f"/chats/{CHAT}").text


def test_merge_needs_confirmation_then_split(admin, crowd, services):
    alice, work = crowd[7], crowd[70]
    preview = admin.post(f"/people/{work}/merge", {"target": str(alice)})
    assert preview.status_code == 200 and "Merge Alice (work) into Alice?" in preview.text
    assert services.people.get(work) is not None

    done = admin.post(f"/people/{work}/merge", {"target": str(alice), "confirm": "yes"})
    assert done.headers["location"] == f"/people/{alice}"
    assert services.people.get(work) is None
    assert {a.user_id for a in services.people.get(alice).accounts} == {7, 70}
    assert "Make a separate person" in admin.client.get(f"/people/{alice}").text

    split = admin.post(f"/people/{alice}/split", {"user_id": "70"})
    assert split.status_code == 303
    assert services.people.for_user(70).id != alice
    assert admin.post(f"/people/{alice}/split", {"user_id": "8"}).status_code == 400
    assert admin.post(f"/people/{alice}/merge", {"target": str(alice)}).status_code == 400
    assert admin.client.get("/people/99999").status_code == 404


def test_import_preview_maps_people(admin, importer, services, chat):
    services.members.upsert_live(-4001, 7, "Alice T.", "alice")
    record_id = int(upload(admin, FIXTURE.read_bytes()).headers["HX-Redirect"].rsplit("/", 1)[1])
    preview = admin.client.get(f"/import/{record_id}").text
    assert "People in this export" in preview
    assert 'name="name-7" value="Alice Tan"' in preview and "Alice T." in preview
    assert "new: not seen live yet" in preview

    bob_person = services.people.touch_live(80, "Bobby", "bobby")
    admin.post(f"/import/{record_id}/start", {
        "target": "-4001", "name-7": "Alice Tan", "merge-7": "",
        "name-8": "Bob", "merge-8": str(bob_person)})
    for _ in range(200):
        if importer.repo.get(record_id).status == "done":
            break
        time.sleep(0.02)
    assert services.people.for_user(7).display_name == "Alice Tan"
    assert services.people.for_user(8).id == bob_person


# ------------------------------------------------------ agent steps, board

def test_run_detail_shows_steps_and_tools(admin, chat, services):
    run_id = services.runs.start(chat_id=CHAT, skill="banter")
    services.runs.update(run_id, status="ok", response="Found it.", model_requests=2,
                         tool_calls=1, steps=[
        {"type": "model", "request": 1, "latency_ms": 800, "finish_reason": "tool_calls",
         "text": "", "tool_calls": [{"id": "a", "name": "search_chat",
                                     "arguments": {"query": "grill"}}],
         "reasoning": "look it up"},
        {"type": "tool", "id": "a", "name": "search_chat", "arguments": {"query": "grill"},
         "result": "[3] Alice (Sat): bring the grill", "error": False, "duration_ms": 4},
        {"type": "model", "request": 2, "latency_ms": 500, "finish_reason": "stop",
         "text": "Found it.", "tool_calls": []},
    ])
    listing = admin.client.get("/runs").text
    assert "search_chat" in listing and "(2 req.)" in listing
    detail = admin.client.get(f"/runs/{run_id}").text
    assert "Model request 1" in detail and "Model request 2" in detail
    assert "Tool <code>search_chat</code>" in detail and "bring the grill" in detail
    assert '"query": "grill"' in detail and "look it up" in detail


def test_board_edit_publish_and_clear(admin, chat, services):
    from fakes import FakeBot
    services.chats.set_status(CHAT, "enabled")
    bot = FakeBot()
    services.telegram = bot
    page = admin.client.get(f"/chats/{CHAT}").text
    assert 'id="board"' in page and "Not sent to the chat yet." in page

    response = admin.post(f"/chats/{CHAT}/board", {"plans": "[x] BBQ Sat\n- Book the pit",
                                                   "decided": "", "questions": "Grill?",
                                                   "publish": "1"})
    assert response.status_code == 303
    board = services.boards.get(CHAT)
    assert [(i.text, i.done) for i in board.items("plans")] == [("BBQ Sat", True),
                                                                ("Book the pit", False)]
    assert board.pinned and bot.api_calls[0][0] == "sendRichMessage"
    page = admin.client.get(f"/chats/{CHAT}").text
    assert "Sent and pinned the board." in page and "[x] BBQ Sat" in page

    admin.post(f"/chats/{CHAT}/board/publish", {"fresh": "1"})
    assert len(bot.pins) == 2 and bot.unpins  # a new pinned board replaces the old one

    confirm = admin.post(f"/chats/{CHAT}/board/clear")
    assert "Clear the board?" in confirm.text and services.boards.exists(CHAT)
    admin.post(f"/chats/{CHAT}/board/clear", {"confirm": "yes"})
    assert not services.boards.exists(CHAT)


def test_board_save_without_the_bot(admin, chat, services):
    admin.post(f"/chats/{CHAT}/board", {"plans": "", "decided": "Splitwise", "questions": "",
                                        "publish": "1"})
    assert [i.text for i in services.boards.get(CHAT).items("decided")] == ["Splitwise"]
    assert "the board wasn&#39;t sent" in admin.client.get(f"/chats/{CHAT}").text


# ------------------------------------------------------------------ memory

def test_memory_page_add_edit_lock_delete(admin, chat, services):
    page = admin.client.get(f"/chats/{CHAT}").text
    assert 'id="digest"' in page and "Memory: 0 notes" in page and 'id="reminders"' in page
    person_id = services.people.person_id_for(7)
    admin.post(f"/chats/{CHAT}/memory", {"content": "Alice is vegetarian",
                                         "category": "preference", "person": str(person_id),
                                         "locked": "1"})
    note = services.notes.for_chat(CHAT)[0]
    assert note.locked and note.person_id == person_id and note.created_by == "owner"
    page = admin.client.get(f"/chats/{CHAT}/memory").text
    assert 'value="Alice is vegetarian"' in page and "locked" in page

    admin.post(f"/chats/{CHAT}/memory/{note.id}", {"content": "Alice is vegan",
                                                   "category": "preference", "person": ""})
    note = services.notes.get(note.id)  # the owner may edit locked notes
    assert note.content == "Alice is vegan" and note.person_id is None
    admin.post(f"/chats/{CHAT}/memory/{note.id}/lock")
    assert not services.notes.get(note.id).locked
    assert "History (4 changes)" in admin.client.get(f"/chats/{CHAT}/memory").text
    assert "vegan" in admin.client.get(f"/chats/{CHAT}/memory", params={"q": "vegan"}).text
    assert "No notes match." in admin.client.get(f"/chats/{CHAT}/memory",
                                                 params={"q": "pizza"}).text
    assert admin.post(f"/chats/{CHAT}/memory", {"content": "x", "person": "999"}).status_code == 400

    confirm = admin.post(f"/chats/{CHAT}/memory/{note.id}/delete")
    assert f"Delete note {note.id}?" in confirm.text and services.notes.get(note.id)
    admin.post(f"/chats/{CHAT}/memory/{note.id}/delete", {"confirm": "yes"})
    assert services.notes.get(note.id) is None

    services.notes.add(CHAT, "fact", created_by="bot", actor="t")
    assert "Delete all 1 memory notes?" in admin.post(f"/chats/{CHAT}/memory-clear").text
    admin.post(f"/chats/{CHAT}/memory-clear", {"confirm": "yes"})
    assert services.notes.count(CHAT) == 0


def test_digest_and_reminders_on_the_chat_page(admin, chat, services):
    from naruto.memory.keeper import MemoryKeeper

    admin.post(f"/chats/{CHAT}/digest", {"text": "- BBQ on Saturday\r\n- Pit 42"})
    assert services.digests.get(CHAT).text == "- BBQ on Saturday\n- Pit 42"
    page = admin.client.get(f"/chats/{CHAT}").text
    assert "- BBQ on Saturday" in page and "3 messages not read into it yet" in page

    services.keeper = MemoryKeeper(services)
    admin.post(f"/chats/{CHAT}/digest/update")
    assert "isn&#39;t enabled" in admin.client.get(f"/chats/{CHAT}").text
    services.chats.set_status(CHAT, "enabled")
    admin.post(f"/chats/{CHAT}/digest/update")
    assert CHAT in services.keeper._requested

    assert "Clear the digest?" in admin.post(f"/chats/{CHAT}/digest/clear").text
    admin.post(f"/chats/{CHAT}/digest/clear", {"confirm": "yes"})
    assert services.digests.get(CHAT) is None

    reminder = services.reminders.create(CHAT, "Bring the grill", int(time.time()) + 600,
                                         created_by="t")
    assert "Bring the grill" in admin.client.get(f"/chats/{CHAT}").text
    admin.post(f"/chats/{CHAT}/reminders/{reminder.id}/cancel")
    assert services.reminders.get(reminder.id).status == "cancelled"
    admin.post(f"/chats/{CHAT}/reminders/{reminder.id}/cancel")
    page = admin.client.get(f"/chats/{CHAT}").text  # both notices, not just the first
    assert "Cancelled reminder 1." in page and "Reminder 1 is already cancelled." in page


def test_import_page_shows_distillation(admin, importer, services):
    record = importer.repo.create(file_name="result.json", file_path="/nowhere", file_size=1)
    importer.repo.update(record.id, status="done", chat_id=CHAT, distill_status="running",
                         distill_total=4, distill_done=1, notes_added=2)
    page = admin.client.get(f"/import/{record.id}").text
    assert "part 1 of 4 (25%), 2 notes so far" in page
    importer.repo.update(record.id, distill_status="done", notes_added=5)
    partial = admin.client.get(f"/import/{record.id}/distill").text
    assert "<strong>5</strong> new memory notes" in partial and "hx-get" not in partial
