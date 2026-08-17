import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType, PLAN_DETAILS, get_dynamic_plan_info
from bot.services.database import get_session
from bot.services.referral import award_referral_bonus

logger = logging.getLogger(__name__)
config = get_settings()

from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

def format_remaining_time(expires_at: Optional[datetime]) -> str:
    """แปลงเวลาคงเหลือให้อ่านง่ายเป็นภาษาไทย"""
    if expires_at is None:
        return "-"
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


async def sync_pending_members(bot: Bot) -> dict:
    """
    ตรวจสอบและซิงค์ผู้ใช้ที่มีสถานะ PENDING ในฐานข้อมูล:
    - หากผู้ใช้เข้าไปอยู่ใน Channel แล้ว -> เปิดใช้งาน ACTIVE ให้ทันที
    - หากหมดอายุแล้วขณะบอทออฟไลน์ -> สั่ง Soft-Kick ทันที
    - หากค้าง PENDING เกิน 24 ชม. และไม่ได้เข้า Channel -> ปรับเป็น EXPIRED
    """
    now = datetime.now(timezone.utc)
    results = {"activated": 0, "kicked_expired": 0, "stale_cleaned": 0}

    async with get_session() as session:
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(Subscription.status == SubStatus.PENDING.value)
        )
        pending_subs = (await session.execute(stmt)).scalars().all()

        if not pending_subs:
            return results

        logger.info(f"Syncing {len(pending_subs)} PENDING subscription(s)...")

        for sub in pending_subs:
            user_id = sub.user_id
            user_obj = sub.user

            try:
                chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
                in_channel = chat_member.status in (
                    ChatMemberStatus.MEMBER,
                    ChatMemberStatus.RESTRICTED,
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                )
            except Exception as e:
                logger.debug(f"Could not check member status for User {user_id}: {e}")
                in_channel = False

            if in_channel:
                # ผู้ใช้อยู่ใน Channel แล้ว -> คำนวณเวลาและ Activate
                joined_time = sub.created_at if sub.created_at else now
                if joined_time.tzinfo is None:
                    joined_time = joined_time.replace(tzinfo=timezone.utc)

                sub.joined_at = joined_time
                referred_by_to_award = None
                friend_snapshot = None

                # ตรวจสอบว่าเคยใช้ Trial ไปแล้วหรือไม่
                if sub.plan_type == PlanType.TRIAL_15M.value and user_obj and user_obj.trial_used:
                    # ไม่อนุญาตให้ใช้ Trial ซ้ำ -> สั่งเตะออกทันที
                    try:
                        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, revoke_messages=False)
                        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, only_if_banned=True)
                        sub.status = SubStatus.KICKED.value
                        results["kicked_expired"] += 1
                        logger.warning(f"[SYNC] User {user_id} attempted trial abuse in channel. Kicked successfully.")
                    except Exception as e:
                        sub.status = SubStatus.KICK_FAILED.value
                        logger.warning(f"[SYNC] Failed to kick trial abuse user {user_id}: {e}")
                    session.add(sub)
                    continue

                plan_title = "สมาชิก VIP"
                duration_str = "30 วัน"
                if sub.plan_type == PlanType.TRIAL_15M.value:
                    sub.expires_at = joined_time + timedelta(minutes=config.TRIAL_DURATION_MINUTES)
                    plan_title = f"ทดลองใช้งานฟรี {config.TRIAL_DURATION_MINUTES} นาที"
                    duration_str = f"{config.TRIAL_DURATION_MINUTES} นาที"
                    if user_obj:
                        if not user_obj.trial_used and user_obj.referred_by_id:
                            referred_by_to_award = user_obj.referred_by_id
                            friend_snapshot = user_obj

                        user_obj.trial_used = True
                        session.add(user_obj)
                elif sub.plan_type == PlanType.REFERRAL_VIP.value:
                    bonus_days = user_obj.referral_bonus_days if (user_obj and user_obj.referral_bonus_days > 0) else 1
                    sub.expires_at = joined_time + timedelta(days=bonus_days)
                    plan_title = f"สมาชิก 🎁 VIP โบนัสชวนเพื่อน ({bonus_days} วัน)"
                    duration_str = f"{bonus_days} วัน"
                elif sub.plan_type in PLAN_DETAILS:
                    p_info = get_dynamic_plan_info(sub.plan_type)
                    sub.expires_at = joined_time + timedelta(days=p_info["days"])
                    plan_title = f"สมาชิก {p_info['badge']}"
                    duration_str = f"{p_info['days']} วัน"
                elif sub.plan_type.startswith("PROMOTION_"):
                    try:
                        days = int(sub.plan_type.replace("PROMOTION_", "").replace("D", ""))
                    except Exception:
                        days = 30
                    sub.expires_at = joined_time + timedelta(days=days)
                    plan_title = f"สมาชิก 🔥 โปรโมชั่นพิเศษ {days} วัน"
                    duration_str = f"{days} วัน"
                elif sub.plan_type.startswith("MANUAL_VIP_"):
                    try:
                        days = int(sub.plan_type.replace("MANUAL_VIP_", "").replace("D", ""))
                    except Exception:
                        days = 30
                    sub.expires_at = joined_time + timedelta(days=days)
                    plan_title = f"สมาชิก VIP {days} วัน"
                    duration_str = f"{days} วัน"
                else:
                    sub.expires_at = joined_time + timedelta(days=30)
                    plan_title = f"สมาชิก {sub.plan_type}"
                    duration_str = "30 วัน"

                if sub.expires_at <= now:
                    # หมดอายุแล้ว -> เตะออกทันที
                    try:
                        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, revoke_messages=False)
                        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, only_if_banned=True)
                        sub.status = SubStatus.KICKED.value
                        results["kicked_expired"] += 1
                        logger.info(f"[SYNC] User {user_id} was PENDING but already expired in channel. Kicked successfully.")
                    except Exception as e:
                        sub.status = SubStatus.KICK_FAILED.value
                        logger.warning(f"[SYNC] Failed to kick expired user {user_id}: {e}")
                else:
                    # ยังไม่หมดอายุ -> เปิดใช้งาน ACTIVE
                    sub.status = SubStatus.ACTIVE.value
                    results["activated"] += 1
                    sub_id = sub.id
                    logger.info(f"[SYNC] Activated PENDING sub #{sub_id} for User {user_id}. Expires at {sub.expires_at}")

                    # ส่ง DM ต้อนรับหาผู้ใช้
                    start_time_thai = format_thai_datetime(joined_time)
                    expires_at_thai = format_thai_datetime(sub.expires_at)
                    try:
                        welcome_dm = (
                            f"🎉 <b>ยินดีต้อนรับเข้าสู่ VIP Channel!</b>\n\n"
                            f"แพ็กเกจ <b>{plan_title}</b> ของคุณเปิดใช้งานเรียบร้อยแล้ว 🚀\n\n"
                            f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
                            f"⏰ <b>เวลาเริ่มต้น:</b> <code>{start_time_thai} น.</code>\n"
                            f"📅 <b>หมดอายุวันที่:</b> <code>{expires_at_thai} น.</code>\n\n"
                            f"ขอให้เพลิดเพลินกับเนื้อหาพิเศษของเราครับ!"
                        )
                        await bot.send_message(chat_id=user_id, text=welcome_dm, parse_mode="HTML")
                    except Exception as e:
                        logger.debug(f"[SYNC] Could not send welcome DM to User {user_id}: {e}")

                    # ส่งแจ้งเตือนเข้ากลุ่ม Admin
                    user_handle = f"@{user_obj.username}" if (user_obj and user_obj.username) else "ไม่มี Username"
                    full_name_safe = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")
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
                        await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_log_msg, parse_mode="HTML")
                    except Exception as e:
                        logger.warning(f"[SYNC] Could not send join notification to Admin Group: {e}")

                session.add(sub)

                if referred_by_to_award and friend_snapshot:
                    try:
                        await award_referral_bonus(bot=bot, referrer_id=referred_by_to_award, friend_user=friend_snapshot)
                    except Exception as e:
                        logger.error(f"[SYNC] Failed to award referral bonus: {e}")

            else:
                # ไม่ได้อยู่ใน Channel -> เช็คว่าเกิน 24 ชม. หรือไม่
                sub_created = sub.created_at if sub.created_at else now
                if sub_created.tzinfo is None:
                    sub_created = sub_created.replace(tzinfo=timezone.utc)

                if now - sub_created > timedelta(hours=24):
                    sub.status = SubStatus.EXPIRED.value
                    session.add(sub)
                    results["stale_cleaned"] += 1
                    logger.info(f"[SYNC] Cleaned up stale PENDING sub for User {user_id}")

    return results


async def check_expired_subscriptions(bot: Bot) -> None:
    """
    Background worker ทำงานตามช่วงเวลาที่กำหนด
    1. ตรวจสอบซิงค์ผู้ใช้ PENDING ที่เข้า Channel แล้ว
    2. ค้นหาแพ็กเกจสมาชิกที่หมดอายุ (ACTIVE หรือ KICK_FAILED ที่ต้องลองเตะซ้ำ)
    ดำเนินการ Soft-kick ออกจาก Channel อัปเดตสถานะในฐานข้อมูล และส่งแจ้งเตือน
    """
    # 1. ซิงค์สถานะ PENDING ก่อน
    try:
        await sync_pending_members(bot)
    except Exception as e:
        logger.error(f"Error in sync_pending_members background worker: {e}", exc_info=True)

    now = datetime.now(timezone.utc)
    logger.debug(f"Checking for expired subscriptions at {now.isoformat()}...")

    async with get_session() as session:
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                or_(
                    Subscription.status == SubStatus.ACTIVE.value,
                    Subscription.status == SubStatus.KICK_FAILED.value,
                ),
                Subscription.expires_at.is_not(None),
                Subscription.expires_at <= now,
            )
        )
        result = await session.execute(stmt)
        expired_subs = result.scalars().all()

        if not expired_subs:
            return

        logger.info(f"Found {len(expired_subs)} expired/retry subscription(s) to process.")

        for sub in expired_subs:
            user_id = sub.user_id
            plan = sub.plan_type
            sub_id = sub.id
            was_already_failed = (sub.status == SubStatus.KICK_FAILED.value)
            user_obj = sub.user
            user_handle = f"@{user_obj.username}" if (user_obj and user_obj.username) else ""
            user_name = html.escape(user_obj.full_name) if (user_obj and user_obj.full_name) else f"User {user_id}"

            logger.info(f"Processing expiration for Subscription ID={sub_id}, User ID={user_id}, Plan={plan}")

            # ตรวจสอบว่าเป็น Admin/Owner ของ Channel หรือไม่ (ถ้าใช่ไม่ต้องเตะ)
            is_channel_admin = False
            try:
                chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
                if chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                    is_channel_admin = True
                    logger.info(f"User ID={user_id} is Channel Administrator/Creator. Skipping soft-kick.")
            except Exception as e:
                logger.debug(f"Could not check chat member status for User {user_id}: {e}")

            if is_channel_admin:
                sub.status = SubStatus.EXPIRED.value
                session.add(sub)
                continue

            # ดำเนินการ Soft-Kick จาก Channel (แบน และ ปลดแบนทันที)
            kicked_successfully = False
            fail_reason = None
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
                err_msg = e.message.lower()
                if any(kw in err_msg for kw in ["not found", "not a member", "not in the chat", "user_not_participant"]):
                    # ผู้ใช้ออกจาก Channel ไปแล้วด้วยตนเอง
                    kicked_successfully = True
                    logger.info(f"User ID={user_id} is already not in Channel: {e.message}")
                else:
                    fail_reason = f"TelegramBadRequest: {e.message}"
                    logger.warning(f"TelegramBadRequest kicking User ID={user_id}: {e.message}")
            except TelegramForbiddenError as e:
                fail_reason = f"TelegramForbiddenError (บอทไม่มีสิทธิ์ Ban Users หรือถูกจำกัด): {e.message}"
                logger.error(f"TelegramForbiddenError kicking User ID={user_id}: {e.message}")
            except TelegramRetryAfter as e:
                fail_reason = f"Rate Limit (รอ {e.retry_after} วินาที)"
                logger.warning(f"Hit Telegram rate limit for User ID={user_id}. Retry after {e.retry_after}s: {e}")
            except Exception as e:
                fail_reason = f"Error: {e}"
                logger.error(f"Unexpected error soft-kicking User ID={user_id}: {e}", exc_info=True)

            # อัปเดตสถานะใน DB
            if kicked_successfully:
                sub.status = SubStatus.KICKED.value
                session.add(sub)

                # ส่งข้อความแจ้งเตือนทาง DM ให้ผู้ใช้
                try:
                    if plan == PlanType.TRIAL_15M.value:
                        dm_text = (
                            f"⏰ <b>หมดเวลาทดลองใช้งานฟรี ({config.TRIAL_DURATION_MINUTES} นาที) แล้วครับ</b>\n\n"
                            "ระยะเวลาทดลองใช้งานฟรีของคุณสิ้นสุดลง และระบบได้นำคุณออกจาก Channel เรียบร้อยแล้วครับ\n\n"
                            "✨ <b>ต้องการเข้าใช้งานต่อเนื่องแบบไม่จำกัด?</b>\n"
                            "คุณสามารถพิมพ์ <b>/start</b> และกดสมัครแพ็กเกจ VIP 30 วันเข้ามาใหม่ได้เลยครับ"
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
            else:
                # บันทึกสถานะ KICK_FAILED เพื่อให้รอบต่อไปลองเตะซ้ำ
                sub.status = SubStatus.KICK_FAILED.value
                session.add(sub)

                # แจ้งเตือนเข้า Admin Group เฉพาะครั้งแรกที่เตะไม่สำเร็จ (ป้องกัน spam ทุก 60 วินาที)
                if not was_already_failed:
                    try:
                        alert_admin_msg = (
                            "⚠️ <b>[แจ้งเตือน] บอทไม่สามารถเตะสมาชิกที่หมดอายุได้!</b>\n\n"
                            f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
                            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                            f"📦 <b>แพ็กเกจ:</b> {plan}\n"
                            f"❌ <b>สาเหตุ:</b> <code>{html.escape(fail_reason or 'Unknown error')}</code>\n\n"
                            "👉 <b>แนวทางแก้ไข:</b>\n"
                            "1. ตรวจสอบสิทธิ์ของบอทใน Channel ว่าเปิด <b>'Ban Users'</b> หรือไม่\n"
                            f"2. หรือใช้คำสั่ง <code>/kick {user_id}</code> เพื่อบังคับเตะ"
                        )
                        await bot.send_message(
                            chat_id=config.ADMIN_GROUP_ID,
                            text=alert_admin_msg,
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.error(f"Failed to send kick failure notification to Admin Group: {e}")


async def build_active_members_report(bot: Optional[Bot] = None) -> str:
    """สร้างรายงานสรุปรายชื่อสมาชิกที่กำลัง Active อยู่ในระบบ พร้อมเปรียบเทียบกับจำนวนใน Channel จริง"""
    now = datetime.now(timezone.utc)
    now_thai = format_thai_datetime(now)

    # 1. ดึงจำนวนสมาชิกจริงจาก Telegram Channel (ไม่นับ Admin และ Bot)
    channel_member_count = None
    if bot:
        try:
            total_count = await bot.get_chat_member_count(chat_id=config.CHANNEL_ID)
            admins = await bot.get_chat_administrators(chat_id=config.CHANNEL_ID)
            # ใน Telegram Channel บอทและแอดมินจะรวมอยู่ในรายชื่อ administrators
            channel_member_count = total_count - len(admins)
        except Exception as e:
            logger.warning(f"Could not fetch chat member count for channel {config.CHANNEL_ID}: {e}")

    async with get_session() as session:
        # 2. ดึง Subscription ที่ ACTIVE อยู่ทั้งหมด พร้อมข้อมูล User
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

        # 3. ดึงรายการที่ KICK_FAILED (ค้างเตะไม่สำเร็จ)
        stmt_failed = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(Subscription.status == SubStatus.KICK_FAILED.value)
            .order_by(Subscription.expires_at.asc())
        )
        failed_subs = (await session.execute(stmt_failed)).scalars().all()

        # 4. ดึงรายการ PENDING (รอผู้ใช้กดลิงก์เข้า Channel)
        stmt_pending = (
            select(Subscription)
            .where(Subscription.status == SubStatus.PENDING.value)
        )
        pending_subs = (await session.execute(stmt_pending)).scalars().all()

        total_active = len(active_subs)
        total_failed = len(failed_subs)
        total_pending = len(pending_subs)

        report = (
            f"📊 <b>รายงานสรุปสถานะสมาชิก Channel VIP</b>\n"
            f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 <b>สมาชิก Active ในระบบบอท:</b> <b>{total_active} คน</b>\n"
        )

        if channel_member_count is not None:
            report += f"📱 <b>จำนวนสมาชิกใน Channel จริง:</b> <b>{channel_member_count} คน</b>\n"

        if total_pending > 0:
            report += f"🟡 <b>สมาชิกรอกดเข้าร่วม (Pending):</b> <b>{total_pending} คน</b>\n"

        if total_failed > 0:
            report += f"🔴 <b>สมาชิกค้างเตะไม่สำเร็จ (Kick Failed):</b> <b>{total_failed} คน</b> ⚠️\n"

        report += "━━━━━━━━━━━━━━━━━━━━\n\n"

        # แสดงรายการที่เตะไม่สำเร็จก่อน (ถ้ามี) เพื่อให้แอดมินแก้ไข
        if total_failed > 0:
            report += "⚠️ <b>[สมาชิกที่หมดอายุแต่เตะไม่สำเร็จ]:</b>\n"
            for sub in failed_subs:
                user = sub.user
                user_handle = f"@{user.username}" if (user and user.username) else ""
                full_name = html.escape(user.full_name) if (user and user.full_name) else f"User {sub.user_id}"
                exp_thai = format_thai_datetime(sub.expires_at)
                report += (
                    f"• <b>{full_name}</b> ({user_handle}) | ID: <code>{sub.user_id}</code>\n"
                    f"  หมดอายุเมื่อ: <code>{exp_thai} น.</code> (ใช้ <code>/kick {sub.user_id}</code>)\n"
                )
            report += "\n"

        if total_active == 0:
            report += "ℹ️ <i>ขณะนี้ไม่มีสมาชิกที่อยู่ในสถานะ Active ในระบบ</i>\n\n"
        else:
            report += "📋 <b>รายชื่อสมาชิก Active ปัจจุบัน:</b>\n\n"
            for i, sub in enumerate(active_subs, start=1):
                user = sub.user
                user_handle = f"@{user.username}" if (user and user.username) else "ไม่มี Username"
                full_name = html.escape(user.full_name) if (user and user.full_name) else "ไม่ระบุชื่อ"
                
                if sub.plan_type == PlanType.TRIAL_15M.value:
                    plan_name = f"ทดลองใช้ฟรี {config.TRIAL_DURATION_MINUTES} นาที"
                elif sub.plan_type in PLAN_DETAILS:
                    plan_name = get_dynamic_plan_info(sub.plan_type)["badge"]
                elif sub.plan_type.startswith("MANUAL_VIP_"):
                    plan_name = sub.plan_type.replace("MANUAL_VIP_", "VIP ").replace("D", " วัน")
                else:
                    plan_name = sub.plan_type

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
        report += "🤖 <i>ระบบจัดการสมาชิก BareLive Membership Bot</i>"

        if channel_member_count is not None and bot:
            expected_count = total_active + total_failed
            if channel_member_count != expected_count:
                alert_msg = (
                    "🚨 <b>แจ้งเตือนความผิดปกติของจำนวนสมาชิก!</b>\n\n"
                    f"👥 จำนวนคนใน Channel จริง (ไม่รวม Admin/Bot): <b>{channel_member_count} คน</b>\n"
                    f"📝 จำนวนคนที่ควรจะมี (Active + เตะไม่สำเร็จ): <b>{expected_count} คน</b>\n\n"
                    "⚠️ <i>จำนวนคนไม่ตรงกับในระบบ! อาจมีคนแอบอยู่ในห้องโดยไม่มีแพ็กเกจ หรือมีคนถูกดึงเข้าห้องโดยไม่ผ่านบอท กรุณาตรวจสอบด่วนครับ!</i>"
                )
                try:
                    await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=alert_msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Failed to send discrepancy alert: {e}")

        return report


async def send_daily_active_summary(bot: Bot) -> None:
    """ฟังก์ชันส่งรายงานสรุปสมาชิก Active ประจำวันเวลา 23:59 น. เข้ากลุ่ม Admin"""
    logger.info("Executing daily active members summary job (23:59 Bangkok time)...")
    try:
        report_text = await build_active_members_report(bot=bot)
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
