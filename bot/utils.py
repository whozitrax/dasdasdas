from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

UNIT = 10 ** 6  # суммы хранятся целыми числами в микро-GRAM, чтобы не было ошибок округления


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def fmt(units: int | None) -> str:
    d = (Decimal(int(units or 0)) / UNIT).quantize(Decimal("0.000001"))
    s = format(d, "f").rstrip("0").rstrip(".")
    return s or "0"


_AMOUNT_RE = re.compile(r"^\d{1,9}(?:\.\d{1,6})?$")


def parse_amount(text: str) -> int | None:
    s = text.strip().replace(",", ".").replace(" ", "")
    if not _AMOUNT_RE.match(s):
        return None
    units = int((Decimal(s) * UNIT).to_integral_value())
    return units if units > 0 else None


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def mention(username: str | None, full_name: str | None, tg_id: int) -> str:
    if username:
        return f"@{esc(username)}"
    return f'<a href="tg://user?id={tg_id}">{esc(full_name or str(tg_id))}</a>'


def plain_name(username: str | None, full_name: str | None, tg_id: int) -> str:
    if username:
        return f"@{username}"
    return (full_name or str(tg_id))[:24]


def req_no(i: int) -> str:
    return f"#{i:06d}"


# ---------- время ----------

def today_str(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def fmt_dt(dt: datetime, tz: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")


# ---------- валидация ----------

_GIFT_RE = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/(?:nft|gift)/[A-Za-z0-9_\-]{1,100}$", re.I)


def normalize_gift_link(text: str) -> str | None:
    s = text.strip()
    if not _GIFT_RE.match(s):
        return None
    if "://" not in s:
        s = "https://" + s
    return "https://" + s.split("://", 1)[1]


def gift_key(link: str) -> str:
    return re.sub(r"^https://telegram\.me", "https://t.me", link.lower())


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


_B64_RE = re.compile(r"^[A-Za-z0-9_\-+/]{48}$")
_RAW_RE = re.compile(r"^-?[01]:[0-9a-fA-F]{64}$")


def valid_wallet(addr: str, mode: str = "ton") -> bool:
    if not addr or any(c.isspace() for c in addr):
        return False
    if mode == "none":
        return 8 <= len(addr) <= 128
    if _RAW_RE.match(addr):
        return True
    if _B64_RE.match(addr):
        try:
            raw = base64.urlsafe_b64decode(addr.replace("+", "-").replace("/", "_"))
        except Exception:
            return False
        return len(raw) == 36 and _crc16(raw[:34]) == int.from_bytes(raw[34:], "big")
    return False
