import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import select, func, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import get_settings
from bot.models.schema import (
    User,
    Subscription,
    SubscriptionGrant,
    PaymentSlip,
    SubStatus,
    GrantType,
    SlipStatus,
    PlanType,
    PLAN_DETAILS,
)
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime, ensure_utc, format_user_title

logger = logging.getLogger(__name__)
config = get_settings()


@dataclass
class UserReconcileResult:
    user_id: int
    username: Optional[str]
    full_name: str
    joined_at: Optional[datetime]
    
    # Referral stats
    ref_count_old: int
    ref_count_new: int
    ref_bonus_days_old: int
    ref_bonus_days_new: int
    excess_ref_grants_deleted: int
    
    # Entitlement breakdown
    purchase_days: int
    admin_days: int
    referral_days: int
    trial_minutes: int
    total_days: int
    total_minutes: int
    
    # Expiry & Status
    status_old: str
    status_new: str
    expires_at_old: Optional[datetime]
    expires_at_new: Optional[datetime]
    
    # Flags
    ref_stats_changed: bool
    expiry_changed: bool
    status_changed: bool
    is_active: bool
    message: str


def format_reconcile_formula(r: UserReconcileResult) -> str:
    """
    จัดรูปแบบข้อความแจกแจงตามสูตรชัดเจน:
    วันหมดอายุใหม่ = วันที่เข้าครั้งแรก (joined_at) + วันซื้อ + วันแอดมินให้ + วันโบนัสเพื่อน (หลัง Reconcile) + ทดลองฟรี
    """
    u_title = format_user_title(r.full_name, r.username, r.user_id)
    join_str = format_thai_datetime(r.joined_at) if r.joined_at else "ไม่ระบุ (ใช้วันสร้างบัญชี)"
    old_exp = format_thai_datetime(r.expires_at_old) if r.expires_at_old else "ไม่มี"
    new_exp = format_thai_datetime(r.expires_at_new) if r.expires_at_new else "ไม่มี"

    # คำนวณส่วนต่างวันหมดอายุ
    diff_text = "ตรงกันอยู่แล้ว ✅"
    if r.expires_at_old and r.expires_at_new:
        sec_diff = (ensure_utc(r.expires_at_new) - ensure_utc(r.expires_at_old)).total_seconds()
        days_diff = round(sec_diff / 86400.0, 1)
        if days_diff > 0:
            diff_text = f"➕ ขยายเพิ่ม {days_diff} วัน"
        elif days_diff < 0:
            diff_text = f"➖ ปรับลด {abs(days_diff)} วัน ⚠️"
    elif not r.expires_at_old and r.expires_at_new:
        diff_text = "🆕 กำหนดวันหมดอายุใหม่"

    ref_note = f"+{r.referral_days} วัน"
    if r.ref_stats_changed or r.excess_ref_grants_deleted > 0:
        ref_note += f" (ลดจากเดิม {r.ref_bonus_days_old} วัน ⚠️)"

    trial_str = f"+{r.trial_minutes} นาที" if r.trial_minutes > 0 else "0 นาที"

    status_note = ""
    if r.status_changed:
        status_note = f" (สถานะเปลี่ยน {r.status_old} -> {r.status_new} ⚠️)"

    lines = [
        f"👤 <b>{u_title}</b>",
        f"├ 📅 <b>วันที่เข้าครั้งแรก (joined_at):</b> <code>{join_str} น.</code>",
        f"├ 💳 <b>วันซื้อ (Purchase):</b> <b>+{r.purchase_days} วัน</b>",
        f"├ 👑 <b>วันแอดมินให้ (Admin):</b> <b>+{r.admin_days} วัน</b>",
        f"├ 🎁 <b>วันโบนัสเพื่อนจริง:</b> <b>{ref_note}</b>",
        f"├ ⏱️ <b>สิทธิ์ทดลองฟรี (Trial):</b> <b>{trial_str}</b>",
        f"├ 📦 <b>รวมสิทธิ์สุทธิ:</b> <b>{r.total_days} วัน {r.total_minutes} นาที</b>",
        f"├ ⏳ <b>วันหมดอายุเดิม:</b> <code>{old_exp} น.</code>",
        f"└ 🎯 <b>วันหมดอายุใหม่ (ตามสูตร):</b> <code>{new_exp} น.</code> [<i>{diff_text}</i>]{status_note}",
    ]
    return "\n".join(lines)


async def reconcile_user(
    session: AsyncSession,
    user_id: int,
    commit: bool = False,
) -> Optional[UserReconcileResult]:
    """
    Reconcile ข้อมูลและวันหมดอายุของผู้ใช้ 1 คน:
    1. ตรวจสอบประวัติการชวนเพื่อน (Referral):
       - นับเพื่อนที่เข้ามาจริงและยังไม่เคยนับซ้ำ (1 คน = 1 วัน)
       - ปรับลดเลขสถิติ referral_count / referral_bonus_days หากมีประวัติเคยแจกซ้ำ
       - ลบแถว SubscriptionGrant โบนัสชวนเพื่อนที่เกินจริงทิ้ง
    2. คำนวณยอดวันทั้งหมดจาก:
       - วันที่เข้าครั้งแรก (joined_at)
       - ยอดวันจากการซื้อแพ็กเกจ (PURCHASE / PROMOTION)
       - ยอดวันจากแอดมินมอบให้ (ADMIN_GRANT)
       - ยอดวันจากโบนัสชวนเพื่อนที่ผ่านการ Reconcile แล้ว (REFERRAL_BONUS)
       - สิทธิ์ทดลองฟรี (15 นาที)
    3. คำนวณวันหมดอายุ (expires_at) ใหม่ และอัปเดตลงฐานข้อมูล
    """
    now = datetime.now(timezone.utc)

    # 1. โหลดข้อมูล User และ Subscription
    user = await session.get(User, user_id)
    if not user:
        logger.warning(f"[RECONCILE] User ID {user_id} not found in database.")
        return None

    sub = await session.get(Subscription, user_id)

    # 2. ตรวจสอบเพื่อนที่ User คนนี้ชวน (Referral Ledger)
    friends_stmt = (
        select(User)
        .options(selectinload(User.subscription))
        .where(User.referred_by_id == user_id, User.telegram_id != user_id)
    )
    referred_friends = (await session.execute(friends_stmt)).scalars().all()

    # ตรวจสอบเพื่อนที่มีสิทธิ์ได้รับโบนัสจริง (ต้องเคยใช้สิทธิ์ทดลอง หรือเคยเข้าห้อง หรือมีสถานะ active/expired)
    valid_friends = []
    for friend in referred_friends:
        f_sub = friend.subscription
        has_joined = bool(
            friend.trial_used
            or getattr(friend, "referral_rewarded", False)
            or (f_sub and (f_sub.joined_at or f_sub.status in (SubStatus.ACTIVE.value, SubStatus.EXPIRED.value, SubStatus.KICKED.value)))
        )
        if has_joined:
            valid_friends.append(friend)
            if not getattr(friend, "referral_rewarded", False):
                friend.referral_rewarded = True
                session.add(friend)

    true_ref_count = len(valid_friends)
    true_ref_bonus_days = true_ref_count  # 1 เพื่อน = 1 วัน

    ref_count_old = user.referral_count or 0
    ref_bonus_days_old = user.referral_bonus_days or 0
    ref_stats_changed = (ref_count_old != true_ref_count) or (ref_bonus_days_old != true_ref_bonus_days)

    if ref_stats_changed:
        user.referral_count = true_ref_count
        user.referral_bonus_days = true_ref_bonus_days
        session.add(user)

    # 3. จัดการ SubscriptionGrant โบนัสชวนเพื่อนใน Ledger (Deduplicate)
    grants_stmt = (
        select(SubscriptionGrant)
        .where(SubscriptionGrant.user_id == user_id)
        .order_by(SubscriptionGrant.id.asc())
    )
    all_grants = (await session.execute(grants_stmt)).scalars().all()

    ref_grants = [g for g in all_grants if g.grant_type == GrantType.REFERRAL_BONUS.value]
    total_ref_grant_days = sum(g.days for g in ref_grants)

    excess_ref_deleted = 0
    if total_ref_grant_days > true_ref_bonus_days:
        # มีการให้โบนัสชวนเพื่อนเกินจำนวนเพื่อนจริง -> ลบ/ปรับ grant ที่เกินออก
        days_to_remove = total_ref_grant_days - true_ref_bonus_days
        for g in reversed(ref_grants):
            if days_to_remove <= 0:
                break
            if g.days <= days_to_remove:
                days_to_remove -= g.days
                await session.delete(g)
                all_grants.remove(g)
                excess_ref_deleted += 1
            else:
                g.days -= days_to_remove
                days_to_remove = 0
                session.add(g)

    # 4. คำนวณยอดวันและนาทีทั้งหมด (Total Entitled Time)
    purchase_days = sum(g.days for g in all_grants if g.grant_type in (GrantType.PURCHASE.value, GrantType.PROMOTION.value))
    admin_days = sum(g.days for g in all_grants if g.grant_type == GrantType.ADMIN_GRANT.value)
    referral_days = sum(g.days for g in all_grants if g.grant_type == GrantType.REFERRAL_BONUS.value)
    
    trial_minutes = config.TRIAL_DURATION_MINUTES if user.trial_used else 0
    other_minutes = sum(g.minutes for g in all_grants if g.grant_type != GrantType.TRIAL.value)
    total_minutes = trial_minutes + other_minutes

    total_days = purchase_days + admin_days + referral_days

    # 5. คำนวณวันหมดอายุใหม่ (Recalculate expires_at)
    status_old = sub.status if sub else "NONE"
    status_new = status_old
    expires_at_old = sub.expires_at if sub else None
    expires_at_new = expires_at_old
    joined_at = sub.joined_at if (sub and sub.joined_at) else user.created_at
    expiry_changed = False
    status_changed = False
    is_active = False

    if sub:
        if sub.status == SubStatus.ACTIVE.value:
            is_active = True
            base_time = ensure_utc(sub.joined_at) or ensure_utc(user.created_at) or now
            calculated_expiry = base_time + timedelta(days=total_days, minutes=total_minutes)
            
            if calculated_expiry <= now:
                status_new = SubStatus.EXPIRED.value
                status_changed = True
            else:
                status_new = SubStatus.ACTIVE.value

            expires_at_new = calculated_expiry
            expiry_changed = (expires_at_old != expires_at_new)

            sub.expires_at = expires_at_new
            sub.status = status_new
            sub.pending_days = 0
            sub.pending_minutes = 0
            sub.pending_has_value = False
            sub.pending_since = None
            session.add(sub)

        elif sub.status == SubStatus.PENDING.value:
            sub.pending_days = total_days
            sub.pending_minutes = total_minutes
            session.add(sub)

        elif sub.status in (SubStatus.EXPIRED.value, SubStatus.KICKED.value, SubStatus.KICK_FAILED.value):
            base_time = ensure_utc(sub.joined_at) or ensure_utc(user.created_at) or now
            calculated_expiry = base_time + timedelta(days=total_days, minutes=total_minutes)
            
            if calculated_expiry > now:
                # ยังมีวันคงเหลือจากการคำนวณใหม่ -> ปรับเป็น ACTIVE
                status_new = SubStatus.ACTIVE.value
                status_changed = True
                is_active = True
                expires_at_new = calculated_expiry
                expiry_changed = (expires_at_old != expires_at_new)
                sub.status = status_new
                sub.expires_at = expires_at_new
                session.add(sub)
            else:
                expires_at_new = calculated_expiry
                expiry_changed = (expires_at_old != expires_at_new)
                sub.expires_at = expires_at_new
                session.add(sub)

    if commit:
        await session.commit()
    else:
        await session.flush()

    msg_parts = []
    if ref_stats_changed:
        msg_parts.append(f"สถิติชวนเพื่อน: {ref_count_old} -> {true_ref_count} คน (โบนัส {ref_bonus_days_old} -> {true_ref_bonus_days} วัน)")
    if excess_ref_deleted > 0:
        msg_parts.append(f"ลบ grant ชวนเพื่อนซ้ำซ้อน: {excess_ref_deleted} รายการ")
    if expiry_changed:
        old_str = format_thai_datetime(expires_at_old) if expires_at_old else "ไม่มี"
        new_str = format_thai_datetime(expires_at_new) if expires_at_new else "ไม่มี"
        msg_parts.append(f"วันหมดอายุ: {old_str} -> {new_str}")
    if status_changed:
        msg_parts.append(f"สถานะ: {status_old} -> {status_new}")
    
    summary_msg = "; ".join(msg_parts) if msg_parts else "ข้อมูลถูกต้องตรงกันทุกจุดแล้ว ไม่ต้องปรับแก้"

    return UserReconcileResult(
        user_id=user.telegram_id,
        username=user.username,
        full_name=user.full_name,
        joined_at=joined_at,
        ref_count_old=ref_count_old,
        ref_count_new=true_ref_count,
        ref_bonus_days_old=ref_bonus_days_old,
        ref_bonus_days_new=true_ref_bonus_days,
        excess_ref_grants_deleted=excess_ref_deleted,
        purchase_days=purchase_days,
        admin_days=admin_days,
        referral_days=referral_days,
        trial_minutes=trial_minutes,
        total_days=total_days,
        total_minutes=total_minutes,
        status_old=status_old,
        status_new=status_new,
        expires_at_old=expires_at_old,
        expires_at_new=expires_at_new,
        ref_stats_changed=ref_stats_changed,
        expiry_changed=expiry_changed,
        status_changed=status_changed,
        is_active=is_active,
        message=summary_msg,
    )


async def reconcile_all_users(
    session: AsyncSession,
    only_active: bool = False,
    commit: bool = False,
) -> List[UserReconcileResult]:
    """
    Reconcile ผู้ใช้ทั้งหมดในฐานข้อมูล (หรือเฉพาะผู้ใช้ที่ Active):
    - ปรับแก้สถิติการชวนเพื่อนที่ซ้ำซ้อน
    - คำนวณวันหมดอายุใหม่ตามฐานข้อมูลและ Ledger ที่ถูกต้อง
    """
    if only_active:
        stmt = select(User.telegram_id).join(Subscription).where(Subscription.status == SubStatus.ACTIVE.value)
    else:
        stmt = select(User.telegram_id)

    user_ids = (await session.execute(stmt)).scalars().all()
    results: List[UserReconcileResult] = []

    for uid in user_ids:
        res = await reconcile_user(session, uid, commit=False)
        if res:
            results.append(res)

    if commit:
        await session.commit()

    return results
