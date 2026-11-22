from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import cards, repo, texts
from .. import keyboards as kb
from ..config import cfg
from ..media import safe_send
from ..panel_ui import edit, head
from ..utils import esc, fmt, mention, parse_amount, req_no

log = logging.getLogger(__name__)
PAGE = 10

router = Router(name="admin")
router.message.filter(F.from_user.id == cfg.admin_id)
router.callback_query.filter(F.from_user.id == cfg.admin_id)


class AdminForm(StatesGroup):
    amount = State()
    reason = State()
    relay = State()
    search = State()


async def _sync_card(msg: Message, rid: int) -> None:
    full = await repo.get_request_full(rid)
    if not full:
        return
    req, user, photos = full
    try:
        await msg.edit_text(cards.admin_card_text(req, user, len(photos)), reply_markup=kb.admin_card_kb(req))
    except TelegramBadRequest:
        pass


async def _strip_kb(msg: Message) -> None:
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "acancel")
async def a_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        await _strip_kb(cb.message)


@router.message(Command("cancel"))
async def a_cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(texts.CANCELLED)


# ============ выплата ============

async def _ask_amount(msg: Message, state: FSMContext, rid: int) -> None:
    await state.set_state(AdminForm.amount)
    await state.update_data(rid=rid)
    await msg.answer(texts.AMOUNT_PROMPT, reply_markup=kb.cancel_kb())


@router.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[1])
    req = await repo.get_request(rid)
    if not req:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if req.status != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        await _sync_card(cb.message, rid)
        return
    await cb.answer()
    await _ask_amount(cb.message, state, rid)


@router.callback_query(F.data.startswith("pa:"))
async def pay_again(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[1])
    req = await repo.get_request(rid)
    if not req or req.status != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        await _strip_kb(cb.message)
        return
    await cb.answer()
    await _strip_kb(cb.message)
    await _ask_amount(cb.message, state, rid)


@router.message(AdminForm.amount, F.text)
async def got_amount(m: Message, state: FSMContext):
    units = parse_amount(m.text)
    if units is None:
        await m.answer(texts.BAD_AMOUNT, reply_markup=kb.cancel_kb())
        return
    rid = (await state.get_data()).get("rid")
    full = await repo.get_request_full(rid) if rid else None
    if not full:
        await state.clear()
        await m.answer("Заявка не найдена.")
        return
    req, user, _ = full
    if req.status != "pending":
        await state.clear()
        await m.answer("Заявка уже обработана — повторная выплата невозможна.")
        return
    await state.clear()  # подтверждение «stateless»: сумма зашита в callback-кнопку
    await m.answer(
        "<b>✅ ПОДТВЕРЖДЕНИЕ ВЫПЛАТЫ</b>\n\n"
        f"👤 Пользователь: {mention(user.username, user.full_name, req.user_id)}\n"
        f"🎁 Подарок: {esc(req.gift_link)}\n"
        f"💎 Выплачено: <b>{fmt(units)} GRAM</b>",
        reply_markup=kb.amount_confirm_kb(rid, units))


@router.callback_query(F.data.startswith("pc:"))
async def confirm_pay(cb: CallbackQuery):
    _, rid_s, units_s = cb.data.split(":")
    rid, units = int(rid_s), int(units_s)
    res = await repo.mark_paid(rid, units, cb.from_user.id)  # атомарно, повтор невозможен
    if res != "ok":
        await cb.answer("Заявка уже обработана — дубль выплаты не создан" if res == "not_pending"
                        else "Заявка не найдена", show_alert=True)
        await _strip_kb(cb.message)
        return
    await cb.answer("Выплата подтверждена")
    full = await repo.get_request_full(rid)
    req, user, _ = full
    try:
        await cb.message.edit_text((cb.message.html_text or "") + "\n\n🟢 <b>Выплата записана</b>", reply_markup=None)
    except TelegramBadRequest:
        pass

    warnings = []
    notified = await safe_send(
        cb.bot, req.user_id,
        "<b>💎 ВЫПЛАТА ВЫПОЛНЕНА</b> 🎉\n\n"
        f"Ваша заявка {req_no(rid)} обработана.\n"
        f"💰 Выплачено: <b>{fmt(units)} GRAM</b>\n"
        f"🎁 Подарок: {esc(req.gift_link)}")
    if not notified:
        warnings.append("не удалось уведомить пользователя (возможно, он заблокировал бота)")
    err = await cards.post_payout_report(cb.bot, req, user, units)
    if err:
        warnings.append(f"отчёт в группу не опубликован: {esc(err)}")
    await cards.refresh_stored_card(cb.bot, rid)
    if warnings:
        await cb.message.answer("⚠️ Выплата записана, но:\n• " + "\n• ".join(warnings))


# ============ отклонение ============

@router.callback_query(F.data.startswith("rej:"))
async def reject(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[1])
    req = await repo.get_request(rid)
    if not req or req.status != "pending":
        await cb.answer("Заявка уже обработана", show_alert=True)
        if req:
            await _sync_card(cb.message, rid)
        return
    await cb.answer()
    await state.set_state(AdminForm.reason)
    await state.update_data(rid=rid)
    await cb.message.answer(f"<b>❌ ОТКЛОНЕНИЕ {req_no(rid)}</b>\n\nНапишите причину — она будет отправлена пользователю.",
                            reply_markup=kb.reject_kb(rid))


async def _do_reject(bot, msg: Message, rid: int, reason: str | None) -> None:
    res = await repo.mark_rejected(rid, reason, cfg.admin_id)
    if res != "ok":
        await msg.answer("Заявка уже обработана.")
        return
    req = (await repo.get_request_full(rid))[0]
    text = f"❌ <b>Заявка {req_no(rid)} отклонена.</b>"
    if reason:
        text += f"\n\n📝 Причина: {esc(reason)}"
    text += "\n\nЕсли есть вопросы — напишите сюда, сообщение дойдёт до администратора."
    ok = await safe_send(bot, req.user_id, text)
    await msg.answer(f"❌ Заявка {req_no(rid)} отклонена" + ("" if ok else " (пользователя уведомить не удалось)"))
    await cards.refresh_stored_card(bot, rid)


@router.message(AdminForm.reason, F.text)
async def got_reason(m: Message, state: FSMContext):
    rid = (await state.get_data()).get("rid")
    await state.clear()
    if rid:
        await _do_reject(m.bot, m, rid, m.text.strip()[:500])


@router.callback_query(F.data.startswith("rn:"))
async def reject_no_reason(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _strip_kb(cb.message)
    await _do_reject(cb.bot, cb.message, int(cb.data.split(":")[1]), None)


# ============ написать пользователю ============

@router.callback_query(F.data.startswith("msg:"))
async def relay_start(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[1])
    user = await repo.get_user(uid)
    await cb.answer()
    await state.set_state(AdminForm.relay)
    await state.update_data(uid=uid)
    who = mention(user.username if user else None, user.full_name if user else None, uid)
    await cb.message.answer(f"💬 Напишите сообщение для {who} — я перешлю его от имени бота.",
                            reply_markup=kb.relay_kb(user.username if user else None))


@router.message(AdminForm.relay, F.text)
async def relay_send(m: Message, state: FSMContext):
    uid = (await state.get_data()).get("uid")
    await state.clear()
    ok = await safe_send(m.bot, uid, f"💬 <b>Сообщение от администратора</b>\n\n{esc(m.text)}")
    await m.answer("✅ Отправлено." if ok else "⚠️ Не удалось доставить (пользователь заблокировал бота?).")


# ============ список заявок ============

async def _menu_text() -> str:
    c = await repo.status_counts()
    return (head("📩 ЗАЯВКИ", "Просмотр и обработка заявок на вывод")
            + f"\n🟡 На проверке: <b>{c['pending']}</b>\n🟢 Выплачено: <b>{c['paid']}</b>\n🔴 Отклонено: <b>{c['rejected']}</b>")


@router.callback_query(F.data == "rq")
async def back_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await edit(cb, await _menu_text(), kb.requests_menu_kb())


TITLES = {"pending": "🟡 Ожидающие", "paid": "✅ Выплаченные", "rejected": "❌ Отклонённые", "all": "📋 Все заявки"}


@router.callback_query(F.data.startswith("rl:"))
async def list_cb(cb: CallbackQuery):
    _, status, page_s = cb.data.split(":")
    if status not in TITLES:
        await cb.answer()
        return
    page = max(0, int(page_s))
    total, rows = await repo.list_requests(status, page, PAGE)
    await cb.answer()
    text = head(TITLES[status], f"Всего: {total}") + ("" if rows else "\nПусто.")
    await edit(cb, text, kb.list_kb(status, page, rows, total, PAGE))


@router.callback_query(F.data.startswith("ro:"))
async def open_request(cb: CallbackQuery):
    rid = int(cb.data.split(":")[1])
    await cb.answer()
    if not await repo.get_request(rid):
        await cb.message.answer("Заявка не найдена.")
        return
    await cards.send_admin_card(cb.bot, rid)


@router.callback_query(F.data == "rs")
async def search_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(AdminForm.search)
    await cb.message.answer(head("🔎 ПОИСК ЗАЯВКИ") + "\nВведите номер заявки, @username, Telegram ID, "
                            "часть ссылки на подарок или адрес кошелька.", reply_markup=kb.cancel_kb())


@router.message(AdminForm.search, F.text)
async def search_do(m: Message, state: FSMContext):
    await state.clear()
    rows = await repo.search_requests(m.text)
    if not rows:
        await m.answer("Ничего не найдено.", reply_markup=kb.search_kb([]))
        return
    await m.answer(f"🔎 Найдено: {len(rows)}", reply_markup=kb.search_kb(rows))


# ============ утилита: ID кастомных эмодзи ============
# Должен идти последним: срабатывает, только если админ прислал сообщение с премиум-эмодзи.

@router.message(F.chat.type == "private",
                F.entities.func(lambda ents: any(e.type == "custom_emoji" for e in ents)))
async def emoji_ids(m: Message):
    ents = [e for e in m.entities if e.type == "custom_emoji"]
    lines = [f"{m.text[e.offset:e.offset + e.length]} <code>{e.custom_emoji_id}</code>" for e in ents]
    await m.answer("🆔 ID эмодзи:\n" + "\n".join(lines))
