"""Настройки бота, редактируемые из админ-панели.

Значения лежат в таблице bot_settings, а при работе читаются из памяти (быстро, без запросов к БД
на каждое сообщение в группе). Также здесь хранится кэш заблокированных пользователей.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import delete, select

from . import db
from .config import cfg
from .db import BotSetting

log = logging.getLogger(__name__)

DEFAULTS: dict[str, str] = {
    "requests_open": "1",                              # приём заявок
    "max_pending": str(cfg.max_pending_per_user),      # лимит открытых заявок на пользователя
    "payout_report": "1",                              # публиковать отчёт о выплате в группу
    "premium_emoji": "1" if cfg.premium_emoji else "0",
    "antispam": "0",
    "antispam_limit": "6",                             # сообщений за окно (10 секунд)
    "antispam_action": "delete",                       # delete | mute
    "mute_minutes": "10",
    "links_mode": "allow",                             # allow | delete | notify
    "links_whitelist": "",                             # через пробел: t.me/nft example.com
}

_cache: dict[str, str] = {}
BANNED: set[int] = set()
_chats: dict[int, str] = {}


async def load() -> None:
    from . import repo
    async with db.session() as s:
        rows = (await s.execute(select(BotSetting))).scalars().all()
    _cache.clear()
    _cache.update({r.key: r.value for r in rows})
    try:
        _chats.clear()
        _chats.update({int(k): v for k, v in json.loads(_cache.get("chats", "{}")).items()})
    except Exception:
        _chats.clear()
    BANNED.clear()
    BANNED.update(await repo.banned_ids())


def get(key: str) -> str:
    return _cache.get(key, DEFAULTS.get(key, ""))


def get_bool(key: str) -> bool:
    return get(key) in ("1", "true", "on", "yes")


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, "0") or 0)


async def set_value(key: str, value: str) -> None:
    async with db.session() as s, s.begin():
        row = await s.get(BotSetting, key)
        if row is None:
            s.add(BotSetting(key=key, value=value))
        else:
            row.value = value
    _cache[key] = value


async def reset_all() -> None:
    """Сбрасывает все настройки к значениям по умолчанию (список известных чатов сохраняется)."""
    async with db.session() as s, s.begin():
        await s.execute(delete(BotSetting).where(BotSetting.key != "chats"))
    for k in list(_cache):
        if k != "chats":
            _cache.pop(k, None)


def whitelist() -> list[str]:
    return [w for w in get("links_whitelist").split() if w]


# ---------- известные группы (для проверки прав бота) ----------

def known_chats() -> dict[int, str]:
    return dict(_chats)


async def remember_chat(chat_id: int, title: str) -> None:
    if _chats.get(chat_id) == title:
        return
    _chats[chat_id] = title
    await set_value("chats", json.dumps({str(k): v for k, v in _chats.items()}, ensure_ascii=False))
