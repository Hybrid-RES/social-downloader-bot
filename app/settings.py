from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _parse_int_set(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise SettingsError(f"Invalid Telegram user ID: {item!r}") from exc
    return frozenset(values)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < 1:
        raise SettingsError(f"{name} must be at least 1")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    download_root: Path
    config_root: Path
    database_path: Path
    max_concurrent_downloads: int
    worker_poll_seconds: float
    subprocess_timeout_seconds: int
    history_limit: int
    log_level: str
    app_version: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SettingsError("TELEGRAM_BOT_TOKEN is required")

        allowed = _parse_int_set(os.getenv("ALLOWED_USER_IDS", ""))
        if not allowed:
            raise SettingsError("ALLOWED_USER_IDS must contain at least one numeric Telegram user ID")

        config_root = Path(os.getenv("CONFIG_ROOT", "/config")).expanduser()
        download_root = Path(os.getenv("DOWNLOAD_ROOT", "/downloads")).expanduser()
        database_path = Path(
            os.getenv("DATABASE_PATH", str(config_root / "social-downloader.sqlite3"))
        ).expanduser()

        try:
            poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
        except ValueError as exc:
            raise SettingsError("WORKER_POLL_SECONDS must be numeric") from exc
        if poll_seconds <= 0:
            raise SettingsError("WORKER_POLL_SECONDS must be greater than zero")

        return cls(
            telegram_bot_token=token,
            allowed_user_ids=allowed,
            download_root=download_root,
            config_root=config_root,
            database_path=database_path,
            max_concurrent_downloads=_positive_int("MAX_CONCURRENT_DOWNLOADS", 1),
            worker_poll_seconds=poll_seconds,
            subprocess_timeout_seconds=_positive_int("SUBPROCESS_TIMEOUT_SECONDS", 7200),
            history_limit=_positive_int("HISTORY_LIMIT", 10),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            app_version=os.getenv("APP_VERSION", "dev"),
        )

    @property
    def cookies_dir(self) -> Path:
        return self.config_root / "cookies"

    @property
    def work_root(self) -> Path:
        return self.download_root / ".work"

    def prepare_directories(self) -> None:
        self.config_root.mkdir(parents=True, exist_ok=True)
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.cookies_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
