"""Runs lab attempts in the background, in the bot's process.

A batch's attempts run in order, interleaving candidates per scenario so the
model server's prompt cache treats them alike, at most
``lab.parallel_attempts`` at a time across all batches. Their model requests
wait in the bot's queue at background priority (task ``lab``), so replies to
people go first and the Queue page's pause holds them.

An attempt starts only while its run's budget has room; one that is running
may finish a little past the limit (by its own requests). Cancelling stops
attempts that haven't finished; a restart marks them interrupted, and a
batch can be resumed.
"""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Callable

from naruto.lab import config
from naruto.lab.sandbox import LabLLM, Sandbox
from naruto.lab.scenario import MINUTE, ScenarioError, parse_scenario
from naruto.llm import LLMClient
from naruto.settings.registry import SettingError

if TYPE_CHECKING:
    from naruto.db.lab import LabAttempt, LabRun
    from naruto.lab.service import LabService

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 5


@dataclass
class _Running:
    task: asyncio.Task
    cancelled: bool = False


class LabExecutor:
    def __init__(self, lab: "LabService",
                 llm_factory: Callable[[Any, "LabAttempt"], Any] | None = None):
        """``llm_factory(settings, attempt)`` makes the model client for one
        attempt (tests pass a scripted one); by default a LabLLM on the
        bot's queue."""
        self.lab = lab
        self.services = lab.services
        self.repo = lab.repo
        self.llm_factory = llm_factory or self._lab_llm
        self._batches: dict[int, asyncio.Task] = {}
        self._attempts: dict[int, _Running] = {}
        self._active = 0
        self._slots = asyncio.Condition()
        self._stopping = False

    # ------------------------------------------------------------- control

    def start(self, batch_id: int) -> None:
        if batch_id in self._batches and not self._batches[batch_id].done():
            return
        task = asyncio.create_task(self._run_batch(batch_id), name=f"lab-batch-{batch_id}")
        self._batches[batch_id] = task
        task.add_done_callback(lambda _: self._batches.pop(batch_id, None))

    def is_running(self, batch_id: int) -> bool:
        task = self._batches.get(batch_id)
        return task is not None and not task.done()

    async def wait(self, batch_id: int, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a batch. True if it finished."""
        task = self._batches.get(batch_id)
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            return False
        return True

    def cancel_batch(self, batch_id: int) -> int:
        """Stop a batch: waiting attempts are cancelled, running ones
        stopped. Returns how many attempts were stopped."""
        stopped = 0
        for attempt in self.repo.attempts(batch_id=batch_id):
            if attempt.status == "queued":
                self.repo.update_attempt(attempt.id, status="cancelled", outcome="cancelled",
                                         reason="Cancelled before it started.",
                                         finished_at=self.services.db.now())
                stopped += 1
            elif attempt.status == "running":
                running = self._attempts.get(attempt.id)
                if running is not None:
                    running.cancelled = True
                    running.task.cancel()
                    stopped += 1
        batch = self.repo.batch(batch_id)
        if batch is not None and batch.status in ("queued", "running", "interrupted"):
            self.repo.update_batch(batch_id, status="cancelled",
                                   finished_at=self.services.db.now())
        return stopped

    def recover(self) -> int:
        """At startup: whatever was waiting or running is interrupted."""
        return self.repo.interrupt_open()

    def resume(self, batch_id: int) -> int:
        """Run an interrupted batch's unfinished attempts again."""
        count = 0
        for attempt in self.repo.attempts(batch_id=batch_id, status="interrupted"):
            self.repo.update_attempt(attempt.id, status="queued", outcome=None, reason=None,
                                     started_at=None, finished_at=None)
            count += 1
        if count:
            self.repo.update_batch(batch_id, status="queued", finished_at=None)
            self.start(batch_id)
        return count

    async def shutdown(self) -> None:
        self._stopping = True
        for running in list(self._attempts.values()):
            running.task.cancel()
        tasks = list(self._batches.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)

    # --------------------------------------------------------------- slots

    async def _acquire(self) -> None:
        async with self._slots:
            await self._slots.wait_for(
                lambda: self._active < self.services.settings["lab.parallel_attempts"])
            self._active += 1

    async def _release(self) -> None:
        async with self._slots:
            self._active -= 1
            self._slots.notify_all()

    # -------------------------------------------------------------- batches

    async def _run_batch(self, batch_id: int) -> None:
        repo = self.repo
        batch = repo.batch(batch_id)
        if batch is None:
            return
        repo.update_batch(batch_id, status="running")
        running: list[asyncio.Task] = []
        exhausted = None
        try:
            for attempt in repo.attempts(batch_id=batch_id, status="queued"):
                await self._acquire()
                current = repo.attempt(attempt.id)
                if current is None or current.status != "queued":
                    await self._release()
                    continue  # cancelled while waiting for a slot
                exhausted = self.lab.budget_problem(repo.run(attempt.run_id))
                if exhausted:
                    await self._release()
                    break
                repo.update_attempt(attempt.id, status="running",
                                    started_at=self.services.db.now())
                task = asyncio.create_task(self._attempt_task(current),
                                           name=f"lab-attempt-{attempt.id}")
                self._attempts[attempt.id] = _Running(task)
                running.append(task)
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        except asyncio.CancelledError:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
        finally:
            self._finish_batch(batch_id, exhausted)

    def _finish_batch(self, batch_id: int, exhausted: str | None) -> None:
        repo = self.repo
        batch = repo.batch(batch_id)
        if batch is None:
            return
        now = self.services.db.now()
        if exhausted:
            for attempt in repo.attempts(batch_id=batch_id, status="queued"):
                repo.update_attempt(attempt.id, status="skipped", outcome="skipped",
                                    reason=f"Budget: {exhausted}", finished_at=now)
            repo.update_batch(batch_id, status="budget_exhausted", finished_at=now)
            repo.add_event(batch.run_id, "budget_exhausted", batch=batch_id, reason=exhausted)
        elif self._stopping:
            repo.update_batch(batch_id, status="interrupted")
        elif batch.status not in ("cancelled",):
            repo.update_batch(batch_id, status="done", finished_at=now)

    # ------------------------------------------------------------- attempts

    def _lab_llm(self, settings, attempt: "LabAttempt") -> LabLLM:
        running = self._attempts.get(attempt.id)
        client = LLMClient(settings, self.services.bootstrap.openai_api_key,
                           queue=self.services.llm.queue)
        return LabLLM(client, attempt_id=attempt.id,
                      still_wanted=lambda: not (running and running.cancelled)
                      and not self._stopping)

    async def _attempt_task(self, attempt: "LabAttempt") -> None:
        try:
            await self._run_attempt(attempt)
        except asyncio.CancelledError:
            running = self._attempts.get(attempt.id)
            if self._stopping and not (running and running.cancelled):
                self.repo.update_attempt(attempt.id, status="interrupted",
                                         reason="Interrupted by a restart.",
                                         finished_at=self.services.db.now())
            else:
                self.repo.update_attempt(attempt.id, status="cancelled", outcome="cancelled",
                                         reason="Cancelled while it ran.",
                                         finished_at=self.services.db.now())
        except Exception as exc:
            logger.exception("Lab attempt %s failed", attempt.id)
            self.repo.update_attempt(attempt.id, status="done", outcome="error",
                                     reason=f"{type(exc).__name__}: {exc}"[:500],
                                     finished_at=self.services.db.now())
        finally:
            self._attempts.pop(attempt.id, None)
            await self._release()

    def _scenario_for(self, attempt: "LabAttempt", previous: "LabAttempt | None"):
        record = self.repo.scenario(attempt.scenario_id)
        body = dict(record.body)
        first_id = 1
        if previous is not None:
            result = previous.result or {}
            first_id = int(result.get("max_message_id", 0)) + 1
            turns = body.get("turns") or []
            has_date = bool(turns and isinstance(turns[0], dict) and turns[0].get("date"))
            if "time" not in body and not has_date:
                body["time"] = int(result.get("final_clock", 0)) + 2 * MINUTE
        return parse_scenario(body, None, first_id=first_id)

    async def _run_attempt(self, attempt: "LabAttempt") -> None:
        repo = self.repo
        run = repo.run(attempt.run_id)
        candidate = repo.candidate(attempt.candidate_id) if attempt.candidate_id else None
        values = self.lab.effective_settings(run, candidate)
        previous = repo.attempt(attempt.continue_from) if attempt.continue_from else None
        restore = Path(previous.state_path) if previous and previous.state_path else None
        if previous is not None and (restore is None or not restore.exists()):
            self._skip(attempt, "The attempt it continues has no saved state any more.")
            return
        try:
            scenario = self._scenario_for(attempt, previous)
        except ScenarioError as exc:
            self._skip(attempt, f"The scenario can't run: {exc}")
            return
        started = time.monotonic()
        try:
            sandbox = Sandbox(scenario, values,
                              llm_factory=lambda settings: self.llm_factory(settings, attempt),
                              api_key=self.services.bootstrap.openai_api_key, restore=restore)
        except SettingError as exc:
            self._skip(attempt, f"The configuration can't be applied: {exc}")
            return
        state_path = None
        try:
            result = await sandbox.run()
            if result.turns:
                path = self.lab.state_dir / "states" / f"attempt-{attempt.id}.db"
                sandbox.save(path)
                state_path = str(path)
        finally:
            sandbox.close()
        conditions = {
            "endpoint": config.redact_endpoint(run.model_endpoint), "model": run.model_name,
            "models_reported": result.models, "code_fingerprint": config.code_fingerprint(),
            "schema_version": self.services.db.schema_version,
            "scenario": {"id": attempt.scenario_id, "slug": scenario.id,
                         "version": repo.scenario(attempt.scenario_id).version},
            "settings_hash": config.settings_hash(values), "focused": scenario.focused,
            "continues": attempt.continue_from,
        }
        repo.update_attempt(
            attempt.id, status="done", outcome=result.outcome, reason=result.reason,
            result=result.as_dict(), conditions=conditions,
            model_requests=result.model_requests, model_ms=result.model_ms,
            wait_ms=result.wait_ms, duration_ms=int((time.monotonic() - started) * 1000),
            finished_at=self.services.db.now(), state_path=state_path)
        self.lab.note_conditions(run.id, attempt.id, conditions)

    def _skip(self, attempt: "LabAttempt", reason: str) -> None:
        self.repo.update_attempt(attempt.id, status="skipped", outcome="skipped", reason=reason,
                                 finished_at=self.services.db.now())
