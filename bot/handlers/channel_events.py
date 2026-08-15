import logging
import html
from datetime import datetime, timedelta, timezone
from aiogram import Router, Bot
from aiogram.types import ChatMemberUpdated
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType
from bot.services.database import get_session

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="channel_events")

BANGKOK_TZ = timezone(timedelta(hours=7))


def format_thai_datetime(dt: datetime) -> str:
    """แปลงเวลาเป็นเวลาไทย (UTC+7) รูปแบบ วัน/เดือน/ปี ชั่วโมง:นาที:วินาที"""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    thai_dt = dt.astimezone(BANGKOK_TZ)
    return thai_dt.strftime("%d/%m/%Y %H:%M:%S")


@router.chat_member()
async def handle_channel_member_updated(event: ChatMemberUpdated, bot: Bot):
    """
    ตรวจจับเหตุการณ์ ChatMemberUpdated เมื่อผู้ใช้กดเข้าร่วม Channel ผ่านลิงก์ 1 ครั้ง
    เริ่มต้นเปิดใช้งาน Subscription และนับเวลาถอยหลัง 
    ส่งข้อความแจ้งเตือนทาง DM และส่งข้อมูลบันทึกเข้ากลุ่ม Admin (เวลาไทย)
    """
    # ตรวจจับเฉพาะ Event ใน Channel ที่กำหนดเท่านั้น
    if event.chat.id != config.CHANNEL_ID:
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    user = event.new_chat_member.user

    logger.info(
        f"Channel {event.chat.id} member status update for User {user.id} ({user.full_name}): "
        f"{old_status} -> {new_status}"
    )

    # ตรวจสอบการเข้าร่วม Channel (เปลี่ยนสถานะเป็น member หรือ administrator)
    is_joined = (
        old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED)
        and new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    )

    if not is_joined:
        return

    user_id = user.id
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # ค้นหา Subscription ล่าสุดที่มีสถานะ PENDING ของผู้ใช้คนนี้
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubStatus.PENDING.value,
            )
            .order_by(Subscription.id.desc())
        )
        result = await session.execute(stmt)
        sub = result.scalars().first()

        if not sub:
            logger.info(f"User {user_id} joined channel, but has no PENDING subscription.")
            return

        # คำนวณเวลาหมดอายุตามการตั้งค่า (รองรับทั้งโหมดทดสอบและ Production)
        sub.joined_at = now
        sub.status = SubStatus.ACTIVE.value

        if sub.plan_type == PlanType.TRIAL_15M.value:
            trial_minutes = config.TRIAL_DURATION_MINUTES
            sub.expires_at = now + timedelta(minutes=trial_minutes)
            plan_title = f"ทดลองใช้งานฟรี {trial_minutes} นาที"
            duration_str = f"{trial_minutes} นาที"

            # บันทึกว่าผู้ใช้ได้ใช้สิทธิ์ trial แล้วเมื่อเข้าร่วม Channel จริง
            user_stmt = select(User).where(User.telegram_id == user_id)
            user_obj = (await session.execute(user_stmt)).scalar_one_or_none()
            if user_obj:
                user_obj.trial_used = True
                session.add(user_obj)

        elif sub.plan_type == PlanType.MONTHLY_30D.value:
            paid_minutes = config.PAID_DURATION_MINUTES
            sub.expires_at = now + timedelta(minutes=paid_minutes)
            if paid_minutes >= 1440:
                days = paid_minutes // 1440
                plan_title = f"สมาชิก VIP {days} วัน"
                duration_str = f"{days} วัน"
            else:
                plan_title = f"สมาชิก VIP {paid_minutes} นาที (โหมดทดสอบ)"
                duration_str = f"{paid_minutes} นาที"
        else:
            sub.expires_at = now + timedelta(minutes=config.PAID_DURATION_MINUTES)
            plan_title = sub.plan_type
            duration_str = f"{config.PAID_DURATION_MINUTES} นาที"

        session.add(sub)
        sub_id = sub.id
        start_time_thai = format_thai_datetime(now)
        expires_at_thai = format_thai_datetime(sub.expires_at)
        logger.info(
            f"Activated Subscription ID={sub_id} for User ID={user_id}. "
            f"Plan={sub.plan_type}, Expires={expires_at_thai}"
        )

    # 1. ส่งข้อความต้อนรับและแจ้งเวลาหมดอายุให้ผู้ใช้ทาง DM (เวลาไทย)
    try:
        welcome_dm = (
            f"🎉 <b>ยินดีต้อนรับเข้าสู่ VIP Channel!</b>\n\n"
            f"แพ็กเกจ <b>{plan_title}</b> ของคุณเปิดใช้งานเรียบร้อยแล้ว 🚀\n\n"
            f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
            f"⏰ <b>เวลาเริ่มต้น:</b> <code>{start_time_thai} น.</code>\n"
            f"📅 <b>หมดอายุวันที่:</b> <code>{expires_at_thai} น.</code>\n\n"
            f"ขอให้เพลิดเพลินกับเนื้อหาพิเศษของเราครับ!"
        )
        await bot.send_message(
            chat_id=user_id,
            text=welcome_dm,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not send channel join welcome DM to User ID={user_id}: {e}")

    # 2. ส่งข้อความแจ้งเตือนเข้ากลุ่ม Admin สำหรับตรวจสอบสมาชิกล่าสุด (เวลาไทย)
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    admin_log_msg = (
        "🚪 <b>มีสมาชิกกดเข้าร่วม Channel แล้ว!</b>\n\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📦 <b>แผนที่ใช้งาน:</b> <b>{plan_title}</b>\n"
        f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
        f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
        f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
        f"🆔 <b>Subscription ID:</b> <code>#{sub_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <i>บันทึกประวัติการเข้าใช้งานลงฐานข้อมูลเรียบร้อย</i>"
    )

    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_log_msg,
            parse_mode="HTML",
        )
        logger.info(f"Sent channel join audit log to Admin Group {config.ADMIN_GROUP_ID} for User {user_id}")
    except Exception as e:
        logger.warning(f"Could not send join log to Admin Group {config.ADMIN_GROUP_ID}: {e}")
