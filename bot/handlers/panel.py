"""Админ-панель 💜 (/panel — единственная команда входа). Все разделы открываются inline-кнопками.

Схема callback_data: ``p:<раздел>:<действие>[:аргументы]``. Раздел «Заявки» использует существующие
callback'и ``rq`` / ``rl:`` / ``ro:`` / ``rs`` из handlers/admin.py (без дублирования обработчиков).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, Message

from .. import commands, repo, settings
from ..config import cfg
from ..panel_ui import MAIN, edit, head, kb, onoff, paginate
from ..utils import esc, fmt, fmt_dt, mention, parse_amount, plain_name, req_no, today_str
from .moderation import mute, norm_link

log = logging.getLogger(__name__)

router = Router(name="panel")
router.message.filter(F.from_user.id == cfg.admin_id, F.chat.type == "private")
router.callback_query.filter(F.from_user.id == cfg.admin_id)


class PanelForm(StatesGroup):
    bc_wait = State()       # ждём сообщение для рассылки
    us_search = State()     # ждём поисковый запрос
    wl_edit = State()       # ждём белый список ссылок
    acc_amount = State()    # ждём сумму начисления/списания


Screen = tuple[str, object]  # (текст, клавиатура)

# Команды (/cancel, /panel и др.) в режиме ввода не перехватываются — они обрабатываются своими обработчиками.
NOT_CMD = F.func(lambda m: not (m.text or "").startswith("/"))

USERS_PAGE = 8
LOG_PAGE = 8


# ====================================================================================
# Главное меню
# ====================================================================================

async def screen_main() -> Screen:
    c = await repo.status_counts()
    complaints = await repo.count_complaints("open")
    text = (head("💜 GRAM · ПАНЕЛЬ УПРАВЛЕНИЯ", "Центр управления ботом")
            + f"\n🟡 Заявок на проверке: <b>{c['pending']}</b>"
              f"\n🚩 Открытых жалоб: <b>{complaints}</b>"
              "\n\nВыберите раздел 👇")
    req_label = "📩 Заявки" + (f" · {c['pending']}" if c["pending"] else "")
    mod_label = "🛡 Модерация" + (f" · {complaints}" if complaints else "")
    return text, kb([
        [B(text=req_label, callback_data="rq"), B(text="📊 Статистика", callback_data="p:st:m")],
        [B(text="📢 Рассылка", callback_data="p:bc:m"), B(text=mod_label, callback_data="p:md:m")],
        [B(text="👥 Пользователи", callback_data="p:us:m"), B(text="📜 Журнал действий", callback_data="p:lg:all:0")],
        [B(text="⚙️ Настройки", callback_data="p:set:m")],
    ], with_nav=False)


_cmds_refreshed = False


@router.message(Command("panel"))
async def cmd_panel(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await screen_main()
    await m.answer(text, reply_markup=markup)
    global _cmds_refreshed
    if not _cmds_refreshed:  # меню команд могло не установиться при запуске (админ ещё не писал боту)
        _cmds_refreshed = True
        await commands.setup_commands(m.bot)


@router.callback_query(F.data == MAIN)
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = await screen_main()
    await edit(cb, text, markup)


# ====================================================================================
# 📊 Статистика
# ====================================================================================

async def screen_stats() -> Screen:
    day = today_str(cfg.timezone)
    users, banned = await repo.count_users(), await repo.count_banned()
    c = await repo.status_counts()
    today, total, paid = await repo.totals(day)
    a24, a7 = await repo.activity(24), await repo.activity(24 * 7)
    now = fmt_dt(datetime.now(timezone.utc), cfg.timezone)
    text = (
        head("📊 СТАТИСТИКА", "Основные показатели бота")
        + f"\n👥 <b>Пользователи</b>\n• Всего в базе: <b>{users}</b>\n• Заблокировано: {banned}\n"
        + f"\n📩 <b>Заявки</b>\n• Всего: <b>{sum(c.values())}</b>\n"
          f"• 🟡 На проверке: {c['pending']} · 🟢 Выплачено: {c['paid']} · 🔴 Отклонено: {c['rejected']}\n"
        + f"\n💎 <b>Финансы, GRAM</b>\n• Выплачено всего: <b>{fmt(paid)}</b>\n"
          f"• Заработано сегодня: {fmt(today)}\n• Заработано за всё время: {fmt(total)}\n"
        + "\n⚡️ <b>Активность</b>  <i>(24 ч / 7 дн)</i>\n"
          f"• Новые заявки: {a24['requests']} / {a7['requests']}\n"
          f"• Активных заявителей: {a24['users']} / {a7['users']}\n"
          f"• Выплаты: {a24['payouts']} / {a7['payouts']}\n"
          f"• Начисления: {a24['accruals']} / {a7['accruals']}\n"
        + f"\n🕒 Обновлено: {now}"
    )
    return text, kb([[B(text="🔄 Обновить", callback_data="p:st:m")]], back=MAIN)


@router.callback_query(F.data == "p:st:m")
async def cb_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Обновлено")
    text, markup = await screen_stats()
    await edit(cb, text, markup)


# ====================================================================================
# 📢 Рассылка
# ====================================================================================

_bc_running = False


async def screen_broadcast() -> Screen:
    n = len(await repo.broadcast_audience(cfg.admin_id))
    last = await repo.last_log("broadcast")
    last_txt = "— ещё не было"
    if last:
        last_txt = f"{fmt_dt(last.created_at, cfg.timezone)} · {esc(_broadcast_result(last.details))}"
    text = (head("📢 РАССЫЛКА", "Объявление для всех пользователей бота")
            + f"\n👥 Получателей: <b>{n}</b>\n🕒 Последняя рассылка: {last_txt}\n\n"
              "Как это работает: вы присылаете сообщение → видите предпросмотр → подтверждаете отправку. "
              "Тем, кому Telegram не даёт писать (не запускали бота / заблокировали), сообщение пропускается "
              "автоматически.")
    return text, kb([[B(text="✍️ Создать объявление", callback_data="p:bc:new")]], back=MAIN)


def _broadcast_result(details: str) -> str:
    kv = dict(re.findall(r"(\w+)=(\S+)", details))
    return f"доставлено {kv.get('sent', '0')}, не доставлено {kv.get('failed', '0')}"


@router.callback_query(F.data == "p:bc:m")
async def cb_bc_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = await screen_broadcast()
    await edit(cb, text, markup)


@router.callback_query(F.data.in_({"p:bc:new", "p:bc:edit"}))
async def cb_bc_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(PanelForm.bc_wait)
    await edit(cb, head("✍️ НОВОЕ ОБЪЯВЛЕНИЕ", "Шаг 1 из 2")
               + "\nОтправьте сообщение, которое получат пользователи: текст с форматированием, фото, видео "
                 "или документ с подписью.\n\n<i>Отправлю точную копию — предпросмотр вы увидите перед отправкой.</i>",
               kb([[B(text="✖️ Отмена", callback_data="p:bc:m")]], with_nav=False))


@router.message(PanelForm.bc_wait, NOT_CMD)
async def got_bc_draft(m: Message, state: FSMContext):
    await state.clear()
    n = len(await repo.broadcast_audience(cfg.admin_id))
    try:
        await m.bot.copy_message(m.chat.id, m.chat.id, m.message_id)  # предпросмотр
    except TelegramAPIError as e:
        await m.answer(f"⚠️ Этот тип сообщения нельзя разослать: {esc(str(e))}")
        return
    await m.answer(
        head("👁 ПРЕДПРОСМОТР", "Шаг 2 из 2") + f"\nВыше — точный вид объявления.\n👥 Получателей: <b>{n}</b>\n\n"
        "Отправить сейчас?",
        reply_markup=kb([
            [B(text="✅ Отправить", callback_data=f"p:bc:go:{m.message_id}")],
            [B(text="✏️ Изменить", callback_data="p:bc:edit"), B(text="✖️ Отмена", callback_data="p:bc:m")],
        ], with_nav=False))


@router.callback_query(F.data.startswith("p:bc:go:"))
async def cb_bc_go(cb: CallbackQuery):
    global _bc_running
    mid = int(cb.data.split(":")[3])
    if _bc_running:
        await cb.answer("Рассылка уже выполняется — дождитесь результата.", show_alert=True)
        return
    ids = await repo.broadcast_audience(cfg.admin_id)
    if not ids:
        await cb.answer("Нет получателей.", show_alert=True)
        return
    await cb.answer("Рассылка запущена")
    _bc_running = True
    await edit(cb, head("📢 РАССЫЛКА", "Идёт отправка…") + f"\n⏳ Получателей: {len(ids)}\n\n"
               "Результат появится здесь автоматически.", None)
    asyncio.create_task(_run_broadcast(cb, mid, ids))


async def _run_broadcast(cb: CallbackQuery, mid: int, ids: list[int]) -> None:
    global _bc_running
    bot = cb.bot
    sent = failed = other = 0
    unreachable: list[int] = []
    aborted = ""
    try:
        for uid in ids:
            for attempt in range(2):
                try:
                    await bot.copy_message(uid, cfg.admin_id, mid)
                    sent += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramForbiddenError:
                    failed += 1
                    unreachable.append(uid)
                except TelegramBadRequest as e:
                    low = str(e).lower()
                    if "message to copy not found" in low or "can't be copied" in low:
                        aborted = "исходное сообщение удалено или не может быть скопировано"
                    elif any(k in low for k in ("chat not found", "user is deactivated", "peer_id_invalid")):
                        failed += 1
                        unreachable.append(uid)
                    else:
                        failed += 1
                        other += 1
                except Exception:
                    log.exception("Рассылка: ошибка отправки %s", uid)
                    failed += 1
                    other += 1
                break
            if aborted:
                break
            await asyncio.sleep(0.05)
        await repo.mark_unreachable(unreachable)
        await repo.log_action(cfg.admin_id, "broadcast", f"sent={sent} failed={failed} total={len(ids)}")
    except Exception:
        log.exception("Рассылка: непредвиденная ошибка")
        aborted = aborted or "непредвиденная ошибка (см. логи)"
    finally:
        _bc_running = False
    status = "⚠️ Остановлена" if aborted else "✅ Завершена"
    text = (head("📢 РЕЗУЛЬТАТ РАССЫЛКИ", status) + f"\n📨 Доставлено: <b>{sent}</b>\n"
            f"🚫 Не доставлено: {failed} (недоступны для бота: {len(unreachable)}, прочие ошибки: {other})\n"
            f"👥 Всего получателей: {len(ids)}")
    if aborted:
        text += f"\n\n⚠️ Причина остановки: {esc(aborted)}"
    try:
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=kb([], back=MAIN))
    except Exception:
        log.exception("Рассылка: не удалось отправить отчёт администратору")


# ====================================================================================
# 🛡 Модерация
# ====================================================================================

_LINK_MODES = ("allow", "delete", "notify")
_LINK_TITLES = {"allow": "🔴 Разрешены", "delete": "🟢 Удалять молча", "notify": "🟢 Удалять и предупреждать"}
_LIMITS = ("4", "6", "8", "12")
_MUTES = ("5", "10", "30", "60")


def _cycle(seq: tuple[str, ...], cur: str) -> str:
    return seq[(seq.index(cur) + 1) % len(seq)] if cur in seq else seq[0]


def _antispam_line() -> str:
    if not settings.get_bool("antispam"):
        return "🔴 Выкл"
    act = "удаление" if settings.get("antispam_action") == "delete" else f"мут {settings.get('mute_minutes')} мин"
    return f"🟢 Вкл · {settings.get('antispam_limit')} сообщ./10 с → {act}"


async def screen_mod() -> Screen:
    n = await repo.count_complaints("open")
    text = (head("🛡 МОДЕРАЦИЯ", "Защита чатов, где работает бот")
            + f"\n🧹 Антиспам: {_antispam_line()}\n🔗 Ссылки: {_LINK_TITLES[settings.get('links_mode')]}\n"
              f"🚩 Открытых жалоб: <b>{n}</b>\n\n"
              "<i>Администраторы чата и главный админ бота проверкам не подвергаются.</i>")
    return text, kb([
        [B(text="🧹 Антиспам", callback_data="p:md:as"), B(text="🔗 Ссылки", callback_data="p:md:ln")],
        [B(text=f"🚩 Жалобы · {n}" if n else "🚩 Жалобы", callback_data="p:cp:l:0"),
         B(text="🩺 Права бота", callback_data="p:md:rt")],
    ], back=MAIN)


async def screen_antispam() -> Screen:
    on = settings.get_bool("antispam")
    text = (head("🧹 АНТИСПАМ", "Защита от флуда")
            + f"\nСостояние: {onoff(on)}\n📏 Порог: <b>{settings.get('antispam_limit')}</b> сообщений за 10 секунд\n"
              f"⚖️ Действие: <b>{'удалять лишние сообщения' if settings.get('antispam_action') == 'delete' else 'удалять и глушить на ' + settings.get('mute_minutes') + ' мин'}</b>\n\n"
              "<i>Нужны права бота: удаление сообщений и ограничение участников.</i>")
    act = "удаление" if settings.get("antispam_action") == "delete" else "удаление + мут"
    rows = [
        [B(text=f"Антиспам: {onoff(on)}", callback_data="p:md:as:t")],
        [B(text=f"📏 Порог: {settings.get('antispam_limit')} / 10 с", callback_data="p:md:as:l")],
        [B(text=f"⚖️ Действие: {act}", callback_data="p:md:as:a")],
    ]
    if settings.get("antispam_action") == "mute":
        rows.append([B(text=f"⏱ Длительность мута: {settings.get('mute_minutes')} мин", callback_data="p:md:as:m")])
    return text, kb(rows, back="p:md:m")


async def screen_links() -> Screen:
    wl = settings.whitelist()
    text = (head("🔗 ССЫЛКИ", "Обработка ссылок в чатах")
            + f"\nРежим: <b>{_LINK_TITLES[settings.get('links_mode')]}</b>\n"
            + "✅ Разрешённые адреса: " + (", ".join(f"<code>{esc(w)}</code>" for w in wl) if wl else "не заданы")
            + "\n\n<i>Если режим не «Разрешены», сообщения со ссылками от обычных участников удаляются, "
              "кроме адресов из белого списка (например t.me/nft).</i>")
    rows = [[B(text=f"Режим: {_LINK_TITLES[settings.get('links_mode')]}", callback_data="p:md:ln:m")],
            [B(text="✏️ Белый список", callback_data="p:md:ln:w")]]
    if wl:
        rows.append([B(text="🗑 Очистить белый список", callback_data="p:md:ln:wc")])
    return text, kb(rows, back="p:md:m")


async def _log_setting(key: str, value: str) -> None:
    await repo.log_action(cfg.admin_id, "setting", f"{key}={value}")


@router.callback_query(F.data == "p:md:m")
async def cb_mod(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = await screen_mod()
    await edit(cb, text, markup)


@router.callback_query(F.data.startswith("p:md:as"))
async def cb_antispam(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    act = cb.data[len("p:md:as"):].lstrip(":")
    toast = None
    if act == "t":
        v = not settings.get_bool("antispam")
        await settings.set_value("antispam", "1" if v else "0")
        await _log_setting("antispam", "on" if v else "off")
        toast = "Антиспам включён" if v else "Антиспам выключен"
    elif act == "l":
        v = _cycle(_LIMITS, settings.get("antispam_limit"))
        await settings.set_value("antispam_limit", v)
        await _log_setting("antispam_limit", v)
        toast = f"Порог: {v} сообщений / 10 с"
    elif act == "a":
        v = "mute" if settings.get("antispam_action") == "delete" else "delete"
        await settings.set_value("antispam_action", v)
        await _log_setting("antispam_action", v)
        toast = "Действие: удаление + мут" if v == "mute" else "Действие: удаление"
    elif act == "m":
        v = _cycle(_MUTES, settings.get("mute_minutes"))
        await settings.set_value("mute_minutes", v)
        await _log_setting("mute_minutes", v)
        toast = f"Мут: {v} мин"
    await cb.answer(toast)
    text, markup = await screen_antispam()
    await edit(cb, text, markup)


@router.callback_query(F.data.startswith("p:md:ln"))
async def cb_links(cb: CallbackQuery, state: FSMContext):
    act = cb.data[len("p:md:ln"):].lstrip(":")
    if act == "w":
        await cb.answer()
        await state.set_state(PanelForm.wl_edit)
        await edit(cb, head("✏️ БЕЛЫЙ СПИСОК", "Разрешённые ссылки")
                   + "\nОтправьте адреса через пробел или запятую, например:\n<code>t.me/nft t.me/gift example.com</code>\n\n"
                     "Разрешатся и вложенные адреса (<code>t.me/nft/Имя-1</code>). Новый список заменит текущий.",
                   kb([[B(text="✖️ Отмена", callback_data="p:md:ln")]], with_nav=False))
        return
    await state.clear()
    toast = None
    if act == "m":
        v = _cycle(_LINK_MODES, settings.get("links_mode"))
        await settings.set_value("links_mode", v)
        await _log_setting("links_mode", v)
        toast = _LINK_TITLES[v]
    elif act == "wc":
        await settings.set_value("links_whitelist", "")
        await _log_setting("links_whitelist", "cleared")
        toast = "Белый список очищен"
    await cb.answer(toast)
    text, markup = await screen_links()
    await edit(cb, text, markup)


@router.message(PanelForm.wl_edit, F.text, NOT_CMD)
async def got_whitelist(m: Message, state: FSMContext):
    items = []
    for tok in re.split(r"[\s,;]+", m.text.lower()):
        n = norm_link(tok)
        if n and "." in n and n not in items:
            items.append(n)
    if not items:
        await m.answer("⚠️ Не нашёл ни одного адреса. Пример: <code>t.me/nft example.com</code>")
        return
    await state.clear()
    items = items[:30]
    await settings.set_value("links_whitelist", " ".join(items))
    await _log_setting("links_whitelist", f"{len(items)} шт.")
    text, markup = await screen_links()
    await m.answer("✅ Белый список сохранён.\n\n" + text, reply_markup=markup)


@router.message(PanelForm.wl_edit, NOT_CMD)
async def got_whitelist_other(m: Message):
    await m.answer("⚠️ Пришлите адреса текстом, например: <code>t.me/nft example.com</code>")


@router.callback_query(F.data == "p:md:rt")
async def cb_rights(cb: CallbackQuery):
    await cb.answer("Проверяю…")
    bot = cb.bot
    me = await bot.me()
    chats = settings.known_chats()
    if cfg.payout_chat_id is not None:
        chats.setdefault(cfg.payout_chat_id, "Группа выплат")
    lines = [head("🩺 ПРАВА БОТА", "Проверка чатов"),
             "🔒 Режим приватности: " + ("🟢 выключен — бот видит все сообщения" if me.can_read_all_group_messages
                                         else "🟡 включён — бот видит не все сообщения в группах (@BotFather → "
                                              "Bot Settings → Group Privacy → Turn off)"), ""]
    if not chats:
        lines.append("Бот пока не видел ни одной группы. Добавьте его в чат и напишите там любое сообщение.")
    for chat_id, title in list(chats.items())[:10]:
        try:
            mem = await bot.get_chat_member(chat_id, me.id)
        except TelegramAPIError as e:
            lines.append(f"💬 <b>{esc(title)}</b>\n   ⚠️ не удалось проверить: {esc(str(e))}")
            continue
        if mem.status == "administrator":
            lines.append(f"💬 <b>{esc(title)}</b>\n   👑 администратор\n"
                         f"   {'✅' if getattr(mem, 'can_delete_messages', False) else '❌'} удаление сообщений\n"
                         f"   {'✅' if getattr(mem, 'can_restrict_members', False) else '❌'} ограничение участников (мут)")
        elif mem.status == "member":
            lines.append(f"💬 <b>{esc(title)}</b>\n   ❌ бот не администратор — модерация (удаление, мут) работать не будет")
        else:
            lines.append(f"💬 <b>{esc(title)}</b>\n   ❌ бота нет в чате ({esc(mem.status)})")
    await edit(cb, "\n".join(lines), kb([[B(text="🔄 Проверить снова", callback_data="p:md:rt")]], back="p:md:m"))


# ---------- жалобы ----------

async def screen_complaint(offset: int) -> Screen:
    total = await repo.count_complaints("open")
    if total == 0:
        return (head("🚩 ЖАЛОБЫ", "Открытых жалоб нет") + "\nВсё спокойно ✨\n\n"
                "<i>Участники жалуются командой /report ответом на сообщение.</i>",
                kb([], back="p:md:m"))
    offset = min(max(0, offset), total - 1)
    c = await repo.complaint_at(offset)
    tu = await repo.get_user(c.target_id) if c.target_id else None
    ru = await repo.get_user(c.reporter_id)
    link = ""
    if str(c.chat_id).startswith("-100"):
        link = f"\n🔗 <a href=\"https://t.me/c/{str(c.chat_id)[4:]}/{c.message_id}\">Открыть сообщение</a>"
    text = (head(f"🚩 ЖАЛОБА #{c.id}", f"{offset + 1} из {total}")
            + f"\n💬 Чат: {esc(c.chat_title or str(c.chat_id))}\n"
              f"👤 На кого: {mention(tu.username if tu else None, tu.full_name if tu else None, c.target_id or 0)}\n"
              f"📣 Кто пожаловался: {mention(ru.username if ru else None, ru.full_name if ru else None, c.reporter_id)}\n"
              f"📅 {fmt_dt(c.created_at, cfg.timezone)}{link}\n\n📝 <i>{esc(c.excerpt[:400])}</i>")
    rows = [paginate("p:cp:l", offset, total, 1),
            [B(text="🗑 Удалить сообщение", callback_data=f"p:cp:x:{c.id}"),
             B(text="🔇 Мут автора", callback_data=f"p:cp:m:{c.id}")],
            [B(text="✅ Закрыть жалобу", callback_data=f"p:cp:d:{c.id}")]]
    if c.target_id:
        rows.append([B(text="👤 Профиль автора", callback_data=f"p:us:v:{c.target_id}")])
    return text, kb(rows, back="p:md:m")


@router.callback_query(F.data.startswith("p:cp:"))
async def cb_complaints(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = cb.data.split(":")  # p cp <act> <arg>
    act, arg = parts[2], int(parts[3])
    toast = None
    offset = 0
    if act == "l":
        offset = arg
    elif act == "v":  # открыть конкретную жалобу (из уведомления)
        total = await repo.count_complaints("open")
        for i in range(total):
            c = await repo.complaint_at(i)
            if c and c.id == arg:
                offset = i
                break
    else:
        c = await repo.get_complaint(arg)
        if c is None or c.status != "open":
            await cb.answer("Жалоба уже закрыта", show_alert=True)
        else:
            try:
                if act == "x":
                    await cb.bot.delete_message(c.chat_id, c.message_id)
                    toast = "🗑 Сообщение удалено, жалоба закрыта"
                elif act == "m" and not c.target_id:
                    toast = "⚠️ У жалобы нет автора — мут невозможен"
                elif act == "m":
                    await mute(cb.bot, c.chat_id, c.target_id, settings.get_int("mute_minutes"))
                    toast = f"🔇 Автор заглушён на {settings.get('mute_minutes')} мин, жалоба закрыта"
                else:
                    toast = "✅ Жалоба закрыта"
                if not toast.startswith("⚠️"):
                    await repo.close_complaint(arg)
                    await repo.log_action(cfg.admin_id, "complaint", f"id={arg} action={act}")
            except TelegramAPIError as e:
                toast = f"⚠️ Не удалось: {str(e)[:120]} (проверьте права бота)"
    await cb.answer(toast, show_alert=bool(toast and toast.startswith("⚠️")))
    text, markup = await screen_complaint(offset)
    await edit(cb, text, markup)


# ====================================================================================
# 👥 Пользователи
# ====================================================================================

async def screen_users_menu() -> Screen:
    total, banned = await repo.count_users(), await repo.count_banned()
    text = (head("👥 ПОЛЬЗОВАТЕЛИ", "Поиск и управление")
            + f"\n👤 В базе: <b>{total}</b>\n🚫 Заблокировано: <b>{banned}</b>")
    return text, kb([
        [B(text="🔎 Найти пользователя", callback_data="p:us:s")],
        [B(text="🕒 Недавние", callback_data="p:us:r:0"), B(text="🚫 Заблокированные", callback_data="p:us:b:0")],
    ], back=MAIN)


def _user_buttons(users) -> list[list[B]]:
    return [[B(text=f"{plain_name(u.username, u.full_name, u.tg_id)} · {u.tg_id}",
               callback_data=f"p:us:v:{u.tg_id}")] for u in users]


async def screen_user_list(kind: str, page: int) -> Screen:
    banned = kind == "b"
    total, rows = await repo.users_page(page, USERS_PAGE, banned_only=banned)
    title = "🚫 ЗАБЛОКИРОВАННЫЕ" if banned else "🕒 НЕДАВНИЕ ПОЛЬЗОВАТЕЛИ"
    text = head(title, f"Всего: {total}") + ("" if rows else "\nСписок пуст.")
    kbrows = _user_buttons(rows)
    if total > USERS_PAGE:
        kbrows.append(paginate(f"p:us:{kind}", page, total, USERS_PAGE))
    return text, kb(kbrows, back="p:us:m")


async def screen_user(uid: int) -> Screen:
    ov = await repo.user_overview(uid, today_str(cfg.timezone))
    if ov is None:
        return head("👤 ПОЛЬЗОВАТЕЛЬ") + "\nПользователь не найден.", kb([], back="p:us:m")
    u = ov["user"]
    if ov["banned"]:
        status = "🚫 Заблокирован"
    elif ov["unreachable"]:
        status = "📪 Не запускал бота / заблокировал его (личные сообщения недоступны)"
    else:
        status = "🟢 Активен"
    last = fmt_dt(ov["last_request"], cfg.timezone) if ov["last_request"] else "—"
    total_req = ov["pending"] + ov["paid"] + ov["rejected"]
    text = (head("👤 ПОЛЬЗОВАТЕЛЬ", "Карточка")
            + f"\n🏷 Имя: <b>{esc(u.full_name) or '—'}</b>\n🔗 Username: {('@' + esc(u.username)) if u.username else '—'}\n"
              f"🆔 ID: <code>{u.tg_id}</code>\n📌 Статус: {status}\n"
              f"\n📩 Заявки: <b>{total_req}</b> (🟡 {ov['pending']} · 🟢 {ov['paid']} · 🔴 {ov['rejected']})\n"
              f"📅 Последняя заявка: {last}\n"
              f"\n💸 Выплачено ему: <b>{fmt(ov['paid_sum'])} GRAM</b>\n"
              f"🏆 Заработано: сегодня {fmt(ov['today'])} · всего {fmt(ov['total'])} GRAM")
    rows = [
        [B(text="📩 Заявки", callback_data=f"p:us:q:{uid}"), B(text="💬 Написать", callback_data=f"msg:{uid}")],
        [B(text="➕ Начислить", callback_data=f"p:us:a:{uid}:add"), B(text="➖ Списать", callback_data=f"p:us:a:{uid}:sub")],
    ]
    if uid != cfg.admin_id:
        rows.append([B(text="✅ Разблокировать" if ov["banned"] else "🚫 Заблокировать", callback_data=f"p:us:t:{uid}")])
    return text, kb(rows, back="p:us:m")


@router.callback_query(F.data == "p:us:m")
async def cb_users_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = await screen_users_menu()
    await edit(cb, text, markup)


@router.callback_query(F.data.regexp(r"^p:us:[rb]:\d+$"))
async def cb_users_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    _, _, kind, page = cb.data.split(":")
    text, markup = await screen_user_list(kind, int(page))
    await edit(cb, text, markup)


@router.callback_query(F.data == "p:us:s")
async def cb_users_search(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(PanelForm.us_search)
    await edit(cb, head("🔎 ПОИСК ПОЛЬЗОВАТЕЛЯ") + "\nОтправьте Telegram ID, @username или часть имени.",
               kb([[B(text="✖️ Отмена", callback_data="p:us:m")]], with_nav=False))


@router.message(PanelForm.us_search, F.text, NOT_CMD)
async def got_user_query(m: Message, state: FSMContext):
    await state.clear()
    found = await repo.search_users(m.text)
    if not found:
        await m.answer(head("🔎 ПОИСК") + "\nНичего не найдено.", reply_markup=kb(
            [[B(text="🔎 Искать снова", callback_data="p:us:s")]], back="p:us:m"))
        return
    if len(found) == 1:
        text, markup = await screen_user(found[0].tg_id)
        await m.answer(text, reply_markup=markup)
        return
    await m.answer(head("🔎 РЕЗУЛЬТАТЫ ПОИСКА", f"Найдено: {len(found)}"), reply_markup=kb(_user_buttons(found), back="p:us:m"))


@router.message(PanelForm.us_search, NOT_CMD)
async def got_user_query_other(m: Message):
    await m.answer("⚠️ Пришлите запрос текстом: ID, @username или имя.")


@router.callback_query(F.data.regexp(r"^p:us:v:\d+$"))
async def cb_user_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = await screen_user(int(cb.data.split(":")[3]))
    await edit(cb, text, markup)


@router.callback_query(F.data.regexp(r"^p:us:q:\d+$"))
async def cb_user_requests(cb: CallbackQuery):
    await cb.answer()
    uid = int(cb.data.split(":")[3])
    rows = await repo.user_requests(uid, 15)
    icon = {"pending": "🟡", "paid": "🟢", "rejected": "🔴"}
    btns = [[B(text=f"{icon[r.status]} {req_no(r.id)}" + (f" · {fmt(r.paid_amount)} GRAM" if r.status == "paid" else ""),
               callback_data=f"ro:{r.id}")] for r in rows]
    await edit(cb, head("📩 ЗАЯВКИ ПОЛЬЗОВАТЕЛЯ", f"ID {uid}") + ("" if rows else "\nЗаявок нет."),
               kb(btns, back=f"p:us:v:{uid}"))


@router.callback_query(F.data.regexp(r"^p:us:t:\d+$"))
async def cb_user_ban(cb: CallbackQuery):
    uid = int(cb.data.split(":")[3])
    if uid == cfg.admin_id:
        await cb.answer("Нельзя блокировать главного администратора", show_alert=True)
        return
    now_banned = uid not in settings.BANNED
    await repo.set_banned(uid, now_banned)
    (settings.BANNED.add if now_banned else settings.BANNED.discard)(uid)
    await repo.log_action(cfg.admin_id, "ban" if now_banned else "unban", f"user={uid}")
    await cb.answer("🚫 Заблокирован" if now_banned else "✅ Разблокирован")
    text, markup = await screen_user(uid)
    await edit(cb, text, markup)


@router.callback_query(F.data.regexp(r"^p:us:a:\d+:(add|sub)$"))
async def cb_user_accrual(cb: CallbackQuery, state: FSMContext):
    _, _, _, uid_s, kind = cb.data.split(":")
    uid = int(uid_s)
    await cb.answer()
    await state.set_state(PanelForm.acc_amount)
    await state.update_data(uid=uid, kind=kind)
    word = "начислить" if kind == "add" else "списать (коррекция)"
    await edit(cb, head("➕ НАЧИСЛЕНИЕ" if kind == "add" else "➖ СПИСАНИЕ")
               + f"\nВведите сумму GRAM, которую нужно {word}.\nНапример: <code>3.5</code>",
               kb([[B(text="✖️ Отмена", callback_data=f"p:us:v:{uid}")]], with_nav=False))


@router.message(PanelForm.acc_amount, F.text, NOT_CMD)
async def got_accrual_amount(m: Message, state: FSMContext):
    amount = parse_amount(m.text)
    if amount is None:
        await m.answer("⚠️ Не понял сумму. Введите положительное число, например <code>3.5</code>.")
        return
    data = await state.get_data()
    uid, kind = data.get("uid"), data.get("kind")
    await state.clear()
    if not uid or kind not in ("add", "sub"):
        await m.answer("⚠️ Сессия устарела. Откройте карточку пользователя заново.", reply_markup=kb([], back="p:us:m"))
        return
    day = today_str(cfg.timezone)
    if kind == "sub":
        _, total = await repo.user_sums(uid, day)
        if total < amount:
            await m.answer(f"⚠️ Нельзя списать больше, чем заработано всего ({fmt(total)} GRAM).",
                           reply_markup=kb([], back=f"p:us:v:{uid}"))
            return
    acc = await repo.add_accrual(uid, amount, kind, m.from_user.id, day, m.chat.id, m.message_id)
    if acc is None:
        await m.answer("⚠️ Эта операция уже была обработана.", reply_markup=kb([], back=f"p:us:v:{uid}"))
        return
    user = await repo.get_user(uid)
    today, total = await repo.user_sums(uid, day)
    sign, label = ("+", "Начислено") if kind == "add" else ("−", "Списано")
    await m.answer(
        head("✅ НАЧИСЛЕНИЕ ВЫПОЛНЕНО" if kind == "add" else "➖ СПИСАНИЕ ВЫПОЛНЕНО")
        + f"\n👤 {mention(user.username if user else None, user.full_name if user else None, uid)}\n\n"
          f"💎 {label}: {sign}{fmt(amount)} GRAM\n📊 Сегодня: {fmt(today)} GRAM\n🏆 Всего: {fmt(total)} GRAM\n\n🧾 Операция №{acc.id}",
        reply_markup=kb([[B(text="↩️ Отменить операцию", callback_data=f"undo:{acc.id}")],
                         [B(text="👤 К карточке", callback_data=f"p:us:v:{uid}")]], back=MAIN, with_nav=True))


@router.message(PanelForm.acc_amount, NOT_CMD)
async def got_accrual_other(m: Message):
    await m.answer("⚠️ Введите сумму числом, например <code>3.5</code>.")


# ====================================================================================
# 📜 Журнал действий
# ====================================================================================

LOG_FILTERS = [("all", "Все"), ("pay", "Заявки"), ("acc", "Начисления"), ("bc", "Рассылки"), ("cfg", "Настройки")]

_ACTION = {
    "payout": "💸 Выплата", "reject": "❌ Отклонение заявки", "accrual_add": "➕ Начисление",
    "accrual_sub": "➖ Списание", "accrual_revert": "↩️ Отмена операции", "broadcast": "📢 Рассылка",
    "setting": "⚙️ Настройка", "ban": "🚫 Блокировка", "unban": "✅ Разблокировка",
    "complaint": "🚩 Жалоба", "reset": "♻️ Сброс настроек",
}
_SETTING_NAMES = {
    "requests_open": "приём заявок", "max_pending": "лимит заявок", "payout_report": "отчёты в группу",
    "premium_emoji": "премиум-эмодзи", "antispam": "антиспам", "antispam_limit": "порог антиспама",
    "antispam_action": "действие антиспама", "mute_minutes": "длительность мута", "links_mode": "режим ссылок",
    "links_whitelist": "белый список ссылок",
}


def describe_log(action: str, details: str) -> tuple[str, str]:
    """-> (описание, результат)"""
    kv = dict(re.findall(r"(\w+)=(\S+)", details))
    amt = lambda: fmt(int(kv["amount_units"])) + " GRAM" if kv.get("amount_units", "").lstrip("-").isdigit() else "—"
    if action == "payout":
        return f"заявка {req_no(int(kv.get('request', 0)))}, {amt()}", "✅ выполнено"
    if action == "reject":
        m = re.search(r"request=(\d+) reason=(.*)$", details)
        return (f"заявка {req_no(int(m.group(1)))}, причина: {m.group(2)[:60]}" if m else details[:80]), "✅ выполнено"
    if action in ("accrual_add", "accrual_sub"):
        return f"№{kv.get('id', '?')}, ID {kv.get('user', '?')}, {amt()}", "✅ выполнено"
    if action == "accrual_revert":
        return f"отмена №{kv.get('reverts', '?')}, ID {kv.get('user', '?')}, {amt()}", "✅ выполнено"
    if action == "broadcast":
        failed = int(kv.get("failed", 0) or 0)
        return _broadcast_result(details), ("⚠️ с ошибками" if failed else "✅ выполнено")
    if action == "setting":
        k, _, v = details.partition("=")
        return f"{_SETTING_NAMES.get(k, k)} → {v}", "✅ применено"
    if action in ("ban", "unban"):
        return f"ID {kv.get('user', '?')}", "✅ выполнено"
    if action == "complaint":
        return f"жалоба #{kv.get('id', '?')}", "✅ обработана"
    return details[:80], "✅"


async def screen_log(group: str, page: int) -> Screen:
    if group not in repo.LOG_GROUPS:
        group = "all"
    total, rows = await repo.logs_page(group, page, LOG_PAGE)
    pages = max(1, -(-total // LOG_PAGE))
    if page >= pages:
        page = pages - 1
        total, rows = await repo.logs_page(group, page, LOG_PAGE)
    lines = [head("📜 ЖУРНАЛ ДЕЙСТВИЙ", f"Фильтр: {dict(LOG_FILTERS)[group]} · записей: {total}")]
    if not rows:
        lines.append("\nЗаписей пока нет.")
    for r in rows:
        desc, res = describe_log(r.action, r.details)
        lines.append(f"\n🕒 <code>{fmt_dt(r.created_at, cfg.timezone)}</code> · {_ACTION.get(r.action, esc(r.action))}\n"
                     f"   {esc(desc)} — {res}")
    filt = [B(text=("• " if g == group else "") + name, callback_data=f"p:lg:{g}:0") for g, name in LOG_FILTERS]
    rows_kb = [filt[:3], filt[3:]]
    if total > LOG_PAGE:
        rows_kb.append(paginate(f"p:lg:{group}", page, total, LOG_PAGE))
    return "\n".join(lines), kb(rows_kb, back=MAIN)


@router.callback_query(F.data.regexp(r"^p:lg:(all|pay|acc|bc|cfg):\d+$"))
async def cb_log(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    _, _, group, page = cb.data.split(":")
    text, markup = await screen_log(group, int(page))
    await edit(cb, text, markup)


# ====================================================================================
# ⚙️ Настройки
# ====================================================================================

_MAX_PENDING = ("1", "3", "5", "10")


def _db_kind() -> str:
    return "PostgreSQL" if "postgres" in cfg.database_url else "SQLite"


async def screen_settings() -> Screen:
    text = (head("⚙️ НАСТРОЙКИ", "Основные параметры бота")
            + f"\n📥 Приём заявок: {'🟢 открыт' if settings.get_bool('requests_open') else '🔴 приостановлен'}"
              f"\n📌 Лимит открытых заявок на человека: <b>{settings.get_int('max_pending')}</b>"
              f"\n💸 Отчёты о выплатах в группу: {onoff(settings.get_bool('payout_report'))}"
              f"\n✨ Премиум-эмодзи: {onoff(settings.get_bool('premium_emoji'))}"
              f"\n\nℹ️ <b>Информация</b>\n🕒 Часовой пояс: {esc(cfg.timezone)}\n👑 Администратор: <code>{cfg.admin_id}</code>"
              f"\n💬 Группа выплат: {('<code>' + str(cfg.payout_chat_id) + '</code>') if cfg.payout_chat_id else 'не задана (PAYOUT_CHAT_ID)'}"
              f"\n🗄 База данных: {_db_kind()}"
              "\n\n<i>Токен, ID группы и база меняются только в .env — из бота они не редактируются.</i>")
    return text, kb([
        [B(text=f"📥 Приём заявок: {'🟢' if settings.get_bool('requests_open') else '🔴'}", callback_data="p:set:ro")],
        [B(text=f"📌 Лимит заявок: {settings.get_int('max_pending')}", callback_data="p:set:mp")],
        [B(text=f"💸 Отчёты в группу: {'🟢' if settings.get_bool('payout_report') else '🔴'}", callback_data="p:set:pr")],
        [B(text=f"✨ Премиум-эмодзи: {'🟢' if settings.get_bool('premium_emoji') else '🔴'}", callback_data="p:set:pe")],
        [B(text="🔄 Обновить меню команд", callback_data="p:set:cmd"), B(text="♻️ Сбросить", callback_data="p:set:rs")],
    ], back=MAIN)


@router.callback_query(F.data.startswith("p:set:"))
async def cb_settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    act = cb.data.split(":")[2]
    toast = None
    if act == "rs":
        await cb.answer()
        await edit(cb, head("♻️ СБРОС НАСТРОЕК") + "\nВсе параметры панели вернутся к значениям по умолчанию "
                   "(приём заявок, лимиты, антиспам, ссылки, белый список). Заявки, пользователи и выплаты не затрагиваются.\n\nПродолжить?",
                   kb([[B(text="✅ Да, сбросить", callback_data="p:set:rsy"),
                        B(text="✖️ Отмена", callback_data="p:set:m")]], with_nav=False))
        return
    if act == "rsy":
        await settings.reset_all()
        await repo.log_action(cfg.admin_id, "reset", "settings")
        toast = "Настройки сброшены"
    elif act == "ro":
        v = not settings.get_bool("requests_open")
        await settings.set_value("requests_open", "1" if v else "0")
        await _log_setting("requests_open", "on" if v else "off")
        toast = "Приём заявок открыт" if v else "Приём заявок приостановлен"
    elif act == "mp":
        v = _cycle(_MAX_PENDING, str(settings.get_int("max_pending")))
        await settings.set_value("max_pending", v)
        await _log_setting("max_pending", v)
        toast = f"Лимит: {v}"
    elif act == "pr":
        v = not settings.get_bool("payout_report")
        await settings.set_value("payout_report", "1" if v else "0")
        await _log_setting("payout_report", "on" if v else "off")
        toast = "Отчёты в группу включены" if v else "Отчёты в группу выключены"
    elif act == "pe":
        v = not settings.get_bool("premium_emoji")
        await settings.set_value("premium_emoji", "1" if v else "0")
        await _log_setting("premium_emoji", "on" if v else "off")
        toast = "Премиум-эмодзи включены" if v else "Премиум-эмодзи выключены"
    elif act == "cmd":
        problems = await commands.setup_commands(cb.bot)
        toast = "Меню команд обновлено" if not problems else "⚠️ " + problems[0][:150]
    await cb.answer(toast, show_alert=bool(toast and toast.startswith("⚠️")))
    text, markup = await screen_settings()
    await edit(cb, text, markup)
