"""Меню команд Telegram. Админ-команд в меню нет: у администратора только /panel."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault

from .config import cfg

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="my", description="Мои заявки"),
    BotCommand(command="top", description="Рейтинг за сегодня"),
    BotCommand(command="alltime", description="Рейтинг за всё время"),
    BotCommand(command="cancel", description="Отменить действие"),
]
GROUP_COMMANDS = [
    BotCommand(command="top", description="Рейтинг за сегодня"),
    BotCommand(command="alltime", description="Рейтинг за всё время"),
    BotCommand(command="report", description="Пожаловаться (ответом на сообщение)"),
]
ADMIN_COMMANDS = USER_COMMANDS + [BotCommand(command="panel", description="💜 Админ-панель")]


async def setup_commands(bot: Bot) -> list[str]:
    """Устанавливает меню команд. Возвращает список проблем (пустой — всё хорошо)."""
    problems: list[str] = []
    for cmds, scope, name in (
        (USER_COMMANDS, BotCommandScopeDefault(), "для всех"),
        (GROUP_COMMANDS, BotCommandScopeAllGroupChats(), "для групп"),
        (ADMIN_COMMANDS, BotCommandScopeChat(chat_id=cfg.admin_id), "для администратора"),
    ):
        try:
            await bot.set_my_commands(cmds, scope=scope)
        except Exception as e:
            log.warning("Меню команд %s не установлено: %s", name, e)
            problems.append(f"{name}: {e}")
    return problems
