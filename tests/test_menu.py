from __future__ import annotations

import asyncio

from app.menu import BOT_COMMANDS, menu_markup, register_commands


def test_command_menu_contains_common_actions() -> None:
    names = [command.command for command in BOT_COMMANDS]

    assert names[0] == "menu"
    assert "status" in names
    assert "queue" in names
    assert "history" in names
    assert "files_on" in names
    assert "files_off" in names
    assert "files_status" in names
    assert "retry" in names
    assert "cancel" in names


def test_inline_menu_contains_file_delivery_controls() -> None:
    markup = menu_markup()
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "menu:status" in callbacks
    assert "menu:queue" in callbacks
    assert "menu:history" in callbacks
    assert "menu:failed" in callbacks
    assert "menu:files_status" in callbacks
    assert "menu:files_on" in callbacks
    assert "menu:files_off" in callbacks
    assert "menu:version" in callbacks
    assert "menu:help" in callbacks
    assert "menu:close" in callbacks


def test_register_commands_uses_telegram_bot_menu() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.commands = None

        async def set_my_commands(self, commands) -> None:
            self.commands = commands

    bot = FakeBot()
    asyncio.run(register_commands(bot))

    assert bot.commands == BOT_COMMANDS
