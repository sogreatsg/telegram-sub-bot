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
from sqlalchemy import select, or_, func
from sqlalchemy.orm import selectinload

from bot.config import get_settings
from bot.models.schema import Subscription, SubStatus, PaymentSlip, SlipStatus, get_dynamic_plan_info, ChatMessage
from bot.services.database import get_session
from bot.services.referral import award_referral_bonus
from bot.services.subscription import PENDING_STALE_HOURS, activate_pending_subscription, is_pending_stale
from bot.services.channel_service import (
    kick_user_from_all_target_channels,
    check_user_in_channel,
    check_user_in_target_channels,
    check_user_presence_all_channels,
    format_user_channel_presence,
    get_all_target_channel_ids,
    get_channel_label,
    is_secondary_channel,
)

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


async def _send_stale_pending_alert(bot: Bot, sub: Subscription) -> None:
    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=(
                "⚠️ <b>[แจ้งเตือน] มีโควต้าที่มีมูลค่าค้างอยู่ แต่ผู้ใช้ยังไม่กดเข้า Channel เกิน "
                f"{PENDING_STALE_HOURS} ชม.!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
                f"👤 <b>User ID:</b> <code>{sub.user_id}</code>\n"
                f"📦 <b>โควต้าค้าง:</b> {sub.pending_days} วัน {sub.pending_minutes} นาที ({sub.source_label})\n"
                f"📅 <b>เริ่มรอตั้งแต่:</b> <code>{format_thai_datetime(sub.pending_since)} น.</code>\n\n"
                "ระบบจะ<b>ไม่</b>ยกเลิกโควต้านี้ให้อัตโนมัติเพราะเป็นแพ็กเกจที่มีมูลค่า "
                "กรุณาตรวจสอบว่าผู้ใช้กดลิงก์เชิญเข้า Channel แล้วหรือยัง หรือบอทมีสิทธิ์ตรวจสอบสมาชิกในห้องหรือไม่"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"[SYNC] Failed to send stale-pending alert to Admin Group: {e}")


async def sync_pending_members(bot: Bot) -> dict:
    """
    ตรวจสอบและซิงค์ผู้ใช้ที่มีสถานะ PENDING ในฐานข้อมูล:
    - หากผู้ใช้เข้าไปอยู่ใน Channel แล้ว -> เปิดใช้งาน ACTIVE ให้ทันที
    - หากหมดอายุแล้วขณะบอทออฟไลน์ -> สั่ง Soft-Kick ทันที
    - หากค้าง PENDING เกิน PENDING_STALE_HOURS และไม่ได้เข้า Channel:
        - โควต้าฟรี (Trial/Referral) -> ปรับเป็น EXPIRED อัตโนมัติ
        - โควต้าที่มีมูลค่า (ซื้อ/แอดมินให้) -> แจ้งเตือนแอดมินแทน ไม่ auto-expire
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

            in_channel = False
            joined_channel_id = None

            # ตรวจสอบทุก Target Channel ที่บอทดูแล
            for cid in get_all_target_channel_ids():
                is_mem, status, tg_user = await check_user_in_channel(bot, cid, user_id)
                if is_mem:
                    in_channel = True
                    joined_channel_id = cid
                    if is_secondary_channel(cid) and user_obj:
                        user_obj.is_moved_to_secondary = True
                        user_obj.assigned_channel = "SECONDARY"
                        session.add(user_obj)
                    if tg_user and user_obj:
                        if tg_user.full_name and user_obj.full_name != tg_user.full_name:
                            user_obj.full_name = tg_user.full_name
                        if tg_user.username and user_obj.username != tg_user.username:
                            user_obj.username = tg_user.username
                        session.add(user_obj)
                    break

            if not in_channel:
                if not is_pending_stale(sub, now):
                    continue

                if not sub.pending_has_value:
                    # Trial/Referral เป็นสิทธิ์ฟรี ไม่มีมูลค่าเงิน ปล่อยหมดอายุอัตโนมัติได้อย่างปลอดภัย
                    sub.status = SubStatus.EXPIRED.value
                    sub.pending_days = 0
                    sub.pending_minutes = 0
                    sub.pending_has_value = False
                    sub.pending_since = None
                    session.add(sub)
                    results["stale_cleaned"] += 1
                    logger.info(f"[SYNC] Cleaned up stale free-grant PENDING for User {user_id}")
                elif not sub.stale_alerted:
                    # แพ็กเกจนี้มีมูลค่า ห้าม auto-expire เงียบๆ เพราะลูกค้าอาจเสียสิทธิ์ที่จ่ายเงินไปโดยไม่มีใครรู้
                    sub.stale_alerted = True
                    session.add(sub)
                    logger.warning(
                        f"[SYNC] PENDING for User {user_id} is stale (>{PENDING_STALE_HOURS}h) but has value. "
                        f"NOT auto-expired -- alerting admin instead."
                    )
                    await _send_stale_pending_alert(bot, sub)
                continue

            # ผู้ใช้อยู่ใน Channel แล้ว -> เปิดใช้งานโควต้าที่สะสมไว้ทันที
            trial_already_used_before = bool(user_obj.trial_used) if user_obj else True
            grant = await activate_pending_subscription(session, user_id=user_id)
            if not grant:
                continue

            plan_title = grant.subscription.source_label or "สมาชิก VIP"
            if grant.granted_days > 0:
                duration_str = f"{grant.granted_days} วัน"
            elif grant.granted_minutes >= 60 and grant.granted_minutes % 60 == 0:
                duration_str = f"{grant.granted_minutes // 60} ชั่วโมง"
            else:
                duration_str = f"{grant.granted_minutes} นาที"
            joined_time = now
            final_expires_at = grant.new_expires_at
            channel_label = get_channel_label(joined_channel_id or config.CHANNEL_ID)

            if final_expires_at <= now:
                # กรณี edge-case: โควต้าที่ได้เท่ากับ 0 พอดี -> เตะออกจากทุกห้องทันที
                try:
                    await kick_user_from_all_target_channels(bot, user_id)
                    grant.subscription.status = SubStatus.KICKED.value
                    session.add(grant.subscription)
                    results["kicked_expired"] += 1
                    logger.info(f"[SYNC] User {user_id} was PENDING but already expired in channel. Kicked successfully from all target channels.")

                    start_time_thai = format_thai_datetime(joined_time)
                    expires_at_thai = format_thai_datetime(final_expires_at)
                    user_handle_sync = f"@{user_obj.username}" if (user_obj and user_obj.username) else "ไม่มี Username"
                    full_name_sync = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")
                    admin_kick_msg = (
                        "🚪 <b>สมาชิกหมดอายุและถูกเตะออกจาก Channel แล้ว!</b>\n\n"
                        f"👤 <b>ผู้ใช้งาน:</b> {full_name_sync} ({user_handle_sync})\n"
                        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                        f"📢 <b>Channel:</b> {channel_label}\n"
                        f"📦 <b>แพ็กเกจ:</b> <b>{plan_title}</b> ({duration_str})\n"
                        f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
                        f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "ℹ️ <i>ตรวจพบสมาชิกหมดอายุในห้องและนำออกจาก Channel เรียบร้อย</i>"
                    )
                    try:
                        await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_kick_msg, parse_mode="HTML")
                    except Exception:
                        pass
                except Exception as e:
                    grant.subscription.status = SubStatus.KICK_FAILED.value
                    session.add(grant.subscription)
                    logger.warning(f"[SYNC] Failed to kick expired user {user_id}: {e}")
                continue

            results["activated"] += 1
            logger.info(f"[SYNC] Activated PENDING subscription for User {user_id} in {channel_label}. Expires at {final_expires_at}")

            # ส่ง DM ต้อนรับหาผู้ใช้
            start_time_thai = format_thai_datetime(joined_time)
            expires_at_thai = format_thai_datetime(final_expires_at)
            try:
                welcome_dm = (
                    f"🎉 <b>ยินดีต้อนรับเข้าสู่ {channel_label}!</b>\n\n"
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
            is_sec = is_secondary_channel(joined_channel_id or config.CHANNEL_ID)
            header_title = "🌟 <b>[Target Channel] มีสมาชิกกดเข้าร่วม Channel ใหม่แล้ว!</b>" if is_sec else "🚪 <b>มีสมาชิกกดเข้าร่วม Channel แล้ว!</b>"

            admin_log_msg = (
                f"{header_title}\n\n"
                f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
                f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                f"📢 <b>Channel:</b> <b>{channel_label}</b> (<code>{joined_channel_id}</code>)\n"
                f"📦 <b>แผนที่ใช้งาน:</b> <b>{plan_title}</b>\n"
                f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
                f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
                f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ <i>บันทึกประวัติการเข้าใช้งานลงฐานข้อมูลเรียบร้อย</i>"
            )
            try:
                await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_log_msg, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"[SYNC] Could not send join notification to Admin Group: {e}")

            # มอบรางวัล Referral Bonus หากมีคนแนะนำและยังไม่เคยให้รางวัล
            if user_obj and user_obj.referred_by_id and not getattr(user_obj, "referral_rewarded", False):
                try:
                    await award_referral_bonus(bot=bot, referrer_id=user_obj.referred_by_id, friend_user=user_obj)
                except Exception as e:
                    logger.error(f"[SYNC] Failed to award referral bonus: {e}")

    return results


async def check_expiring_soon_subscriptions(bot: Bot) -> None:
    """
    ตรวจสอบสมาชิกที่กำลังจะหมดอายุล่วงหน้า และส่งข้อความแจ้งเตือนทาง DM แนะนำให้ต่ออายุ
    1. แจ้งเตือนล่วงหน้า 24 ชั่วโมง: สำหรับแพ็กเกจระยะยาว (> 24 ชม.) ที่เหลือเวลา <= 24 ชม. และยังไม่ได้เตือน 24h
    2. แจ้งเตือนล่วงหน้า 1 ชั่วโมง (ส่งให้ทุกแพ็กเกจ): เมื่อเหลือเวลา <= 1 ชม. และยังไม่ได้เตือน 1h
    """
    now = datetime.now(timezone.utc)
    one_day_later = now + timedelta(hours=24)
    one_hour_later = now + timedelta(hours=1)

    renew_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 ต่ออายุสมาชิก VIP", callback_data="menu:packages"),
                InlineKeyboardButton(text="📊 เช็คสถานะ", callback_data="menu:my_status"),
            ],
        ]
    )

    async with get_session() as session:
        # ดึงสมาชิก Active ที่ยังไม่หมดอายุและไม่ใช่ Trial
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at > now,
                Subscription.expires_at <= one_day_later,
                Subscription.is_trial_active == False,
            )
        )
        active_subs = (await session.execute(stmt)).scalars().all()

        if not active_subs:
            return

        for sub in active_subs:
            user_id = sub.user_id
            user_obj = sub.user
            plan_title = sub.source_label or "สมาชิก VIP"
            user_name = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")
            expires_at_thai = format_thai_datetime(sub.expires_at)
            time_rem = format_remaining_time(sub.expires_at)

            expires_at_utc = ensure_utc(sub.expires_at)
            joined_ref = ensure_utc(sub.joined_at) or ensure_utc(sub.created_at) or now
            total_duration = (expires_at_utc - joined_ref).total_seconds()
            is_long_plan = total_duration > (24 * 3600)  # แพ็กเกจ > 24 ชม. (เช่น 3 วัน, 10 วัน, 30 วัน)

            warn_type = None  # "1h" หรือ "1d"

            # --- ด่านที่ 1: ตรวจสอบแจ้งเตือน 1 ชั่วโมง (ส่งให้ทุกแพ็กเกจเมื่อเหลือ <= 1 ชม.) ---
            if expires_at_utc <= one_hour_later:
                if not getattr(sub, "warned_1h", False):
                    warn_type = "1h"
                    sub.warned_1h = True
                    sub.warned_1d = True  # มาร์ก 1d เป็น True ด้วยเพื่อไม่ให้ส่งซ้ำ
            # --- ด่านที่ 2: ตรวจสอบแจ้งเตือน 24 ชั่วโมง (เฉพาะแพ็กเกจระยะยาว > 24 ชม.) ---
            elif is_long_plan and expires_at_utc <= one_day_later:
                if not sub.warned_1d:
                    warn_type = "1d"
                    sub.warned_1d = True

            if not warn_type:
                continue

            session.add(sub)

            if warn_type == "1h":
                warn_headline = f"🚨 <b>[แจ้งเตือนด่วน] แพ็กเกจสมาชิก VIP ของคุณจะหมดอายุในอีก {time_rem} (1 ชั่วโมงสุดท้าย)!</b>"
            else:
                warn_headline = f"⚠️ <b>[แจ้งเตือน] แพ็กเกจสมาชิก VIP ของคุณจะหมดอายุในอีก {time_rem}!</b>"

            warn_text = (
                f"{warn_headline}\n\n"
                f"เรียนคุณ {user_name} 👋\n"
                f"แพ็กเกจ <b>{plan_title}</b> ของคุณกำลังจะหมดอายุใน:\n"
                f"📅 <code>{expires_at_thai} น.</code> (เหลือเวลาประมาณ {time_rem})\n\n"
                "✨ <b>เพื่อการรับชมและเข้าถึง Channel VIP อย่างต่อเนื่อง:</b>\n"
                "คุณสามารถต่อเวลาสะสมล่วงหน้าได้ทันที โดยพิมพ์ <b>/start</b> หรือกดปุ่ม <b>'💳 ต่ออายุสมาชิก VIP'</b> ด้านล่างนี้ครับ\n\n"
                "💡 <i>(วันใหม่จะถูกนำไปบวกเพิ่มสะสมกับเวลาที่เหลืออยู่อัตโนมัติ โดยคุณไม่ต้องออกจากห้อง VIP ครับ)</i>"
            )

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=warn_text,
                    reply_markup=renew_keyboard,
                    parse_mode="HTML",
                )
                logger.info(f"Sent {warn_type} expiration warning DM ({time_rem} remaining) to User ID={user_id}.")
            except TelegramForbiddenError:
                logger.info(f"User ID={user_id} has blocked the bot. Skipping warning.")
            except Exception as e:
                logger.warning(f"Failed to send warning DM to User ID={user_id}: {e}")

        await session.commit()


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
            plan_title = sub.source_label or "สมาชิก VIP"
            is_trial = sub.is_trial_active
            was_already_failed = (sub.status == SubStatus.KICK_FAILED.value)
            user_obj = sub.user
            user_handle = f"@{user_obj.username}" if (user_obj and user_obj.username) else ""
            user_name = html.escape(user_obj.full_name) if (user_obj and user_obj.full_name) else f"User {user_id}"

            logger.info(f"Processing expiration for User ID={user_id}, Plan={plan_title}")

            # ตรวจสอบว่าเป็น Admin/Owner ของ Channel ใดๆ หรือไม่ (ถ้าใช่ไม่ต้องเตะ)
            is_channel_admin = False
            for cid in get_all_target_channel_ids():
                try:
                    chat_member = await bot.get_chat_member(chat_id=cid, user_id=user_id)
                    if chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        is_channel_admin = True
                        logger.info(f"User ID={user_id} is Channel Administrator/Creator in {cid}. Skipping soft-kick.")
                        break
                except Exception as e:
                    logger.debug(f"Could not check chat member status for User {user_id} in {cid}: {e}")

            if is_channel_admin:
                sub.status = SubStatus.EXPIRED.value
                session.add(sub)
                continue

            # ดำเนินการ Soft-Kick จากทุก Channel (ทั้งกลุ่มเดิมและกลุ่มใหม่)
            kick_results = await kick_user_from_all_target_channels(bot, user_id)
            kicked_successfully = len(kick_results["failed_channels"]) == 0
            fail_reason = None
            if not kicked_successfully:
                fail_reason = "; ".join(f"{cid}: {err}" for cid, err in kick_results["errors"].items())

            # อัปเดตสถานะใน DB
            if kicked_successfully:
                sub.status = SubStatus.KICKED.value
                session.add(sub)

                # 1. ส่งข้อความแจ้งเตือนทาง DM ให้ผู้ใช้
                try:
                    if is_trial:
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
                start_time_thai = format_thai_datetime(sub.joined_at or sub.created_at)
                expires_at_thai = format_thai_datetime(sub.expires_at)
                user_handle_str = f"@{user_obj.username}" if (user_obj and user_obj.username) else "ไม่มี Username"
                full_name_str = html.escape((user_obj.full_name if user_obj else "") or f"User {user_id}")

                admin_kick_msg = (
                    "🚪 <b>สมาชิกหมดอายุและถูกเตะออกจาก Channel แล้ว!</b>\n\n"
                    f"👤 <b>ผู้ใช้งาน:</b> {full_name_str} ({user_handle_str})\n"
                    f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"📦 <b>แพ็กเกจ:</b> <b>{plan_title}</b>\n"
                    f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
                    f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "ℹ️ <i>บอทได้นำผู้ใช้ออกจาก Channel และส่งข้อความแจ้งเตือนทาง DM เรียบร้อย</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=config.ADMIN_GROUP_ID,
                        text=admin_kick_msg,
                        parse_mode="HTML",
                    )
                    logger.info(f"Sent expiration kick notification to Admin Group for User ID={user_id}.")
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
                            f"📦 <b>แพ็กเกจ:</b> {plan_title}\n"
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
    sec_channel_member_count = None
    if bot:
        try:
            total_count = await bot.get_chat_member_count(chat_id=config.CHANNEL_ID)
            admins = await bot.get_chat_administrators(chat_id=config.CHANNEL_ID)
            channel_member_count = total_count - len(admins)
        except Exception as e:
            logger.warning(f"Could not fetch chat member count for channel {config.CHANNEL_ID}: {e}")

        if config.SECONDARY_CHANNEL_ID:
            try:
                sec_total = await bot.get_chat_member_count(chat_id=config.SECONDARY_CHANNEL_ID)
                sec_admins = await bot.get_chat_administrators(chat_id=config.SECONDARY_CHANNEL_ID)
                sec_channel_member_count = sec_total - len(sec_admins)
            except Exception as e:
                logger.warning(f"Could not fetch chat member count for secondary channel {config.SECONDARY_CHANNEL_ID}: {e}")

    async with get_session() as session:
        # 2. ดึง Subscription ที่ ACTIVE อยู่ทั้งหมด พร้อมข้อมูล User (1 แถวต่อ user อยู่แล้ว)
        stmt = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at > now,
            )
            .order_by(Subscription.joined_at.desc().nulls_last(), Subscription.created_at.desc())
        )
        unique_active_subs = (await session.execute(stmt)).scalars().all()

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

        # ตรวจสอบสถานะว่าอยู่ในห้อง Channel ใดจริงหรือไม่ (รองรับการอยู่ทั้ง 2 ห้องพร้อมกัน)
        in_channel_active_subs = []
        left_channel_active_subs = []
        user_channels_map = {}
        both_channels_count = 0
        primary_only_count = 0
        secondary_only_count = 0

        if bot:
            for sub in unique_active_subs:
                in_cids, _, _ = await check_user_presence_all_channels(bot, sub.user_id)
                user_channels_map[sub.user_id] = in_cids
                if len(in_cids) > 0:
                    in_channel_active_subs.append(sub)
                    if len(in_cids) >= 2:
                        both_channels_count += 1
                    elif is_secondary_channel(in_cids[0]):
                        secondary_only_count += 1
                    else:
                        primary_only_count += 1
                else:
                    left_channel_active_subs.append(sub)
        else:
            in_channel_active_subs = list(unique_active_subs)

        total_active = len(unique_active_subs)
        total_in_channel = len(in_channel_active_subs)
        total_left = len(left_channel_active_subs)
        total_failed = len(failed_subs)
        total_pending = len(pending_subs)

        report = (
            f"📊 <b>รายงานสรุปสถานะสมาชิก Channel VIP</b>\n"
            f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 <b>สมาชิก Active ในระบบบอท:</b> <b>{total_active} คน</b> "
            f"<i>(อยู่ในห้อง {total_in_channel} คน | ออกจากห้อง {total_left} คน)</i>\n"
        )

        channel_name = get_channel_label(config.CHANNEL_ID)
        sec_channel_name = get_channel_label(config.SECONDARY_CHANNEL_ID) if config.SECONDARY_CHANNEL_ID else "BareLive V.2"

        if channel_member_count is not None:
            report += f"📱 <b>จำนวนสมาชิกใน {channel_name} (<code>{config.CHANNEL_ID}</code>):</b> <b>{channel_member_count} คน</b>\n"
        if sec_channel_member_count is not None:
            report += f"🌟 <b>จำนวนสมาชิกใน {sec_channel_name} (<code>{config.SECONDARY_CHANNEL_ID}</code>):</b> <b>{sec_channel_member_count} คน</b>\n"

        if config.SECONDARY_CHANNEL_ID and total_in_channel > 0 and bot:
            report += f"👥 <b>สรุปการอยู่ในห้อง:</b> ทั้ง 2 ห้อง <b>{both_channels_count} คน</b> | เฉพาะ {channel_name} <b>{primary_only_count} คน</b> | เฉพาะ {sec_channel_name} <b>{secondary_only_count} คน</b>\n"

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
            report += "📋 <b>รายชื่อสมาชิก Active ปัจจุบัน (เรียงจากเข้าห้องล่าสุด ➔ เก่าสุด):</b>\n\n"
            for i, sub in enumerate(unique_active_subs, start=1):
                user = sub.user
                u_header = format_user_title(user.full_name if user else None, user.username if user else None, sub.user_id)

                plan_name = sub.source_label or "สมาชิก VIP"
                start_time = format_thai_datetime(sub.joined_at)
                end_time = format_thai_datetime(sub.expires_at)
                remaining = format_remaining_time(sub.expires_at)

                # แสดงสถานะการอยู่ในห้อง
                if bot:
                    in_cids = user_channels_map.get(sub.user_id, [])
                    channel_badge = format_user_channel_presence(in_cids)
                else:
                    channel_badge = "🟢 ACTIVE"

                report += (
                    f"<b>{i}.</b> {u_header} — {channel_badge}\n"
                    f"   • <b>แพ็กเกจ:</b> {plan_name}\n"
                    f"   • 🟢 <b>เริ่ม (Start):</b> <code>{start_time} น.</code>\n"
                    f"   • 🔴 <b>หมดอายุ (End):</b> <code>{end_time} น.</code>\n"
                    f"   • ⏳ <b>เวลาคงเหลือ:</b> {remaining}\n\n"
                )

        # แสดงรายการสมาชิกรอกดเข้าร่วม (Pending)
        if total_pending > 0:
            report += "🟡 <b>รายชื่อสมาชิกรอกดเข้าร่วม (Pending):</b>\n\n"
            for p_idx, psub in enumerate(pending_subs, start=1):
                p_user = psub.user
                p_u_header = format_user_title(p_user.full_name if p_user else None, p_user.username if p_user else None, psub.user_id)
                quota_str = f"{psub.pending_days} วัน" if psub.pending_days > 0 else f"{psub.pending_minutes} นาที"
                target_cid = get_user_target_channel_id(p_user)
                target_note = f" ({get_channel_label(target_cid)})" if p_user else ""
                report += (
                    f"<b>{p_idx}.</b> {p_u_header}\n"
                    f"   • <b>โควต้ารอใช้งาน:</b> {quota_str} ({psub.source_label}){target_note}\n"
                    f"   • 🟡 <b>สถานะ:</b> ออกลิงก์แล้ว-รอกดเข้าห้อง\n\n"
                )

        report += "━━━━━━━━━━━━━━━━━━━━\n"
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


PENDING_SLIP_REMINDER_TIMEOUT_SECONDS = 600  # 10 นาที
UNANSWERED_DM_REMINDER_TIMEOUT_SECONDS = 600  # 10 นาที

# แคชเก็บเวลาที่เคยส่งแจ้งเตือน DM ค้างตอบล่าสุดของแต่ละ ChatMessage ID ในหน่วยความจำ (ID -> datetime)
_dm_last_reminded: dict[int, datetime] = {}


async def check_pending_slips_reminder(bot: Bot) -> None:
    """
    ตรวจสอบสลิป/ซองของขวัญที่ค้าง PENDING นานเกิน 10 นาที
    และส่งแจ้งเตือนซ้ำเข้ากลุ่มแอดมินทุกๆ 10 นาที จนกว่าจะมีแอดมินกดอนุมัติหรือปฏิเสธ
    """
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            stmt = (
                select(PaymentSlip)
                .options(selectinload(PaymentSlip.user))
                .where(PaymentSlip.status == SlipStatus.PENDING.value)
                .order_by(PaymentSlip.created_at.asc())
            )
            pending_slips = (await session.execute(stmt)).scalars().all()
            if not pending_slips:
                return

            for slip in pending_slips:
                created_at = ensure_utc(slip.created_at)
                last_reminded = ensure_utc(slip.last_reminded_at) if getattr(slip, "last_reminded_at", None) else None

                # ตรวจสอบว่าค้างเกิน 10 นาที (600 วินาที) นับจากตอนส่งสลิปหรือไม่
                time_since_creation = (now - created_at).total_seconds()
                if time_since_creation < PENDING_SLIP_REMINDER_TIMEOUT_SECONDS:
                    continue

                # หากเคยแจ้งเตือนซ้ำไปแล้ว ให้เว้นระยะห่างอย่างน้อย 10 นาที (600 วินาที)
                if last_reminded:
                    time_since_last_reminder = (now - last_reminded).total_seconds()
                    if time_since_last_reminder < PENDING_SLIP_REMINDER_TIMEOUT_SECONDS:
                        continue

                # อัปเดตสถิติการแจ้งเตือน
                slip.reminder_count = (getattr(slip, "reminder_count", 0) or 0) + 1
                slip.last_reminded_at = now
                session.add(slip)

                # คำนวณเวลาที่รอนาน
                wait_sec = int(time_since_creation)
                wait_min = wait_sec // 60
                wait_rem_sec = wait_sec % 60
                if wait_min > 0 and wait_rem_sec > 0:
                    wait_str = f"{wait_min} นาที {wait_rem_sec} วินาที"
                elif wait_min > 0:
                    wait_str = f"{wait_min} นาที"
                else:
                    wait_str = f"{wait_rem_sec} วินาที"

                user = slip.user
                telegram_user_id = slip.user_id
                user_handle = f"@{user.username}" if (user and user.username) else "ไม่มี Username"
                full_name_safe = html.escape((user.full_name if user else None) or f"User {telegram_user_id}")
                submitted_time_thai = format_thai_datetime(created_at)
                plan_info = get_dynamic_plan_info(slip.plan_type or "VIP_30D")

                slip_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ อนุมัติ",
                                callback_data=f"admin:approve:{slip.id}",
                            ),
                            InlineKeyboardButton(
                                text="❌ ปฏิเสธ",
                                callback_data=f"admin:reject:{slip.id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text="👤 ดูข้อมูลสมาชิก",
                                callback_data=f"admin:view_user:{telegram_user_id}",
                            ),
                            InlineKeyboardButton(
                                text="📜 ดูประวัติการคุย",
                                callback_data=f"admin:view_chat:{telegram_user_id}",
                            ),
                        ]
                    ]
                )

                if slip.payment_method == "TRUEMONEY_ANGPAO":
                    angpao_url = slip.file_id
                    admin_text = (
                        f"🚨 <b>[แจ้งเตือนซ้ำ #{slip.reminder_count}] ซองของขวัญ TrueMoney รอดำเนินการนานเกิน {wait_str}!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION} กรุณาตรวจสอบหรือกดอนุมัติ\n\n"
                        f"🆔 <b>รหัสรายการ:</b> <code>#{slip.id}</code>\n"
                        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
                        f"🔢 <b>User ID:</b> <code>{telegram_user_id}</code>\n"
                        f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
                        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วัน\n"
                        f"📅 <b>เวลาที่ส่งครั้งแรก:</b> <code>{submitted_time_thai} น.</code> (<i>รอนาน {wait_str} แล้ว</i>)\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔗 <b>ลิงก์ซองของขวัญ (TrueMoney Angpao):</b>\n"
                        f"👉 <a href=\"{angpao_url}\">{html.escape(angpao_url)}</a>\n\n"
                        f"📋 <b>แตะเพื่อคัดลอกลิงก์:</b>\n"
                        f"<code>{angpao_url}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚠️ <b>แอดมินยังไม่ได้กดตอบรับ กรุณากดรับซองและเลือกดำเนินการด้านล่าง:</b>"
                    )
                    try:
                        await bot.send_message(
                            chat_id=config.ADMIN_GROUP_ID,
                            text=admin_text,
                            reply_markup=slip_kb,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                        logger.info(f"[REMINDER] Sent repeat reminder #{slip.reminder_count} for TrueMoney slip #{slip.id} to Admin Group")
                    except Exception as e:
                        logger.error(f"[REMINDER] Failed to re-send TrueMoney slip #{slip.id} to Admin: {e}")

                else:  # PROMPTPAY photo / document
                    admin_caption = (
                        f"🚨 <b>[แจ้งเตือนซ้ำ #{slip.reminder_count}] สลิปโอนเงินรอดำเนินการนานเกิน {wait_str}!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION} กรุณาตรวจสอบหรือกดอนุมัติ\n\n"
                        f"🆔 <b>รหัสสลิป:</b> <code>#{slip.id}</code>\n"
                        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
                        f"🔢 <b>User ID:</b> <code>{telegram_user_id}</code>\n"
                        f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
                        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วัน\n"
                        f"📅 <b>เวลาที่ส่งครั้งแรก:</b> <code>{submitted_time_thai} น.</code> (<i>รอนาน {wait_str} แล้ว</i>)\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "⚠️ <b>แอดมินยังไม่ได้กดตอบรับ กรุณาตรวจสอบสลิปและเลือกการดำเนินการด้านล่าง:</b>"
                    )
                    try:
                        try:
                            await bot.send_photo(
                                chat_id=config.ADMIN_GROUP_ID,
                                photo=slip.file_id,
                                caption=admin_caption,
                                reply_markup=slip_kb,
                                parse_mode="HTML",
                            )
                        except Exception:
                            await bot.send_document(
                                chat_id=config.ADMIN_GROUP_ID,
                                document=slip.file_id,
                                caption=admin_caption,
                                reply_markup=slip_kb,
                                parse_mode="HTML",
                            )
                        logger.info(f"[REMINDER] Sent repeat reminder #{slip.reminder_count} for payment slip #{slip.id} to Admin Group")
                    except Exception as e:
                        logger.error(f"[REMINDER] Failed to re-send slip photo/doc #{slip.id} to Admin: {e}")

            await session.commit()
    except Exception as e:
        logger.error(f"[REMINDER] Error in check_pending_slips_reminder job: {e}", exc_info=True)


async def check_unanswered_user_dms_reminder(bot: Bot) -> None:
    """
    ตรวจสอบข้อความล่าสุดที่เป็น DM จาก User หากแอดมินยังไม่ตอบกลับและรอนานเกิน 10 นาที
    จะรวบรวมรายชื่อผู้ใช้ที่ยังไม่ได้รับการตอบกลับแล้วส่งแจ้งเตือนเข้า Admin Group ทุก 10 นาที
    หากรอบไหนไม่มีข้อความค้างตอบ จะไม่ส่งข้อความเตือน
    """
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            # ดึงข้อความล่าสุดของแต่ละ user_id
            subq = (
                select(
                    ChatMessage.user_id,
                    func.max(ChatMessage.id).label("max_id")
                )
                .group_by(ChatMessage.user_id)
                .subquery()
            )

            stmt = (
                select(ChatMessage)
                .options(selectinload(ChatMessage.user))
                .join(subq, ChatMessage.id == subq.c.max_id)
                .where(
                    ChatMessage.sender_role == "USER",
                    ~ChatMessage.message_text.startswith("/"),
                    ~ChatMessage.message_text.startswith("[")
                )
                .order_by(ChatMessage.created_at.asc())
            )
            latest_user_msgs = (await session.execute(stmt)).scalars().all()

            # ล้างประวัติข้อความเก่าที่ตอบไปแล้วหรือไม่มีในรายการค้างตอบแล้ว
            active_msg_ids = {msg.id for msg in latest_user_msgs}
            for old_id in list(_dm_last_reminded.keys()):
                if old_id not in active_msg_ids:
                    _dm_last_reminded.pop(old_id, None)

            unanswered = []
            for msg in latest_user_msgs:
                created_at = ensure_utc(msg.created_at)
                time_waiting = (now - created_at).total_seconds()
                # ต้องรอนานเกิน 10 นาที (600 วินาที)
                if time_waiting < UNANSWERED_DM_REMINDER_TIMEOUT_SECONDS:
                    continue

                # ตรวจสอบว่าเคยแจ้งเตือนไปแล้วหรือไม่ หากเคยแจ้งเตือนแล้ว ต้องเว้นระยะห่างอย่างน้อย 10 นาที (600 วินาที)
                last_reminded = _dm_last_reminded.get(msg.id)
                if last_reminded:
                    time_since_last_reminder = (now - last_reminded).total_seconds()
                    if time_since_last_reminder < UNANSWERED_DM_REMINDER_TIMEOUT_SECONDS:
                        continue

                wait_sec = int(time_waiting)
                wait_min = wait_sec // 60
                wait_rem_sec = wait_sec % 60
                if wait_min > 0 and wait_rem_sec > 0:
                    wait_str = f"{wait_min} นาที {wait_rem_sec} วินาที"
                elif wait_min > 0:
                    wait_str = f"{wait_min} นาที"
                else:
                    wait_str = f"{wait_rem_sec} วินาที"
                unanswered.append((msg, wait_str, msg.user))

            # ถ้ารอบนี้ไม่มีข้อความที่ครบกำหนดส่งเตือน -> ไม่ต้องส่งเตือน
            if not unanswered:
                return

            # จัดรูปแบบข้อความรวบรวมรายชื่อผู้ใช้ที่ยังไม่ตอบ
            count = len(unanswered)
            lines = [
                f"🚨 <b>[แจ้งเตือนข้อความค้างตอบ] มีผู้ใช้รอแอดมินตอบกลับ {count} คน!</b>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n",
                "📌 <i>ข้อความล่าสุดเป็นของผู้ใช้ที่ยังไม่ได้รับการตอบกลับเกิน 10 นาที:</i>\n",
            ]

            for i, (msg, wait_str, u_obj) in enumerate(unanswered[:10], start=1):
                u_name = html.escape((u_obj.full_name if u_obj else None) or f"User {msg.user_id}")
                u_handle = f"@{u_obj.username}" if (u_obj and u_obj.username) else "ไม่มี Username"
                t_str = format_thai_datetime(msg.created_at)

                # ตัดข้อความให้กระชับ
                raw_text = msg.message_text.replace("\n", " ")
                snippet = html.escape(raw_text[:60] + ("..." if len(raw_text) > 60 else ""))

                lines.append(
                    f"<b>{i}.</b> <b>{u_name}</b> ({u_handle}) | ID: <code>{msg.user_id}</code>\n"
                    f"   • ⏳ <b>รอนาน:</b> <b>{wait_str}</b>\n"
                    f"   • 💬 <b>ข้อความ:</b> <i>\"{snippet}\"</i>\n"
                    f"   • ⏰ <b>ส่งเมื่อ:</b> <code>{t_str} น.</code>\n"
                    f"   • ✍️ <code>/reply {msg.user_id} </code>\n"
                )

            if count > 10:
                lines.append(f"<i>...และยังมีผู้ใช้รออยู่อีก {count - 10} คน</i>\n")

            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append("💡 <i>แตะคำสั่ง <code>/reply [User ID] [ข้อความ]</code> เพื่อตอบกลับ หรือกดปุ่มด้านล่างเพื่อปิดจบการสนทนา</i>")

            # สร้างปุ่มสำหรับ Action
            keyboard_rows = []
            for msg, wait_str, u_obj in unanswered[:4]:
                u_label = (u_obj.full_name if u_obj else f"User {msg.user_id}")[:10]
                keyboard_rows.append([
                    InlineKeyboardButton(text=f"📜 ดูแชท ({u_label})", callback_data=f"admin:view_chat:{msg.user_id}"),
                    InlineKeyboardButton(text=f"✅ ปิดจบ ({u_label})", callback_data=f"admin:resolve_chat:{msg.user_id}"),
                ])

            if count > 1:
                keyboard_rows.append([
                    InlineKeyboardButton(text=f"✅ ปิดจบทั้งหมด ({count} คน)", callback_data="admin:resolve_all_chats")
                ])

            reply_kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

            try:
                await bot.send_message(
                    chat_id=config.ADMIN_GROUP_ID,
                    text="\n".join(lines),
                    reply_markup=reply_kb,
                    parse_mode="HTML",
                )
                # บันทึกเวลาที่ส่งแจ้งเตือนล่าสุดของแต่ละข้อความ
                for msg, _, _ in unanswered:
                    _dm_last_reminded[msg.id] = now
                logger.info(f"[DM_REMINDER] Sent unanswered DMs reminder for {count} users to Admin Group {config.ADMIN_GROUP_ID}")
            except Exception as e:
                logger.error(f"[DM_REMINDER] Failed to send unanswered DMs reminder: {e}")


    except Exception as e:
        logger.error(f"[DM_REMINDER] Error in check_unanswered_user_dms_reminder job: {e}", exc_info=True)


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

    # 3. Job แจ้งเตือนสลิป/ซองของขวัญค้าง PENDING นานเกิน 10 นาที (ทำงานตรวจเช็คทุก 30 วินาที)
    scheduler.add_job(
        check_pending_slips_reminder,
        trigger="interval",
        seconds=30,
        args=[bot],
        id="check_pending_slips_reminder_job",
        name="Check and remind pending payment slips to Admin Group",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 4. Job แจ้งเตือนข้อความ DM จากผู้ใช้ที่ค้างตอบเกิน 10 นาที (ทำงานตรวจเช็คทุก 60 วินาที)
    scheduler.add_job(
        check_unanswered_user_dms_reminder,
        trigger="interval",
        seconds=60,
        args=[bot],
        id="check_unanswered_user_dms_reminder_job",
        name="Check and remind unanswered user DMs to Admin Group",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
