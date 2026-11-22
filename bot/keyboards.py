from __future__ import annotations

import math

from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

from .utils import plain_name

ICON = {"pending": "🟡", "paid": "🟢", "rejected": "🔴"}


def apply_kb() -> M:
    return M(inline_keyboard=[[B(text="💜 Подать заявку на вывод", callback_data="apply")]])


def continue_kb() -> M:
    return M(inline_keyboard=[[B(text="➡️ Продолжить", callback_data="pcont")]])


def confirm_kb() -> M:
    return M(inline_keyboard=[
        [B(text="✅ Подать заявку", callback_data="sub")],
        [B(text="🔄 Ввести данные заново", callback_data="redo")],
    ])


def admin_card_kb(req) -> M:
    rows = []
    if req.status == "pending":
        rows.append([B(text="✅ Выплачено", callback_data=f"pay:{req.id}"),
                     B(text="❌ Отклонить", callback_data=f"rej:{req.id}")])
    rows.append([B(text="💬 Написать пользователю", callback_data=f"msg:{req.user_id}")])
    return M(inline_keyboard=rows)


def amount_confirm_kb(rid: int, units: int) -> M:
    return M(inline_keyboard=[
        [B(text="✅ Подтвердить выплату", callback_data=f"pc:{rid}:{units}")],
        [B(text="🔄 Изменить сумму", callback_data=f"pa:{rid}"), B(text="❌ Отмена", callback_data="acancel")],
    ])


def cancel_kb() -> M:
    return M(inline_keyboard=[[B(text="✖️ Отмена", callback_data="acancel")]])


def reject_kb(rid: int) -> M:
    return M(inline_keyboard=[[B(text="Без причины", callback_data=f"rn:{rid}"),
                               B(text="✖️ Отмена", callback_data="acancel")]])


def relay_kb(username: str | None) -> M:
    rows = []
    if username:
        rows.append([B(text="👤 Открыть чат", url=f"https://t.me/{username}")])
    rows.append([B(text="✖️ Отмена", callback_data="acancel")])
    return M(inline_keyboard=rows)


def reply_kb(uid: int) -> M:
    return M(inline_keyboard=[[B(text="💬 Ответить", callback_data=f"msg:{uid}")]])


HOME = "p:main"


def requests_menu_kb() -> M:
    return M(inline_keyboard=[
        [B(text="🟡 Ожидающие", callback_data="rl:pending:0"), B(text="✅ Выплаченные", callback_data="rl:paid:0")],
        [B(text="❌ Отклонённые", callback_data="rl:rejected:0"), B(text="📋 Все заявки", callback_data="rl:all:0")],
        [B(text="🔎 Поиск заявки", callback_data="rs")],
        [B(text="⬅️ Назад", callback_data=HOME)],
    ])


def list_kb(status: str, page: int, rows, total: int, size: int) -> M:
    kb = [[B(text=f"#{r.id} — "
                  f"{plain_name(r.username, r.full_name, r.user_id)} — {ICON[r.status]}",
             callback_data=f"ro:{r.id}")] for r in rows]
    pages = max(1, math.ceil(total / size))
    nav = []
    if page > 0:
        nav.append(B(text="◀️", callback_data=f"rl:{status}:{page - 1}"))
    nav.append(B(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(B(text="▶️", callback_data=f"rl:{status}:{page + 1}"))
    kb.append(nav)
    kb.append([B(text="⬅️ Назад", callback_data="rq"), B(text="🏠 Главное меню", callback_data=HOME)])
    return M(inline_keyboard=kb)


def search_kb(rows) -> M:
    kb = [[B(text=f"#{r.id} — {plain_name(r.username, r.full_name, r.user_id)} — {ICON[r.status]}",
             callback_data=f"ro:{r.id}")] for r in rows]
    kb.append([B(text="⬅️ Назад", callback_data="rq"), B(text="🏠 Главное меню", callback_data=HOME)])
    return M(inline_keyboard=kb)


def undo_kb(acc_id: int) -> M:
    return M(inline_keyboard=[[B(text="↩️ Отменить начисление", callback_data=f"undo:{acc_id}")]])
