import asyncio
from pathlib import Path

from app.database import Database


def run(coro):
    return asyncio.run(coro)


def test_enqueue_duplicate_and_retry(tmp_path: Path) -> None:
    db = Database(tmp_path / "queue.sqlite3")
    run(db.initialize())

    first = run(
        db.enqueue(
            url="https://example.com/post/1",
            normalized_url="https://example.com/post/1",
            platform="other",
            platform_folder="Other",
            chat_id=1,
            user_id=2,
            message_id=3,
        )
    )
    duplicate = run(
        db.enqueue(
            url="https://example.com/post/1?utm_source=x",
            normalized_url="https://example.com/post/1",
            platform="other",
            platform_folder="Other",
            chat_id=1,
            user_id=2,
            message_id=4,
        )
    )
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.job_id == first.job_id

    claimed = run(db.claim_next())
    assert claimed and claimed["id"] == first.job_id
    run(db.fail(first.job_id, "failure"))
    assert run(db.retry(first.job_id, 2)) == "queued"


def test_cancel_queued_job(tmp_path: Path) -> None:
    db = Database(tmp_path / "queue.sqlite3")
    run(db.initialize())
    job = run(
        db.enqueue(
            url="https://example.com/2",
            normalized_url="https://example.com/2",
            platform="other",
            platform_folder="Other",
            chat_id=1,
            user_id=2,
            message_id=None,
        )
    )
    assert run(db.request_cancel(job.job_id, 2)) == "cancelled"
    stored = run(db.get_job(job.job_id, 2))
    assert stored and stored["status"] == "cancelled"
