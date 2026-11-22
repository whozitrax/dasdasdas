from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, event, text)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    username_lc: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_ACTIVE = text("status IN ('pending','paid')")


class Request(Base):
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    gift_link: Mapped[str] = mapped_column(String(300))
    gift_key: Mapped[str] = mapped_column(String(300))
    wallet: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    paid_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # в микро-GRAM (1e-6)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Один и тот же подарок не может иметь две активные (ожидающие/выплаченные) заявки
    __table_args__ = (
        Index("uq_active_gift", "gift_key", unique=True, sqlite_where=_ACTIVE, postgresql_where=_ACTIVE),
    )


class RequestPhoto(Base):
    __tablename__ = "request_photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, ForeignKey("requests.id"), index=True)
    file_id: Mapped[str] = mapped_column(String(256))
    file_unique_id: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Accrual(Base):
    """История начислений. Исправления — отдельные записи (sub / revert), ничего не удаляется."""
    __tablename__ = "accruals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # может быть отрицательной (sub / revert)
    kind: Mapped[str] = mapped_column(String(16))  # add | sub | revert
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD по Europe/Kyiv
    admin_id: Mapped[int] = mapped_column(BigInteger)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)
    src_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    src_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Одно и то же сообщение-команда не может начислить дважды
    __table_args__ = (UniqueConstraint("src_chat_id", "src_msg_id", name="uq_accrual_src"),)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, ForeignKey("requests.id"), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminLog(Base):
    __tablename__ = "admin_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- новые таблицы (создаются автоматически, существующие данные не затрагиваются) ----------

class BotSetting(Base):
    """Настройки бота, которые меняются из админ-панели."""
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class UserFlag(Base):
    """Флаги пользователя: блокировка администратором, недоступность для рассылки."""
    __tablename__ = "user_flags"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    unreachable: Mapped[bool] = mapped_column(Boolean, default=False)


class Complaint(Base):
    """Жалобы на сообщения в группах (/report ответом на сообщение)."""
    __tablename__ = "complaints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    chat_title: Mapped[str] = mapped_column(String(256), default="")
    message_id: Mapped[int] = mapped_column(Integer)
    reporter_id: Mapped[int] = mapped_column(BigInteger)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | closed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def _normalize(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


async def init_db(url: str) -> None:
    global _engine, _sessionmaker
    url = _normalize(url)
    if url.startswith("sqlite"):
        path = url.split(":///", 1)[1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session():
    assert _sessionmaker is not None, "init_db() не вызван"
    return _sessionmaker()


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()
