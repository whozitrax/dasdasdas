from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import db
from .db import (Accrual, AdminLog, Complaint, Payout, Request, RequestPhoto, User, UserFlag, utcnow)


class DuplicateGift(Exception):
    pass


def _log(s, admin_id: int, action: str, details: str = "") -> None:
    s.add(AdminLog(admin_id=admin_id, action=action, details=details))


# ---------- пользователи ----------

async def upsert_user(tg_id: int, username: str | None, full_name: str | None) -> None:
    uname = username or None
    for _ in range(2):
        try:
            async with db.session() as s, s.begin():
                u = await s.get(User, tg_id)
                if u is None:
                    s.add(User(tg_id=tg_id, username=uname, username_lc=uname.lower() if uname else None,
                               full_name=full_name or ""))
                else:
                    u.username = uname
                    u.username_lc = uname.lower() if uname else None
                    u.full_name = full_name or ""
                    u.updated_at = utcnow()
            return
        except IntegrityError:  # параллельная вставка того же пользователя
            continue


async def ensure_user(tg_id: int) -> None:
    async with db.session() as s:
        exists = await s.get(User, tg_id)
    if not exists:
        await upsert_user(tg_id, None, str(tg_id))


async def get_user(tg_id: int) -> User | None:
    async with db.session() as s:
        return await s.get(User, tg_id)


async def find_user_by_username(username: str) -> User | None:
    uname = username.lstrip("@").lower()
    async with db.session() as s:
        r = await s.execute(select(User).where(User.username_lc == uname).order_by(User.updated_at.desc()).limit(1))
        return r.scalar_one_or_none()


# ---------- заявки ----------

async def gift_taken(key: str) -> bool:
    async with db.session() as s:
        r = await s.execute(select(Request.id).where(Request.gift_key == key,
                                                      Request.status.in_(("pending", "paid"))).limit(1))
        return r.first() is not None


async def count_pending(user_id: int) -> int:
    async with db.session() as s:
        r = await s.execute(select(func.count()).select_from(Request).where(Request.user_id == user_id,
                                                                            Request.status == "pending"))
        return int(r.scalar_one())


async def create_request(user_id: int, link: str, key: str, wallet: str, photos: list[tuple[str, str]]) -> int:
    try:
        async with db.session() as s, s.begin():
            req = Request(user_id=user_id, gift_link=link, gift_key=key, wallet=wallet)
            s.add(req)
            await s.flush()
            for i, (fid, uq) in enumerate(photos):
                s.add(RequestPhoto(request_id=req.id, file_id=fid, file_unique_id=uq, position=i))
        return req.id
    except IntegrityError:
        raise DuplicateGift()


async def get_request(rid: int) -> Request | None:
    async with db.session() as s:
        return await s.get(Request, rid)


async def get_request_full(rid: int):
    """-> (request, user, [file_id, ...]) или None"""
    async with db.session() as s:
        req = await s.get(Request, rid)
        if not req:
            return None
        user = await s.get(User, req.user_id)
        r = await s.execute(select(RequestPhoto.file_id).where(RequestPhoto.request_id == rid)
                            .order_by(RequestPhoto.position))
        return req, user, [x[0] for x in r.all()]


async def set_admin_msg(rid: int, msg_id: int) -> None:
    async with db.session() as s, s.begin():
        await s.execute(update(Request).where(Request.id == rid).values(admin_msg_id=msg_id))


async def status_counts() -> dict[str, int]:
    async with db.session() as s:
        r = await s.execute(select(Request.status, func.count()).group_by(Request.status))
        d = {k: int(v) for k, v in r.all()}
    return {"pending": d.get("pending", 0), "paid": d.get("paid", 0), "rejected": d.get("rejected", 0)}


def _list_cols():
    return select(Request.id, Request.status, Request.user_id, User.username, User.full_name).join(
        User, User.tg_id == Request.user_id)


async def list_requests(status: str, page: int, size: int):
    cond = [] if status == "all" else [Request.status == status]
    async with db.session() as s:
        total = (await s.execute(select(func.count()).select_from(Request).where(*cond))).scalar_one()
        rows = (await s.execute(_list_cols().where(*cond).order_by(Request.id.desc())
                                .offset(page * size).limit(size))).all()
    return int(total), rows


async def search_requests(q: str, limit: int = 20):
    q = q.strip().lstrip("#")
    if not q:
        return []
    conds = [User.username_lc == q.lstrip("@").lower(), Request.gift_link.ilike(f"%{q}%"),
             Request.wallet.ilike(f"%{q}%")]
    if q.isdigit() and len(q) <= 18:
        conds += [Request.id == int(q), Request.user_id == int(q)]
    async with db.session() as s:
        return (await s.execute(_list_cols().where(or_(*conds)).order_by(Request.id.desc()).limit(limit))).all()


async def user_requests(user_id: int, limit: int = 10):
    async with db.session() as s:
        r = await s.execute(select(Request).where(Request.user_id == user_id).order_by(Request.id.desc())
                            .limit(limit))
        return list(r.scalars())


async def mark_paid(rid: int, amount: int, admin_id: int) -> str:
    """Атомарно: pending -> paid + запись выплаты. Повторный вызов вернёт 'not_pending'."""
    async with db.session() as s, s.begin():
        req = await s.get(Request, rid)
        if not req:
            return "not_found"
        res = await s.execute(update(Request).where(Request.id == rid, Request.status == "pending")
                              .values(status="paid", paid_amount=amount, processed_at=utcnow(),
                                      processed_by=admin_id))
        if res.rowcount != 1:
            return "not_pending"
        s.add(Payout(request_id=rid, user_id=req.user_id, amount=amount, admin_id=admin_id))
        _log(s, admin_id, "payout", f"request={rid} user={req.user_id} amount_units={amount}")
        return "ok"


async def mark_rejected(rid: int, reason: str | None, admin_id: int) -> str:
    async with db.session() as s, s.begin():
        res = await s.execute(update(Request).where(Request.id == rid, Request.status == "pending")
                              .values(status="rejected", reject_reason=reason, processed_at=utcnow(),
                                      processed_by=admin_id))
        if res.rowcount != 1:
            return "not_pending"
        _log(s, admin_id, "reject", f"request={rid} reason={reason or '-'}")
        return "ok"


# ---------- начисления ----------

async def add_accrual(user_id: int, amount: int, kind: str, admin_id: int, day: str,
                      src_chat: int | None, src_msg: int | None) -> Accrual | None:
    try:
        async with db.session() as s, s.begin():
            a = Accrual(user_id=user_id, amount=amount if kind == "add" else -amount, kind=kind, day=day,
                        admin_id=admin_id, src_chat_id=src_chat, src_msg_id=src_msg)
            s.add(a)
            await s.flush()
            _log(s, admin_id, f"accrual_{kind}", f"id={a.id} user={user_id} amount_units={a.amount}")
        return a
    except IntegrityError:
        return None


async def undo_accrual(acc_id: int, admin_id: int):
    """-> (status, accrual). status: ok | not_found | already | is_revert"""
    async with db.session() as s, s.begin():
        a = await s.get(Accrual, acc_id)
        if not a:
            return "not_found", None
        if a.kind == "revert":
            return "is_revert", a
        res = await s.execute(update(Accrual).where(Accrual.id == acc_id, Accrual.reverted.is_(False))
                              .values(reverted=True))
        if res.rowcount != 1:
            return "already", a
        r = Accrual(user_id=a.user_id, amount=-a.amount, kind="revert", day=a.day, admin_id=admin_id, ref_id=a.id)
        s.add(r)
        await s.flush()
        _log(s, admin_id, "accrual_revert", f"id={r.id} reverts={a.id} user={a.user_id} amount_units={r.amount}")
        return "ok", a


async def user_sums(user_id: int, day: str) -> tuple[int, int]:
    async with db.session() as s:
        base = select(func.coalesce(func.sum(Accrual.amount), 0)).where(Accrual.user_id == user_id)
        today = (await s.execute(base.where(Accrual.day == day))).scalar_one()
        total = (await s.execute(base)).scalar_one()
    return int(today), int(total)


async def ranking(day: str | None):
    tot = func.sum(Accrual.amount)
    q = (select(Accrual.user_id, User.username, User.full_name, tot.label("t"))
         .join(User, User.tg_id == Accrual.user_id))
    if day:
        q = q.where(Accrual.day == day)
    q = q.group_by(Accrual.user_id, User.username, User.full_name).having(tot > 0).order_by(tot.desc())
    async with db.session() as s:
        rows = (await s.execute(q)).all()
    return [(r.user_id, r.username, r.full_name, int(r.t)) for r in rows]


async def totals(day: str) -> tuple[int, int, int]:
    """-> (заработано сегодня, заработано всего, выплачено всего)"""
    async with db.session() as s:
        acc = select(func.coalesce(func.sum(Accrual.amount), 0))
        today = (await s.execute(acc.where(Accrual.day == day))).scalar_one()
        total = (await s.execute(acc)).scalar_one()
        paid = (await s.execute(select(func.coalesce(func.sum(Payout.amount), 0)))).scalar_one()
    return int(today), int(total), int(paid)


async def recent_logs(n: int = 15):
    async with db.session() as s:
        r = await s.execute(select(AdminLog).order_by(AdminLog.id.desc()).limit(n))
        return list(r.scalars())


# ====================================================================================
# Админ-панель: журнал, статистика, пользователи, флаги, жалобы
# ====================================================================================

async def log_action(admin_id: int, action: str, details: str = "") -> None:
    async with db.session() as s, s.begin():
        _log(s, admin_id, action, details)


LOG_GROUPS: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "pay": ("payout", "reject"),
    "acc": ("accrual_add", "accrual_sub", "accrual_revert"),
    "bc": ("broadcast",),
    "cfg": ("setting", "ban", "unban", "complaint", "reset"),
}


async def logs_page(group: str, page: int, size: int):
    actions = LOG_GROUPS.get(group)
    cond = [] if not actions else [AdminLog.action.in_(actions)]
    async with db.session() as s:
        total = (await s.execute(select(func.count()).select_from(AdminLog).where(*cond))).scalar_one()
        rows = (await s.execute(select(AdminLog).where(*cond).order_by(AdminLog.id.desc())
                                .offset(page * size).limit(size))).scalars().all()
    return int(total), list(rows)


async def last_log(action: str) -> AdminLog | None:
    async with db.session() as s:
        r = await s.execute(select(AdminLog).where(AdminLog.action == action).order_by(AdminLog.id.desc()).limit(1))
        return r.scalar_one_or_none()


async def count_users() -> int:
    async with db.session() as s:
        return int((await s.execute(select(func.count()).select_from(User))).scalar_one())


async def activity(hours: int) -> dict[str, int]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with db.session() as s:
        req = (await s.execute(select(func.count()).select_from(Request).where(Request.created_at >= since))).scalar_one()
        users = (await s.execute(select(func.count(func.distinct(Request.user_id))).where(
            Request.created_at >= since))).scalar_one()
        pay = (await s.execute(select(func.count()).select_from(Payout).where(Payout.created_at >= since))).scalar_one()
        acc = (await s.execute(select(func.count()).select_from(Accrual).where(
            Accrual.kind == "add", Accrual.created_at >= since))).scalar_one()
    return {"requests": int(req), "users": int(users), "payouts": int(pay), "accruals": int(acc)}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def search_users(q: str, limit: int = 8):
    q = q.strip()
    if not q:
        return []
    conds = [User.username_lc.like(_like(q.lstrip("@").lower()), escape="\\"),
             User.full_name.ilike(_like(q), escape="\\")]
    if q.isdigit() and len(q) <= 18:
        conds.append(User.tg_id == int(q))
    async with db.session() as s:
        r = await s.execute(select(User).where(or_(*conds)).order_by(User.updated_at.desc()).limit(limit))
        return list(r.scalars())


async def users_page(page: int, size: int, banned_only: bool = False):
    async with db.session() as s:
        base = select(User)
        cnt = select(func.count()).select_from(User)
        if banned_only:
            sub = select(UserFlag.tg_id).where(UserFlag.banned.is_(True))
            base = base.where(User.tg_id.in_(sub))
            cnt = cnt.where(User.tg_id.in_(sub))
        total = (await s.execute(cnt)).scalar_one()
        rows = (await s.execute(base.order_by(User.updated_at.desc()).offset(page * size).limit(size))).scalars().all()
    return int(total), list(rows)


async def user_overview(uid: int, day: str) -> dict | None:
    async with db.session() as s:
        user = await s.get(User, uid)
        if user is None:
            return None
        flag = await s.get(UserFlag, uid)
        rc = {k: int(v) for k, v in (await s.execute(
            select(Request.status, func.count()).where(Request.user_id == uid).group_by(Request.status))).all()}
        paid = (await s.execute(select(func.coalesce(func.sum(Payout.amount), 0)).where(Payout.user_id == uid))).scalar_one()
        last = (await s.execute(select(func.max(Request.created_at)).where(Request.user_id == uid))).scalar_one()
    today, total = await user_sums(uid, day)
    return {"user": user, "banned": bool(flag and flag.banned), "unreachable": bool(flag and flag.unreachable),
            "pending": rc.get("pending", 0), "paid": rc.get("paid", 0), "rejected": rc.get("rejected", 0),
            "paid_sum": int(paid), "today": today, "total": total, "last_request": last}


# ---------- флаги пользователей ----------

async def banned_ids() -> set[int]:
    async with db.session() as s:
        r = await s.execute(select(UserFlag.tg_id).where(UserFlag.banned.is_(True)))
        return {x[0] for x in r.all()}


async def set_banned(uid: int, banned: bool) -> None:
    async with db.session() as s, s.begin():
        f = await s.get(UserFlag, uid)
        if f is None:
            s.add(UserFlag(tg_id=uid, banned=banned, unreachable=False))
        else:
            f.banned = banned


async def mark_unreachable(ids: list[int]) -> None:
    if not ids:
        return
    async with db.session() as s, s.begin():
        for uid in ids:
            f = await s.get(UserFlag, uid)
            if f is None:
                s.add(UserFlag(tg_id=uid, banned=False, unreachable=True))
            else:
                f.unreachable = True


async def broadcast_audience(exclude: int) -> list[int]:
    async with db.session() as s:
        bad = select(UserFlag.tg_id).where(or_(UserFlag.banned.is_(True), UserFlag.unreachable.is_(True)))
        r = await s.execute(select(User.tg_id).where(User.tg_id > 0, User.tg_id != exclude,
                                                     User.tg_id.not_in(bad)).order_by(User.tg_id))
        return [x[0] for x in r.all()]


async def count_banned() -> int:
    return len(await banned_ids())


# ---------- жалобы ----------

async def add_complaint(chat_id: int, chat_title: str, message_id: int, reporter_id: int,
                        target_id: int | None, excerpt: str) -> Complaint | None:
    async with db.session() as s, s.begin():
        dup = (await s.execute(select(Complaint.id).where(Complaint.chat_id == chat_id,
                                                           Complaint.message_id == message_id,
                                                           Complaint.status == "open").limit(1))).first()
        if dup:
            return None
        c = Complaint(chat_id=chat_id, chat_title=chat_title[:256], message_id=message_id,
                      reporter_id=reporter_id, target_id=target_id, excerpt=excerpt[:500])
        s.add(c)
        await s.flush()
        return c


async def count_complaints(status: str = "open") -> int:
    async with db.session() as s:
        return int((await s.execute(select(func.count()).select_from(Complaint).where(
            Complaint.status == status))).scalar_one())


async def complaint_at(offset: int) -> Complaint | None:
    async with db.session() as s:
        r = await s.execute(select(Complaint).where(Complaint.status == "open").order_by(Complaint.id)
                            .offset(offset).limit(1))
        return r.scalar_one_or_none()


async def get_complaint(cid: int) -> Complaint | None:
    async with db.session() as s:
        return await s.get(Complaint, cid)


async def close_complaint(cid: int) -> bool:
    async with db.session() as s, s.begin():
        res = await s.execute(update(Complaint).where(Complaint.id == cid, Complaint.status == "open")
                              .values(status="closed", closed_at=utcnow()))
        return res.rowcount == 1
