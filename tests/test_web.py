"""Web admin: authentication, CSRF, pages and actions."""

import re
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
