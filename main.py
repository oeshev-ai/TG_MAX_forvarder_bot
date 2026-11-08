import asyncio
import logging
import signal
import sys

import config
import discovery
import linking
import pairs
from forward_queue import forward_queue
from loader import max_dp, max_bot, tg_dp, tg_bot
import handlers.max_handlers  # noqa: F401 — register handlers
import handlers.tg_handlers  # noqa: F401 — register handlers
import handlers.max_link  # noqa: F401 — register link wizard
import handlers.tg_link  # noqa: F401 — register link wizard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def _close_bots() -> None:
    if hasattr(tg_bot, "session") and tg_bot.session is not None:
        try:
            await tg_bot.session.close()
        except Exception as exc:
            logger.warning("Error closing Telegram session: %s", exc)

    close_session = getattr(max_bot, "close_session", None)
    if close_session is not None:
        try:
            result = close_session()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("Error closing MAX session: %s", exc)


async def main() -> None:
    pairs.load()
    await linking.remember_bots()
    summary = ", ".join(
        f"TG {tg_id} ↔ MAX {max_id}" for tg_id, max_id in config.TG_TO_MAX.items()
    )
    logger.info(
        "Starting Telegram ↔ MAX bridge with %d pair(s)%s",
        len(config.TG_TO_MAX),
        f": {summary}" if summary else "",
    )
    discovery.log_discovery_summary()

    stop_event = asyncio.Event()

    def _request_stop(*_args) -> None:
        if not stop_event.is_set():
            logger.info("Shutdown signal received")
            stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)
    else:
        signal.signal(signal.SIGINT, _request_stop)
        # SIGTERM is available on Windows but rarely used interactively.
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (ValueError, OSError):
            pass

    max_task = asyncio.create_task(max_dp.start_polling(max_bot), name="max-polling")
    tg_task = asyncio.create_task(tg_dp.start_polling(tg_bot), name="tg-polling")
    stopper = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    done, pending = await asyncio.wait(
        {max_task, tg_task, stopper},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stopper not in done:
        stopper.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                logger.error("Polling crashed: %s", exc, exc_info=exc)

    logger.info("Stopping polling...")
    for task in (max_task, tg_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(max_task, tg_task, stopper, return_exceptions=True)
    await forward_queue.drain()
    await _close_bots()
    logger.info("Stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
