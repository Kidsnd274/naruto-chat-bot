"""Entry point: one asyncio loop runs the Telegram bot, the web admin and the
background jobs, sharing one SQLite database."""

import asyncio
import logging
from pathlib import Path
import signal
import sys

from dotenv import load_dotenv
from telegram.error import InvalidToken
import uvicorn

from naruto.bootstrap import Bootstrap, load_bootstrap
from naruto.db import open_database
from naruto.importer.service import ImportService
from naruto.jobs import start_background_jobs
from naruto.logs import flush_periodically, set_level, setup_logging
from naruto.services import Services
from naruto.settings.seed import apply_seed_if_needed, collect_seed
from naruto.tg.bot import TelegramBot
from naruto.web.app import create_app
from naruto.web.auth import session_secret

logger = logging.getLogger("naruto")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM set ``stop``. uvicorn swaps in its own handlers while
    serving and re-raises the signal to these once it has shut down."""
    loop = asyncio.get_running_loop()

    def handler(signum, frame):
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


async def _serve_web(services: Services, bootstrap: Bootstrap, stop: asyncio.Event) -> None:
    app = create_app(services, session_secret=session_secret(services.db))
    config = uvicorn.Config(app, host=bootstrap.web_host, port=bootstrap.web_port,
                            log_config=None, access_log=False, lifespan="off")
    server = uvicorn.Server(config)

    async def stop_server_on_signal():
        # Covers a signal that arrives before uvicorn installs its handlers.
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(stop_server_on_signal())
    try:
        # Awaited in this task, not a separate one: uvicorn calls sys.exit()
        # when it cannot bind, and a SystemExit escaping a task would stop
        # the whole event loop.
        await server.serve()
    except SystemExit:
        logger.error("The web admin could not start on %s:%s; the bot keeps running.",
                     bootstrap.web_host, bootstrap.web_port)
        await stop.wait()
    finally:
        watcher.cancel()


async def run() -> None:
    load_dotenv()
    log_handler = setup_logging("INFO")
    bootstrap = load_bootstrap()

    db = open_database(bootstrap.database_path)
    seed = collect_seed()
    services = Services.create(bootstrap, db, seed)
    apply_seed_if_needed(db, services.settings, services.chats, seed)
    services.imports = ImportService(
        services, Path(bootstrap.database_path).resolve().parent / "imports")
    services.imports.recover()
    set_level(services.settings["general.log_level"])
    services.settings.on_change(
        lambda key, value: set_level(value) if key == "general.log_level" else None)

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    tasks = [asyncio.create_task(flush_periodically(log_handler, services.logs))]
    bot = TelegramBot(services)
    try:
        await bot.start()
        tasks.extend(start_background_jobs(services))
        logger.info("Bot started")
        if bootstrap.web_enabled:
            logger.info("Web admin on http://%s:%s/", bootstrap.web_host, bootstrap.web_port)
            await _serve_web(services, bootstrap, stop)
        else:
            await stop.wait()
    finally:
        logger.info("Shutting down")
        try:
            await bot.stop()
        except Exception:
            logger.exception("Error while stopping the bot")
        await services.imports.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log_handler.flush_to(services.logs)
        db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except InvalidToken:
        # The exception text contains the token itself; don't print it.
        logger.critical("Telegram rejected TELEGRAM_BOT_TOKEN. Check the token in .env.")
        sys.exit(1)
    except Exception:
        # Logged through the redacting formatter rather than Python's
        # default traceback printer.
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
