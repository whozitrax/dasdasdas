from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Message, Update, User as TgUser

from . import repo, settings
from .emoji import decorate

log = logging.getLogger(__name__)


class UserTracker(BaseMiddleware):
    """Запоминает username/имя всех, кого видит бот (нужно для /add @username и рейтингов)."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[str | None, str]] = {}

    async def _track(self, u: TgUser | None) -> None:
        if u is None or u.is_bot:
            return
        val = (u.username, u.full_name)
        if self._cache.get(u.id) == val:
            return
        await repo.upsert_user(u.id, u.username, u.full_name)
        self._cache[u.id] = val

    async def __call__(self, handler, event: Update, data):
        await self._track(data.get("event_from_user"))
        msg = event.message
        if msg and msg.reply_to_message:
            await self._track(msg.reply_to_message.from_user)
        return await handler(event, data)


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    """Подменяет обычные эмодзи на премиум во всех исходящих сообщениях (HTML-режим).
    Если Telegram отклонил запрос из-за эмодзи — повторяет без подмены."""

    FIELDS = {SendMessage: "text", EditMessageText: "text", SendPhoto: "caption"}

    async def __call__(self, make_request, bot, method):
        field = self.FIELDS.get(type(method))
        if field and settings.get_bool("premium_emoji"):
            pm = getattr(method, "parse_mode", None)
            html_mode = isinstance(pm, Default) or (isinstance(pm, str) and pm.upper() == "HTML")
            value = getattr(method, field, None)
            if html_mode and isinstance(value, str):
                new = decorate(value)
                if new != value:
                    try:
                        return await make_request(bot, method.model_copy(update={field: new}))
                    except TelegramBadRequest as e:
                        if "emoji" not in str(e).lower() and "entit" not in str(e).lower():
                            raise
                        log.warning("Премиум-эмодзи отклонены Telegram, отправляю обычные: %s", e)
        return await make_request(bot, method)


class AccessibleMessage(BaseMiddleware):
    """В aiogram 3.13+ у callback'а на старом (>48 ч) сообщении message — InaccessibleMessage без методов
    answer()/edit_*(): раньше кнопки на таких сообщениях падали с AttributeError. Подменяем на обычный Message
    с теми же chat/message_id, чтобы кнопки работали."""

    async def __call__(self, handler, event: CallbackQuery, data):
        m = event.message
        if m is not None and not isinstance(m, Message):
            fake = Message(message_id=m.message_id, date=datetime.now(timezone.utc), chat=m.chat).as_(event.bot)
            event = event.model_copy(update={"message": fake})
        return await handler(event, data)
