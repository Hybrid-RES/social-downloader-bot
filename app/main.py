from __future__ import annotations

import logging
import signal
from pathlib import Path

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import bot as handlers
from . import menu
from .database import Database
from .settings import Settings, SettingsError
from .worker import WorkerPool

LOGGER = logging.getLogger(__name__)
HEARTBEAT = Path("/tmp/social-downloader-heartbeat")


async def on_start(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    database: Database = application.bot_data["database"]
    await database.initialize()
    await menu.register_commands(application.bot)
    worker = WorkerPool(settings, database, application.bot)
    application.bot_data["workers"] = worker
    worker.start()
    HEARTBEAT.touch()
    LOGGER.info("Social Downloader Bot %s started", settings.app_version)


async def on_stop(application: Application) -> None:
    worker: WorkerPool | None = application.bot_data.get("workers")
    if worker:
        await worker.stop()
    LOGGER.info("Social Downloader Bot stopped")


def build_application(settings: Settings) -> Application:
    database = Database(settings.database_path)
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(on_start)
        .post_shutdown(on_stop)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database

    application.add_handler(CommandHandler(("start", "help"), handlers.start))
    application.add_handler(CommandHandler("menu", menu.show_menu))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(CommandHandler("queue", handlers.queue))
    application.add_handler(CommandHandler("history", handlers.history))
    application.add_handler(CommandHandler("failed", handlers.failed))
    application.add_handler(CommandHandler("retry", handlers.retry))
    application.add_handler(CommandHandler("cancel", handlers.cancel))
    application.add_handler(CommandHandler("files_on", handlers.files_on))
    application.add_handler(CommandHandler("files_off", handlers.files_off))
    application.add_handler(CommandHandler("files_status", handlers.files_status))
    application.add_handler(CommandHandler("version", handlers.version))
    application.add_handler(
        CallbackQueryHandler(menu.handle_menu_callback, pattern=r"^menu:")
    )
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handlers.handle_links,
        )
    )
    return application


def main() -> None:
    try:
        settings = Settings.from_env()
        settings.prepare_directories()
    except SettingsError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = build_application(settings)
    application.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=False,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
