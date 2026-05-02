"""
Async job-queue manager with VIP priority and per-user concurrency control.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

import config


@dataclass(order=True)
class QueueItem:
    """Priority wrapper — lower ``priority`` values run first."""
    priority: int
    job_id: int = field(compare=False)
    user_id: int = field(compare=False)
    is_vip: bool = field(compare=False)
    coro_factory: Callable[[], Awaitable[Any]] = field(compare=False, repr=False)
    enqueued_at: float = field(default_factory=time.monotonic, compare=False)
    cancelled: bool = field(default=False, compare=False)
    # Per-user active-job cap. ``None`` means "unlimited" (admin).
    user_max_active: Optional[int] = field(default=None, compare=False)


class JobQueue:
    """Bounded-concurrency async queue with VIP skip-ahead and per-user caps."""

    def __init__(self, max_workers: int = config.MAX_CONCURRENT_JOBS) -> None:
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue()
        self._sem = asyncio.Semaphore(max_workers)
        self._active: Dict[int, QueueItem] = {}   # job_id -> item
        self._pending: List[QueueItem] = []        # shadow list for position queries
        self._workers_task: Optional[asyncio.Task[None]] = None
        # ``user_id -> currently-running job count``. Used to enforce
        # per-tier active-job caps (free=1, vip=2, admin=∞).
        self._user_active: Dict[int, int] = defaultdict(int)
        # Wakeup signal — bumped whenever a job completes so the
        # consumer loop can re-evaluate per-user gating without busy-waiting.
        self._slot_event = asyncio.Event()

    # ── Public API ──────────────────────────────────────────
    async def start(self) -> None:
        """Spawn the background consumer loop."""
        if self._workers_task is None or self._workers_task.done():
            self._workers_task = asyncio.create_task(self._consumer_loop())
            logger.info(
                "Job queue consumer started (max_concurrent={}, "
                "free/vip per-user caps {}/{})",
                config.MAX_CONCURRENT_JOBS,
                config.FREE_USER_MAX_ACTIVE_JOBS,
                config.VIP_USER_MAX_ACTIVE_JOBS,
            )

    async def stop(self) -> None:
        if self._workers_task and not self._workers_task.done():
            self._workers_task.cancel()
            try:
                await self._workers_task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, item: QueueItem) -> int:
        """Add a job; returns current queue position (0-based)."""
        self._pending.append(item)
        await self._queue.put(item)
        pos = self.position(item.job_id)
        logger.info(
            "Job {} enqueued at position {} (user={}, vip={}, cap={})",
            item.job_id, pos, item.user_id, item.is_vip, item.user_max_active,
        )
        return pos

    def position(self, job_id: int) -> int:
        """Return 0-based queue position, or -1 if running/gone."""
        for idx, item in enumerate(self._pending):
            if item.job_id == job_id and not item.cancelled:
                return idx
        return -1

    def cancel(self, job_id: int) -> bool:
        """Mark a pending job as cancelled. Returns True if found."""
        for item in self._pending:
            if item.job_id == job_id and not item.cancelled:
                item.cancelled = True
                self._pending = [i for i in self._pending if not i.cancelled]
                logger.info("Job {} cancelled in queue", job_id)
                return True
        return False

    def user_active_count(self, user_id: int) -> int:
        """How many of *user_id*'s jobs are currently running."""
        return self._user_active.get(user_id, 0)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def active_jobs(self) -> Dict[int, QueueItem]:
        return dict(self._active)

    # ── Internal ────────────────────────────────────────────
    async def _consumer_loop(self) -> None:
        # Items we pulled off the priority queue but couldn't run yet
        # because the user's per-tier cap was already saturated. Re-fed
        # back into the queue when a slot opens up.
        deferred: List[QueueItem] = []

        while True:
            # If we have deferred items, push the oldest one back first
            # so it doesn't starve while new arrivals keep flowing.
            for d in deferred:
                await self._queue.put(d)
            deferred.clear()

            item = await self._queue.get()

            if item.cancelled:
                self._queue.task_done()
                continue

            # Per-user cap check: ``None`` = unlimited (admin).
            if item.user_max_active is not None:
                running_for_user = self._user_active.get(item.user_id, 0)
                if running_for_user >= item.user_max_active:
                    # User already at their cap — wait until a slot
                    # opens, then re-feed this item. Don't busy-loop.
                    self._slot_event.clear()
                    deferred.append(item)
                    self._queue.task_done()
                    # Block until a job finishes (or 30s tick).
                    try:
                        await asyncio.wait_for(self._slot_event.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

            # Remove from pending shadow list before launch.
            self._pending = [i for i in self._pending if i.job_id != item.job_id]
            self._queue.task_done()
            asyncio.create_task(self._run(item))

    async def _run(self, item: QueueItem) -> None:
        async with self._sem:
            self._active[item.job_id] = item
            self._user_active[item.user_id] += 1
            try:
                logger.info(
                    "Job {} started (user={}, user_active={})",
                    item.job_id, item.user_id,
                    self._user_active[item.user_id],
                )
                await item.coro_factory()
            except Exception:
                logger.exception("Job {} raised", item.job_id)
            finally:
                self._active.pop(item.job_id, None)
                # Decrement user counter; never let it go negative.
                self._user_active[item.user_id] = max(
                    0, self._user_active.get(item.user_id, 0) - 1,
                )
                # Wake the consumer so any deferred jobs get a chance.
                self._slot_event.set()
                logger.info("Job {} finished", item.job_id)
