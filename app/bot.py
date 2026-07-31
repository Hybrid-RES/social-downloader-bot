from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from .database import ACTIVE_STATUSES, Database
from .platforms import detect_platform, normalize_url
from .settings import Settings
from .urls import extract_urls
from .worker import human_bytes

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def restricted(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or user.id not in _settings(context).allowed_user_ids:
            if message:
                await message.reply_text("⛔ Доступ запрещён")
            return
        await handler(update, context)

    return wrapped


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    await update.effective_message.reply_text(
        "Отправьте одну или несколько ссылок на YouTube, Instagram, X/Twitter, TikTok, "
        "Facebook, Threads, LinkedIn, Pinterest или Tumblr. Бот добавит их в очередь, "
        "сохранит медиа на сервере и при включённой отправке вернёт небольшие файлы в этот чат.\n\n"
        "Команды: /status /queue /history /failed /retry ID /cancel ID "
        "/files_on /files_off /files_status /version"
    )


@restricted
async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    text = message.text or message.caption or ""
    urls = extract_urls(text)
    if not urls:
        await message.reply_text("Ссылка не найдена. Отправьте сообщение, содержащее http:// или https:// URL.")
        return

    database = _database(context)
    replies: list[str] = []
    for raw_url in urls:
        normalized = normalize_url(raw_url)
        platform = detect_platform(normalized)
        result = await database.enqueue(
            url=raw_url,
            normalized_url=normalized,
            platform=platform.key,
            platform_folder=platform.folder,
            chat_id=chat.id,
            user_id=user.id,
            message_id=message.message_id,
        )
        if result.created:
            replies.append(f"📥 №{result.job_id} — {platform.folder}: добавлено в очередь")
        elif result.status == "completed":
            suffix = f" ({result.output_dir})" if result.output_dir else ""
            replies.append(f"⚠️ №{result.job_id} — уже скачано{suffix}")
        else:
            replies.append(f"⚠️ №{result.job_id} — уже находится в состоянии {result.status}")

    await message.reply_text("\n".join(replies), disable_web_page_preview=True)


def _format_job(job: dict) -> str:
    return f"№{job['id']} [{job['status']}] {job['platform_folder']} — {job['url'][:120]}"


async def _file_delivery_enabled(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> bool:
    settings = _settings(context)
    if not settings.telegram_send_files:
        return False
    return await _database(context).get_chat_send_files(chat_id, default=True)


@restricted
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user and update.effective_chat
    counts = await _database(context).counts(update.effective_user.id)
    usage = shutil.disk_usage(_settings(context).download_root)
    delivery = await _file_delivery_enabled(context, update.effective_chat.id)
    lines = [
        "📊 Состояние загрузчика",
        f"В очереди: {counts.get('queued', 0)}",
        f"В работе: {counts.get('downloading', 0) + counts.get('postprocessing', 0)}",
        f"Завершено: {counts.get('completed', 0)}",
        f"Ошибки: {counts.get('failed', 0)}",
        f"Отправка файлов в чат: {'включена' if delivery else 'выключена'}",
        f"Свободно на диске: {human_bytes(usage.free)} из {human_bytes(usage.total)}",
    ]
    await update.effective_message.reply_text("\n".join(lines))


@restricted
async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    jobs = await _database(context).list_jobs(
        user_id=update.effective_user.id, statuses=ACTIVE_STATUSES, limit=20
    )
    await update.effective_message.reply_text(
        "\n".join(_format_job(job) for job in jobs) if jobs else "Очередь пуста"
    )


@restricted
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    jobs = await _database(context).list_jobs(
        user_id=update.effective_user.id,
        statuses=("completed",),
        limit=_settings(context).history_limit,
    )
    await update.effective_message.reply_text(
        "\n".join(
            f"№{job['id']} ✅ {job['platform_folder']} — {job['files_count']} файл(ов), "
            f"{human_bytes(job['total_bytes'])}, {job['output_dir']}"
            for job in jobs
        )
        if jobs
        else "История завершённых загрузок пуста"
    )


@restricted
async def failed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    jobs = await _database(context).list_jobs(
        user_id=update.effective_user.id,
        statuses=("failed",),
        limit=_settings(context).history_limit,
    )
    await update.effective_message.reply_text(
        "\n\n".join(
            f"№{job['id']} ❌ {job['platform_folder']}\n{(job['error'] or 'Без описания')[:500]}"
            for job in jobs
        )
        if jobs
        else "Неудачных заданий нет"
    )


def _parse_job_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


@restricted
async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    job_id = _parse_job_id(context)
    if job_id is None:
        await update.effective_message.reply_text("Использование: /retry 12")
        return
    result = await _database(context).retry(job_id, update.effective_user.id)
    if result is None:
        text = "Задание не найдено"
    elif result == "queued":
        text = f"🔁 Задание №{job_id} снова добавлено в очередь"
    else:
        text = f"Задание №{job_id} нельзя повторить: текущее состояние {result}"
    await update.effective_message.reply_text(text)


@restricted
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_user
    job_id = _parse_job_id(context)
    if job_id is None:
        await update.effective_message.reply_text("Использование: /cancel 12")
        return
    result = await _database(context).request_cancel(job_id, update.effective_user.id)
    mapping = {
        None: "Задание не найдено",
        "cancelled": f"🚫 Задание №{job_id} отменено",
        "requested": f"🚫 Для задания №{job_id} запрошена остановка",
    }
    await update.effective_message.reply_text(
        mapping.get(result, f"Задание №{job_id} уже находится в состоянии {result}")
    )


@restricted
async def files_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_chat
    await _database(context).set_chat_send_files(update.effective_chat.id, True)
    settings = _settings(context)
    if settings.telegram_send_files:
        text = (
            "📎 Автоматическая отправка файлов в этот чат включена.\n"
            f"Будут отправляться файлы размером до {settings.telegram_max_upload_mb} MB."
        )
    else:
        text = (
            "📎 Настройка чата включена, но глобальная отправка отключена переменной "
            "TELEGRAM_SEND_FILES=false."
        )
    await update.effective_message.reply_text(text)


@restricted
async def files_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_chat
    await _database(context).set_chat_send_files(update.effective_chat.id, False)
    await update.effective_message.reply_text(
        "📁 Автоматическая отправка файлов в этот чат выключена. "
        "Скачивания продолжат сохраняться на сервере."
    )


@restricted
async def files_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message and update.effective_chat
    settings = _settings(context)
    chat_enabled = await _database(context).get_chat_send_files(
        update.effective_chat.id,
        default=True,
    )
    effective = settings.telegram_send_files and chat_enabled
    await update.effective_message.reply_text(
        "\n".join(
            [
                "📎 Отправка скачанных файлов",
                f"Глобальная настройка: {'включена' if settings.telegram_send_files else 'выключена'}",
                f"Настройка этого чата: {'включена' if chat_enabled else 'выключена'}",
                f"Фактическое состояние: {'включено' if effective else 'выключено'}",
                f"Максимальный размер: {settings.telegram_max_upload_mb} MB",
            ]
        )
    )


def _command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0] if output else f"exit {result.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"недоступно: {exc}"


@restricted
async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message
    settings = _settings(context)
    versions = await asyncio.gather(
        asyncio.to_thread(_command_version, ["yt-dlp", "--version"]),
        asyncio.to_thread(_command_version, ["gallery-dl", "--version"]),
        asyncio.to_thread(_command_version, ["ffmpeg", "-version"]),
        asyncio.to_thread(_command_version, ["deno", "--version"]),
    )
    await update.effective_message.reply_text(
        "\n".join(
            [
                f"Приложение: {settings.app_version}",
                f"yt-dlp: {versions[0]}",
                f"gallery-dl: {versions[1]}",
                f"ffmpeg: {versions[2]}",
                f"Deno: {versions[3]}",
            ]
        )
    )
