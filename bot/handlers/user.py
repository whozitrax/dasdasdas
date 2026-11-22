from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import cards, repo, texts
from .. import keyboards as kb
from .. import settings
from ..config import cfg
from ..media import safe_send, send_image
from ..utils import esc, fmt, gift_key, mention, normalize_gift_link, req_no, valid_wallet

log = logging.getLogger(__name__)

MIN_PHOTOS, MAX_PHOTOS = 3, 10

router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class Form(StatesGroup):
    gift = State()
    photos = State()
    wallet = State()
    confirm = State()


_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_counter_tasks: dict[int, asyncio.Task] = {}
_counter_msgs: dict[int, int] = {}


# ---------- базовые команды ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await send_image(m.bot, m.chat.id, cfg.welcome_image, texts.WELCOME, reply_markup=kb.apply_kb())


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(texts.CANCELLED, reply_markup=kb.apply_kb())


@router.message(Command("my"))
async def cmd_my(m: Message):
    rows = await repo.user_requests(m.from_user.id, 10)
    if not rows:
        await m.answer("У вас пока нет заявок.", reply_markup=kb.apply_kb())
        return
    lines = ["<b>📂 ВАШИ ЗАЯВКИ</b>", ""]
    for r in rows:
        extra = f" — {fmt(r.paid_amount)} GRAM" if r.status == "paid" else ""
        lines.append(f"{kb.ICON[r.status]} {req_no(r.id)} — {cards.STATUS_LABEL[r.status][2:]}{extra}")
    await m.answer("\n".join(lines))


# ---------- шаг 1: ссылка ----------

@router.callback_query(F.data == "apply")
async def apply(cb: CallbackQuery, state: FSMContext):
    if not settings.get_bool("requests_open"):
        await cb.answer("⏸ Приём заявок временно приостановлен. Попробуйте позже.", show_alert=True)
        return
    if await repo.count_pending(cb.from_user.id) >= settings.get_int("max_pending"):
        await cb.answer("У вас слишком много заявок на проверке. Дождитесь ответа администратора.", show_alert=True)
        return
    await cb.answer()
    await _start_form(cb.message, state)


async def _start_form(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Form.gift)
    await state.update_data(photos=[])
    await msg.answer(texts.STEP_GIFT)


@router.message(Form.gift, F.text)
async def step_gift(m: Message, state: FSMContext):
    link = normalize_gift_link(m.text)
    if not link:
        await m.answer(texts.BAD_GIFT)
        return
    key = gift_key(link)
    if await repo.gift_taken(key):
        await m.answer(texts.GIFT_TAKEN)
        return
    await state.update_data(gift=link, gift_key=key)
    await state.set_state(Form.photos)
    await m.answer(texts.STEP_PHOTOS)


@router.message(Form.gift)
async def step_gift_other(m: Message):
    await m.answer(texts.BAD_GIFT)


# ---------- шаг 2: фотографии ----------

@router.message(Form.photos, F.photo)
async def step_photo(m: Message, state: FSMContext):
    uid = m.from_user.id
    async with _locks[uid]:  # альбом приходит пачкой параллельных апдейтов
        data = await state.get_data()
        photos: list = data.get("photos", [])
        ph = m.photo[-1]
        if len(photos) < MAX_PHOTOS and all(p[1] != ph.file_unique_id for p in photos):
            photos.append([ph.file_id, ph.file_unique_id])
            await state.update_data(photos=photos)
    _schedule_counter(m, state)


@router.message(Form.photos)
async def step_photo_other(m: Message):
    await m.answer(texts.PHOTOS_HINT)


def _schedule_counter(m: Message, state: FSMContext):
    chat_id = m.chat.id
    old = _counter_tasks.get(chat_id)
    if old and not old.done():
        old.cancel()
    _counter_tasks[chat_id] = asyncio.create_task(_send_counter(m, state))


async def _send_counter(m: Message, state: FSMContext):
    try:
        await asyncio.sleep(0.8)  # собираем весь альбом в одно сообщение-счётчик
    except asyncio.CancelledError:
        return
    chat_id = m.chat.id
    n = len((await state.get_data()).get("photos", []))
    prev = _counter_msgs.pop(chat_id, None)
    if prev:
        try:
            await m.bot.delete_message(chat_id, prev)
        except Exception:
            pass
    if n < MIN_PHOTOS:
        sent = await m.bot.send_message(chat_id, f"📸 Загружено: {n}/{MIN_PHOTOS}\nОтправьте ещё: {MIN_PHOTOS - n}")
    else:
        sent = await m.bot.send_message(
            chat_id, f"✅ Загружено: {n} (минимум {MIN_PHOTOS} выполнен)\nМожно продолжить или добавить ещё "
                     f"(максимум {MAX_PHOTOS}).", reply_markup=kb.continue_kb())
    _counter_msgs[chat_id] = sent.message_id


@router.callback_query(Form.photos, F.data == "pcont")
async def photos_continue(cb: CallbackQuery, state: FSMContext):
    n = len((await state.get_data()).get("photos", []))
    if n < MIN_PHOTOS:
        await cb.answer(texts.PHOTOS_NEED_MORE, show_alert=True)
        return
    await cb.answer()
    await state.set_state(Form.wallet)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(texts.STEP_WALLET)


# ---------- шаг 3: кошелёк ----------

@router.message(Form.wallet, F.text)
async def step_wallet(m: Message, state: FSMContext):
    addr = m.text.strip()
    if not valid_wallet(addr, cfg.wallet_validation):
        await m.answer(texts.BAD_WALLET)
        return
    await state.update_data(wallet=addr)
    await state.set_state(Form.confirm)
    data = await state.get_data()
    n = len(data["photos"])
    await m.answer(
        "<b>📋 ВАША ЗАЯВКА</b>\n\n"
        f"🎁 Подарок: {esc(data['gift'])}\n"
        f"📸 Доказательства: {cards.photos_word(n)}\n"
        f"💎 GRAM-кошелёк: <code>{esc(addr)}</code>\n\n"
        "📌 Статус: Готова к отправке",
        reply_markup=kb.confirm_kb())


@router.message(Form.wallet)
async def step_wallet_other(m: Message):
    await m.answer(texts.BAD_WALLET)


# ---------- шаг 4: подтверждение ----------

@router.callback_query(Form.confirm, F.data == "redo")
async def redo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _start_form(cb.message, state)


@router.callback_query(Form.confirm, F.data == "sub")
async def submit(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    async with _locks[uid]:
        # повторная проверка внутри блокировки — защита от двойного нажатия
        if await state.get_state() != Form.confirm.state:
            await cb.answer("Заявка уже отправлена")
            return
        data = await state.get_data()
        photos = [(p[0], p[1]) for p in data.get("photos", [])]
        if len(photos) < MIN_PHOTOS or not data.get("gift") or not data.get("wallet"):
            await cb.answer("Данные неполные. Начните заново.", show_alert=True)
            return
        await state.clear()
        await repo.upsert_user(uid, cb.from_user.username, cb.from_user.full_name)
        try:
            rid = await repo.create_request(uid, data["gift"], data["gift_key"], data["wallet"], photos)
        except repo.DuplicateGift:
            await cb.answer()
            await cb.message.answer(texts.GIFT_TAKEN, reply_markup=kb.apply_kb())
            return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        f"✅ <b>Заявка {req_no(rid)} отправлена!</b>\n\nАдминистратор проверит данные. "
        "Мы уведомим вас о результате. Статус: /my")
    try:
        await cards.send_admin_card(cb.bot, rid)
    except Exception:
        log.exception("Не удалось отправить заявку %s администратору (он нажал /start у бота?)", rid)


# ---------- сообщения пользователя администратору ----------

@router.message(StateFilter(None), F.from_user.id != cfg.admin_id,
                F.func(lambda m: not (m.text or "").startswith("/")))
async def relay_to_admin(m: Message):
    header = (f"💬 <b>Сообщение от пользователя</b>\n"
              f"{mention(m.from_user.username, m.from_user.full_name, m.from_user.id)} · "
              f"<code>{m.from_user.id}</code>")
    ok = await safe_send(m.bot, cfg.admin_id, header, reply_markup=kb.reply_kb(m.from_user.id))
    if ok:
        try:
            await m.copy_to(cfg.admin_id)
        except Exception:
            pass
        await m.answer("✅ Сообщение передано администратору.")
    else:
        await m.answer("Не удалось передать сообщение. Попробуйте позже.")
