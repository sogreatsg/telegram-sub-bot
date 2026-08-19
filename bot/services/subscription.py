import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.models.schema import PLAN_DETAILS, GrantType, PlanType, Subscription, SubStatus, SubscriptionGrant, User, get_dynamic_plan_info
from bot.utils.time_utils import ensure_utc, format_remaining_time

logger = logging.getLogger(__name__)
config = get_settings()

# จำนวนชั่วโมงที่ยอมให้โควต้าที่ยังไม่เริ่มนับ (pending) ค้างรอโดยผู้ใช้ยังไม่กดเข้า Channel
PENDING_STALE_HOURS = 48


def parse_plan_days(plan_type: str) -> Tuple[int, int]:
    """
    แปลง plan_type (string เก่าแบบ store-catalog เช่น 'VIP_30D') เป็น (days, minutes)

    ใช้เฉพาะตอน (1) migrate ข้อมูลเก่า และ (2) แปลงตัวเลือกจากเมนูร้านค้า (PLAN_DETAILS) เป็นจำนวนวัน
    ณ จุดที่แอดมิน/ระบบกำลังจะเติมวันให้ผู้ใช้ -- ไม่มีที่ไหนใน runtime ที่ต้อง parse
    ค่าที่เก็บไว้ในฐานข้อมูลย้อนหลังอีกต่อไป (Subscription ไม่มี plan_type แล้ว)
    """
    if plan_type == PlanType.TRIAL_15M.value:
        return 0, config.TRIAL_DURATION_MINUTES

    if plan_type.startswith("REFERRAL_VIP"):
        if "_" in plan_type and plan_type.endswith("D"):
            try:
                return int(plan_type.replace("REFERRAL_VIP_", "").replace("D", "")), 0
            except (ValueError, TypeError):
                logger.warning(f"parse_plan_days: could not parse REFERRAL_VIP days from {plan_type!r}, defaulting to 1")
        return 1, 0

    if plan_type in PLAN_DETAILS:
        info = get_dynamic_plan_info(plan_type)
        days = info.get("days", 0)
        minutes = info.get("minutes", 0)
        if "hours" in info and info["hours"] > 0 and not minutes:
            minutes = info["hours"] * 60
        return days, minutes

    if plan_type.startswith("PROMOTION_"):
        try:
            return int(plan_type.replace("PROMOTION_", "").replace("D", "")), 0
        except (ValueError, TypeError):
            logger.warning(f"parse_plan_days: could not parse PROMOTION days from {plan_type!r}, defaulting to 30")
            return 30, 0

    if plan_type.startswith("MANUAL_VIP_"):
        try:
            return int(plan_type.replace("MANUAL_VIP_", "").replace("D", "")), 0
        except (ValueError, TypeError):
            logger.warning(f"parse_plan_days: could not parse MANUAL_VIP days from {plan_type!r}, defaulting to 30")
            return 30, 0

    logger.warning(f"parse_plan_days: unknown plan_type={plan_type!r}, defaulting to 30 days")
    return 30, 0


def plan_badge_text(plan_type: str) -> str:
    """คืนป้ายชื่อแพ็กเกจสำหรับแสดงผล จาก store-catalog key (ใช้ตอนสร้าง source_label / migrate ข้อมูลเก่า)"""
    if plan_type == PlanType.TRIAL_15M.value:
        return f"ทดลองใช้งานฟรี {config.TRIAL_DURATION_MINUTES} นาที"
    if plan_type.startswith("REFERRAL_VIP"):
        days, _ = parse_plan_days(plan_type)
        return f"สมาชิก 🎁 VIP โบนัสชวนเพื่อน ({days} วัน)"
    if plan_type in PLAN_DETAILS:
        info = get_dynamic_plan_info(plan_type)
        return f"สมาชิก {info['badge']}"
    if plan_type.startswith("PROMOTION_"):
        days, _ = parse_plan_days(plan_type)
        return f"สมาชิก 🔥 โปรโมชั่นพิเศษ {days} วัน"
    if plan_type.startswith("MANUAL_VIP_"):
        days, _ = parse_plan_days(plan_type)
        return f"สมาชิก VIP {days} วัน"
    return f"สมาชิก {plan_type}"


def is_free_grant_plan(plan_type: str) -> bool:
    """Trial/Referral คือสิทธิ์ฟรีที่ไม่มีมูลค่าเงิน (ใช้ตอน migrate ข้อมูลเก่าเท่านั้น)"""
    return plan_type == PlanType.TRIAL_15M.value or plan_type.startswith("REFERRAL_VIP")


def subscription_status_label(sub: Optional[Subscription]) -> Tuple[str, str]:
    """คืนค่า (label, quota_str) สำหรับแสดงผล จากแถว Subscription ปัจจุบันของผู้ใช้ (ใช้ร่วมกันทุกหน้าจอ)"""
    if not sub:
        return "ไม่มีข้อมูล", "-"

    label = sub.source_label or "สมาชิก VIP"

    if sub.status == SubStatus.PENDING.value:
        if sub.pending_days > 0:
            quota_str = f"{sub.pending_days} วัน ({sub.pending_days * 24} ชั่วโมง)"
        elif sub.pending_minutes > 0:
            quota_str = f"{sub.pending_minutes} นาที"
        else:
            quota_str = "-"
    elif sub.expires_at:
        now = datetime.now(timezone.utc)
        quota_str = f"เหลือ {format_remaining_time(sub.expires_at)}" if ensure_utc(sub.expires_at) > now else "หมดอายุแล้ว"
    else:
        quota_str = "-"

    return label, quota_str


def is_pending_stale(sub: Subscription, now: datetime) -> bool:
    """ตรวจสอบว่าโควต้า PENDING ค้างเกิน PENDING_STALE_HOURS โดยยังไม่ถูกใช้งานหรือไม่"""
    since = ensure_utc(sub.pending_since) or ensure_utc(sub.created_at) or now
    return (now - since) > timedelta(hours=PENDING_STALE_HOURS)


def compute_new_expiry(
    current_expires_at: Optional[datetime],
    now: datetime,
    days: int,
    minutes: int = 0,
) -> datetime:
    """
    สูตรคำนวณวันหมดอายุใหม่จุดเดียวที่ใช้ร่วมกันทุกจุดที่เติมวัน (ระบบ 'เติมวัน' ล้วนๆ ไม่มีแนวคิดแพ็กเกจ)
    base_time = max(เวลาหมดอายุเดิม, ตอนนี้) แล้วบวกจำนวนวัน/นาทีที่ได้รับเพิ่ม
    """
    base_time = max(ensure_utc(current_expires_at), now) if current_expires_at else now
    return base_time + timedelta(days=days, minutes=minutes)


@dataclass
class GrantResult:
    subscription: Subscription
    new_expires_at: Optional[datetime]
    is_stack_extension: bool
    is_new_pending: bool
    granted_days: int = 0
    granted_minutes: int = 0


async def _get_or_create_subscription(session: AsyncSession, user_id: int) -> Subscription:
    sub = await session.get(Subscription, user_id)
    if sub is None:
        sub = Subscription(user_id=user_id, status=SubStatus.PENDING.value)
        session.add(sub)
        await session.flush()
    return sub


async def grant_subscription(
    session: AsyncSession,
    *,
    user_id: int,
    days: int,
    minutes: int = 0,
    source_label: str,
    grant_type: str = GrantType.PURCHASE.value,
    referred_friend_id: Optional[int] = None,
    has_value: bool = True,
    is_trial: bool = False,
    is_in_channel: bool = False,
) -> GrantResult:
    """
    "เติมวัน" ให้ผู้ใช้แบบจุดเดียวที่ใช้ร่วมกันทั้งระบบ (ไม่มีแนวคิด 'แพ็กเกจ' ผูกกับผู้ใช้อีกต่อไป)

    ทุกครั้งที่เรียก จะ (1) บันทึกลง SubscriptionGrant ledger เพื่อเก็บประวัติแบบ append-only เสมอ
    และ (2) ปรับ Subscription แถวเดียวของผู้ใช้คนนี้ตามกติกา:

    - ถ้ามี ACTIVE subscription ที่ยังไม่หมดอายุอยู่แล้ว (ไม่ใช่ Trial) จะ "บวกวันเพิ่มทันที"
      โดยไม่รอผู้ใช้กดเข้า Channel หรือรอ background sync งวดถัดไป — เพราะฐานข้อมูล (ACTIVE + ยังไม่หมดอายุ)
      คือ source of truth อยู่แล้ว ไม่ต้องพึ่งการเช็คสถานะ in-channel สดๆ ซึ่งอาจพลาดจาก Telegram API ชั่วคราว
    - ถ้ายังไม่มี ACTIVE และผู้ใช้อยู่ใน Channel อยู่แล้ว (ทราบจาก caller) จะเปิด ACTIVE ให้ทันที
    - ถ้ายังไม่มี ACTIVE และผู้ใช้ยังไม่อยู่ใน Channel จะสะสมเข้า pending_days/pending_minutes
      รอผู้ใช้กดลิงก์เชิญเข้าห้อง (รวมกับโควต้า pending เดิมที่อาจมีอยู่แล้วโดยอัตโนมัติ)
    """
    now = datetime.now(timezone.utc)
    sub = await _get_or_create_subscription(session, user_id)

    session.add(SubscriptionGrant(
        user_id=user_id,
        days=days,
        minutes=minutes,
        source_label=source_label,
        grant_type=grant_type,
        referred_friend_id=referred_friend_id,
        has_value=has_value,
    ))

    is_active_now = sub.status == SubStatus.ACTIVE.value and sub.expires_at and ensure_utc(sub.expires_at) > now and not sub.is_trial_active

    if is_active_now:
        # มี ACTIVE ที่ยังไม่หมดอายุอยู่แล้ว -> บวกวันเพิ่มทันที (รวมโควต้า pending ค้างเก่า ถ้ามี เพื่อไม่ให้ตกหล่น)
        total_days = days + (sub.pending_days or 0)
        total_minutes = minutes + (sub.pending_minutes or 0)
        new_expires_at = compute_new_expiry(sub.expires_at, now, total_days, total_minutes)
        sub.expires_at = new_expires_at
        sub.warned_1d = False
        sub.source_label = source_label
        sub.pending_days = 0
        sub.pending_minutes = 0
        sub.pending_has_value = False
        sub.pending_since = None
        session.add(sub)
        return GrantResult(sub, new_expires_at, True, False, total_days, total_minutes)

    if is_in_channel:
        # ยังไม่มี ACTIVE แต่ผู้ใช้อยู่ใน Channel อยู่แล้ว -> เปิด ACTIVE ให้ทันที (รวมโควต้า pending ค้างเก่าด้วย)
        total_days = days + (sub.pending_days or 0)
        total_minutes = minutes + (sub.pending_minutes or 0)
        new_expires_at = now + timedelta(days=total_days, minutes=total_minutes)
        sub.status = SubStatus.ACTIVE.value
        sub.joined_at = now
        sub.expires_at = new_expires_at
        sub.is_trial_active = is_trial and total_days == 0
        sub.source_label = source_label
        sub.warned_1d = False
        sub.pending_days = 0
        sub.pending_minutes = 0
        sub.pending_has_value = False
        sub.pending_since = None
        session.add(sub)
        return GrantResult(sub, new_expires_at, False, False, total_days, total_minutes)

    # ยังไม่มี ACTIVE และผู้ใช้ยังไม่อยู่ใน Channel -> สะสมเข้าโควต้า pending รอกดเข้าห้อง
    sub.status = SubStatus.PENDING.value
    sub.pending_days = (sub.pending_days or 0) + days
    sub.pending_minutes = (sub.pending_minutes or 0) + minutes
    sub.pending_has_value = bool(sub.pending_has_value) or has_value
    if sub.pending_since is None:
        sub.pending_since = now
    sub.source_label = source_label
    session.add(sub)
    return GrantResult(sub, None, False, True, days, minutes)


async def activate_pending_subscription(
    session: AsyncSession,
    *,
    user_id: int,
) -> Optional[GrantResult]:
    """
    เปิดใช้งานโควต้าที่สะสมไว้ (pending_days/pending_minutes) ทันทีที่ผู้ใช้กดเข้า Channel สำเร็จ
    คืนค่า None หากไม่มีโควต้า pending ให้เปิดใช้งาน (เช่น เข้าห้องโดยไม่มีสิทธิ์)
    """
    now = datetime.now(timezone.utc)
    sub = await session.get(Subscription, user_id)
    if sub is None or sub.status != SubStatus.PENDING.value:
        return None
    if (sub.pending_days or 0) <= 0 and (sub.pending_minutes or 0) <= 0:
        return None

    is_trial_only = sub.pending_days == 0 and sub.pending_minutes > 0
    if is_trial_only:
        # กันการใช้สิทธิ์ทดลองซ้ำ (เช่น ลิงก์เก่าที่หลุดมาใช้ทีหลัง หลังจากใช้สิทธิ์ไปแล้วทางอื่น)
        user = await session.get(User, user_id)
        if user and user.trial_used:
            logger.warning(f"Rejected trial activation for User {user_id}: trial already used")
            sub.status = SubStatus.EXPIRED.value
            sub.pending_days = 0
            sub.pending_minutes = 0
            sub.pending_has_value = False
            sub.pending_since = None
            session.add(sub)
            return None
        if user:
            user.trial_used = True
            session.add(user)

    granted_days = sub.pending_days
    granted_minutes = sub.pending_minutes

    is_stack_extension = False
    if sub.expires_at and ensure_utc(sub.expires_at) > now and not sub.is_trial_active:
        # กรณีพิเศษ: มีเวลาที่ยัง valid ค้างอยู่ (ปกติไม่ควรเกิดพร้อม PENDING แต่กันไว้เพื่อไม่ให้เวลาเดิมหาย)
        new_expires_at = compute_new_expiry(sub.expires_at, now, granted_days, granted_minutes)
        is_stack_extension = True
    else:
        new_expires_at = now + timedelta(days=granted_days, minutes=granted_minutes)

    sub.status = SubStatus.ACTIVE.value
    sub.joined_at = now
    sub.expires_at = new_expires_at
    sub.is_trial_active = is_trial_only
    sub.warned_1d = False
    sub.pending_days = 0
    sub.pending_minutes = 0
    sub.pending_has_value = False
    sub.pending_since = None
    session.add(sub)
    return GrantResult(sub, new_expires_at, is_stack_extension, False, granted_days, granted_minutes)
