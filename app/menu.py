from __future__ import annotations

import logging

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import bot as handlers

LOGGER = logging.getLogger(__name__)

BOT_COMMANDS = (
    BotCommand("menu", "открыть меню с кнопками"),
    BotCommand("status", "состояние загрузчика"),
    BotCommand("queue", "очередь и выполняемые задания"),
    BotCommand("history", "последние успешные загрузки"),
    BotCommand("failed", "последние ошибки"),
    BotCommand("files_status", "состояние отправки файлов"),
    BotCommand("files_on", "включить отправку файлов в чат"),
    BotCommand("files_off", "выключить отправку файлов в чат"),
    BotCommand("retry", "повторить задание: /retry ID"),
    BotCommand("cancel", "отменить задание: /cancel ID"),
    BotCommand("version", "версии приложения и загрузчиков"),
    BotCommand("help", "краткая инструкция"),
)


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Состояние", callback_data="menu:status"),
                InlineKeyboardButton("⏳ Очередь", callback_data="menu:queue"),
            ],
            [
                InlineKeyboardButton("✅ История", callback_data="menu:history"),
                InlineKeyboardButton("❌ Ошибки", callback_data="menu:failed"),
            ],
            [
                InlineKeyboardButton(
                    "📎 Состояние отправки файлов",
                    callback_data="menu:files_status",
                )
            ],
            [
                InlineKeyboardButton("🔔 Включить файлы", callback_data="menu:files_on"),
                InlineKeyboardButton("🔕 Выключить файлы", callback_data="menu:files_off"),
            ],
            [
                InlineKeyboardButton("ℹ️ Версии", callback_data="menu:version"),
                InlineKeyboardButton("❓ Помощь", callback_data="menu:help"),
            ],
            [InlineKeyboardButton("✖️ Закрыть меню", callback_data="menu:close")],
        ]
    )


async def register_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:  # noqa: BLE001 - menu registration must not stop the bot
        LOGGER.exception("Unable to register Telegram command menu")


@handlers.restricted
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Выберите действие. Ссылки для скачивания можно отправлять обычным сообщением.",
        reply_markup=menu_markup(),
    )


@handlers.restricted
async def handle_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    action = str(query.data or "")
    if action == "menu:close":
        await query.answer()
        try:
            await query.delete_message()
        except Exception:  # noqa: BLE001 - stale menu is not a fatal error
            LOGGER.debug("Unable to delete Telegram menu message", exc_info=True)
        return

    callbacks = {
        "menu:status": handlers.status,
        "menu:queue": handlers.queue,
        "menu:history": handlers.history,
        "menu:failed": handlers.failed,
        "menu:files_status": handlers.files_status,
        "menu:files_on": handlers.files_on,
        "menu:files_off": handlers.files_off,
        "menu:version": handlers.version,
        "menu:help": handlers.start,
    }
    callback = callbacks.get(action)
    if callback is None:
        await query.answer("Неизвестный пункт меню", show_alert=True)
        return

    await query.answer()
    await callback(update, context)
