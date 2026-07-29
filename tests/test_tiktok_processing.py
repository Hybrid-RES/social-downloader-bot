from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.downloader import Downloader, MediaProbe


async def _never_cancel() -> bool:
    return False


def _downloader() -> Downloader:
    return Downloader(SimpleNamespace())  # type: ignore[arg-type]


def test_tiktok_rejects_video_without_audio(tmp_path: Path) -> None:
    source = tmp_path / "silent.mp4"
    source.write_bytes(b"video")
    downloader = _downloader()

    async def probe(path: Path, cancel_check):  # type: ignore[no-untyped-def]
        assert path == source
        return MediaProbe(video_codec="hevc", audio_codec=None)

    downloader._probe_media = probe  # type: ignore[method-assign]

    ready, detail, transcoded = asyncio.run(
        downloader._prepare_tiktok_media(tmp_path, _never_cancel)
    )

    assert ready is False
    assert "audio stream is missing" in detail
    assert transcoded is False


def test_tiktok_transcodes_hevc_with_audio(tmp_path: Path) -> None:
    source = tmp_path / "hevc.mp4"
    source.write_bytes(b"video")
    downloader = _downloader()
    transcoded_paths: list[Path] = []

    async def probe(path: Path, cancel_check):  # type: ignore[no-untyped-def]
        assert path == source
        return MediaProbe(video_codec="hevc", audio_codec="aac")

    async def transcode(path: Path, cancel_check):  # type: ignore[no-untyped-def]
        transcoded_paths.append(path)
        return path

    downloader._probe_media = probe  # type: ignore[method-assign]
    downloader._transcode_tiktok_video = transcode  # type: ignore[method-assign]

    ready, detail, transcoded = asyncio.run(
        downloader._prepare_tiktok_media(tmp_path, _never_cancel)
    )

    assert ready is True
    assert "H.264/AAC" in detail
    assert transcoded is True
    assert transcoded_paths == [source]


def test_tiktok_keeps_existing_h264_aac(tmp_path: Path) -> None:
    source = tmp_path / "compatible.mp4"
    source.write_bytes(b"video")
    downloader = _downloader()

    async def probe(path: Path, cancel_check):  # type: ignore[no-untyped-def]
        assert path == source
        return MediaProbe(video_codec="h264", audio_codec="aac")

    async def unexpected_transcode(path: Path, cancel_check):  # type: ignore[no-untyped-def]
        raise AssertionError("compatible video must not be transcoded again")

    downloader._probe_media = probe  # type: ignore[method-assign]
    downloader._transcode_tiktok_video = unexpected_transcode  # type: ignore[method-assign]

    ready, detail, transcoded = asyncio.run(
        downloader._prepare_tiktok_media(tmp_path, _never_cancel)
    )

    assert ready is True
    assert "already uses H.264/AAC" in detail
    assert transcoded is False
