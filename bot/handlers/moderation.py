"""Модерация групп: антиспам, ссылки, блокировка, жалобы.

Работает как outer-middleware, а не как обычный обработчик: проверка выполняется ДО остальных обработчиков,
не «съедает» сообщение (команды /add, /top и т.д. продолжают работать) и обёрнута в try/except —
сбой модерации никогда не ломает остальной функционал бота.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton as B, InlineKeyboardMarkup as M, Message

from .. import repo, settings
from ..config import cfg
from ..media import safe_send, say
from ..utils import esc, mention

log = logging.getLogger(__name__)

FLOOD_WINDOW = 10.0   # секунд
GROUPS = ("group", "supergroup")

router = Router(name="moderation")


# ---------- вспомогательное ----------

async def mute(bot: Bot, chat_id: int, user_id: int, minutes: int) -> None:
    from datetime import timedelta
    await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False),
                                   until_date=timedelta(minutes=max(1, minutes)))


def _schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: float = 8.0) -> None:
    async def _job():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    asyncio.create_task(_job())


async def _notice(m: Message, text: str) -> None:
    try:
        sent = await say(m, text)
        _schedule_delete(m.bot, sent.chat.id, sent.message_id)
    except Exception as e:
        log.debug("Не удалось отправить уведомление модерации: %s", e)


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def norm_link(s: str) -> str:
    s = _URL_SCHEME.sub("", s.strip().lower())
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip("/")


def extract_links(m: Message) -> list[str]:
    out: list[str] = []
    for text, ents in ((m.text, m.entities), (m.caption, m.caption_entities)):
        for e in ents or []:
            if e.type == "url" and text:
                out.append(e.extract_from(text))
            elif e.type == "text_link" and e.url:
                out.append(e.url)
    return out


def link_allowed(link: str, whitelist: list[str]) -> bool:
    n = norm_link(link)
    for w in whitelist:
        w = norm_link(w)
        if w and (n == w or n.startswith(w + "/") or n.startswith(w + "?")):
            return True
    return False


# ---------- middleware ----------

class ModerationMiddleware(BaseMiddleware):
    def __init__(self, edited: bool = False) -> None:
        self.edited = edited
        self._flood: dict[tuple[int, int], deque] = defaultdict(deque)
        self._admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
        self._muted: dict[tuple[int, int], float] = {}

    async def _is_chat_admin(self, bot: Bot, chat_id: int, uid: int) -> bool:
        key = (chat_id, uid)
        hit = self._admin_cache.get(key)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        try:
            mem = await bot.get_chat_member(chat_id, uid)
            res = mem.status in ("administrator", "creator")
        except TelegramAPIError:
            res = False
        self._admin_cache[key] = (res, now + 120)
        return res

    async def __call__(self, handler, event: Message, data):
        try:
            if await self._check(event):
                return None  # сообщение удалено модерацией — дальше не передаём
        except Exception:
            log.exception("Модерация: сбой проверки (остальные обработчики продолжают работать)")
        return await handler(event, data)

    async def _delete(self, m: Message) -> bool:
        try:
            await m.delete()
            return True
        except TelegramAPIError as e:
            log.warning("Модерация: не удалось удалить сообщение в чате %s: %s "
                        "(у бота нет права удалять сообщения?)", m.chat.id, e)
            return False

    async def _check(self, m: Message) -> bool:
        if m.chat.type not in GROUPS:
            return False
        await settings.remember_chat(m.chat.id, m.chat.title or str(m.chat.id))
        u = m.from_user
        if u is None or u.is_bot or m.sender_chat is not None or u.id == cfg.admin_id:
            return False

        # заблокированные администратором — сообщения удаляются
        if u.id in settings.BANNED:
            return await self._delete(m)

        links_mode = settings.get("links_mode")
        antispam = settings.get_bool("antispam") and not self.edited
        has_links = links_mode != "allow"
        if not antispam and not has_links:
            return False
        if await self._is_chat_admin(m.bot, m.chat.id, u.id):
            return False

        if antispam and await self._flooding(m):
            return True
        if has_links:
            bad = [l for l in extract_links(m) if not link_allowed(l, settings.whitelist())]
            if bad:
                ok = await self._delete(m)
                if ok and links_mode == "notify":
                    await _notice(m, f"⚠️ {mention(u.username, u.full_name, u.id)}, ссылки в этом чате запрещены.")
                return ok
        return False

    async def _flooding(self, m: Message) -> bool:
        u = m.from_user
        key = (m.chat.id, u.id)
        now = time.monotonic()
        q = self._flood[key]
        q.append(now)
        while q and now - q[0] > FLOOD_WINDOW:
            q.popleft()
        if len(q) <= settings.get_int("antispam_limit"):
            return False
        await self._delete(m)
        if settings.get("antispam_action") == "mute" and self._muted.get(key, 0) < now:
            minutes = settings.get_int("mute_minutes")
            try:
                await mute(m.bot, m.chat.id, u.id, minutes)
                self._muted[key] = now + minutes * 60
                await _notice(m, f"🔇 {mention(u.username, u.full_name, u.id)} заглушён на {minutes} мин. за спам.")
            except TelegramAPIError as e:
                log.warning("Антиспам: не удалось ограничить пользователя %s в чате %s: %s", u.id, m.chat.id, e)
        return True


class BanGuard(BaseMiddleware):
    """Заблокированные администратором не могут пользоваться ботом в личке (в группах их сообщения удаляет модерация)."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is not None and u.id != cfg.admin_id and u.id in settings.BANNED:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("🚫 Доступ к боту ограничен администратором.", show_alert=True)
                except TelegramAPIError:
                    pass
                return None
            if isinstance(event, Message) and event.chat.type == "private":
                try:
                    await event.answer("🚫 Доступ к боту ограничен администратором.")
                except TelegramAPIError:
                    pass
                return None
        return await handler(event, data)


# ---------- жалобы ----------

@router.message(Command("report"), F.chat.type.in_(GROUPS))
async def cmd_report(m: Message):
    """/report — ответом на сообщение: жалоба уходит администратору в панель."""
    target = m.reply_to_message
    try:
        await m.delete()
    except TelegramAPIError:
        pass
    if target is None or target.from_user is None or target.from_user.is_bot:
        await _notice(m, "ℹ️ Чтобы пожаловаться, ответьте командой /report на сообщение участника.")
        return
    excerpt = (target.text or target.caption or "").strip() or "[сообщение без текста]"
    c = await repo.add_complaint(m.chat.id, m.chat.title or "", target.message_id, m.from_user.id,
                                 target.from_user.id, excerpt)
    if c is None:
        await _notice(m, "ℹ️ Жалоба на это сообщение уже отправлена.")
        return
    await _notice(m, "🚩 Жалоба отправлена администратору.")
    await safe_send(m.bot, cfg.admin_id, f"🚩 <b>Новая жалоба #{c.id}</b>\nЧат: {esc(m.chat.title or str(m.chat.id))}",
                    reply_markup=M(inline_keyboard=[[B(text="📋 Открыть", callback_data=f"p:cp:v:{c.id}")]]))
