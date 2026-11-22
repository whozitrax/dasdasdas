from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, Message

log = logging.getLogger(__name__)
_file_ids: dict[str, str] = {}


async def send_image(bot: Bot, chat_id: int, path: Path, caption: str, *, thread_id: int | None = None,
                     reply_markup=None) -> Message:
    """Отправляет картинку с подписью (file_id кэшируется). Если файла нет — просто текст."""
    if not path.exists():
        log.warning("Файл %s не найден, отправляю текст", path)
        return await bot.send_message(chat_id, caption, message_thread_id=thread_id, reply_markup=reply_markup)
    key = str(path)
    msg = await bot.send_photo(chat_id, _file_ids.get(key) or FSInputFile(path), caption=caption,
                               message_thread_id=thread_id, reply_markup=reply_markup)
    if msg.photo:
        _file_ids[key] = msg.photo[-1].file_id
    return msg


async def safe_send(bot: Bot, chat_id: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(chat_id, text, **kw)
        return True
    except Exception as e:  # пользователь заблокировал бота и т.п.
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


async def say(m: Message, text: str, **kw) -> Message:
    """Ответ в тот же чат и ту же тему (для групп с темами)."""
    return await m.bot.send_message(m.chat.id, text,
                                    message_thread_id=m.message_thread_id if m.is_topic_message else None, **kw)
