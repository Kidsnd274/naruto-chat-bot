"""The shared model queue: priorities, limits, live resizing, pause,
cancellation, expiry, retries and the request log."""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from naruto import llm as llm_module
from naruto.llm import LLMClient, LLMError, RequestNotRun
from naruto.model_queue import (
    BACKGROUND,
    FOREGROUND,
    MAX_WAITING,
    ModelQueue,
    QueueRefused,
    RequestInfo,
    compute_limits,
)


@pytest.fixture
def queue(services):
    return ModelQueue(services.settings, services.requests)


class Job:
    """One caller: waits for a slot, records when it started, holds the
    slot until finish()."""

    def __init__(self, queue: ModelQueue, name: str, priority: str, started: list,
                 **info):
        self.queue, self.name, self.started = queue, name, started
        self.done = asyncio.Event()
        self.ticket = None
        self.task = asyncio.create_task(self._run(priority, info))

    async def _run(self, priority, info):
        self.ticket = await self.queue.acquire(RequestInfo(task=self.name, **info), priority)
        self.started.append(self.name)
        await self.done.wait()
        self.queue.release(self.ticket)

    def finish(self):
        self.done.set()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def setting(services, key, value):
    services.settings.set(key, value, actor="t")


async def test_replies_go_first_and_each_priority_is_first_come_first_served(services, queue):
    started = []
    running = Job(queue, "bg-running", BACKGROUND, started)
    await settle()
    jobs = [Job(queue, name, priority, started) for name, priority in (
        ("bg-1", BACKGROUND), ("fg-1", FOREGROUND), ("bg-2", BACKGROUND), ("fg-2", FOREGROUND))]
    await settle()
    assert started == ["bg-running"]
    snapshot = queue.snapshot()
    assert [t.info.task for t in snapshot.waiting[FOREGROUND]] == ["fg-1", "fg-2"]
    assert [t.info.task for t in snapshot.waiting[BACKGROUND]] == ["bg-1", "bg-2"]
    for job in [running, *jobs]:
        job.finish()
        await settle()
    assert started == ["bg-running", "fg-1", "fg-2", "bg-1", "bg-2"]
    assert queue.in_flight == 0 and queue.waiting == 0


async def test_background_cap_and_slots_kept_for_replies(services, queue):
    setting(services, "model.parallel_requests", 4)
    started = []
    background = [Job(queue, f"bg-{i}", BACKGROUND, started) for i in range(3)]
    await settle()
    assert started == ["bg-0"]  # background cap 1
    replies = [Job(queue, f"fg-{i}", FOREGROUND, started) for i in range(3)]
    await settle()
    assert started == ["bg-0", "fg-0", "fg-1", "fg-2"]

    setting(services, "model.background_requests", 4)  # reserve 1 still applies
    for job in replies:
        job.finish()
    await settle()
    assert sorted(started[4:]) == ["bg-1", "bg-2"]
    assert queue.snapshot().running_count(BACKGROUND) == 3
    late = Job(queue, "fg-late", FOREGROUND, started)
    await settle()
    assert started[-1] == "fg-late"  # the kept slot was free for it
    for job in [*background, late]:
        job.finish()
    await settle()


async def test_one_slot_server_still_runs_background_work(services, queue):
    limits = compute_limits(services.settings)
    assert (limits.total, limits.effective_reserved, limits.effective_background) == (1, 0, 1)
    started = []
    digest = Job(queue, "digest", BACKGROUND, started)
    await settle()
    assert started == ["digest"]
    reply = Job(queue, "reply", FOREGROUND, started)
    following = Job(queue, "next chunk", BACKGROUND, started)
    await settle()
    assert started == ["digest"]  # the reply waits for the running request...
    digest.finish()
    await settle()
    assert started == ["digest", "reply"]  # ...but goes ahead of the next one
    reply.finish()
    following.finish()
    await settle()
    assert started[-1] == "next chunk"


async def test_limits_change_while_requests_wait_and_run(services, queue):
    started = []
    jobs = [Job(queue, f"fg-{i}", FOREGROUND, started) for i in range(4)]
    await settle()
    assert started == ["fg-0"]
    setting(services, "model.parallel_requests", 3)  # applies to waiting requests at once
    await settle()
    assert started == ["fg-0", "fg-1", "fg-2"]

    setting(services, "model.parallel_requests", 1)  # running ones carry on
    await settle()
    assert queue.snapshot().over_limit == 2
    jobs[0].finish()
    await settle()
    assert started == ["fg-0", "fg-1", "fg-2"]  # still 2 running > 1: nothing new
    jobs[1].finish()
    jobs[2].finish()
    await settle()
    assert started[-1] == "fg-3" and queue.in_flight == 1
    jobs[3].finish()
    await settle()


async def test_pausing_holds_background_work_only(services, queue):
    setting(services, "model.background_paused", True)
    started = []
    digest = Job(queue, "digest", BACKGROUND, started)
    reply = Job(queue, "reply", FOREGROUND, started)
    await settle()
    assert started == ["reply"]
    reply.finish()
    await settle()
    assert started == ["reply"] and queue.waiting == 1
    setting(services, "model.background_paused", False)
    await settle()
    assert started == ["reply", "digest"]
    digest.finish()
    await settle()


async def test_a_cancelled_waiter_never_reaches_the_model(services, queue):
    started = []
    running = Job(queue, "running", FOREGROUND, started)
    waiting = Job(queue, "waiting", BACKGROUND, started, chat_id=-5)
    await settle()
    request_id = queue.snapshot().waiting[BACKGROUND][0].id
    assert queue.cancel(running.ticket.id) is False  # running requests finish
    assert queue.cancel(request_id) is True
    with pytest.raises(QueueRefused) as refused:
        await waiting.task
    assert refused.value.reason == "cancelled"
    running.finish()
    await settle()
    assert started == ["running"]
    row = services.requests.recent(chat_id=-5)[0]
    assert row.state == "cancelled" and row.started_at is None


async def test_a_request_nobody_wants_any_more_expires(services, queue):
    started = []
    running = Job(queue, "running", FOREGROUND, started)
    await settle()
    wanted = {"value": True}
    stale = Job(queue, "stale", FOREGROUND, started, still_wanted=lambda: wanted["value"])
    await settle()
    wanted["value"] = False  # e.g. the chat was disabled while it waited
    running.finish()
    with pytest.raises(QueueRefused) as refused:
        await stale.task
    assert refused.value.reason == "expired" and started == ["running"]
    assert services.requests.recent(task="stale")[0].state == "expired"


async def test_a_caller_that_gives_up_leaves_the_queue(services, queue):
    started = []
    running = Job(queue, "running", FOREGROUND, started)
    await settle()
    waiter = asyncio.create_task(queue.acquire(RequestInfo(task="gives-up"), FOREGROUND))
    await settle()
    assert queue.waiting == 1
    waiter.cancel()  # the run's deadline
    await asyncio.gather(waiter, return_exceptions=True)
    assert queue.waiting == 0
    running.finish()
    await settle()
    assert queue.in_flight == 0
    assert services.requests.recent(task="gives-up")[0].state == "expired"


async def test_a_full_queue_says_so(services, queue, monkeypatch):
    monkeypatch.setitem(MAX_WAITING, BACKGROUND, 2)
    started = []
    running = Job(queue, "running", FOREGROUND, started)
    waiting = [Job(queue, f"bg-{i}", BACKGROUND, started) for i in range(2)]
    await settle()
    with pytest.raises(QueueRefused) as refused:
        await queue.acquire(RequestInfo(task="one too many"), BACKGROUND)
    assert refused.value.reason == "busy"
    for job in [running, *waiting]:
        job.finish()
    await settle()
    await asyncio.gather(*(job.task for job in waiting))


def test_interrupted_requests_are_marked_at_startup(services, queue):
    info = RequestInfo(task="reply", chat_id=-5)
    queued = services.requests.queued(info, FOREGROUND, 1.0)
    running = services.requests.queued(info, FOREGROUND, 1.0)
    services.requests.started(running, 2.0)
    done = services.requests.queued(info, FOREGROUND, 1.0)
    services.requests.finished(done, "done", 3.0, None)
    assert services.requests.interrupt_open() == 2
    states = {r.id: r.state for r in services.requests.recent()}
    assert states == {queued: "interrupted", running: "interrupted", done: "done"}


# ------------------------------------------------------------- LLMClient

def api_response(text="ok"):
    message = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=None, model="m")


def connection_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


@pytest.fixture
def client(services, monkeypatch):
    monkeypatch.setattr(llm_module, "RETRY_DELAY_SECONDS", 0.01)
    services.settings.set("model.name", "m", actor="t")
    return LLMClient(services.settings, "key", services.requests)


async def test_a_retry_keeps_its_place_in_the_queue(services, client):
    order = []
    gates = {"B": asyncio.Event()}
    attempts = {"A": 0}

    class Server:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            name = kwargs["messages"][0]["content"]
            order.append(name)
            if name == "A":
                attempts["A"] += 1
                if attempts["A"] == 1:
                    raise connection_error()
            if name in gates:
                await gates[name].wait()
            return api_response(name)

    client._get_client = lambda: Server()

    def ask(name):
        return asyncio.create_task(client.chat([{"role": "user", "content": name}],
                                               info=RequestInfo(task=name)))

    a = ask("A")
    await settle()
    b = ask("B")  # takes the slot while A waits to retry
    await settle()
    await asyncio.sleep(0.03)  # A is queued again, ahead of C
    c = ask("C")
    await settle()
    gates["B"].set()
    await asyncio.gather(a, b, c)
    assert order == ["A", "B", "A", "C"]
    row = services.requests.recent(task="A")[0]
    assert row.attempts == 2 and row.state == "done"


async def test_time_outs_and_other_errors_are_not_retried(services, client):
    calls = []

    class Server:
        def __init__(self, error):
            self.error = error
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            calls.append(1)
            raise self.error

    for error in (openai.APITimeoutError(request=httpx.Request("POST", "http://x")),
                  openai.BadRequestError("bad", response=httpx.Response(
                      400, request=httpx.Request("POST", "http://x")), body=None)):
        calls.clear()
        client._get_client = lambda: Server(error)
        with pytest.raises(LLMError):
            await client.chat([{"role": "user", "content": "x"}])
        assert len(calls) == 1
    assert {r.state for r in services.requests.recent()} == {"failed"}
    # The OpenAI client doesn't retry on its own (that would hold the slot).
    assert LLMClient(services.settings, "key")._get_client().max_retries == 0


async def test_a_cancelled_request_is_reported_as_not_run(services, client):
    started = asyncio.Event()
    release = asyncio.Event()

    class Server:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            started.set()
            await release.wait()
            return api_response()

    client._get_client = lambda: Server()
    first = asyncio.create_task(client.chat([{"role": "user", "content": "1"}]))
    await started.wait()
    second = asyncio.create_task(client.chat([{"role": "user", "content": "2"}],
                                             background=True, info=RequestInfo(task="digest")))
    await settle()
    waiting = client.queue.snapshot().waiting[BACKGROUND][0]
    assert client.queue.cancel(waiting.id)
    with pytest.raises(RequestNotRun) as refused:
        await second
    assert refused.value.reason == "cancelled"
    release.set()
    await first
    assert client.in_flight == 0 and client.waiting == 0
