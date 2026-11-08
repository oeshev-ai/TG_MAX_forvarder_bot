"""Limited-concurrency background job runner for forward tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class ForwardQueue:
    """Run forward jobs in the background with a concurrency cap."""

    def __init__(self, concurrency: int = 3) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> None:
        async def _runner() -> None:
            async with self._sem:
                try:
                    await coro
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Background forward job failed (%s)", name or "job")

        task = asyncio.create_task(_runner(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


forward_queue = ForwardQueue(concurrency=3)
