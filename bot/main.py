from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent

from . import db, settings
from .commands import setup_commands
from .config import cfg
from .handlers import admin, moderation, panel, stats, user
from .middlewares import AccessibleMessage, PremiumEmojiMiddleware, UserTracker

log = logging.getLogger("bot")

# Последний в цепочке роутер: ловит только те нажатия, которые не подошли ни одному обработчику
# (устаревшие кнопки, потерянное после перезапуска состояние формы).
# ВАЖНО: он должен быть именно ПОДКЛЮЧЁННЫМ роутером в конце. Обработчик, зарегистрированный прямо на Dispatcher,
# вызывается РАНЬШЕ всех подключённых роутеров и «съедает» любое нажатие — именно так перестали работать кнопки.
fallback = Router(name="fallback")


@fallback.callback_query()
async def _stale_button(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела. Откройте меню заново (/start или /panel).", show_alert=True)


async def _on_error(event: ErrorEvent) -> bool:
    """Ошибка в одном обработчике не должна ломать остальные: логируем, а нажавшему кнопку отвечаем."""
    log.error("Необработанная ошибка в обработчике: %s", event.exception, exc_info=event.exception)
    cq = event.update.callback_query
    if cq is not None:
        try:
            await cq.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
    return True


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(UserTracker())
    dp.callback_query.outer_middleware(AccessibleMessage())
    dp.callback_query.outer_middleware(moderation.BanGuard())
    dp.message.outer_middleware(moderation.BanGuard())
    dp.message.outer_middleware(moderation.ModerationMiddleware())
    dp.edited_message.outer_middleware(moderation.ModerationMiddleware(edited=True))

    # Порядок важен: админ-панель → заявки админа → начисления → рейтинги → жалобы → пользователь → fallback
    dp.include_router(panel.router)
    dp.include_router(admin.router)
    dp.include_router(stats.adm)
    dp.include_router(stats.public)
    dp.include_router(moderation.router)
    dp.include_router(user.router)
    dp.include_router(fallback)
    dp.errors.register(_on_error)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.bot_token:
        raise SystemExit("Укажите BOT_TOKEN в .env")
    await db.init_db(cfg.database_url)
    await settings.load()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                          link_preview_is_disabled=True))
    bot.session.middleware(PremiumEmojiMiddleware())  # включается/выключается в панели (⚙️ Настройки)
    dp = build_dispatcher()

    await setup_commands(bot)
    await bot.delete_webhook()
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "edited_message", "callback_query"])
    finally:
        await bot.session.close()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
