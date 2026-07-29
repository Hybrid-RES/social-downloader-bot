from __future__ import annotations

import asyncio
import json
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
_TIKTOK_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
_TIKTOK_GALLERY_ATTEMPTS = 3
_TIKTOK_MAX_FALLBACK_URLS = 20
_TIKTOK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)


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


@dataclass(frozen=True, slots=True)
class MediaProbe:
    video_codec: str | None
    audio_codec: str | None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


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

    def _gallery_dl_urls_command(self, url: str, cookie: Path | None) -> list[str]:
        command = [
            "gallery-dl",
            "--quiet",
            "--config-ignore",
            "--config",
            str(self.gallery_config),
            "--get-urls",
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

    @staticmethod
    def _parse_gallery_dl_fallback_urls(output: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line.startswith("|"):
                continue
            candidate = line[1:].strip()
            if not candidate.startswith(("https://", "http://")):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= _TIKTOK_MAX_FALLBACK_URLS:
                break
        return urls

    async def _probe_media(self, path: Path, cancel_check: CancelCheck) -> MediaProbe:
        return_code, output = await self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(path),
            ],
            cancel_check,
        )
        if return_code != 0:
            tail = " ".join(output.strip().splitlines()[-2:])[:500]
            raise RuntimeError(f"ffprobe failed for {path.name}: {tail or f'exit={return_code}'}")

        try:
            streams = json.loads(output).get("streams", [])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ffprobe returned invalid JSON for {path.name}") from exc

        video_codec = next(
            (stream.get("codec_name") for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        audio_codec = next(
            (stream.get("codec_name") for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        return MediaProbe(video_codec=video_codec, audio_codec=audio_codec)

    @staticmethod
    def _replace_source_with_mp4(source: Path, temp: Path) -> Path:
        destination = source.with_suffix(".mp4")
        if destination != source and destination.exists():
            for index in range(2, 10000):
                candidate = source.with_name(f"{source.stem}_h264_{index:02d}.mp4")
                if not candidate.exists():
                    destination = candidate
                    break
            else:
                temp.unlink(missing_ok=True)
                raise RuntimeError(f"Unable to find a free H.264 filename for {source.name}")

        # Path.replace() atomically removes the old file when destination is source.
        temp.replace(destination)
        if destination != source:
            source.unlink(missing_ok=True)
        return destination

    async def _transcode_tiktok_video(
        self,
        source: Path,
        cancel_check: CancelCheck,
    ) -> Path:
        temp = source.with_name(f".{source.name}.h264.tmp")
        temp.unlink(missing_ok=True)

        return_code, output = await self._run(
            [
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
                "0:a:0",
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
            ],
            cancel_check,
        )
        if return_code != 0 or not temp.is_file() or temp.stat().st_size == 0:
            temp.unlink(missing_ok=True)
            tail = " ".join(output.strip().splitlines()[-2:])[:500]
            raise RuntimeError(
                f"ffmpeg failed for {source.name}: {tail or f'exit={return_code}'}"
            )

        probe = await self._probe_media(temp, cancel_check)
        if probe.video_codec != "h264" or probe.audio_codec != "aac":
            temp.unlink(missing_ok=True)
            raise RuntimeError(
                f"ffmpeg output validation failed for {source.name}: "
                f"video={probe.video_codec or 'none'}, audio={probe.audio_codec or 'none'}"
            )

        return self._replace_source_with_mp4(source, temp)

    async def _transcode_tiktok_fallback_url(
        self,
        *,
        media_url: str,
        source: Path,
        candidate_number: int,
        cancel_check: CancelCheck,
    ) -> tuple[bool, str]:
        temp = source.parent / f".tiktok-fallback-{candidate_number:02d}.tmp"
        temp.unlink(missing_ok=True)

        return_code, output = await self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-nostdin",
                "-y",
                "-user_agent",
                _TIKTOK_USER_AGENT,
                "-headers",
                "Referer: https://www.tiktok.com/\r\n",
                "-i",
                media_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
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
            ],
            cancel_check,
        )
        if return_code != 0 or not temp.is_file() or temp.stat().st_size == 0:
            temp.unlink(missing_ok=True)
            tail = " ".join(output.strip().splitlines()[-2:])[:300]
            return False, f"candidate #{candidate_number}: ffmpeg exit={return_code}; {tail or 'no output'}"

        try:
            probe = await self._probe_media(temp, cancel_check)
        except RuntimeError as exc:
            temp.unlink(missing_ok=True)
            return False, f"candidate #{candidate_number}: {exc}"

        if probe.video_codec != "h264" or probe.audio_codec != "aac":
            temp.unlink(missing_ok=True)
            return (
                False,
                f"candidate #{candidate_number}: invalid output "
                f"video={probe.video_codec or 'none'}, audio={probe.audio_codec or 'none'}",
            )

        destination = self._replace_source_with_mp4(source, temp)
        return True, f"candidate #{candidate_number}: restored audio as {destination.name}"

    async def _find_single_silent_tiktok_video(
        self,
        root: Path,
        cancel_check: CancelCheck,
    ) -> Path | None:
        silent: list[Path] = []
        for path in media_files(root):
            if path.suffix.lower() not in _TIKTOK_VIDEO_SUFFIXES:
                continue
            try:
                probe = await self._probe_media(path, cancel_check)
            except RuntimeError:
                continue
            if probe.has_video and not probe.has_audio:
                silent.append(path)
        return silent[0] if len(silent) == 1 else None

    async def _recover_tiktok_audio_from_fallbacks(
        self,
        *,
        page_url: str,
        source: Path,
        cookie: Path | None,
        cancel_check: CancelCheck,
    ) -> tuple[bool, str, str]:
        return_code, output = await self._run(
            self._gallery_dl_urls_command(page_url, cookie),
            cancel_check,
        )
        fallback_urls = self._parse_gallery_dl_fallback_urls(output)
        diagnostics = [
            f"fallback discovery: exit={return_code}; candidates={len(fallback_urls)}"
        ]

        if not fallback_urls:
            return False, "gallery-dl returned no fallback media URLs", "\n".join(diagnostics)

        for candidate_number, media_url in enumerate(fallback_urls, 1):
            success, detail = await self._transcode_tiktok_fallback_url(
                media_url=media_url,
                source=source,
                candidate_number=candidate_number,
                cancel_check=cancel_check,
            )
            diagnostics.append(detail)
            if success:
                return True, detail, "\n".join(diagnostics)

        return (
            False,
            f"none of {len(fallback_urls)} fallback URLs contained usable video and audio",
            "\n".join(diagnostics),
        )

    async def _prepare_tiktok_media(
        self,
        root: Path,
        cancel_check: CancelCheck,
    ) -> tuple[bool, str, bool]:
        videos = [
            path
            for path in media_files(root)
            if path.suffix.lower() in _TIKTOK_VIDEO_SUFFIXES
        ]
        if not videos:
            return True, "TikTok post contains no video files", False

        probes: dict[Path, MediaProbe] = {}
        for path in videos:
            try:
                probe = await self._probe_media(path, cancel_check)
            except RuntimeError as exc:
                return False, str(exc), False
            probes[path] = probe
            if not probe.has_video:
                return False, f"{path.name}: video stream is missing", False
            if not probe.has_audio:
                return False, f"{path.name}: audio stream is missing", False

        transcoded = 0
        for path in videos:
            probe = probes[path]
            if (
                path.suffix.lower() == ".mp4"
                and probe.video_codec == "h264"
                and probe.audio_codec == "aac"
            ):
                continue
            try:
                await self._transcode_tiktok_video(path, cancel_check)
            except RuntimeError as exc:
                return False, str(exc), transcoded > 0
            transcoded += 1

        if transcoded:
            return True, f"normalized {transcoded} TikTok video(s) to H.264/AAC MP4", True
        return True, "TikTok video already uses H.264/AAC MP4", False

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
            recovered_engine = "recovered"
            if platform.key == "tiktok":
                ready, detail, transcoded = await self._prepare_tiktok_media(
                    work_dir, cancel_check
                )
                if not ready:
                    LOGGER.warning("job=%s recovered TikTok media rejected: %s", job_id, detail)
                    shutil.rmtree(work_dir)
                    work_dir.mkdir(parents=True, exist_ok=True)
                else:
                    if transcoded:
                        recovered_engine += "+ffmpeg"
                    stored = finalize_files(
                        work_dir=work_dir,
                        download_root=self.settings.download_root,
                        platform_folder=platform.folder,
                        created_at=job["created_at"],
                    )
                    return DownloadResult(engine=recovered_engine, stored=stored)
            else:
                stored = finalize_files(
                    work_dir=work_dir,
                    download_root=self.settings.download_root,
                    platform_folder=platform.folder,
                    created_at=job["created_at"],
                )
                return DownloadResult(engine=recovered_engine, stored=stored)

        cookie = self._cookie_file(platform)
        attempts: list[tuple[str, int | None, str]] = []

        for engine in platform.engines:
            max_attempts = (
                _TIKTOK_GALLERY_ATTEMPTS
                if platform.key == "tiktok" and engine == "gallery-dl"
                else 1
            )

            for attempt_number in range(1, max_attempts + 1):
                if await cancel_check():
                    raise DownloadCancelled("Cancelled before starting downloader")

                # Keep every engine isolated to avoid treating diagnostic files as success.
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
                    break

                attempt_label = (
                    f"{engine}#{attempt_number}" if max_attempts > 1 else engine
                )
                LOGGER.info("job=%s engine=%s starting", job_id, attempt_label)
                return_code, output = await self._run(command, cancel_check)

                if await cancel_check():
                    raise DownloadCancelled("Cancelled while downloader was running")

                if media_files(engine_dir):
                    transcoded = False
                    if platform.key == "tiktok":
                        ready, detail, transcoded = await self._prepare_tiktok_media(
                            engine_dir, cancel_check
                        )
                        output = f"{output}\nTikTok validation: {detail}".strip()

                        if (
                            not ready
                            and engine == "gallery-dl"
                            and (
                                silent_source := await self._find_single_silent_tiktok_video(
                                    engine_dir, cancel_check
                                )
                            )
                        ):
                            recovered, recovery_detail, recovery_output = (
                                await self._recover_tiktok_audio_from_fallbacks(
                                    page_url=job["url"],
                                    source=silent_source,
                                    cookie=cookie,
                                    cancel_check=cancel_check,
                                )
                            )
                            output = (
                                f"{output}\nTikTok fallback recovery: {recovery_detail}\n"
                                f"{recovery_output}"
                            ).strip()
                            if recovered:
                                ready, final_detail, final_transcoded = (
                                    await self._prepare_tiktok_media(
                                        engine_dir, cancel_check
                                    )
                                )
                                output = (
                                    f"{output}\nTikTok final validation: {final_detail}"
                                ).strip()
                                transcoded = True
                                detail = final_detail

                        if not ready:
                            attempts.append((attempt_label, return_code, output))
                            LOGGER.warning(
                                "job=%s engine=%s rejected TikTok media: %s",
                                job_id,
                                attempt_label,
                                detail,
                            )
                            if attempt_number < max_attempts:
                                await asyncio.sleep(min(2 * attempt_number, 6))
                                continue
                            break

                    attempts.append((attempt_label, return_code, output))
                    stored = finalize_files(
                        work_dir=engine_dir,
                        download_root=self.settings.download_root,
                        platform_folder=platform.folder,
                        created_at=job["created_at"],
                    )
                    result_engine = f"{engine}+ffmpeg" if transcoded else engine
                    return DownloadResult(engine=result_engine, stored=stored)

                attempts.append((attempt_label, return_code, output))
                LOGGER.warning(
                    "job=%s engine=%s produced no media (exit=%s)",
                    job_id,
                    attempt_label,
                    return_code,
                )
                break

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
