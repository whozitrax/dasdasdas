from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InputMediaPhoto

from . import keyboards as kb
from . import repo, settings
from .config import cfg
from .media import send_image
from .utils import esc, fmt, fmt_dt, mention, plural, req_no

log = logging.getLogger(__name__)

STATUS_LABEL = {"pending": "🟡 Ожидает проверки", "paid": "🟢 Выплачено", "rejected": "🔴 Отклонена"}


def photos_word(n: int) -> str:
    return f"{n} {plural(n, 'фотография', 'фотографии', 'фотографий')}"


def admin_card_text(req, user, n_photos: int) -> str:
    uname = user.username if user else None
    fname = user.full_name if user else None
    title = "🔔 НОВАЯ ЗАЯВКА" if req.status == "pending" else "📄 ЗАЯВКА"
    lines = [
        f"<b>{title} {req_no(req.id)}</b>", "",
        f"👤 Пользователь: {mention(uname, fname, req.user_id)}",
        f"🆔 Telegram ID: <code>{req.user_id}</code>",
        f"🎁 Подарок: {esc(req.gift_link)}",
        f"💎 GRAM-кошелёк: <code>{esc(req.wallet)}</code>",
        f"📸 Доказательства: {n_photos} {plural(n_photos, 'скриншот', 'скриншота', 'скриншотов')}",
        f"📅 Дата: {fmt_dt(req.created_at, cfg.timezone)}",
    ]
    if req.status == "paid":
        lines.append(f"💰 Выплачено: <b>{fmt(req.paid_amount)} GRAM</b>")
    if req.status == "rejected" and req.reject_reason:
        lines.append(f"📝 Причина: {esc(req.reject_reason)}")
    lines += ["", f"📌 Статус: {STATUS_LABEL[req.status]}"]
    return "\n".join(lines)


async def send_admin_card(bot: Bot, rid: int) -> None:
    """Фотографии альбомом + карточка с кнопками. Запоминает message_id карточки для обновления."""
    full = await repo.get_request_full(rid)
    if not full:
        return
    req, user, photos = full
    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            await bot.send_photo(cfg.admin_id, chunk[0])
        else:
            await bot.send_media_group(cfg.admin_id, [InputMediaPhoto(media=f) for f in chunk])
    msg = await bot.send_message(cfg.admin_id, admin_card_text(req, user, len(photos)),
                                 reply_markup=kb.admin_card_kb(req))
    await repo.set_admin_msg(rid, msg.message_id)


async def refresh_stored_card(bot: Bot, rid: int) -> None:
    full = await repo.get_request_full(rid)
    if not full or not full[0].admin_msg_id:
        return
    req, user, photos = full
    try:
        await bot.edit_message_text(admin_card_text(req, user, len(photos)), chat_id=cfg.admin_id,
                                    message_id=req.admin_msg_id, reply_markup=kb.admin_card_kb(req))
    except Exception as e:
        log.debug("Не удалось обновить карточку #%s: %s", rid, e)


def payout_report_text(req, user, amount: int) -> str:
    return (
        "<b>💎 ВЫПЛАТА ЗАВЕРШЕНА</b>\n\n"
        f"👤 Пользователь: {mention(user.username if user else None, user.full_name if user else None, req.user_id)}\n"
        f"💰 Выплачено: <b>{fmt(amount)} GRAM</b>\n"
        f"🎁 Подарок: {esc(req.gift_link)}\n"
        "✅ Статус: Выплачено"
    )


async def post_payout_report(bot: Bot, req, user, amount: int) -> str | None:
    """Публикует отчёт в тему группы. Возвращает текст ошибки или None."""
    if not settings.get_bool("payout_report"):
        return None  # публикация отчётов отключена в панели (⚙️ Настройки)
    if cfg.payout_chat_id is None:
        return "PAYOUT_CHAT_ID не задан в .env"
    try:
        await send_image(bot, cfg.payout_chat_id, cfg.payout_image, payout_report_text(req, user, amount),
                         thread_id=cfg.payout_thread_id)
        return None
    except Exception as e:
        log.exception("Ошибка публикации отчёта")
        return str(e)
