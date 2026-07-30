from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .database import Database
from .platforms import PLATFORMS
from .settings import Settings
from .threads_downloader import DownloadCancelled, DownloadFailed, Downloader

LOGGER = logging.getLogger(__name__)


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


class WorkerPool:
    def __init__(self, settings: Settings, database: Database, bot: Bot):
        self.settings = settings
        self.database = database
        self.bot = bot
        self.downloader = Downloader(settings)
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self.tasks:
            return
        for number in range(self.settings.max_concurrent_downloads):
            self.tasks.append(asyncio.create_task(self._worker(number + 1)))

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _worker(self, number: int) -> None:
        LOGGER.info("worker-%s started", number)
        while not self.stop_event.is_set():
            job = await self.database.claim_next()
            if job is None:
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    continue
                continue
            await self._process(job)

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except TelegramError:
            LOGGER.exception("Unable to send Telegram notification to chat=%s", chat_id)

    async def _process(self, job: dict) -> None:
        job_id = int(job["id"])
        chat_id = int(job["telegram_chat_id"])
        platform = PLATFORMS.get(job["platform"], PLATFORMS["other"])
        await self._notify(
            chat_id,
            f"⏳ Задание №{job_id}: скачивание началось\nПлатформа: {platform.folder}",
        )

        async def cancel_check() -> bool:
            return await self.database.cancel_requested(job_id)

        try:
            result = await self.downloader.download(
                job=job, platform=platform, cancel_check=cancel_check
            )
            await self.database.mark_postprocessing(job_id, result.engine)
            relative_dir = result.stored.output_dir.relative_to(self.settings.download_root)
            await self.database.complete(
                job_id,
                engine=result.engine,
                output_dir=str(relative_dir),
                files_count=len(result.stored.files),
                total_bytes=result.stored.total_bytes,
            )
            await self._notify(
                chat_id,
                "\n".join(
                    [
                        f"✅ Задание №{job_id} завершено",
                        f"Платформа: {platform.folder}",
                        f"Загрузчик: {result.engine}",
                        f"Файлов: {len(result.stored.files)}",
                        f"Размер: {human_bytes(result.stored.total_bytes)}",
                        f"Папка: {relative_dir}",
                    ]
                ),
            )
        except DownloadCancelled as exc:
            await self.database.mark_cancelled(job_id, str(exc))
            await self._notify(chat_id, f"🚫 Задание №{job_id} отменено")
        except DownloadFailed as exc:
            report = (
                str(exc.report_path.relative_to(self.settings.download_root))
                if exc.report_path
                else "не создан"
            )
            await self.database.fail(job_id, str(exc))
            await self._notify(
                chat_id,
                f"❌ Задание №{job_id} не выполнено\n{str(exc)[:900]}\nОтчёт: {report}",
            )
        except Exception as exc:  # noqa: BLE001 - worker must survive any individual job
            LOGGER.exception("job=%s failed unexpectedly", job_id)
            await self.database.fail(job_id, f"{type(exc).__name__}: {exc}")
            await self._notify(
                chat_id,
                f"❌ Задание №{job_id} завершилось внутренней ошибкой\n{type(exc).__name__}: {exc}",
            )
