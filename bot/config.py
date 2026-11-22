from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name: str, default: int | None = None) -> int | None:
    v = os.getenv(name, "").strip()
    return int(v) if v else default


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    payout_chat_id: int | None
    payout_thread_id: int | None
    database_url: str
    timezone: str
    wallet_validation: str
    max_pending_per_user: int
    premium_emoji: bool
    welcome_image: Path
    payout_image: Path
    currency: str = "GRAM"


cfg = Config(
    bot_token=os.getenv("BOT_TOKEN", "").strip(),
    admin_id=_int("ADMIN_ID", 8923235001),
    payout_chat_id=_int("PAYOUT_CHAT_ID"),
    payout_thread_id=_int("PAYOUT_THREAD_ID"),
    database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db").strip(),
    timezone=os.getenv("TIMEZONE", "Europe/Kyiv").strip(),
    wallet_validation=os.getenv("WALLET_VALIDATION", "ton").strip().lower(),
    max_pending_per_user=_int("MAX_PENDING_PER_USER", 5),
    premium_emoji=os.getenv("PREMIUM_EMOJI", "true").strip().lower() in ("1", "true", "yes", "on"),
    welcome_image=BASE_DIR / "assets" / "welcome.png",
    payout_image=BASE_DIR / "assets" / "payout.png",
)
