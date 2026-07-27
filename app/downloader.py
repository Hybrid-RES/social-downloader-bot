from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .platforms import Platform
from .settings import Settings
from .storage import StoredResult, finalize_files, media_files, write_failure_report

LOGGER = logging.getLogger(__name__)
CancelCheck = Callable[[], Awaitable[bool]]


class DownloadCancelled(RuntimeError):
    pass


class DownloadFailed(RuntimeError):
    def __init__(self, message: str, report_path: Path | None = None):
        super().__init__(message)
        self.report_path = report_path


@dataclass(frozen=True, slots=True)
class DownloadResult:
    engine: str
    stored: StoredResult


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.gallery_config = Path("/app/config/gallery-dl.conf")

    def _cookie_file(self, platform: Platform) -> Path | None:
        for name in platform.cookie_names:
            candidate = self.settings.cookies_dir / name
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _yt_dlp_command(self, url: str, destination: Path, cookie: Path | None) -> list[str]:
        command = [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--continue",
            "--part",
            "--no-overwrites",
            "--windows-filenames",
            "--trim-filenames",
            "180",
            "--concurrent-fragments",
            "4",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--retry-sleep",
            "exp=1:10",
            "--merge-output-format",
            "mp4",
            "--output",
            str(destination / "%(upload_date)s_%(uploader)s_%(title).120B_[%(id)s].%(ext)s"),
        ]
        if cookie:
            command.extend(["--cookies", str(cookie)])
        command.extend(["--", url])
        return command

    def _gallery_dl_command(self, url: str, destination: Path, cookie: Path | None) -> list[str]:
        command = [
            "gallery-dl",
            "--config-ignore",
            "--config",
            str(self.gallery_config),
            "--directory",
            str(destination),
            "--windows-filenames",
            "--no-input",
            "--retries",
            "5",
            "--http-timeout",
            "45",
        ]
        if cookie:
            command.extend(["--cookies", str(cookie)])
        command.extend(["--", url])
        return command

    async def download(
        self,
        *,
        job: dict,
        platform: Platform,
        cancel_check: CancelCheck,
    ) -> DownloadResult:
        job_id = int(job["id"])
        work_dir = self.settings.work_root / f"job-{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)

        # A previous process may have completed the download before being stopped.
        if media_files(work_dir):
            stored = finalize_files(
                work_dir=work_dir,
                download_root=self.settings.download_root,
                platform_folder=platform.folder,
                created_at=job["created_at"],
            )
            return DownloadResult(engine="recovered", stored=stored)

        cookie = self._cookie_file(platform)
        attempts: list[tuple[str, int | None, str]] = []

        for engine in platform.engines:
            if await cancel_check():
                raise DownloadCancelled("Cancelled before starting downloader")

            # Keep every engine isolated to avoid treating its diagnostic files as success.
            engine_dir = work_dir / engine
            if engine_dir.exists():
                shutil.rmtree(engine_dir)
            engine_dir.mkdir(parents=True, exist_ok=True)

            if engine == "yt-dlp":
                command = self._yt_dlp_command(job["url"], engine_dir, cookie)
            elif engine == "gallery-dl":
                command = self._gallery_dl_command(job["url"], engine_dir, cookie)
            else:
                attempts.append((engine, None, "Unknown engine"))
                continue

            LOGGER.info("job=%s engine=%s starting", job_id, engine)
            return_code, output = await self._run(command, cancel_check)
            attempts.append((engine, return_code, output))

            if await cancel_check():
                raise DownloadCancelled("Cancelled while downloader was running")

            if media_files(engine_dir):
                stored = finalize_files(
                    work_dir=engine_dir,
                    download_root=self.settings.download_root,
                    platform_folder=platform.folder,
                    created_at=job["created_at"],
                )
                return DownloadResult(engine=engine, stored=stored)

            LOGGER.warning(
                "job=%s engine=%s produced no media (exit=%s)", job_id, engine, return_code
            )

        report = write_failure_report(
            download_root=self.settings.download_root,
            platform_folder=platform.folder,
            created_at=job["created_at"],
            job_id=job_id,
            url=job["url"],
            attempts=attempts,
        )
        summaries = []
        for engine, code, output in attempts:
            tail = " ".join(output.strip().splitlines()[-2:])[:500]
            summaries.append(f"{engine}: exit={code}; {tail or 'no output'}")
        raise DownloadFailed(" | ".join(summaries) or "No downloader was attempted", report)

    async def _run(self, command: list[str], cancel_check: CancelCheck) -> tuple[int | None, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_parts: list[bytes] = []

        async def collect() -> None:
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                output_parts.append(chunk)
                # Bound memory usage while retaining the diagnostic tail.
                if sum(map(len, output_parts)) > 2_000_000:
                    output_parts[:] = [b"".join(output_parts)[-1_000_000:]]

        collector = asyncio.create_task(collect())
        elapsed = 0.0
        interval = 2.0
        try:
            while process.returncode is None:
                if await cancel_check():
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                    raise DownloadCancelled("Cancelled by user")
                if elapsed >= self.settings.subprocess_timeout_seconds:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                    raise TimeoutError(
                        f"Downloader exceeded {self.settings.subprocess_timeout_seconds} seconds"
                    )
                try:
                    await asyncio.wait_for(process.wait(), timeout=interval)
                except TimeoutError:
                    elapsed += interval
            await collector
        finally:
            if not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)

        return process.returncode, b"".join(output_parts).decode("utf-8", errors="replace")
