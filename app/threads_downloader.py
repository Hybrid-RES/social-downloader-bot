from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .downloader import (
    DownloadCancelled,
    DownloadFailed,
    DownloadResult,
    Downloader as BaseDownloader,
)
from .platforms import Platform
from .storage import finalize_files, media_files
from .threads_extractor import ThreadsExtractionError, ThreadsExtractor

LOGGER = logging.getLogger(__name__)


class Downloader(BaseDownloader):
    async def _normalize_threads_video(self, source: Path, cancel_check) -> bool:  # type: ignore[no-untyped-def]
        probe = await self._probe_media(source, cancel_check)
        if not probe.has_video:
            raise ThreadsExtractionError(f"{source.name}: video stream is missing")

        compatible_audio = probe.audio_codec in {None, "aac"}
        if source.suffix.lower() == ".mp4" and probe.video_codec == "h264" and compatible_audio:
            return False

        temp = source.with_name(f".{source.name}.threads-h264.tmp")
        temp.unlink(missing_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temp),
        ]
        return_code, output = await self._run(command, cancel_check)
        if return_code != 0 or not temp.is_file() or temp.stat().st_size == 0:
            temp.unlink(missing_ok=True)
            tail = " ".join(output.strip().splitlines()[-2:])[:500]
            raise ThreadsExtractionError(
                f"ffmpeg failed for {source.name}: {tail or f'exit={return_code}'}"
            )

        normalized = await self._probe_media(temp, cancel_check)
        if normalized.video_codec != "h264":
            temp.unlink(missing_ok=True)
            raise ThreadsExtractionError(
                f"Threads video validation failed: video={normalized.video_codec or 'none'}"
            )
        if probe.has_audio and normalized.audio_codec != "aac":
            temp.unlink(missing_ok=True)
            raise ThreadsExtractionError(
                f"Threads audio validation failed: audio={normalized.audio_codec or 'none'}"
            )

        self._replace_source_with_mp4(source, temp)
        return True

    async def _download_threads_native(
        self,
        *,
        url: str,
        destination: Path,
        cookie: Path | None,
        cancel_check,
    ) -> tuple[int, int]:  # downloaded files, transcoded videos
        if await cancel_check():
            raise DownloadCancelled("Cancelled before starting Threads extractor")

        extractor = ThreadsExtractor(cookie)
        downloaded = await asyncio.to_thread(
            extractor.extract_and_download,
            url,
            destination,
        )

        if await cancel_check():
            raise DownloadCancelled("Cancelled after Threads media download")

        transcoded = 0
        for item in downloaded:
            if item.kind != "video":
                continue
            if await self._normalize_threads_video(item.path, cancel_check):
                transcoded += 1
        return len(downloaded), transcoded

    async def download(
        self,
        *,
        job: dict,
        platform: Platform,
        cancel_check,
    ) -> DownloadResult:
        if platform.key != "threads":
            return await super().download(
                job=job,
                platform=platform,
                cancel_check=cancel_check,
            )

        job_id = int(job["id"])
        work_dir = self.settings.work_root / f"job-{job_id}"

        # Failed generic extractors can leave thumbnails or incomplete media behind.
        # Start a Threads retry from a clean workspace instead of finalizing stale files.
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        cookie = self._cookie_file(platform)
        native_dir = work_dir / "threads-native"
        native_dir.mkdir(parents=True, exist_ok=True)

        try:
            files_count, transcoded = await self._download_threads_native(
                url=job["url"],
                destination=native_dir,
                cookie=cookie,
                cancel_check=cancel_check,
            )
            if not media_files(native_dir):
                raise ThreadsExtractionError("native extractor produced no media files")
            stored = finalize_files(
                work_dir=native_dir,
                download_root=self.settings.download_root,
                platform_folder=platform.folder,
                created_at=job["created_at"],
            )
            engine = "threads-native+ffmpeg" if transcoded else "threads-native"
            LOGGER.info(
                "job=%s Threads native extractor completed files=%s transcoded=%s",
                job_id,
                files_count,
                transcoded,
            )
            return DownloadResult(engine=engine, stored=stored)
        except DownloadCancelled:
            raise
        except Exception as native_exc:  # noqa: BLE001 - fall back to generic engines
            LOGGER.warning(
                "job=%s Threads native extractor failed: %s",
                job_id,
                native_exc,
            )
            shutil.rmtree(native_dir, ignore_errors=True)
            try:
                return await super().download(
                    job=job,
                    platform=platform,
                    cancel_check=cancel_check,
                )
            except DownloadFailed as fallback_exc:
                raise DownloadFailed(
                    f"threads-native: {type(native_exc).__name__}: {native_exc} | {fallback_exc}",
                    fallback_exc.report_path,
                ) from fallback_exc


__all__ = ["DownloadCancelled", "DownloadFailed", "Downloader"]
