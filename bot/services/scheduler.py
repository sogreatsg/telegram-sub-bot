import logging
import html
from datetime import datetime, timezone, timedelta
from typing import Optional
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime, ensure_utc, split_text_chunks, format_user_title

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


def get_plan_display_name(plan_type: str) -> tuple[str, str]:
    """คืนค่า (plan_title, duration_str) สำหรับแสดงผล"""
    if plan_type == PlanType.TRIAL_15M.value:
        return f"ทดลองใช้งานฟรี {config.TRIAL_DURATION_MINUTES} นาที", f"{config.TRIAL_DURATION_MINUTES} นาที"
    elif plan_type == PlanType.REFERRAL_VIP.value:
        return "สมาชิก 🎁 VIP โบนัสชวนเพื่อน", "1 วัน"
    elif plan_type in PLAN_DETAILS:
        p_info = get_dynamic_plan_info(plan_type)
        return f"สมาชิก {p_info['badge']}", f"{p_info['days']} วัน"
    elif plan_type.startswith("PROMOTION_"):
        try:
            days = int(plan_type.replace("PROMOTION_", "").replace("D", ""))
        except Exception:
            days = 30
        return f"สมาชิก 🔥 โปรโมชั่นพิเศษ {days} วัน", f"{days} วัน"
    elif plan_type.startswith("MANUAL_VIP_"):
        try:
            days = int(plan_type.replace("MANUAL_VIP_", "").replace("D", ""))
        except Exception:
            days = 30
        return f"สมาชิก VIP {days} วัน", f"{days} วัน"
    else:
        return f"สมาชิก {plan_type}", "30 วัน"


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

        # จัดกลุ่ม PENDING ตาม user_id
        user_pending_map = {}
        for sub in pending_subs:
            user_pending_map.setdefault(sub.user_id, []).append(sub)

        for user_id, u_subs in user_pending_map.items():
            primary_sub = u_subs[0]
            user_obj = primary_sub.user

            try:
                chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
                in_channel = chat_member.status in (
                    ChatMemberStatus.MEMBER,
                    ChatMemberStatus.RESTRICTED,
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                )
                tg_user = getattr(chat_member, "user", None)
                if tg_user and user_obj:
                    if tg_user.full_name and user_obj.full_name != tg_user.full_name:
                        user_obj.full_name = tg_user.full_name
                    if tg_user.username and user_obj.username != tg_user.username:
                        user_obj.username = tg_user.username
                    session.add(user_obj)
            except Exception as e:
                logger.debug(f"Could not check member status for User {user_id}: {e}")
                in_channel = False

            if in_channel:
                # ผู้ใช้อยู่ใน Channel แล้ว -> รวมโควต้า PENDING ทั้งหมดของผู้ใช้นี้
                referred_by_to_award = None
                friend_snapshot = None
                total_days = 0
                total_minutes = 0
                primary_plan_badge = None
                valid_subs = []

                for sub in u_subs:
                    # ตรวจสอบว่าเคยใช้ Trial ไปแล้วหรือไม่
                    if sub.plan_type == PlanType.TRIAL_15M.value and user_obj and user_obj.trial_used:
                        sub.status = SubStatus.EXPIRED.value
                        session.add(sub)
                        continue

                    valid_subs.append(sub)
                    p_type = sub.plan_type
                    if p_type == PlanType.TRIAL_15M.value:
                        total_minutes += config.TRIAL_DURATION_MINUTES
                        if user_obj:
                            if not user_obj.trial_used and user_obj.referred_by_id:
                                referred_by_to_award = user_obj.referred_by_id
                                friend_snapshot = user_obj
                            user_obj.trial_used = True
                            session.add(user_obj)
                        if not primary_plan_badge:
                            primary_plan_badge = f"ทดลองใช้งานฟรี {config.TRIAL_DURATION_MINUTES} นาที"
                    elif p_type.startswith("REFERRAL_VIP"):
                        bonus_days = 1
                        if "_" in p_type and p_type.endswith("D"):
                            try:
                                bonus_days = int(p_type.replace("REFERRAL_VIP_", "").replace("D", ""))
                            except Exception:
                                bonus_days = 1
                        total_days += bonus_days
                        if not primary_plan_badge:
                            primary_plan_badge = f"สมาชิก 🎁 VIP โบนัสชวนเพื่อน ({bonus_days} วัน)"
                    elif p_type in PLAN_DETAILS:
                        p_info = get_dynamic_plan_info(p_type)
                        total_days += p_info["days"]
                        if not primary_plan_badge:
                            primary_plan_badge = f"สมาชิก {p_info['badge']}"
                    elif p_type.startswith("PROMOTION_"):
                        try:
                            days = int(p_type.replace("PROMOTION_", "").replace("D", ""))
                        except Exception:
                            days = 30
                        total_days += days
                        if not primary_plan_badge:
                            primary_plan_badge = f"สมาชิก 🔥 โปรโมชั่นพิเศษ {days} วัน"
                    elif p_type.startswith("MANUAL_VIP_"):
                        try:
                            days = int(p_type.replace("MANUAL_VIP_", "").replace("D", ""))
                        except Exception:
                            days = 30
                        total_days += days
                        if not primary_plan_badge:
                            primary_plan_badge = f"สมาชิก VIP {days} วัน"
                    else:
                        total_days += 30
                        if not primary_plan_badge:
                            primary_plan_badge = f"สมาชิก {p_type}"

                if not valid_subs:
                    continue

                # 1. ตรวจสอบว่าผู้ใช้มี Subscription ที่ ACTIVE อยู่แล้วหรือไม่
                active_stmt = (
                    select(Subscription)
                    .where(
                        Subscription.user_id == user_id,
                        Subscription.status == SubStatus.ACTIVE.value,
                        Subscription.expires_at > now,
                    )
                    .order_by(Subscription.id.desc())
                )
                existing_active = (await session.execute(active_stmt)).scalar_one_or_none()

                if existing_active and existing_active.plan_type != PlanType.TRIAL_15M.value:
                    base_time = max(ensure_utc(existing_active.expires_at), now)
                    existing_active.status = SubStatus.EXPIRED.value
                    session.add(existing_active)
                    is_stack_extension = True
                else:
                    if existing_active:
                        existing_active.status = SubStatus.EXPIRED.value
                        session.add(existing_active)
                    base_time = now
                    is_stack_extension = False

                final_expires_at = base_time + timedelta(days=total_days, minutes=total_minutes)
                joined_time = now

                for sub in valid_subs:
                    sub.joined_at = joined_time

                if len(valid_subs) > 1 or is_stack_extension:
                    duration_str = f"{total_days} วัน (ต่อเวลาสะสมรวม {len(valid_subs)} รายการ)" if is_stack_extension else f"{total_days} วัน (รวม {len(valid_subs)} แพ็กเกจ)"
                    plan_title = f"สมาชิก VIP รวมสะสม {total_days} วัน"
                else:
                    duration_str = f"{total_days} วัน" if total_days > 0 else f"{total_minutes} นาที"
                    plan_title = primary_plan_badge or f"สมาชิก VIP ({duration_str})"

                if final_expires_at <= now:
                    # หมดอายุแล้ว -> เตะออกทันที
                    try:
                        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, revoke_messages=False)
                        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, only_if_banned=True)
                        for s in valid_subs:
                            s.status = SubStatus.KICKED.value
                            s.expires_at = final_expires_at
                            session.add(s)
                        results["kicked_expired"] += 1
                        logger.info(f"[SYNC] User {user_id} was PENDING but already expired in channel. Kicked successfully.")

                        # ส่งแจ้งเตือนเข้ากลุ่ม Admin
                        start_time_thai = format_thai_datetime(joined_time)
                        expires_at_thai = format_thai_datetime(final_expires_at)
                        user_handle_sync = f"@{user_obj.username}" if (user_obj and user_obj.username) else "ไม่มี Username"
                        full_name_sync = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")
                        admin_kick_msg = (
                            "🚪 <b>สมาชิกหมดอายุและถูกเตะออกจาก Channel แล้ว!</b>\n\n"
                            f"👤 <b>ผู้ใช้งาน:</b> {full_name_sync} ({user_handle_sync})\n"
                            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                            f"📦 <b>แพ็กเกจ:</b> <b>{plan_title}</b> ({duration_str})\n"
                            f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
                            f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
                            f"🆔 <b>Subscription ID:</b> <code>#{primary_sub.id}</code>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "ℹ️ <i>ตรวจพบสมาชิกหมดอายุในห้องและนำออกจาก Channel เรียบร้อย</i>"
                        )
                        try:
                            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_kick_msg, parse_mode="HTML")
                        except Exception:
                            pass
                    except Exception as e:
                        for s in valid_subs:
                            s.status = SubStatus.KICK_FAILED.value
                            session.add(s)
                        logger.warning(f"[SYNC] Failed to kick expired user {user_id}: {e}")
                else:
                    # ยังไม่หมดอายุ -> เปิดใช้งาน ACTIVE
                    primary_sub.status = SubStatus.ACTIVE.value
                    primary_sub.expires_at = final_expires_at
                    primary_sub.warned_1d = False
                    session.add(primary_sub)

                    for s in valid_subs[1:]:
                        s.status = SubStatus.EXPIRED.value
                        s.expires_at = final_expires_at
                        session.add(s)

                    results["activated"] += 1
                    sub_id = primary_sub.id
                    logger.info(f"[SYNC] Activated PENDING sub #{sub_id} for User {user_id}. Expires at {final_expires_at}")

                    # ส่ง DM ต้อนรับหาผู้ใช้
                    start_time_thai = format_thai_datetime(joined_time)
                    expires_at_thai = format_thai_datetime(final_expires_at)
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

                if referred_by_to_award and friend_snapshot:
                    try:
                        await award_referral_bonus(bot=bot, referrer_id=referred_by_to_award, friend_user=friend_snapshot)
                    except Exception as e:
                        logger.error(f"[SYNC] Failed to award referral bonus: {e}")

            else:
                for sub in u_subs:
                    sub_created = sub.created_at if sub.created_at else now
                    if sub_created.tzinfo is None:
                        sub_created = sub_created.replace(tzinfo=timezone.utc)

                    if now - sub_created > timedelta(hours=48):
                        sub.status = SubStatus.EXPIRED.value
                        session.add(sub)
                        results["stale_cleaned"] += 1
                    logger.info(f"[SYNC] Cleaned up stale PENDING sub for User {user_id}")

    return results


async def check_expiring_soon_subscriptions(bot: Bot) -> None:
    """
    ตรวจสอบสมาชิกที่กำลังจะหมดอายุล่วงหน้า 1 วัน (24 ชั่วโมง)
    และส่งข้อความแจ้งเตือนทาง DM แนะนำให้กด /start หรือกดปุ่มต่ออายุสมาชิก
    """
    now = datetime.now(timezone.utc)
    one_day_later = now + timedelta(hours=24)

    async with get_session() as session:
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at > now,
                Subscription.expires_at <= one_day_later,
                Subscription.warned_1d == False,
                Subscription.plan_type != PlanType.TRIAL_15M.value,
            )
        )
        expiring_subs = (await session.execute(stmt)).scalars().all()

        if not expiring_subs:
            return

        logger.info(f"Found {len(expiring_subs)} subscription(s) expiring within 24 hours. Sending 1-day warning...")

        for sub in expiring_subs:
            user_id = sub.user_id
            user_obj = sub.user
            sub_id = sub.id
            plan = sub.plan_type

            # ตรวจสอบว่ามี ACTIVE subscription อื่นที่มีวันหมดอายุเกิน 24 ชั่วโมงข้างหน้าหรือไม่
            other_active_later_stmt = (
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.id != sub_id,
                    Subscription.status == SubStatus.ACTIVE.value,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at > one_day_later,
                )
            )
            has_later_sub = (await session.execute(other_active_later_stmt)).scalars().first()
            if has_later_sub:
                sub.warned_1d = True
                session.add(sub)
                continue

            plan_title, duration_str = get_plan_display_name(plan)
            expires_at_thai = format_thai_datetime(sub.expires_at)
            time_rem = format_remaining_time(sub.expires_at)
            user_name = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")

            # มาร์กว่าได้ส่งแจ้งเตือน 1 วันแล้ว
            sub.warned_1d = True
            session.add(sub)

            # ส่งข้อความเตือนเข้า DM ของผู้ใช้
            warn_text = (
                "⚠️ <b>[แจ้งเตือน] แพ็กเกจสมาชิก VIP ของคุณจะหมดอายุในอีก 24 ชั่วโมง!</b>\n\n"
                f"เรียนคุณ {user_name} 👋\n"
                f"แพ็กเกจ <b>{plan_title}</b> ของคุณกำลังจะหมดอายุใน:\n"
                f"📅 <code>{expires_at_thai} น.</code> (เหลือเวลาประมาณ {time_rem})\n\n"
                "✨ <b>เพื่อการรับชมและเข้าถึง Channel VIP อย่างต่อเนื่อง:</b>\n"
                "คุณสามารถต่อเวลาสะสมล่วงหน้าได้ทันที โดยพิมพ์ <b>/start</b> หรือกดปุ่ม <b>'💳 ต่ออายุสมาชิก VIP'</b> ด้านล่างนี้ครับ\n\n"
                "💡 <i>(วันใหม่จะถูกนำไปบวกเพิ่มสะสมกับเวลาที่เหลืออยู่อัตโนมัติ โดยคุณไม่ต้องออกจากห้อง VIP ครับ)</i>"
            )

            renew_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💳 ต่ออายุสมาชิก VIP", callback_data="menu:packages"),
                        InlineKeyboardButton(text="📊 เช็คสถานะ", callback_data="menu:status"),
                    ],
                ]
            )

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=warn_text,
                    reply_markup=renew_keyboard,
                    parse_mode="HTML",
                )
                logger.info(f"Sent 1-day expiration warning DM to User ID={user_id} (Sub #{sub_id}).")
            except TelegramForbiddenError:
                logger.info(f"User ID={user_id} has blocked the bot. Skipping 1-day warning.")
            except Exception as e:
                logger.warning(f"Failed to send 1-day warning DM to User ID={user_id}: {e}")


async def check_expired_subscriptions(bot: Bot) -> None:
    """
    Background worker ทำงานตามช่วงเวลาที่กำหนด
    1. ตรวจสอบซิงค์ผู้ใช้ PENDING ที่เข้า Channel แล้ว
    2. ส่งแจ้งเตือนผู้ใช้ที่กำลังจะหมดอายุล่วงหน้า 24 ชม.
    3. ค้นหาแพ็กเกจสมาชิกที่หมดอายุ (ACTIVE หรือ KICK_FAILED ที่ต้องลองเตะซ้ำ)
    ดำเนินการ Soft-kick ออกจาก Channel อัปเดตสถานะในฐานข้อมูล และส่งแจ้งเตือน
    """
    # 1. ซิงค์สถานะ PENDING ก่อน
    try:
        await sync_pending_members(bot)
    except Exception as e:
        logger.error(f"Error in sync_pending_members background worker: {e}", exc_info=True)

    # 2. แจ้งเตือนสมาชิกที่กำลังจะหมดอายุล่วงหน้า 24 ชม.
    try:
        await check_expiring_soon_subscriptions(bot)
    except Exception as e:
        logger.error(f"Error in check_expiring_soon_subscriptions: {e}", exc_info=True)

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

            # ตรวจสอบก่อนว่า User คนนี้มี ACTIVE subscription อื่นที่ยังไม่หมดอายุหรือไม่!
            other_active_stmt = (
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.id != sub_id,
                    Subscription.status == SubStatus.ACTIVE.value,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at > now,
                )
            )
            other_active = (await session.execute(other_active_stmt)).scalars().first()
            if other_active:
                logger.info(f"[EXPIRE_CHECK] User {user_id} has another active sub #{other_active.id} (expires {other_active.expires_at}). Marking #{sub_id} as EXPIRED without kicking.")
                sub.status = SubStatus.EXPIRED.value
                session.add(sub)
                continue

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

                # 1. ส่งข้อความแจ้งเตือนทาง DM ให้ผู้ใช้
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

                # 2. ส่งข้อความแจ้งเตือนเข้ากลุ่ม Admin พร้อม Start/End และข้อมูลแพ็กเกจ
                plan_title, duration_str = get_plan_display_name(plan)
                start_time_thai = format_thai_datetime(sub.joined_at or sub.created_at)
                expires_at_thai = format_thai_datetime(sub.expires_at)
                user_handle_str = f"@{user_obj.username}" if (user_obj and user_obj.username) else "ไม่มี Username"
                full_name_str = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")

                admin_kick_msg = (
                    "🚪 <b>สมาชิกหมดอายุและถูกเตะออกจาก Channel แล้ว!</b>\n\n"
                    f"👤 <b>ผู้ใช้งาน:</b> {full_name_str} ({user_handle_str})\n"
                    f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"📦 <b>แพ็กเกจ:</b> <b>{plan_title}</b> ({duration_str})\n"
                    f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
                    f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
                    f"🆔 <b>Subscription ID:</b> <code>#{sub_id}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "ℹ️ <i>บอทได้นำผู้ใช้ออกจาก Channel และส่งข้อความแจ้งเตือนทาง DM เรียบร้อย</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=config.ADMIN_GROUP_ID,
                        text=admin_kick_msg,
                        parse_mode="HTML",
                    )
                    logger.info(f"Sent expiration kick notification to Admin Group for User ID={user_id} (Sub #{sub_id}).")
                except Exception as e:
                    logger.warning(f"Failed to send expiration kick log to Admin Group: {e}")
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
    if bot:
        try:
            await sync_pending_members(bot)
        except Exception as e:
            logger.warning(f"Failed to sync pending members before report: {e}")

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
            .options(selectinload(Subscription.user))
            .where(Subscription.status == SubStatus.PENDING.value)
        )
        pending_subs = (await session.execute(stmt_pending)).scalars().all()

        # Deduplicate active subscriptions by user_id
        user_active_map = {}
        for sub in active_subs:
            user_active_map.setdefault(sub.user_id, []).append(sub)

        unique_active_subs = []
        for uid, u_subs in user_active_map.items():
            best_sub = max(u_subs, key=lambda s: ensure_utc(s.expires_at))
            unique_active_subs.append(best_sub)

        unique_active_subs.sort(key=lambda s: ensure_utc(s.expires_at))

        # ตรวจสอบสถานะว่าอยู่ในห้อง Channel จริงหรือไม่
        in_channel_active_subs = []
        left_channel_active_subs = []

        if bot:
            for sub in unique_active_subs:
                is_in = False
                try:
                    cm = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=sub.user_id)
                    if cm.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        is_in = True
                    tg_u = getattr(cm, "user", None)
                    if tg_u and sub.user:
                        if tg_u.full_name and sub.user.full_name != tg_u.full_name:
                            sub.user.full_name = tg_u.full_name
                        if tg_u.username and sub.user.username != tg_u.username:
                            sub.user.username = tg_u.username
                        session.add(sub.user)
                except Exception:
                    is_in = False

                if is_in:
                    in_channel_active_subs.append(sub)
                else:
                    left_channel_active_subs.append(sub)
        else:
            in_channel_active_subs = unique_active_subs

        # จัดกลุ่ม PENDING รายบุคคล
        user_pending_map = {}
        for psub in pending_subs:
            user_pending_map.setdefault(psub.user_id, []).append(psub)

        total_active = len(unique_active_subs)
        total_in_channel = len(in_channel_active_subs)
        total_left = len(left_channel_active_subs)
        total_failed = len(failed_subs)
        total_pending = len(user_pending_map)

        report = (
            f"📊 <b>รายงานสรุปสถานะสมาชิก Channel VIP</b>\n"
            f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 <b>สมาชิก Active ในระบบบอท:</b> <b>{total_active} คน</b> "
            f"<i>(อยู่ในห้อง {total_in_channel} คน | ออกจากห้อง {total_left} คน)</i>\n"
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
                u_header = format_user_title(user.full_name if user else None, user.username if user else None, sub.user_id)
                exp_thai = format_thai_datetime(sub.expires_at)
                report += (
                    f"• {u_header}\n"
                    f"  หมดอายุเมื่อ: <code>{exp_thai} น.</code> (ใช้ <code>/kick {sub.user_id}</code>)\n"
                )
            report += "\n"

        # แสดงรายการสมาชิก Active
        if total_active == 0:
            report += "ℹ️ <i>ขณะนี้ไม่มีสมาชิกที่อยู่ในสถานะ Active ในระบบ</i>\n\n"
        else:
            report += "📋 <b>รายชื่อสมาชิก Active ปัจจุบัน:</b>\n\n"
            for i, sub in enumerate(unique_active_subs, start=1):
                user = sub.user
                u_header = format_user_title(user.full_name if user else None, user.username if user else None, sub.user_id)
                
                plan_name, _ = get_plan_display_name(sub.plan_type)
                start_time = format_thai_datetime(sub.joined_at)
                end_time = format_thai_datetime(sub.expires_at)
                remaining = format_remaining_time(sub.expires_at)
                
                # แสดงสถานะการอยู่ในห้อง
                if bot:
                    channel_badge = "🟢 ใน Channel" if sub in in_channel_active_subs else "⚪ ออกจากห้องแล้ว"
                else:
                    channel_badge = "🟢 ACTIVE"

                report += (
                    f"<b>{i}.</b> {u_header} — {channel_badge}\n"
                    f"   • <b>แพ็กเกจ:</b> {plan_name}\n"
                    f"   • 🟢 <b>เริ่ม (Start):</b> <code>{start_time} น.</code>\n"
                    f"   • 🔴 <b>หมดอายุ (End):</b> <code>{end_time} น.</code>\n"
                    f"   • ⏳ <b>เวลาคงเหลือ:</b> {remaining}\n"
                    f"   • 🆔 <b>Sub ID:</b> <code>#{sub.id}</code>\n\n"
                )

        # แสดงรายการสมาชิกรอกดเข้าร่วม (Pending)
        if total_pending > 0:
            report += "🟡 <b>รายชื่อสมาชิกรอกดเข้าร่วม (Pending):</b>\n\n"
            for p_idx, (p_uid, p_list) in enumerate(user_pending_map.items(), start=1):
                p_user = p_list[0].user
                p_u_header = format_user_title(p_user.full_name if p_user else None, p_user.username if p_user else None, p_uid)
                p_plan_names = []
                for s in p_list:
                    p_title, _ = get_plan_display_name(s.plan_type)
                    p_plan_names.append(p_title)
                p_plans_str = ", ".join(p_plan_names)
                report += (
                    f"<b>{p_idx}.</b> {p_u_header}\n"
                    f"   • <b>โควต้ารอใช้งาน:</b> {p_plans_str}\n"
                    f"   • 🟡 <b>สถานะ:</b> ออกลิงก์แล้ว-รอกดเข้าห้อง\n\n"
                )

        report += "━━━━━━━━━━━━━━━━━━━━\n"

        if channel_member_count is not None and bot:
            expected_in_channel = total_in_channel + total_failed
            if channel_member_count > expected_in_channel:
                diff_count = channel_member_count - expected_in_channel
                report += (
                    "🚨 <b>แจ้งเตือนความผิดปกติของจำนวนสมาชิก!</b>\n\n"
                    f"👥 จำนวนคนใน Channel จริง (ไม่รวม Admin/Bot): <b>{channel_member_count} คน</b>\n"
                    f"📝 จำนวนคนที่ควรจะมี (Active ในห้อง + เตะไม่สำเร็จ): <b>{expected_in_channel} คน</b>\n\n"
                    f"⚠️ <i>จำนวนคนในห้องจริงมากกว่าในระบบ {diff_count} คน! อาจมีคนแอบอยู่ในห้องโดยไม่มีแพ็กเกจ หรือมีคนถูกดึงเข้าห้องโดยไม่ผ่านบอท แนะนำให้ใช้คำสั่ง <code>/deep_scan</code> เพื่อตรวจสอบครับ</i>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                )
            elif channel_member_count == expected_in_channel:
                report += "✅ <i>จำนวนคนใน Channel ตรงกับสมาชิกที่มีสิทธิ์ในระบบ 100%</i>\n━━━━━━━━━━━━━━━━━━━━\n"

        report += "🤖 <i>ระบบจัดการสมาชิก BareLive Membership Bot</i>"
        return report


async def send_daily_active_summary(bot: Bot) -> None:
    """ฟังก์ชันส่งรายงานสรุปสมาชิก Active ประจำวันเวลา 23:59 น. เข้ากลุ่ม Admin"""
    logger.info("Executing daily active members summary job (23:59 Bangkok time)...")
    try:
        report_text = await build_active_members_report(bot=bot)
        chunks = split_text_chunks(report_text, max_chunk_size=3800)
        for chunk in chunks:
            await bot.send_message(
                chat_id=config.ADMIN_GROUP_ID,
                text=chunk,
                parse_mode="HTML",
            )
        logger.info(f"Successfully sent daily active summary report ({len(chunks)} chunks) to Admin Group {config.ADMIN_GROUP_ID}")
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
