from __future__ import annotations

import asyncio
from pathlib import Path

from app.database import Database
from app.settings import Settings
from app.worker import WorkerPool


class FakeBot:
    def __init__(self) -> None:
        self.documents: list[tuple[int, str, bytes]] = []
        self.videos: list[tuple[int, str, bytes]] = []
        self.messages: list[str] = []

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append(text)

    async def send_document(
        self,
        *,
        chat_id: int,
        document,
        filename: str,
        **kwargs,
    ) -> None:
        self.documents.append((chat_id, filename, document.read()))

    async def send_video(
        self,
        *,
        chat_id: int,
        video,
        filename: str,
        **kwargs,
    ) -> None:
        self.videos.append((chat_id, filename, video.read()))


class FakeDatabase:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    async def get_chat_send_files(self, chat_id: int, *, default: bool = True) -> bool:
        return self.enabled


def _settings(tmp_path: Path, *, master: bool = True, max_mb: int = 49) -> Settings:
    config = tmp_path / "config"
    downloads = tmp_path / "downloads"
    return Settings(
        telegram_bot_token="test-token",
        allowed_user_ids=frozenset({1}),
        download_root=downloads,
        config_root=config,
        database_path=config / "test.sqlite3",
        max_concurrent_downloads=1,
        worker_poll_seconds=1.0,
        subprocess_timeout_seconds=60,
        history_limit=10,
        telegram_send_files=master,
        telegram_max_upload_mb=max_mb,
        log_level="INFO",
        app_version="test",
    )


def test_send_documents_and_mp4_video(tmp_path: Path) -> None:
    image = tmp_path / "image.jpg"
    video = tmp_path / "video.mp4"
    image.write_bytes(b"image-data")
    video.write_bytes(b"video-data")

    bot = FakeBot()
    worker = WorkerPool(_settings(tmp_path), FakeDatabase(True), bot)  # type: ignore[arg-type]

    asyncio.run(
        worker._send_downloaded_files(
            chat_id=100,
            job_id=25,
            platform_folder="Tumblr",
            files=(image, video),
        )
    )

    assert [(item[0], item[1]) for item in bot.documents] == [(100, "image.jpg")]
    assert [(item[0], item[1]) for item in bot.videos] == [(100, "video.mp4")]
    assert bot.messages == []


def test_oversized_file_stays_on_server_and_is_reported(tmp_path: Path) -> None:
    oversized = tmp_path / "large.bin"
    with oversized.open("wb") as handle:
        handle.seek(1024 * 1024)
        handle.write(b"x")

    bot = FakeBot()
    worker = WorkerPool(
        _settings(tmp_path, max_mb=1),
        FakeDatabase(True),
        bot,  # type: ignore[arg-type]
    )

    asyncio.run(
        worker._send_downloaded_files(
            chat_id=100,
            job_id=26,
            platform_folder="Other",
            files=(oversized,),
        )
    )

    assert bot.documents == []
    assert bot.videos == []
    assert len(bot.messages) == 1
    assert "large.bin" in bot.messages[0]
    assert "больше лимита 1 MB" in bot.messages[0]
    assert oversized.exists()


def test_master_and_chat_switches_disable_delivery(tmp_path: Path) -> None:
    media = tmp_path / "media.jpg"
    media.write_bytes(b"data")

    master_off_bot = FakeBot()
    master_off = WorkerPool(
        _settings(tmp_path, master=False),
        FakeDatabase(True),
        master_off_bot,  # type: ignore[arg-type]
    )
    asyncio.run(
        master_off._send_downloaded_files(
            chat_id=100,
            job_id=27,
            platform_folder="Instagram",
            files=(media,),
        )
    )

    chat_off_bot = FakeBot()
    chat_off = WorkerPool(
        _settings(tmp_path, master=True),
        FakeDatabase(False),
        chat_off_bot,  # type: ignore[arg-type]
    )
    asyncio.run(
        chat_off._send_downloaded_files(
            chat_id=100,
            job_id=28,
            platform_folder="Instagram",
            files=(media,),
        )
    )

    assert master_off_bot.documents == []
    assert chat_off_bot.documents == []


def test_chat_delivery_preference_is_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "preferences.sqlite3")
    asyncio.run(database.initialize())

    assert asyncio.run(database.get_chat_send_files(123, default=True)) is True
    asyncio.run(database.set_chat_send_files(123, False))
    assert asyncio.run(database.get_chat_send_files(123, default=True)) is False
    asyncio.run(database.set_chat_send_files(123, True))
    assert asyncio.run(database.get_chat_send_files(123, default=False)) is True
