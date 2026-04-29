"""
Async job-queue manager with VIP priority and concurrency control.
"""

from __future__ import annotations

import asyncio
import time
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


class JobQueue:
    """Bounded-concurrency async queue with VIP skip-ahead."""

    def __init__(self, max_workers: int = config.MAX_CONCURRENT_JOBS) -> None:
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue()
        self._sem = asyncio.Semaphore(max_workers)
        self._active: Dict[int, QueueItem] = {}   # job_id -> item
        self._pending: List[QueueItem] = []        # shadow list for position queries
        self._workers_task: Optional[asyncio.Task[None]] = None

    # ── Public API ──────────────────────────────────────────
    async def start(self) -> None:
        """Spawn the background consumer loop."""
        if self._workers_task is None or self._workers_task.done():
            self._workers_task = asyncio.create_task(self._consumer_loop())
            logger.info("Job queue consumer started (concurrency={})", config.MAX_CONCURRENT_JOBS)

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
        logger.info("Job {} enqueued at position {}", item.job_id, pos)
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
        while True:
            item = await self._queue.get()
            if item.cancelled:
                self._queue.task_done()
                continue
            # Remove from pending shadow list
            self._pending = [i for i in self._pending if i.job_id != item.job_id]
            asyncio.create_task(self._run(item))

    async def _run(self, item: QueueItem) -> None:
        async with self._sem:
            self._active[item.job_id] = item
            try:
                logger.info("Job {} started", item.job_id)
                await item.coro_factory()
            except Exception:
                logger.exception("Job {} raised", item.job_id)
            finally:
                self._active.pop(item.job_id, None)
                logger.info("Job {} finished", item.job_id)
