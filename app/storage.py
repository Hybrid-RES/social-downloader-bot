from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


IGNORED_SUFFIXES = {
    ".part",
    ".ytdl",
    ".tmp",
    ".temp",
    ".json",
    ".description",
    ".vtt",
    ".srt",
    ".ass",
    ".lrc",
}
_MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".gif",
    ".gifv",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".heic",
    ".bmp",
    ".mp3",
    ".m4a",
    ".aac",
    ".opus",
    ".ogg",
    ".wav",
    ".flac",
}
_INVALID = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


@dataclass(frozen=True, slots=True)
class StoredResult:
    output_dir: Path
    files: tuple[Path, ...]
    total_bytes: int


def media_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        lower_name = path.name.lower()
        if any(lower_name.endswith(suffix) for suffix in (".part", ".ytdl", ".tmp", ".temp")):
            continue
        if path.suffix.lower() in _MEDIA_SUFFIXES:
            result.append(path)
    return sorted(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str, max_length: int = 220) -> str:
    clean = _INVALID.sub("_", name).strip().rstrip(".")
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        clean = "media"
    stem, suffix = os.path.splitext(clean)
    available = max(20, max_length - len(suffix))
    return f"{stem[:available].rstrip()}{suffix}"


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / _safe_name(name)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10000):
        alternative = directory / f"{stem}_{index:02d}{suffix}"
        if not alternative.exists():
            return alternative
    raise RuntimeError(f"Unable to find a free filename for {name!r}")


def finalize_files(
    *,
    work_dir: Path,
    download_root: Path,
    platform_folder: str,
    created_at: str,
) -> StoredResult:
    candidates = media_files(work_dir)
    if not candidates:
        raise RuntimeError("Downloader completed without producing a media file")

    month = datetime.fromisoformat(created_at).strftime("%Y-%m")
    output_dir = download_root / month / platform_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_hashes: dict[str, Path] = {}
    for existing in output_dir.iterdir():
        if existing.is_file() and existing.suffix.lower() in _MEDIA_SUFFIXES:
            try:
                existing_hashes[_sha256(existing)] = existing
            except OSError:
                continue

    moved: list[Path] = []
    seen_in_job: set[str] = set()
    for source in candidates:
        checksum = _sha256(source)
        if checksum in seen_in_job:
            source.unlink(missing_ok=True)
            continue
        seen_in_job.add(checksum)
        if checksum in existing_hashes:
            source.unlink(missing_ok=True)
            moved.append(existing_hashes[checksum])
            continue

        destination = _unique_destination(output_dir, source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        existing_hashes[checksum] = destination
        moved.append(destination)

    # Remove empty extractor subdirectories but preserve failed remnants for diagnostics.
    for directory in sorted((p for p in work_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

    unique_moved = tuple(dict.fromkeys(moved))
    return StoredResult(
        output_dir=output_dir,
        files=unique_moved,
        total_bytes=sum(path.stat().st_size for path in unique_moved if path.exists()),
    )


def write_failure_report(
    *,
    download_root: Path,
    platform_folder: str,
    created_at: str,
    job_id: int,
    url: str,
    attempts: Iterable[tuple[str, int | None, str]],
) -> Path:
    month = datetime.fromisoformat(created_at).strftime("%Y-%m")
    failed_dir = download_root / month / "Failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    report = failed_dir / f"job-{job_id}.txt"
    lines = [
        f"Job: {job_id}",
        f"Platform: {platform_folder}",
        f"URL: {url}",
        f"Created: {created_at}",
        "",
        "Attempts:",
    ]
    for engine, code, output in attempts:
        lines.extend([f"--- {engine} (exit={code}) ---", output[-12000:], ""])
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
