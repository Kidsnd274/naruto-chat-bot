"""Background jobs and serving the web admin next to the bot."""

import asyncio
from dataclasses import replace
import logging
import socket
import time

import httpx

from naruto import jobs
from naruto.main import _serve_web


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -------------------------------------------------------------------- jobs

def test_cleanup_logs_follows_retention(services):
    now = time.time()
    services.logs.insert_many([(now - 40 * 86400, 20, "x", None, "old"),
                               (now, 20, "x", None, "new")])
    assert jobs.cleanup_logs(services) == "1 log records"
    assert [r.message for r in services.logs.query()] == ["new"]
    services.settings.set("retention.logs_days", 0, actor="t")
    services.logs.insert_many([(now - 400 * 86400, 20, "x", None, "ancient")])
    assert jobs.cleanup_logs(services) is None


async def test_stale_pending_groups_are_left(services):
    left = []

    class FakeAccess:
        async def leave(self, chat_id, actor):
            left.append(chat_id)

    services.access = FakeAccess()
    services.chats.upsert_seen(-1)
    services.chats.upsert_seen(-2)
    services.db.execute("UPDATE chats SET created_at = ? WHERE chat_id = -1", (time.time() - 7200,))
    assert await jobs.leave_stale_pending(services) == 0  # off by default

    services.settings.set("behaviour.pending_leave_hours", 1, actor="t")
    assert await jobs.leave_stale_pending(services) == 1
    assert left == [-1]


async def test_maintenance_survives_a_failing_step(services, monkeypatch, caplog):
    def broken(services):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "cleanup_steps", [broken, lambda s: "3 things"])
    with caplog.at_level(logging.INFO):
        await jobs.run_maintenance(services)
    assert "Cleanup step broken failed" in caplog.text
    assert "Retention cleanup removed 3 things" in caplog.text


# ---------------------------------------------------------------- web admin

async def test_web_admin_serves_until_stop(services):
    port = free_port()
    boot = replace(services.bootstrap, web_port=port)
    stop = asyncio.Event()
    task = asyncio.create_task(_serve_web(services, boot, stop))
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/healthz")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
    assert response.text == "ok"
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_web_admin_bind_failure_keeps_running_until_stop(services, caplog):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        boot = replace(services.bootstrap, web_port=blocker.getsockname()[1])
        stop = asyncio.Event()
        with caplog.at_level(logging.ERROR):
            task = asyncio.create_task(_serve_web(services, boot, stop))
            for _ in range(100):
                if "could not start" in caplog.text:
                    break
                await asyncio.sleep(0.05)
        assert "The web admin could not start" in caplog.text
        assert not task.done()  # the bot would keep running
        stop.set()
        await asyncio.wait_for(task, timeout=5)


def test_cleanup_agent_runs_follows_retention(services):
    old = services.runs.start(chat_id=-1, skill="banter")
    services.db.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                        (time.time() - 40 * 86400, old))
    services.runs.start(chat_id=-1, skill="banter")
    assert jobs.cleanup_agent_runs(services) == "1 agent runs"
    assert services.runs.recent()[1] == 1
    services.settings.set("retention.agent_runs_days", 0, actor="t")
    assert jobs.cleanup_agent_runs(services) is None
