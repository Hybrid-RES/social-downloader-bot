from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ACTIVE_STATUSES = ("queued", "downloading", "postprocessing")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int
    created: bool
    status: str
    output_dir: str | None = None


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_folder TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','downloading','postprocessing','completed','failed','cancelled'
                    )),
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    engine TEXT,
                    output_dir TEXT,
                    files_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_settings (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    send_files INTEGER NOT NULL CHECK(send_files IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_jobs_normalized_url
                    ON jobs(normalized_url, id DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_user_created
                    ON jobs(telegram_user_id, created_at DESC);
                """
            )
            # A hard stop or container update must not leave work permanently locked.
            db.execute(
                """
                UPDATE jobs
                   SET status='queued', updated_at=?, error=COALESCE(error, 'Recovered after restart')
                 WHERE status IN ('downloading','postprocessing')
                """,
                (utc_now(),),
            )

    async def enqueue(
        self,
        *,
        url: str,
        normalized_url: str,
        platform: str,
        platform_folder: str,
        chat_id: int,
        user_id: int,
        message_id: int | None,
    ) -> EnqueueResult:
        return await asyncio.to_thread(
            self._enqueue_sync,
            url,
            normalized_url,
            platform,
            platform_folder,
            chat_id,
            user_id,
            message_id,
        )

    def _enqueue_sync(
        self,
        url: str,
        normalized_url: str,
        platform: str,
        platform_folder: str,
        chat_id: int,
        user_id: int,
        message_id: int | None,
    ) -> EnqueueResult:
        with self._connect() as db:
            existing = db.execute(
                """
                SELECT id, status, output_dir
                  FROM jobs
                 WHERE normalized_url=?
                   AND status IN ('queued','downloading','postprocessing','completed')
                 ORDER BY id DESC LIMIT 1
                """,
                (normalized_url,),
            ).fetchone()
            if existing:
                return EnqueueResult(
                    job_id=existing["id"],
                    created=False,
                    status=existing["status"],
                    output_dir=existing["output_dir"],
                )

            now = utc_now()
            cursor = db.execute(
                """
                INSERT INTO jobs (
                    url, normalized_url, platform, platform_folder, status,
                    telegram_chat_id, telegram_user_id, telegram_message_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    normalized_url,
                    platform,
                    platform_folder,
                    chat_id,
                    user_id,
                    message_id,
                    now,
                    now,
                ),
            )
            return EnqueueResult(job_id=int(cursor.lastrowid), created=True, status="queued")

    async def claim_next(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._claim_next_sync)

    def _claim_next_sync(self) -> dict[str, Any] | None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM jobs
                 WHERE status='queued'
                 ORDER BY created_at, id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = utc_now()
            db.execute(
                """
                UPDATE jobs
                   SET status='downloading', started_at=COALESCE(started_at, ?),
                       updated_at=?, attempts=attempts+1, error=NULL, cancel_requested=0
                 WHERE id=? AND status='queued'
                """,
                (now, now, row["id"]),
            )
            db.commit()
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return dict(claimed) if claimed else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def mark_postprocessing(self, job_id: int, engine: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE jobs SET status='postprocessing', engine=?, updated_at=? WHERE id=?",
            (engine, utc_now(), job_id),
        )

    async def complete(
        self,
        job_id: int,
        *,
        engine: str,
        output_dir: str,
        files_count: int,
        total_bytes: int,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            """
            UPDATE jobs SET status='completed', engine=?, output_dir=?, files_count=?,
                total_bytes=?, error=NULL, finished_at=?, updated_at=? WHERE id=?
            """,
            (engine, output_dir, files_count, total_bytes, utc_now(), utc_now(), job_id),
        )

    async def fail(self, job_id: int, error: str) -> None:
        await asyncio.to_thread(
            self._execute,
            """
            UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?
            """,
            (error[:16000], utc_now(), utc_now(), job_id),
        )

    async def request_cancel(self, job_id: int, user_id: int) -> str | None:
        return await asyncio.to_thread(self._request_cancel_sync, job_id, user_id)

    def _request_cancel_sync(self, job_id: int, user_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE id=? AND telegram_user_id=?", (job_id, user_id)
            ).fetchone()
            if not row:
                return None
            status = row["status"]
            now = utc_now()
            if status == "queued":
                db.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
                return "cancelled"
            if status in {"downloading", "postprocessing"}:
                db.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now, job_id)
                )
                return "requested"
            return status

    async def cancel_requested(self, job_id: int) -> bool:
        return await asyncio.to_thread(self._cancel_requested_sync, job_id)

    def _cancel_requested_sync(self, job_id: int) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row[0])

    async def mark_cancelled(self, job_id: int, message: str = "Cancelled by user") -> None:
        await asyncio.to_thread(
            self._execute,
            """
            UPDATE jobs SET status='cancelled', error=?, finished_at=?, updated_at=? WHERE id=?
            """,
            (message, utc_now(), utc_now(), job_id),
        )

    async def retry(self, job_id: int, user_id: int) -> str | None:
        return await asyncio.to_thread(self._retry_sync, job_id, user_id)

    def _retry_sync(self, job_id: int, user_id: int) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE id=? AND telegram_user_id=?", (job_id, user_id)
            ).fetchone()
            if not row:
                return None
            if row["status"] not in {"failed", "cancelled"}:
                return row["status"]
            db.execute(
                """
                UPDATE jobs SET status='queued', updated_at=?, started_at=NULL, finished_at=NULL,
                    engine=NULL, output_dir=NULL, files_count=0, total_bytes=0,
                    error=NULL, cancel_requested=0 WHERE id=?
                """,
                (utc_now(), job_id),
            )
            return "queued"

    async def get_job(self, job_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_job_sync, job_id, user_id)

    def _get_job_sync(self, job_id: int, user_id: int | None) -> dict[str, Any] | None:
        query = "SELECT * FROM jobs WHERE id=?"
        params: tuple[Any, ...] = (job_id,)
        if user_id is not None:
            query += " AND telegram_user_id=?"
            params = (job_id, user_id)
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
            return dict(row) if row else None

    async def list_jobs(
        self,
        *,
        user_id: int,
        statuses: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_jobs_sync, user_id, tuple(statuses or ()), limit)

    def _list_jobs_sync(
        self, user_id: int, statuses: tuple[str, ...], limit: int
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs WHERE telegram_user_id=?"
        params: list[Any] = [user_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    async def counts(self, user_id: int) -> dict[str, int]:
        return await asyncio.to_thread(self._counts_sync, user_id)

    def _counts_sync(self, user_id: int) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS count FROM jobs WHERE telegram_user_id=? GROUP BY status",
                (user_id,),
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}

    async def get_chat_send_files(self, chat_id: int, *, default: bool = True) -> bool:
        return await asyncio.to_thread(self._get_chat_send_files_sync, chat_id, default)

    def _get_chat_send_files_sync(self, chat_id: int, default: bool) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT send_files FROM chat_settings WHERE telegram_chat_id=?",
                (chat_id,),
            ).fetchone()
            return bool(row["send_files"]) if row else default

    async def set_chat_send_files(self, chat_id: int, enabled: bool) -> None:
        await asyncio.to_thread(self._set_chat_send_files_sync, chat_id, enabled)

    def _set_chat_send_files_sync(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO chat_settings (telegram_chat_id, send_files, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    send_files=excluded.send_files,
                    updated_at=excluded.updated_at
                """,
                (chat_id, int(enabled), utc_now()),
            )

    def _execute(self, query: str, params: tuple[Any, ...]) -> None:
        with self._connect() as db:
            db.execute(query, params)
