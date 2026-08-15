import logging
import html
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType
from bot.services.database import get_session

logger = logging.getLogger(__name__)
config = get_settings()

BANGKOK_TZ = timezone(timedelta(hours=7))


def format_thai_datetime(dt: datetime) -> str:
    """แปลงเวลาเป็นเวลาไทย (UTC+7) รูปแบบ วัน/เดือน/ปี ชั่วโมง:นาที:วินาที"""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    thai_dt = dt.astimezone(BANGKOK_TZ)
    return thai_dt.strftime("%d/%m/%Y %H:%M:%S")


def format_remaining_time(expires_at: datetime) -> str:
    """แปลงเวลาคงเหลือให้อ่านง่ายเป็นภาษาไทย"""
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    diff = expires_at - now
    if diff.total_seconds() <= 0:
        return "หมดอายุแล้ว"
    
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} วัน")
    if hours > 0:
        parts.append(f"{hours} ชม.")
    if minutes > 0 or (days == 0 and hours == 0):
        parts.append(f"{minutes} นาที")
    if days == 0 and hours == 0 and minutes < 5:
        parts.append(f"{seconds} วินาที")
    
    return " ".join(parts)


async def check_expired_subscriptions(bot: Bot) -> None:
    """
    Background worker ทำงานตามช่วงเวลาที่กำหนด
    ค้นหาแพ็กเกจสมาชิกที่หมดอายุ ดำเนินการ Soft-kick ออกจาก Channel
    อัปเดตสถานะในฐานข้อมูลเป็น EXPIRED/KICKED และส่งแจ้งเตือนทาง DM
    """
    now = datetime.now(timezone.utc)
    logger.debug(f"Checking for expired subscriptions at {now.isoformat()}...")

    async with get_session() as session:
        stmt = (
            select(Subscription)
            .where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at <= now,
            )
        )
        result = await session.execute(stmt)
        expired_subs = result.scalars().all()

        if not expired_subs:
            return

        logger.info(f"Found {len(expired_subs)} expired subscription(s) to process.")

        for sub in expired_subs:
            user_id = sub.user_id
            plan = sub.plan_type
            sub_id = sub.id

            logger.info(f"Processing expiration for Subscription ID={sub_id}, User ID={user_id}, Plan={plan}")

            # 1. ดำเนินการ Soft-Kick จาก Channel (แบน และ ปลดแบนทันที)
            kicked_successfully = False
            try:
                await bot.ban_chat_member(
                    chat_id=config.CHANNEL_ID,
                    user_id=user_id,
                    revoke_messages=False,
                )
                await bot.unban_chat_member(
                    chat_id=config.CHANNEL_ID,
                    user_id=user_id,
                    only_if_banned=True,
                )
                kicked_successfully = True
                logger.info(f"Successfully soft-kicked User ID={user_id} from Channel {config.CHANNEL_ID}.")
            except TelegramBadRequest as e:
                logger.warning(
                    f"TelegramBadRequest while kicking User ID={user_id} (may have already left): {e.message}"
                )
            except TelegramForbiddenError as e:
                logger.error(
                    f"TelegramForbiddenError: Bot lacks permission to kick users in Channel {config.CHANNEL_ID}: {e.message}"
                )
            except TelegramRetryAfter as e:
                logger.warning(f"Hit Telegram rate limit. Retry after {e.retry_after}s: {e}")
            except Exception as e:
                logger.error(f"Unexpected error soft-kicking User ID={user_id}: {e}", exc_info=True)

            # 2. อัปเดตสถานะใน DB
            sub.status = SubStatus.KICKED.value if kicked_successfully else SubStatus.EXPIRED.value
            session.add(sub)

            # 3. ส่งข้อความแจ้งเตือนทาง DM ภาษาไทย
            try:
                if plan == PlanType.TRIAL_15M.value:
                    dm_text = (
                        f"⏰ <b>หมดเวลาทดลองใช้งานฟรี ({config.TRIAL_DURATION_MINUTES} นาที) แล้วครับ</b>\n\n"
                        "ระยะเวลาทดลองใช้งานฟรีของคุณสิ้นสุดลง และระบบได้นำคุณออกจาก Channel เรียบร้อยแล้วครับ\n\n"
                        "✨ <b>ต้องการเข้าใช้งานต่อเนื่องแบบไม่จำกัด?</b>\n"
                        "คุณสามารถพิมพ์ <b>/start</b> และกดสมัครแพ็กเกจ 30 วันเข้ามาใหม่ได้เลยครับ"
                    )
                else:
                    dm_text = (
                        "⏰ <b>แพ็กเกจสมาชิก VIP ของคุณหมดอายุแล้วครับ</b>\n\n"
                        "ระยะเวลาสมาชิกของคุณสิ้นสุดลง และระบบได้นำคุณออกจาก Channel เรียบร้อยแล้วครับ ขอบคุณที่ร่วมเป็นสมาชิกกับเรา!\n\n"
                        "🔄 <b>ต่ออายุสมาชิก:</b>\n"
                        "คุณสามารถต่ออายุสมาชิก VIP ได้ง่ายๆ เพียงพิมพ์ <b>/start</b> และกดสมัครแพ็กเกจ 30 วันเข้ามาใหม่ได้เลยครับ"
                    )

                await bot.send_message(
                    chat_id=user_id,
                    text=dm_text,
                    parse_mode="HTML",
                )
                logger.info(f"Sent expiration DM notification to User ID={user_id}.")
            except TelegramForbiddenError:
                logger.info(f"Cannot send expiration DM: User ID={user_id} has blocked the bot.")
            except Exception as e:
                logger.warning(f"Failed to send expiration DM to User ID={user_id}: {e}")


async def build_active_members_report() -> str:
    """สร้างรายงานสรุปรายชื่อสมาชิกที่กำลัง Active อยู่ในระบบ"""
    now = datetime.now(timezone.utc)
    now_thai = format_thai_datetime(now)

    async with get_session() as session:
        # ดึง Subscription ที่ ACTIVE อยู่ทั้งหมด พร้อมข้อมูล User
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.asc())
        )
        result = await session.execute(stmt)
        active_subs = result.scalars().all()

        total_active = len(active_subs)

        report = (
            f"📊 <b>รายงานสรุปสมาชิก Active ประจำวัน (23:59 น.)</b>\n"
            f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>\n"
            f"👥 <b>จำนวนสมาชิก Active ทั้งหมด:</b> <b>{total_active} คน</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        if total_active == 0:
            report += "ℹ️ <i>ขณะนี้ไม่มีสมาชิกที่อยู่ในสถานะ Active ในระบบ</i>"
            return report

        for i, sub in enumerate(active_subs, start=1):
            user = sub.user
            user_handle = f"@{user.username}" if (user and user.username) else "ไม่มี Username"
            full_name = html.escape(user.full_name) if (user and user.full_name) else "ไม่ระบุชื่อ"
            
            if sub.plan_type == PlanType.TRIAL_15M.value:
                plan_name = f"ทดลองใช้ฟรี {config.TRIAL_DURATION_MINUTES} นาที"
            else:
                paid_min = config.PAID_DURATION_MINUTES
                plan_name = f"สมาชิก VIP {paid_min // 1440} วัน" if paid_min >= 1440 else f"สมาชิก VIP {paid_min} นาที (Test)"

            start_time = format_thai_datetime(sub.joined_at)
            end_time = format_thai_datetime(sub.expires_at)
            remaining = format_remaining_time(sub.expires_at)

            report += (
                f"<b>{i}. {full_name}</b> ({user_handle})\n"
                f"   • <b>User ID:</b> <code>{sub.user_id}</code>\n"
                f"   • <b>แพ็กเกจ:</b> {plan_name}\n"
                f"   • 🟢 <b>เริ่ม (Start):</b> <code>{start_time} น.</code>\n"
                f"   • 🔴 <b>หมดอายุ (End):</b> <code>{end_time} น.</code>\n"
                f"   • ⏳ <b>เวลาคงเหลือ:</b> {remaining}\n"
                f"   • 🆔 <b>Sub ID:</b> <code>#{sub.id}</code>\n\n"
            )

        report += "━━━━━━━━━━━━━━━━━━━━\n"
        report += "🤖 <i>ส่งรายงานอัตโนมัติประจำวันโดยระบบ Membership Bot</i>"
        return report


async def send_daily_active_summary(bot: Bot) -> None:
    """ฟังก์ชันส่งรายงานสรุปสมาชิก Active ประจำวันเวลา 23:59 น. เข้ากลุ่ม Admin"""
    logger.info("Executing daily active members summary job (23:59 Bangkok time)...")
    try:
        report_text = await build_active_members_report()
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=report_text,
            parse_mode="HTML",
        )
        logger.info(f"Successfully sent daily active summary report to Admin Group {config.ADMIN_GROUP_ID}")
    except Exception as e:
        logger.error(f"Failed to send daily active summary report to Admin Group: {e}", exc_info=True)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """สร้างและตั้งค่า APScheduler AsyncIOScheduler ตามช่วงเวลาที่กำหนด"""
    scheduler = AsyncIOScheduler(timezone=BANGKOK_TZ)

    # 1. Job ตรวจสอบและเตะสมาชิกที่หมดอายุ (ทำงานทุกๆ ช่วงเวลาที่กำหนด)
    scheduler.add_job(
        check_expired_subscriptions,
        trigger="interval",
        seconds=config.CHECK_INTERVAL_SECONDS,
        args=[bot],
        id="check_expired_subscriptions_job",
        name="Check and kick expired channel subscriptions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 2. Job ส่งรายงานสรุปสมาชิก Active ประจำวันเวลา 23:59 น. (เวลาไทย)
    scheduler.add_job(
        send_daily_active_summary,
        trigger=CronTrigger(hour=23, minute=59, timezone=BANGKOK_TZ),
        args=[bot],
        id="daily_active_summary_job",
        name="Send daily active members summary to Admin Group at 23:59",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
