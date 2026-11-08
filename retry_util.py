"""Async retry with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "operation",
) -> T:
    """
    Run async factory up to `attempts` times with exponential backoff.
    """
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if index + 1 >= attempts:
                break
            delay = base_delay * (2**index)
            logger.warning(
                "%s failed (%s: %s), retry in %.1fs (%d/%d)",
                label,
                type(exc).__name__,
                exc,
                delay,
                index + 1,
                attempts,
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last
