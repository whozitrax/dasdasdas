from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .. import repo
from .. import keyboards as kb
from ..config import cfg
from ..media import say
from ..utils import esc, fmt, mention, parse_amount, today_str

public = Router(name="public_stats")  # /top, /alltime — доступны всем

adm = Router(name="admin_stats")
adm.message.filter(F.from_user.id == cfg.admin_id)
adm.callback_query.filter(F.from_user.id == cfg.admin_id)

MEDALS = ["🥇", "🥈", "🥉"]


# ============ рейтинги ============

def render_ranking(rows, title: str, total_label: str, empty: str, rest_header: str | None) -> str:
    if not rows:
        return f"<b>{title}</b>\n\n{empty}"
    lines = [f"<b>{title}</b>", ""]
    for i, (uid, uname, fname, t) in enumerate(rows[:3]):
        lines += [f"{MEDALS[i]} {mention(uname, fname, uid)} — {fmt(t)} GRAM", ""]
    rest = rows[3:]
    if rest:
        if rest_header:
            lines += [f"<b>{rest_header}</b>", ""]
        budget = 3300
        shown = 0
        for i, (uid, uname, fname, t) in enumerate(rest, start=4):
            line = f"{i}. {mention(uname, fname, uid)} — {fmt(t)} GRAM"
            budget -= len(line)
            if budget < 0:
                lines.append(f"… и ещё {len(rest) - shown}")
                break
            lines.append(line)
            shown += 1
        lines.append("")
    lines.append(f"<b>{total_label}: {fmt(sum(r[3] for r in rows))} GRAM</b>")
    return "\n".join(lines)


@public.message(Command("top"))
async def cmd_top(m: Message):
    rows = await repo.ranking(today_str(cfg.timezone))
    await say(m, render_ranking(rows, "🏆 РЕЙТИНГ ЗА СЕГОДНЯ", "💎 Всего заработано",
                                "Сегодня начислений пока нет.", "📊 Остальные участники"))


@public.message(Command("alltime"))
async def cmd_alltime(m: Message):
    rows = await repo.ranking(None)
    await say(m, render_ranking(rows, "💎 ОБЩИЙ РЕЙТИНГ", "💰 Общий заработок",
                                "Начислений пока нет.", None))


# ============ начисления ============

USAGE = ("Использование:\n<code>/add @username 3.5</code>\n<code>/add 123456789 3.5</code>\n"
         "или ответьте на сообщение участника: <code>/add 3.5</code>")


async def _resolve_target(m: Message, tokens: list[str]) -> tuple[int | None, str | None]:
    for e in m.entities or []:
        if e.type == "text_mention" and e.user and not e.user.is_bot:
            await repo.upsert_user(e.user.id, e.user.username, e.user.full_name)
            return e.user.id, None
    if len(tokens) >= 2:
        t = tokens[0]
        if t.startswith("@"):
            u = await repo.find_user_by_username(t)
            if u:
                return u.tg_id, None
            return None, (f"⚠️ Пользователь {esc(t)} мне пока не встречался. Пусть напишет в группе/боту "
                          "или ответьте командой на его сообщение.")
        if t.isdigit() and len(t) <= 18:
            await repo.ensure_user(int(t))
            return int(t), None
        return None, USAGE
    rp = m.reply_to_message
    if rp and rp.from_user and not rp.from_user.is_bot:
        # в темах «ответом» может оказаться служебное сообщение создания темы — не засчитываем
        if rp.forum_topic_created is None and not (m.is_topic_message and rp.message_id == m.message_thread_id):
            return rp.from_user.id, None
    return None, USAGE


async def _accrue(m: Message, command: CommandObject, kind: str) -> None:
    tokens = (command.args or "").split()
    if not tokens:
        await say(m, USAGE)
        return
    amount = parse_amount(tokens[-1])
    if amount is None:
        await say(m, "⚠️ Сумма указана неверно.\n\n" + USAGE)
        return
    uid, err = await _resolve_target(m, tokens)
    if uid is None:
        await say(m, err)
        return
    day = today_str(cfg.timezone)
    if kind == "sub":
        _, total = await repo.user_sums(uid, day)
        if total < amount:
            await say(m, f"⚠️ Нельзя списать больше, чем заработано всего ({fmt(total)} GRAM).")
            return
    acc = await repo.add_accrual(uid, amount, kind, m.from_user.id, day, m.chat.id, m.message_id)
    if acc is None:
        await say(m, "⚠️ Эта команда уже была обработана.")
        return
    user = await repo.get_user(uid)
    today, total = await repo.user_sums(uid, day)
    title = "✅ НАЧИСЛЕНИЕ ВЫПОЛНЕНО" if kind == "add" else "➖ СПИСАНИЕ ВЫПОЛНЕНО (коррекция)"
    sign = "+" if kind == "add" else "−"
    label = "Начислено" if kind == "add" else "Списано"
    await say(m,
              f"<b>{title}</b>\n\n"
              f"👤 {mention(user.username, user.full_name, uid)}\n\n"
              f"💎 {label}: {sign}{fmt(amount)} GRAM\n"
              f"📊 Заработано сегодня: {fmt(today)} GRAM\n"
              f"🏆 Заработано всего: {fmt(total)} GRAM\n\n"
              f"🧾 Операция №{acc.id}",
              reply_markup=kb.undo_kb(acc.id))


@adm.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    await _accrue(m, command, "add")


@adm.message(Command("sub"))
async def cmd_sub(m: Message, command: CommandObject):
    await _accrue(m, command, "sub")


async def _undo(acc_id: int, admin_id: int) -> tuple[str, str | None]:
    status, a = await repo.undo_accrual(acc_id, admin_id)
    if status == "not_found":
        return "⚠️ Операция не найдена.", None
    if status == "is_revert":
        return "⚠️ Это уже отмена — отменить её нельзя. Сделайте новое начисление.", None
    if status == "already":
        return "⚠️ Эта операция уже отменена.", None
    today, total = await repo.user_sums(a.user_id, today_str(cfg.timezone))
    user = await repo.get_user(a.user_id)
    return (f"↩️ Операция №{acc_id} отменена ({mention(user.username, user.full_name, a.user_id)}, "
            f"{'+' if a.amount > 0 else '−'}{fmt(abs(a.amount))} GRAM).\n"
            f"📊 Сегодня: {fmt(today)} GRAM · 🏆 Всего: {fmt(total)} GRAM"), "ok"


@adm.message(Command("undo"))
async def cmd_undo(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#№")
    if not arg.isdigit():
        await say(m, "Использование: <code>/undo НОМЕР_ОПЕРАЦИИ</code>")
        return
    text, _ = await _undo(int(arg), m.from_user.id)
    await say(m, text)


@adm.callback_query(F.data.startswith("undo:"))
async def cb_undo(cb: CallbackQuery):
    text, ok = await _undo(int(cb.data.split(":")[1]), cb.from_user.id)
    await cb.answer("Отменено" if ok else text.replace("⚠️ ", ""), show_alert=not ok)
    if ok:
        try:
            await cb.message.edit_text((cb.message.html_text or "") + "\n\n" + text, reply_markup=None)
        except TelegramBadRequest:
            pass
    else:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
