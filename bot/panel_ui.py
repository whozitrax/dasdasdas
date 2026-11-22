"""Оформление админ-панели: заголовки, навигация, безопасное редактирование сообщений."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as M, Message

log = logging.getLogger(__name__)

LINE = "💜━━━━━━━━━━━━━━💜"
MAIN = "p:main"


def head(title: str, sub: str | None = None) -> str:
    s = f"<b>{title}</b>\n{LINE}"
    if sub:
        s += f"\n<i>{sub}</i>"
    return s + "\n"


def nav(back: str | None = None) -> list[list[B]]:
    """Нижняя строка навигации: «Назад» и «Главное меню». Из корневых разделов «Назад» ведёт в главное меню."""
    if back is None or back == MAIN:
        return [[B(text="⬅️ Назад", callback_data=MAIN)]]
    return [[B(text="⬅️ Назад", callback_data=back), B(text="🏠 Главное меню", callback_data=MAIN)]]


def kb(rows: list[list[B]], back: str | None = None, with_nav: bool = True) -> M:
    rows = [r for r in rows if r]
    if with_nav:
        rows = rows + nav(back)
    return M(inline_keyboard=rows)


def onoff(v: bool) -> str:
    return "🟢 Вкл" if v else "🔴 Выкл"


def paginate(prefix: str, page: int, total: int, size: int) -> list[B]:
    pages = max(1, -(-total // size))
    row = []
    if page > 0:
        row.append(B(text="◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(B(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(B(text="▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


async def edit(cb: CallbackQuery, text: str, markup: M | None) -> None:
    """Редактирует сообщение с кнопкой; если нельзя (старое/удалённое/недоступное) — шлёт новое."""
    msg = cb.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.debug("edit_text не удался (%s), отправляю новое сообщение", e)
    await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup)
