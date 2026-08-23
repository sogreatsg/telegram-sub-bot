import logging
import html
import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Union
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select, func, delete, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.models.schema import User, PaymentSlip, Subscription, SubscriptionGrant, ChatMessage, SlipStatus, SubStatus, PlanType, GrantType, PLAN_DETAILS, get_dynamic_plan_info, format_plan_duration
from bot.services.database import get_session, get_or_create_user
from bot.services.scheduler import build_active_members_report, sync_pending_members
from bot.services.subscription import grant_subscription, subscription_status_label, parse_plan_days
from bot.services.reconciliation import reconcile_user, reconcile_all_users, format_reconcile_formula
from bot.services.chat_logger import log_chat_message
from bot.services.referral import is_referral_active, update_referral_settings
from bot.services.trial import is_trial_active, update_trial_settings
from bot.services.notification_settings import (
    is_unanswered_dm_reminder_active,
    update_unanswered_dm_reminder_setting,
    get_notification_settings,
    get_saved_chat_group_id,
    save_chat_group_id,
)
from bot.services.channel_service import (
    get_user_target_channel_id,
    get_all_target_channel_ids,
    is_target_channel,
    is_secondary_channel,
    is_user_v2_member,
    get_channel_label,
    get_discussion_chat_id,
    resolve_chat_group,
    kick_user_from_all_target_channels,
    unban_user_in_channel,
    unban_user_in_all_target_channels,
    check_user_in_channel,
    check_user_in_target_channels,
    check_user_presence_all_channels,
    format_user_channel_presence,
)
from bot.services.chat_cleaner import clean_user_chat_messages
from bot.services.payment_settings import (
    is_promptpay_active,
    update_promptpay_setting,
    is_truemoney_active,
    update_truemoney_setting,
)
from bot.handlers.user_menu import get_main_menu_keyboard
from bot.utils.time_utils import (
    BANGKOK_TZ,
    format_thai_datetime,
    ensure_utc,
    split_text_chunks,
    format_user_title,
    format_remaining_time,
    parse_duration_input,
)

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="admin")


def is_admin_chat(chat_id: int) -> bool:
    """ตรวจสอบว่าเป็นกลุ่มแอดมินหรือไม่ (รองรับทั้งรูปแบบมีและไม่มี -100)"""
    target = config.ADMIN_GROUP_ID
    if chat_id == target:
        return True
    str_chat = str(chat_id).replace("-100", "").replace("-", "")
    str_target = str(target).replace("-100", "").replace("-", "")
    return str_chat == str_target


def format_time_remaining(expires_at: datetime) -> str:
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
        parts.append(f"{hours} ชั่วโมง")
    if minutes > 0 or (days == 0 and hours == 0):
        parts.append(f"{minutes} นาที")
    if days == 0 and hours == 0 and minutes < 5:
        parts.append(f"{seconds} วินาที")
    
    return " ".join(parts)


def format_subscription_status_display(sub: Optional[Subscription]) -> str:
    """คืนข้อความสถานะสมาชิกแบบมีไอคอนชัดเจน เช่น 🟢 ACTIVE หรือ 🔴 EXPIRED (หมดอายุแล้ว)"""
    if not sub:
        return "⚪ ไม่มีข้อมูล"
    now = datetime.now(timezone.utc)
    if sub.status == SubStatus.ACTIVE.value and sub.expires_at and ensure_utc(sub.expires_at) > now:
        label = f" ({sub.source_label})" if sub.source_label else ""
        return f"🟢 ACTIVE{label}"
    elif sub.status == SubStatus.PENDING.value:
        label = f" ({sub.source_label})" if sub.source_label else ""
        return f"🟡 PENDING (รอกดเข้าห้อง{label})"
    elif sub.status == SubStatus.KICKED.value:
        return "🔴 KICKED (ถูกเตะออกจากห้องแล้ว)"
    elif sub.status == SubStatus.KICK_FAILED.value:
        return "⚠️ KICK_FAILED (ค้างเตะไม่สำเร็จ)"
    else:
        return "🔴 EXPIRED (หมดอายุแล้ว)"


def get_bot_version_info() -> str:
    """ดึงข้อมูลเวอร์ชันและประวัติ Commit ล่าสุดแบบไดนามิก"""
    import subprocess
    import os
    import json
    from pathlib import Path

    commit_hash = os.environ.get("BOT_APP_VERSION", "Unknown")
    commit_date = os.environ.get("BOT_APP_DATE", "Unknown")
    commit_msg = os.environ.get("BOT_APP_MESSAGE", "Unknown")
    recent_logs = []

    # 1. พยายามอ่านจาก bot/version.json (สร้างอัตโนมัติขณะ Deploy)
    # ใช้ค่าจากไฟล์แทนที่ env var เฉพาะเมื่อไฟล์มีค่าจริง (ไม่ใช่ "Unknown")
    # เพื่อกันไม่ให้ทับค่าที่ถูกต้องจาก env var ด้วยค่าว่างเปล่าหากสร้างไฟล์ไม่สำเร็จ
    version_file = Path(__file__).parent.parent / "version.json"
    if version_file.exists():
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                v_data = json.load(f)
                if v_data.get("commit", "Unknown") != "Unknown":
                    commit_hash = v_data["commit"]
                    commit_date = v_data.get("date", commit_date)
                    commit_msg = v_data.get("message", commit_msg)
                if v_data.get("recent_logs"):
                    recent_logs = v_data["recent_logs"]
        except Exception:
            pass

    # 2. พยายามอ่านจาก Git โดยตรงถ้ายังไม่มี recent_logs
    if not recent_logs:
        try:
            git_cmd = [
                "git", "log", "-n", "5",
                "--format=%h|%ad|%s",
                "--date=format:%d/%m/%Y %H:%M"
            ]
            res = subprocess.run(
                git_cmd,
                capture_output=True,
                text=True,
                timeout=3,
                encoding="utf-8",
            )
            if res.returncode == 0 and res.stdout.strip():
                lines = res.stdout.strip().split("\n")
                if lines:
                    parts = lines[0].split("|", 2)
                    if len(parts) >= 3:
                        commit_hash = parts[0]
                        commit_date = parts[1]
                        commit_msg = parts[2]
                    
                    recent_logs = []
                    for line in lines:
                        p = line.split("|", 2)
                        if len(p) >= 3:
                            h, d, m = p[0], p[1], p[2]
                            recent_logs.append(f"• <code>{h}</code>: {html.escape(m)} (<i>{d}</i>)")
        except Exception:
            pass

    # 3. ถ้าไม่มี recent_logs แต่มี ENV variables
    if not recent_logs and commit_hash != "Unknown":
        recent_logs.append(f"• <code>{commit_hash}</code>: {html.escape(commit_msg)}")

    # 4. จัดรูปแบบข้อความ
    text = (
        "🤖 <b>Bot Version Info</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Commit:</b> <code>{commit_hash}</code>\n"
        f"📅 <b>Date:</b> <code>{commit_date}</code>\n"
        f"📝 <b>Message:</b> <i>{html.escape(commit_msg)}</i>\n"
    )

    if recent_logs:
        text += (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📜 <b>ประวัติการอัปเดตล่าสุด:</b>\n"
            + "\n".join(recent_logs)
        )

    return text


@router.message(Command("version"))
async def handle_admin_version_command(message: Message):
    """(Admin) เช็คเวอร์ชันปัจจุบันของบอท"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    try:
        text = get_bot_version_info()
    except Exception as e:
        text = f"🤖 <b>Bot Version Info</b>\nไม่สามารถอ่านเวอร์ชันได้: <code>{html.escape(str(e))}</code>"

    await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:approve:"))
async def handle_admin_approve(callback: CallbackQuery, bot: Bot):
    """จัดการเมื่อ Admin กดยืนยัน/อนุมัติสลิปสำหรับสมาชิก VIP พร้อมระบบสะสมวัน (Day Stacking)"""
    if not callback.from_user or not callback.message:
        return

    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คุณไม่มีสิทธิ์ดำเนินการนี้ (เฉพาะแอดมินเท่านั้น)", show_alert=True)
        return

    admin_user = callback.from_user
    slip_id_str = callback.data.split(":")[-1]
    
    try:
        slip_id = int(slip_id_str)
    except ValueError:
        await callback.answer("❌ รหัสสลิปไม่ถูกต้อง", show_alert=True)
        return

    try:
        now = datetime.now(timezone.utc)
        is_stack_extension = False
        new_expires_at = None
        target_user_id = None
        requested_plan = PlanType.VIP_30D.value

        async with get_session() as session:
            stmt = select(PaymentSlip).where(PaymentSlip.id == slip_id)
            result = await session.execute(stmt)
            slip = result.scalar_one_or_none()

            if not slip:
                await callback.answer("❌ ไม่พบข้อมูลสลิปในระบบ", show_alert=True)
                return

            if slip.status != SlipStatus.PENDING.value:
                await callback.answer(
                    f"⚠️ สลิปนี้ได้รับการดำเนินการไปแล้ว ({slip.status})!",
                    show_alert=True,
                )
                return

            # 1. อัปเดตสถานะสลิปเป็น APPROVED และบันทึก ID แอดมิน
            slip.status = SlipStatus.APPROVED.value
            slip.admin_id = admin_user.id
            session.add(slip)
            target_user_id = slip.user_id

            # ปิดสลิปซ้ำที่ส่งด้วย file_id / URL เดียวกันของผู้ใช้คนนี้ (ถ้ามี)
            dup_slips_stmt = select(PaymentSlip).where(
                PaymentSlip.user_id == target_user_id,
                PaymentSlip.file_id == slip.file_id,
                PaymentSlip.id != slip.id,
                PaymentSlip.status == SlipStatus.PENDING.value,
            )
            dup_slips = (await session.execute(dup_slips_stmt)).scalars().all()
            for ds in dup_slips:
                ds.status = SlipStatus.APPROVED.value
                ds.admin_id = admin_user.id
                session.add(ds)

            requested_plan = getattr(slip, "plan_type", None) or PlanType.VIP_30D.value

            plan_info = get_dynamic_plan_info(requested_plan)
            additional_days, additional_minutes = parse_plan_days(requested_plan)
            grant_type_value = GrantType.PROMOTION.value if requested_plan == PlanType.PROMOTION.value else GrantType.PURCHASE.value

            # ดึง User เพื่อหา target channel ประจำตัว (หากถูกย้ายไป Channel ใหม่ จะใช้ Channel ใหม่)
            target_user_obj = await session.get(User, target_user_id)
            target_channel_id = get_user_target_channel_id(target_user_obj)
            target_channel_label = get_channel_label(target_channel_id)

            # ตรวจสอบสถานะจริงใน Channel เป้าหมาย
            is_in_channel = False
            try:
                chat_member = await bot.get_chat_member(chat_id=target_channel_id, user_id=target_user_id)
                is_in_channel = chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
            except Exception:
                is_in_channel = False

            grant = await grant_subscription(
                session,
                user_id=target_user_id,
                days=additional_days,
                minutes=additional_minutes,
                source_label=f"สมาชิก {plan_info['badge']}",
                grant_type=grant_type_value,
                has_value=True,
                is_in_channel=is_in_channel,
            )
            is_stack_extension = grant.is_stack_extension
            new_expires_at = grant.new_expires_at

            if is_stack_extension:
                logger.info(
                    f"Approve slip #{slip_id}: Extended active sub for User {target_user_id} by +{additional_days}d {additional_minutes}m in {target_channel_label}. "
                    f"New expires_at: {new_expires_at}"
                )

        plan_info = get_dynamic_plan_info(requested_plan)
        plan_badge = plan_info["badge"]
        plan_desc = format_plan_duration(plan_info)
        user_dm_sent = False

        if is_stack_extension and new_expires_at:
            # ส่ง DM แจ้งการต่อเวลาสะสม
            exp_thai = format_thai_datetime(new_expires_at)
            time_rem = format_time_remaining(new_expires_at)
            user_message = (
                "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>แพ็กเกจที่ซื้อเพิ่ม:</b> {plan_badge} (+{plan_desc})\n"
                "⏳ <b>ระบบได้ต่อเวลาสะสมให้คุณเรียบร้อยแล้ว!</b>\n"
                f"📅 <b>วันหมดอายุใหม่ของคุณ:</b> <code>{exp_thai} น.</code>\n"
                f"⏰ <b>เวลาคงเหลือรวม:</b> {time_rem}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>คุณสามารถรับชมเนื้อหาใน {target_channel_label} ได้ต่อเนื่องทันทีโดยไม่ต้องกดเข้าห้องใหม่ครับ! 🚀</i>"
            )
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=user_message,
                    parse_mode="HTML",
                )
                user_dm_sent = True
            except Exception as e:
                logger.warning(f"Could not send stacked extension DM to User ID={target_user_id}: {e}")

        elif is_in_channel:
            # กรณีไม่มี ACTIVE เดิม แต่ผู้ใช้อยู่ใน Channel อยู่แล้ว -> เปิดใช้งานให้ทันที ไม่ต้องออก invite link ใหม่
            exp_thai = format_thai_datetime(new_expires_at) if new_expires_at else "-"
            user_message = (
                "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>แพ็กเกจ:</b> <b>{plan_badge} ({plan_desc})</b> เปิดใช้งานให้ทันทีแล้วครับ\n"
                f"📅 <b>วันหมดอายุ:</b> <code>{exp_thai} น.</code>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>คุณอยู่ใน {target_channel_label} อยู่แล้ว สามารถใช้งานต่อได้ทันทีโดยไม่ต้องกดลิงก์ใหม่ครับ! 🚀</i>"
            )
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=user_message,
                    parse_mode="HTML",
                )
                user_dm_sent = True
            except Exception as e:
                logger.warning(f"Could not send instant-activate DM to User ID={target_user_id}: {e}")

        else:
            # ปลดแบนผู้ใช้ใน Channel ก่อนสร้างลิงก์เสมอ (ป้องกันกรณีเคยถูกเตะแล้วติด blacklist ใน Telegram)
            await unban_user_in_channel(bot, target_channel_id, target_user_id)

            # สร้างลิงก์เชิญแบบ 1 ครั้งให้ผู้ใช้
            try:
                invite_link_obj = await bot.create_chat_invite_link(
                    chat_id=target_channel_id,
                    member_limit=1,
                    expire_date=now + timedelta(days=7),
                    name=f"VIP-{target_user_id}",
                )
                invite_url = invite_link_obj.invite_link
            except Exception as e:
                logger.error(f"Failed to generate invite link for approved user {target_user_id} in {target_channel_id}: {e}", exc_info=True)
                await callback.answer(
                    "⚠️ ไม่สามารถสร้างลิงก์เชิญได้! กรุณาตรวจสอบว่าบอทมีสิทธิ์สร้างลิงก์ใน Channel",
                    show_alert=True,
                )
                return

            user_message = (
                "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>แพ็กเกจ:</b> <b>{plan_badge} ({plan_desc})</b> พร้อมใช้งานแล้วครับ\n\n"
                f"🔗 <b>ลิงก์เชิญเข้า {target_channel_label} (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
                "📌 <b>ข้อควรทราบ:</b>\n"
                "• ลิงก์นี้สามารถใช้งานได้เพียง 1 ครั้งเท่านั้น\n"
                f"• <b>ระยะเวลาสมาชิก {plan_desc} จะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ! 🚀"
            )
            join_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"🚀 เข้าร่วม {target_channel_label} ตอนนี้", url=invite_url)]
                ]
            )
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=user_message,
                    reply_markup=join_keyboard,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                user_dm_sent = True
            except Exception as e:
                logger.warning(f"Could not send approval DM to User ID={target_user_id}: {e}")

        # อัปเดตข้อความในกลุ่ม Admin
        admin_name = f"@{admin_user.username}" if admin_user.username else html.escape(admin_user.full_name)
        timestamp_thai = format_thai_datetime(now)
        base_text = callback.message.caption or callback.message.text or ""

        if is_stack_extension:
            action_label = f"✅ <b>อนุมัติแล้ว (ต่อเวลาสะสม +{plan_desc})</b>"
        elif is_in_channel:
            action_label = f"✅ <b>อนุมัติแล้ว (เปิดใช้งานทันที อยู่ใน Channel อยู่แล้ว)</b>"
        else:
            action_label = f"✅ <b>อนุมัติแล้ว (ออกลิงก์เชิญใหม่)</b>"
        expiry_note = f"\n⏳ หมดอายุใหม่: <code>{format_thai_datetime(new_expires_at)} น.</code>" if new_expires_at else ""

        updated_text = (
            f"{base_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{action_label} โดย {admin_name} (<code>{admin_user.id}</code>){expiry_note}\n"
            f"📅 <code>{timestamp_thai} น.</code>\n"
            f"📨 ส่ง DM: {'สำเร็จ ✅' if user_dm_sent else 'ไม่สำเร็จ (ผู้ใช้บล็อกบอท) ⚠️'}"
        )

        try:
            if callback.message:
                if callback.message.caption is not None:
                    await callback.message.edit_caption(
                        caption=updated_text,
                        reply_markup=None,
                        parse_mode="HTML",
                    )
                else:
                    await callback.message.edit_text(
                        text=updated_text,
                        reply_markup=None,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
        except Exception as e:
            logger.error(f"Failed to update admin message caption/text: {e}")

        try:
            await log_chat_message(
                user_id=target_user_id,
                sender_role="ADMIN",
                message_text=f"[แอดมิน {admin_name} อนุมัติสลิป #{slip_id} ({plan_badge})]"
            )
        except Exception:
            pass

        await callback.answer(f"✅ อนุมัติการชำระเงินเรียบร้อย ({'ต่อเวลาสะสม' if is_stack_extension else 'ส่งลิงก์เชิญ'})")
    except Exception as e:
        logger.error(f"Failed to approve slip #{slip_id}: {e}", exc_info=True)
        await callback.answer(f"❌ เกิดข้อผิดพลาด: {e}", show_alert=True)


@router.callback_query(F.data.startswith("admin:reject:"))
async def handle_admin_reject(callback: CallbackQuery, bot: Bot):
    """จัดการเมื่อ Admin กดปฏิเสธสลิป (เวลาไทย)"""
    if not callback.from_user or not callback.message:
        return

    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คุณไม่มีสิทธิ์ดำเนินการนี้ (เฉพาะแอดมินเท่านั้น)", show_alert=True)
        return

    admin_user = callback.from_user
    slip_id_str = callback.data.split(":")[-1]

    try:
        slip_id = int(slip_id_str)
    except ValueError:
        await callback.answer("❌ รหัสสลิปไม่ถูกต้อง", show_alert=True)
        return

    try:
        requested_plan = PlanType.VIP_30D.value

        async with get_session() as session:
            stmt = select(PaymentSlip).where(PaymentSlip.id == slip_id)
            result = await session.execute(stmt)
            slip = result.scalar_one_or_none()

            if not slip:
                await callback.answer("❌ ไม่พบข้อมูลสลิปในระบบ", show_alert=True)
                return

            if slip.status != SlipStatus.PENDING.value:
                await callback.answer(
                    f"⚠️ สลิปนี้ได้รับการดำเนินการไปแล้ว ({slip.status})!",
                    show_alert=True,
                )
                return

            slip.status = SlipStatus.REJECTED.value
            slip.admin_id = admin_user.id
            session.add(slip)
            target_user_id = slip.user_id
            requested_plan = getattr(slip, "plan_type", None) or PlanType.VIP_30D.value

        plan_info = get_dynamic_plan_info(requested_plan)
        plan_price_str = f"{plan_info['price']:,} บาท"

        # 1. ส่งข้อความแจ้งผู้ใช้ทาง DM
        try:
            rejection_message = (
                "❌ <b>แจ้งเตือนผลการตรวจสอบการชำระเงิน</b>\n\n"
                "ทีมงานไม่สามารถยืนยันการชำระเงินสำหรับสมาชิก VIP ของคุณได้ครับ\n\n"
                "สาเหตุที่เป็นไปได้:\n"
                f"• ยอดเงินไม่ตรงกับค่าบริการ ({plan_price_str})\n"
                "• ลิงก์ซองของขวัญ TrueMoney ไม่ถูกต้อง หรือถูกกดรับไปแล้ว\n"
                "• รูปภาพสลิปไม่ชัดเจน หรือไม่สามารถตรวจสอบได้\n"
                "• วันที่หรือเวลาในสลิปไม่ถูกต้อง\n\n"
                "👉 กรุณาพิมพ์ /start เพื่อทำรายการใหม่อีกครั้ง หรือติดต่อแอดมินหากมีข้อสงสัยครับ"
            )
            await bot.send_message(
                chat_id=target_user_id,
                text=rejection_message,
                parse_mode="HTML",
            )
            logger.info(f"Sent rejection DM to User ID={target_user_id}")
        except Exception as e:
            logger.warning(f"Could not send rejection DM to User ID={target_user_id}: {e}")

        # 2. แก้ไขข้อความในกลุ่ม Admin (เวลาไทย)
        admin_name = f"@{admin_user.username}" if admin_user.username else html.escape(admin_user.full_name)
        timestamp_thai = format_thai_datetime(datetime.now(timezone.utc))

        base_text = callback.message.caption or callback.message.text or ""
        updated_text = (
            f"{base_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ <b>ปฏิเสธแล้ว</b> โดย {admin_name} (<code>{admin_user.id}</code>)\n"
            f"📅 <code>{timestamp_thai} น.</code>"
        )

        try:
            if callback.message:
                if callback.message.caption is not None:
                    await callback.message.edit_caption(
                        caption=updated_text,
                        reply_markup=None,
                        parse_mode="HTML",
                    )
                else:
                    await callback.message.edit_text(
                        text=updated_text,
                        reply_markup=None,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
        except Exception as e:
            logger.error(f"Failed to update admin message caption/text: {e}")

        try:
            await log_chat_message(
                user_id=target_user_id,
                sender_role="ADMIN",
                message_text=f"[แอดมิน {admin_name} ปฏิเสธสลิป #{slip_id}]"
            )
        except Exception:
            pass

        await callback.answer("❌ ปฏิเสธการชำระเงินเรียบร้อยแล้ว")
    except Exception as e:
        logger.error(f"Failed to reject slip #{slip_id}: {e}", exc_info=True)
        await callback.answer(f"❌ เกิดข้อผิดพลาด: {e}", show_alert=True)


def get_admin_menu_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความเมนูหลักและคีย์บอร์ดสำหรับ Admin Panel"""
    admin_menu_text = (
        "👑 <b>เมนูคำสั่งผู้ดูแลระบบ (Admin Panel & Commands)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 <b>1. ตรวจสอบสมาชิก & รายงาน:</b>\n"
        "• <code>/reconcile</code> — 🔍 พรีวิวสรุปยอด & กระทบยอดวันหมดอายุจริง\n"
        "• <code>/reconcile_all</code> — ⚡ ปรับวันหมดอายุของสมาชิกทุกคนทั้งระบบทันที\n"
        "• <code>/audit_user [User ID/@user]</code> — 🔍 ตรวจสอบยอดเวลาสมาชิกรายคน\n"
        "• <code>/top_refs</code> — 🏆 ดูอันดับผู้ใช้งานที่ชวนเพื่อนได้มากที่สุด\n"
        "• <code>/summary</code> — ดูสรุปสมาชิก Active ปัจจุบันใน Channel\n"
        "• <code>/users_latest</code> — ดูรายชื่อผู้ใช้สมัครใหม่ 10 คนล่าสุด\n"
        "• <code>/users [หน้า]</code> — ดูประวัติผู้ใช้งานทั้งหมดในระบบ\n"
        "• <code>/user [User ID/@user]</code> — ดูประวัติเจาะลึกเฉพาะราย (สลิป/วันหมดอายุ)\n\n"
        "💬 <b>2. แชท & ตอบกลับผู้ใช้:</b>\n"
        "• <code>/chat [User ID/@user]</code> — ดูประวัติการสนทนา User กับ Bot\n"
        "• <code>/reply [User ID/@user] [ข้อความ]</code> — ตอบกลับผู้ใช้ทาง DM\n"
        "• <code>/close_chat [User ID/@user]</code> — ✅ ปิดจบการสนทนา/หยุดเตือนค้างตอบ\n\n"
        "📢 <b>3. บรอดแคสต์:</b>\n"
        "• <code>/broadcast_count</code> — ตรวจสอบยอดผู้ใช้ที่บรอดแคสต์ได้\n"
        "• <code>/broadcast_menu</code> — บรอดแคสต์เมนู /start ล่าสุดให้ทุกคน\n"
        "• <code>/broadcast [ข้อความ]</code> — บรอดแคสต์ข้อความข่าวสารให้ทุกคน\n"
        "• <code>/send_menu [User ID/@user]</code> — ส่งเมนู /start ให้เฉพาะบุคคล\n\n"
        "🔄 <b>4. ซิงค์สมาชิก:</b>\n"
        "• <code>/sync</code> — ตรวจเช็คผู้ใช้ค้าง PENDING และเริ่มนับเวลาให้ทันที\n"
        "• <code>/deep_scan</code> — กวาดล้างเตะผู้ใช้ที่หมดอายุแต่ยังค้างในห้อง\n\n"
        "⚙️ <b>5. ระบบ & สิทธิ์บอท:</b>\n"
        "• <code>/admin</code> — 👑 แสดงหน้ารวมเมนูคำสั่งแอดมินนี้\n"
        "• <code>/audit</code> — ตรวจสอบสิทธิ์ของ Bot ใน Channel VIP\n"
        "• <code>/revoke_primary</code> — เพิกถอนและสร้าง Primary Link ใหม่\n"
        "• <code>/version</code> — ตรวจสอบเวอร์ชันและ Uptime\n\n"
        "🛠️ <b>6. จัดการเวลาสมาชิก:</b>\n"
        "• <code>/add_vip [User ID/@user] [เวลา เช่น 30, 12h, 1d 6h]</code> — ➕ เพิ่มเวลา VIP (ส่ง DM บอก User)\n"
        "• <code>/deduct_vip [User ID/@user] [เวลา เช่น 7, 12h, 1d]</code> — ➖ ลดเวลา VIP (ไม่ส่ง DM)\n"
        "• <code>/set_vip [User ID/@user] [เวลา เช่น 30, 12h, 0]</code> — 🎯 ตั้งค่าเวลาคงเหลือใหม่โดยตรง\n"
        "• <code>/move_user [User ID/@user]</code> — 🚀 ย้ายสมาชิกไป Channel ใหม่ (ส่งลิงก์ 7 วัน)\n"
        "• <code>/unmove_user [User ID/@user]</code> — 🔄 ย้ายสมาชิกกลับ Channel เดิม\n"
        "• <code>/kick [User ID]</code> — สั่งเตะออกจาก Channel VIP ทันที\n"
        "• <code>/kick_all_v1</code> — 🚪 สั่งเตะสมาชิกทุกคนออกจาก Channel V.1\n"
        "• <code>/kick_all_chat</code> — 💬 สั่งเตะสมาชิกทุกคนออกจากห้องพูดคุย (Community Chat)\n"
        "• <code>/kick_chat [User ID/@user]</code> — 💬 เตะผู้ใช้รายคนออกจากห้องพูดคุย\n"
        "• <code>/clean_non_v2_chat</code> — 🧹 สั่งลบข้อความบอทใน DM ของทุกคนที่ไม่ใช่ V.2\n"
        "• <code>/clean_chat [User ID/@user]</code> — 🧹 ลบข้อความบอทใน DM ของผู้ใช้รายคน\n\n"
        "🗑️ <b>7. รีเซ็ตข้อมูล:</b>\n"
        "• <code>/reset_user [User ID/@user]</code> — ล้างข้อมูลผู้ใช้ทั้งหมดเพื่อเริ่มใหม่\n"
        "• <code>/reset_trial [User ID/@user]</code> — รีเซ็ตสิทธิ์ทดลองฟรีให้ผู้ใช้\n\n"
        "🎁 <b>8. ระบบโปรโมชั่น:</b>\n"
        "• <code>/promotion</code> (หรือ <code>/promotion_on</code> / <code>/promotion_off</code>) — เปิด/ปิด/ดูสถานะ\n"
        "• <code>/promotion_setting</code> — ตั้งค่าราคาและจำนวนวัน\n"
        "• <code>/promo_broadcast</code> — บรอดแคสต์โปรโมชั่น\n\n"
        "👥 <b>9. ระบบแนะนำเพื่อน:</b>\n"
        "• <code>/referral</code> (หรือ <code>/referral_on</code> / <code>/referral_off</code>) — เปิด/ปิด/ดูสถานะ\n\n"
        "⏱️ <b>10. ระบบทดลองฟรี:</b>\n"
        "• <code>/trial</code> (หรือ <code>/trial_on</code> / <code>/trial_off</code>) — เปิด/ปิด/ดูสถานะ\n\n"
        "🔔 <b>11. ระบบแจ้งเตือนข้อความค้างตอบ (DM Reminder):</b>\n"
        "• <code>/dm_reminder</code> (หรือ <code>/dm_reminder_on</code> / <code>/dm_reminder_off</code>) — เปิด/ปิด/ดูสถานะแจ้งเตือนค้างตอบ\n\n"
        "💳 <b>12. ระบบช่องทางชำระเงิน:</b>\n"
        "• <code>/promptpay</code> (หรือ <code>/promptpay_on</code> / <code>/promptpay_off</code>) — เปิด/ปิด/ดูสถานะชำระผ่าน QR Code\n"
        "• <code>/payment_methods</code> — ดูสถานะช่องทางชำระเงินทั้งหมด\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>แตะปุ่มด่วนด้านล่างเพื่อใช้งานเมนูหลักได้ทันทีครับ</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 พรีวิว Reconcile ยอด", callback_data="admin:refresh_reconcile_preview"),
                InlineKeyboardButton(text="📊 สรุปสมาชิก Active", callback_data="admin_menu:summary"),
            ],
            [
                InlineKeyboardButton(text="🏆 อันดับชวนเพื่อน (/top_refs)", callback_data="admin_menu:top_referrals"),
                InlineKeyboardButton(text="🔄 ซิงค์สมาชิกค้าง (/sync)", callback_data="admin_menu:sync"),
            ],
            [
                InlineKeyboardButton(text="⚡ 10 ผู้ใช้ล่าสุด (/users_lasted)", callback_data="admin_menu:users_latest"),
                InlineKeyboardButton(text="📑 ผู้ใช้ทั้งหมด (/users)", callback_data="admin:users_page:1"),
            ],
            [
                InlineKeyboardButton(text="📢 ยอด Broadcast", callback_data="admin_menu:broadcast_count"),
                InlineKeyboardButton(text="🔍 Audit สิทธิ์บอท", callback_data="admin_menu:audit"),
            ],
            [
                InlineKeyboardButton(text="🎁 ตั้งค่าโปรโมชั่น", callback_data="admin_menu:promotion"),
                InlineKeyboardButton(text="👥 ระบบชวนเพื่อน", callback_data="admin_menu:referral"),
            ],
            [
                InlineKeyboardButton(text="⏱️ ระบบทดลองฟรี", callback_data="admin_menu:trial"),
                InlineKeyboardButton(text="🔔 เตือนข้อความค้างตอบ", callback_data="admin_menu:unanswered_reminder"),
            ],
            [
                InlineKeyboardButton(text="🚪 เตะทุกคนออกจาก V.1", callback_data="admin:confirm_kick_all_v1"),
                InlineKeyboardButton(text="💬 เตะทุกคนออกจากห้องแชท", callback_data="admin:confirm_kick_all_chat"),
            ],
            [
                InlineKeyboardButton(text="🧹 ลบข้อความ DM ทุกคนที่ไม่ใช่ V.2", callback_data="admin:confirm_clean_non_v2_dms"),
            ],
            [
                InlineKeyboardButton(text="💳 ตั้งค่าช่องทางชำระเงิน (QR/TrueMoney)", callback_data="admin_menu:payment_methods"),
            ],
        ]
    )
    return admin_menu_text, keyboard


@router.message(Command("admin", "admin_help", "help_admin"))
async def handle_admin_menu_command(message: Message):
    """คำสั่งแสดงเมนูคำสั่งแอดมินทั้งหมด: /admin (เฉพาะใน Admin Group เท่านั้น)"""
    if not is_admin_chat(message.chat.id):
        if message.chat.type == "private":
            await message.answer(
                "⚠️ <b>คำสั่ง /admin ใช้งานได้เฉพาะในกลุ่มแอดมิน (Admin Group) เท่านั้นครับ</b>\n\n"
                "กรุณาพิมพ์คำสั่งนี้ในกลุ่ม Admin Group ของคุณครับ 🙏",
                parse_mode="HTML"
            )
        return

    admin_menu_text, keyboard = get_admin_menu_text_and_kb()
    try:
        await message.answer(text=admin_menu_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send admin menu: {e}", exc_info=True)
        await message.answer(f"❌ <b>เกิดข้อผิดพลาดในการเปิดเมนูแอดมิน:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:summary")
async def handle_admin_menu_summary_callback(callback: CallbackQuery, bot: Bot):
    """ปุ่มลัดสำหรับเปิดรายงาน Active Summary"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer("📊 กำลังโหลดรายงานสรุป...")
    report_text = await build_active_members_report(bot=bot)
    chunks = split_text_chunks(report_text, max_chunk_size=3800)
    for chunk in chunks:
        await callback.message.answer(text=chunk, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:audit")
async def handle_admin_menu_audit_callback(callback: CallbackQuery, bot: Bot):
    """ปุ่มลัดสำหรับตรวจสอบ System Audit"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    
    status_lines = ["🔍 <b>ตรวจสอบสถานะและความพร้อมของระบบ (System Audit)</b>\n"]
    try:
        bot_info = await bot.get_me()
        chat_info = await bot.get_chat(chat_id=config.CHANNEL_ID)
        member_count = await bot.get_chat_member_count(chat_id=config.CHANNEL_ID)
        bot_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=bot_info.id)

        status_lines.append(f"📢 <b>Channel VIP หลัก:</b> {html.escape(chat_info.title or '')} (<code>{config.CHANNEL_ID}</code>)")
        status_lines.append(f"👥 <b>จำนวนสมาชิก:</b> {member_count} คน")
        
        is_admin = bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        status_lines.append(f"🤖 <b>สถานะบอท:</b> {'✅ Administrator' if is_admin else '❌ ไม่ได้เป็น Admin'}")

        if is_admin and hasattr(bot_member, "can_restrict_members"):
            can_ban = bot_member.can_restrict_members
            can_invite = bot_member.can_invite_users
            status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if can_ban else '❌ ขาดสิทธิ์ (สำคัญมาก!)'}")
            status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if can_invite else '❌ ขาดสิทธิ์'}")
    except Exception as e:
        status_lines.append(f"⚠️ <b>ตรวจสอบ Channel VIP หลักล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

    if config.SECONDARY_CHANNEL_ID:
        status_lines.append("")
        try:
            sec_chat_info = await bot.get_chat(chat_id=config.SECONDARY_CHANNEL_ID)
            sec_member_count = await bot.get_chat_member_count(chat_id=config.SECONDARY_CHANNEL_ID)
            sec_bot_member = await bot.get_chat_member(chat_id=config.SECONDARY_CHANNEL_ID, user_id=bot_info.id)

            status_lines.append(f"🌟 <b>Channel ใหม่ (Target):</b> {html.escape(sec_chat_info.title or '')} (<code>{config.SECONDARY_CHANNEL_ID}</code>)")
            status_lines.append(f"👥 <b>จำนวนสมาชิก:</b> {sec_member_count} คน")
            
            sec_is_admin = sec_bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
            status_lines.append(f"🤖 <b>สถานะบอท:</b> {'✅ Administrator' if sec_is_admin else '❌ ไม่ได้เป็น Admin'}")

            if sec_is_admin and hasattr(sec_bot_member, "can_restrict_members"):
                sec_can_ban = sec_bot_member.can_restrict_members
                sec_can_invite = sec_bot_member.can_invite_users
                status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if sec_can_ban else '❌ ขาดสิทธิ์'}")
                status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if sec_can_invite else '❌ ขาดสิทธิ์'}")
        except Exception as e:
            status_lines.append(f"⚠️ <b>ตรวจสอบ Channel ใหม่ล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

    status_lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    status_lines.append("💡 <i>พิมพ์ <code>/summary</code> เพื่อดูรายชื่อสมาชิก Active\nหรือ <code>/kick [User ID]</code> เพื่อสั่งเตะสมาชิกออกจากห้อง</i>")

    await callback.message.answer(text="\n".join(status_lines), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_menu:broadcast_count")
async def handle_admin_menu_broadcast_count_callback(callback: CallbackQuery):
    """ปุ่มลัดสำหรับตรวจสอบสถิติ Audience Reach / Broadcast Count"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0
        active_users = (await session.execute(
            select(func.count(Subscription.user_id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )).scalar() or 0
        trial_used_users = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0
        never_trial_users = total_users - trial_used_users

    resp = (
        "📢 <b>สถิติฐานผู้ใช้งานที่สามารถ Broadcast ได้ (Audience Reach)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>ผู้ใช้ทั้งหมดในระบบ:</b> <b>{total_users} คน</b>\n"
        f"🟢 <b>สมาชิก VIP Active ปัจจุบัน:</b> {active_users} คน\n"
        f"⏱️ <b>เคยใช้สิทธิ์ทดลองฟรีแล้ว:</b> {trial_used_users} คน\n"
        f"🎁 <b>ยังไม่เคยใช้สิทธิ์ทดลองฟรี:</b> {never_trial_users} คน\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>คำสั่งสำหรับการส่งข้อความ:</b>\n"
        "• <code>/broadcast_menu</code> — บรอดแคสต์เมนูหลัก /start ล่าสุด (พร้อมปุ่มชวนเพื่อน/โปรโมชั่น) ให้ทุกคน\n"
        "• <code>/broadcast [ข้อความ]</code> — บรอดแคสต์ข้อความข่าวสารหรือโปรโมชั่นให้ทุกคน\n"
        "• <code>/send_menu [User ID]</code> — ส่งเมนูหลัก /start ให้เฉพาะบุคคล\n"
        "• <code>/reply [User ID] [ข้อความ]</code> — ส่งข้อความหาเฉพาะบุคคล"
    )
    await callback.message.answer(resp, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_menu:sync")
async def handle_admin_menu_sync_callback(callback: CallbackQuery, bot: Bot):
    """ปุ่มลัดสำหรับสั่งซิงค์สมาชิกตกหล่น"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    
    await callback.answer("⏳ กำลังตรวจสอบและซิงค์ข้อมูลสมาชิก...")
    res = await sync_pending_members(bot=bot)
    sync_msg = (
        "🔄 <b>ผลการตรวจสอบและซิงค์สมาชิกค้าง (Sync Completed)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>ตรวจพบในห้องและเปิดใช้งาน (Activated):</b> {res['activated']} คน\n"
        f"🔴 <b>หมดอายุขณะออฟไลน์และเตะออกแล้ว (Kicked):</b> {res['kicked_expired']} คน\n"
        f"⚪ <b>เคลียร์คำขอเก่าที่ไม่ได้เข้าห้อง (Stale):</b> {res['stale_cleaned']} คน\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>พิมพ์ <code>/summary</code> เพื่อดูรายชื่อสมาชิก Active ล่าสุด</i>"
    )
    await callback.message.answer(text=sync_msg, parse_mode="HTML")


@router.message(Command("sync", "sync_channel"))
async def handle_admin_sync_command(message: Message, bot: Bot):
    """คำสั่งสำหรับแอดมินสั่งซิงค์สมาชิกที่ค้าง PENDING ทั้งหมด: /sync"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    status_msg = await message.answer("⏳ <i>กำลังตรวจสอบรายชื่อสมาชิกและซิงค์ข้อมูลกับ Telegram Channel...</i>", parse_mode="HTML")
    res = await sync_pending_members(bot=bot)

    sync_msg = (
        "🔄 <b>ผลการตรวจสอบและซิงค์สมาชิกค้าง (Sync Completed)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>ตรวจพบในห้องและเปิดใช้งาน (Activated):</b> {res['activated']} คน\n"
        f"🔴 <b>หมดอายุขณะออฟไลน์และเตะออกแล้ว (Kicked):</b> {res['kicked_expired']} คน\n"
        f"⚪ <b>เคลียร์คำขอเก่าที่ไม่ได้เข้าห้อง (Stale):</b> {res['stale_cleaned']} คน\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>พิมพ์ <code>/summary</code> เพื่อดูรายชื่อสมาชิก Active ล่าสุด</i>"
    )
    try:
        await status_msg.edit_text(text=sync_msg, parse_mode="HTML")
    except Exception:
        await message.answer(text=sync_msg, parse_mode="HTML")


@router.message(Command("report", "summary", "stats", "members"))
@router.message(F.text.lower().in_(["report", "summary", "รายงาน", "/report", "/summary"]))
async def handle_admin_report_command(message: Message, bot: Bot):
    """คำสั่งดูรายงานสรุปสมาชิก Active ปัจจุบัน พร้อมเปรียบเทียบ Channel Member จริง (ใน Admin Group หรือ DM สำหรับแอดมิน)"""
    if not is_admin_chat(message.chat.id):
        # หากส่งใน DM ส่วนตัว ตรวจสอบว่าเป็นแอดมินในกลุ่ม Admin หรือไม่
        is_admin_user = False
        if message.from_user:
            try:
                cm = await bot.get_chat_member(chat_id=config.ADMIN_GROUP_ID, user_id=message.from_user.id)
                if cm.status in ("creator", "administrator"):
                    is_admin_user = True
            except Exception:
                pass
        if not is_admin_user:
            return

    status_msg = await message.answer("⏳ <i>กำลังประมวลผลรายงานสรุปสมาชิก...</i>", parse_mode="HTML")
    try:
        report_text = await build_active_members_report(bot=bot)
        chunks = split_text_chunks(report_text, max_chunk_size=3800)
        if chunks:
            try:
                await status_msg.edit_text(text=chunks[0], parse_mode="HTML")
            except Exception:
                await message.answer(text=chunks[0], parse_mode="HTML")
            for chunk in chunks[1:]:
                await message.answer(text=chunk, parse_mode="HTML")
        else:
            await status_msg.edit_text("ℹ️ <i>ไม่มีข้อมูลรายงานสมาชิกในระบบ</i>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error generating active members report: {e}", exc_info=True)
        err_msg = f"❌ <b>เกิดข้อผิดพลาดในการสร้างรายงาน:</b>\n<code>{html.escape(str(e))}</code>"
        try:
            await status_msg.edit_text(text=err_msg, parse_mode="HTML")
        except Exception:
            await message.answer(text=err_msg, parse_mode="HTML")


async def build_user_audit_report(query: str, bot: Bot) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """สร้างรายงาน Audit & Reconciliation ตรวจสอบยอดเวลาและประวัติสมาชิกอย่างละเอียด"""
    now = datetime.now(timezone.utc)
    query_clean = query.strip().lstrip("@")

    async with get_session() as session:
        if query_clean.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query_clean))
            user = (await session.execute(user_stmt)).scalar_one_or_none()
        else:
            user_stmt = select(User).where(User.username.ilike(query_clean))
            user = (await session.execute(user_stmt)).scalar_one_or_none()
            if not user:
                user_stmt = select(User).where(User.full_name.ilike(f"%{query_clean}%"))
                user = (await session.execute(user_stmt)).scalars().first()

        if not user and query_clean.isdigit():
            target_uid = int(query_clean)
            for cid in get_all_target_channel_ids():
                try:
                    cm = await bot.get_chat_member(chat_id=cid, user_id=target_uid)
                    tg_u = getattr(cm, "user", None)
                    if tg_u:
                        user, _ = await get_or_create_user(
                            session=session,
                            telegram_id=target_uid,
                            username=tg_u.username,
                            full_name=tg_u.full_name,
                        )
                        break
                except Exception:
                    pass

        if not user:
            return f"❌ <b>ไม่พบข้อมูลผู้ใช้:</b> <code>{html.escape(query)}</code> ในระบบฐานข้อมูล", None

        # 1. ข้อมูลผู้ใช้ที่ถูกชวนโดย user คนนี้
        ref_stmt = select(User).where(User.referred_by_id == user.telegram_id)
        referred_users = (await session.execute(ref_stmt)).scalars().all()

        # 2. สถานะสมาชิกปัจจุบัน (1 แถวเดียว) + ประวัติการเติมวันทั้งหมด (ledger)
        sub = await session.get(Subscription, user.telegram_id)
        grants_stmt = (
            select(SubscriptionGrant)
            .where(SubscriptionGrant.user_id == user.telegram_id)
            .order_by(SubscriptionGrant.id.asc())
        )
        grants = (await session.execute(grants_stmt)).scalars().all()

        # 3. ข้อมูล PaymentSlips
        slips_stmt = (
            select(PaymentSlip)
            .where(PaymentSlip.user_id == user.telegram_id)
            .order_by(PaymentSlip.id.asc())
        )
        slips = (await session.execute(slips_stmt)).scalars().all()

    # ตรวจสอบสถานะจริงในทุก Channel พร้อมอัปเดตชื่อผู้ใช้ล่าสุดจาก Telegram
    in_channels, status_map, tg_u = await check_user_presence_all_channels(bot, user.telegram_id)
    channel_status_str = format_user_channel_presence(in_channels)
    is_in_channel = len(in_channels) > 0

    if tg_u:
        updated_meta = False
        if tg_u.full_name and user.full_name != tg_u.full_name:
            user.full_name = tg_u.full_name
            updated_meta = True
        if tg_u.username and user.username != tg_u.username:
            user.username = tg_u.username
            updated_meta = True
        if updated_meta:
            async with get_session() as session:
                db_u = (await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalar_one_or_none()
                if db_u:
                    db_u.full_name = user.full_name
                    db_u.username = user.username
                    session.add(db_u)
                    await session.commit()

    # --- คำนวณหมวดที่ 1: การชวนเพื่อน (Referral Ledger) ---
    total_referred_count = len(referred_users)
    active_referred_count = len([u for u in referred_users if u.trial_used])
    earned_ref_bonus_days = user.referral_bonus_days or 0
    ref_match = (user.referral_count == total_referred_count)
    ref_match_str = "✅ ตรงกัน 100%" if ref_match else f"⚠️ ไม่ตรง (ในตาราง {total_referred_count} คน, บันทึก {user.referral_count} คน)"

    # --- คำนวณหมวดที่ 2: แพ็กเกจที่ชำระเงิน & Admin มอบให้ (จาก ledger การเติมวันทั้งหมด) ---
    approved_slips = [s for s in slips if s.status == SlipStatus.APPROVED.value or s.status == "APPROVED"]
    total_paid_thb = sum(s.amount or 0 for s in approved_slips)

    admin_grant_days = sum(g.days for g in grants if g.grant_type == GrantType.ADMIN_GRANT.value)
    package_bought_days = sum(g.days for g in grants if g.grant_type in (GrantType.PURCHASE.value, GrantType.PROMOTION.value))
    referral_grant_days = sum(g.days for g in grants if g.grant_type == GrantType.REFERRAL_BONUS.value)

    # --- คำนวณหมวดที่ 3: รวมสิทธิ์และเวลาทั้งหมด (ผลรวมจาก ledger จุดเดียว ไม่แยกแหล่งข้อมูลอีกต่อไป) ---
    total_entitled_days = sum(g.days for g in grants)

    # โควต้า PENDING / ACTIVE ปัจจุบัน (จากแถว Subscription แถวเดียว)
    has_pending = bool(sub and sub.status == SubStatus.PENDING.value and ((sub.pending_days or 0) > 0 or (sub.pending_minutes or 0) > 0))
    pending_days = sub.pending_days if has_pending else 0
    pending_minutes = sub.pending_minutes if has_pending else 0

    current_active_sub = sub if (sub and sub.status == SubStatus.ACTIVE.value and sub.expires_at and ensure_utc(sub.expires_at) > now) else None

    # ประมาณเวลาที่ใช้ไปแล้วในอดีต = สิทธิ์ตลอดชีพทั้งหมด - เวลาที่ยังเหลืออยู่ตอนนี้ (ACTIVE + PENDING)
    remaining_days_now = 0.0
    if current_active_sub:
        remaining_days_now = (ensure_utc(current_active_sub.expires_at) - now).total_seconds() / 86400.0
    elif has_pending:
        remaining_days_now = pending_days + (pending_minutes / 1440.0)
    consumed_days = max(total_entitled_days - remaining_days_now, 0.0)

    # --- คำนวณหมวดที่ 4: การประเมินผล Audit ---
    verdict_lines = []
    if is_in_channel:
        if current_active_sub:
            verdict_lines.append("• 📢 <b>สถานะใน Channel:</b> ปกติ (อยู่ในห้อง VIP และมีสิทธิ์ ACTIVE ถูกต้อง ✅)")
        else:
            verdict_lines.append("• ⚠️ <b>สถานะใน Channel:</b> ผิดปกติ! (อยู่ในห้อง VIP แต่ไม่มีสิทธิ์ ACTIVE ในระบบ ❌)")
    else:
        if has_pending:
            verdict_lines.append("• 📢 <b>สถานะใน Channel:</b> ปกติ (อยู่นอกห้อง สอดคล้องกับมีโควต้า PENDING รอกดเข้า ✅)")
        elif current_active_sub:
            verdict_lines.append("• ℹ️ <b>สถานะใน Channel:</b> สมาชิกมีสิทธิ์ VIP ACTIVE แต่ยังไม่ได้กดเข้าห้อง")
        else:
            verdict_lines.append("• 📢 <b>สถานะใน Channel:</b> ปกติ (หมดอายุและอยู่นอกห้องถูกต้อง ✅)")

    if current_active_sub:
        exp_thai = format_thai_datetime(current_active_sub.expires_at)
        rem_str = format_time_remaining(current_active_sub.expires_at)
        verdict_lines.append(f"• ⏳ <b>วันหมดอายุสมาชิกปัจจุบัน:</b> <code>{exp_thai} น.</code> (คงเหลือ {rem_str})")
        verdict_lines.append("• 🎯 <b>ผลการกระทบยอด:</b> ถูกต้อง 100% สอดคล้องกับประวัติสะสม ✅")
    elif has_pending:
        verdict_lines.append(f"• ⏳ <b>โควต้ารอกดเข้าห้อง:</b> รวม <b>{pending_days} วัน ({pending_days * 24} ชม.)</b>" + (f" + {pending_minutes} นาที" if pending_minutes else ""))
        verdict_lines.append("• 🎯 <b>ผลการกระทบยอด:</b> ถูกต้อง 100% เมื่อกดเข้าห้องจะเริ่มนับวันหมดอายุตรงตามยอดนี้ ✅")
    else:
        verdict_lines.append("• ⏳ <b>เวลาคงเหลือ:</b> 0 วัน (หมดอายุการใช้งานแล้ว)")
        verdict_lines.append("• 🎯 <b>ผลการกระทบยอด:</b> ประวัติทั้งหมดถูกปิดรอบสมบูรณ์ ✅")

    user_header = format_user_title(user.full_name, user.username, user.telegram_id)

    lines = [
        "🔍 <b>[Audit & Reconciliation] ตรวจสอบยอดและประวัติสมาชิก</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}",
        f"📢 <b>สถานะใน Channel VIP:</b> {channel_status_str}",
        f"📅 <b>เข้าระบบบอทครั้งแรก:</b> <code>{format_thai_datetime(user.created_at)} น.</code>",
        f"⏱️ <b>สิทธิ์ทดลองใช้ฟรี:</b> {'✅ เคยใช้แล้ว' if user.trial_used else '❌ ยังไม่เคยใช้ (โควต้า 15 นาที)'}",
        "",
        "📊 <b>1. สมุดบัญชีการชวนเพื่อน (Referral Ledger):</b>",
        f"• 👥 สถิติชวนเพื่อนทั้งหมด: <b>{total_referred_count} คน</b>",
        f"• 🎁 โบนัสสะสมที่ได้รับ (ตัวนับ): <b>{earned_ref_bonus_days} วัน ({earned_ref_bonus_days * 24} ชั่วโมง)</b>",
        f"• 📒 โบนัสจาก Ledger จริง: <b>{referral_grant_days} วัน</b>"
        + (" ✅" if referral_grant_days == earned_ref_bonus_days else f" ⚠️ ไม่ตรงกับตัวนับ (ต่าง {abs(referral_grant_days - earned_ref_bonus_days)} วัน)"),
        f"• 📋 ตรวจสอบความตรงกัน: {ref_match_str}",
        "",
        "💳 <b>2. สมุดบัญชีแพ็กเกจ & สิทธิ์ที่ได้รับ:</b>",
        f"• 💳 สลิปชำระเงินที่อนุมัติ: <b>{len(approved_slips)} รายการ</b> (ยอดรวม {total_paid_thb:,.2f} บาท -> {package_bought_days} วัน)",
        f"• 👑 สิทธิ์ที่ Admin มอบให้ (/add_vip): <b>{admin_grant_days} วัน</b>",
        f"• 📦 จำนวนครั้งที่เติมวันทั้งหมด (Ledger): <b>{len(grants)} รายการ</b>",
        "",
        "📦 <b>3. งบดุลเวลารวมตลอดชีพ (Total Time Ledger):</b>",
        f"➕ <b>รวมสิทธิ์ที่เคยได้รับตลอดชีพ:</b> <b>{total_entitled_days} วัน</b> ({total_entitled_days * 24} ชั่วโมง)",
        f"➖ <b>เวลาที่ใช้งานแล้วในอดีต (ประมาณ, หมดอายุ):</b> ~{consumed_days:.1f} วัน",
        f"⏳ <b>โควต้ารอกดเข้าห้อง (PENDING):</b> <b>{pending_days} วัน ({pending_days * 24} ชั่วโมง)</b>" + (f" + {pending_minutes} นาที" if pending_minutes else ""),
    ]

    if current_active_sub:
        lines.append(f"🟢 <b>เวลาที่กำลัง Active ในห้อง:</b> หมดอายุ <code>{format_thai_datetime(current_active_sub.expires_at)} น.</code>")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚖️ <b>4. สรุปผลการตรวจสอบความถูกต้อง (Audit Verdict):</b>",
    ])
    lines.extend(verdict_lines)
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        "💡 <i>ระบบคำนวณและกระทบยอดจาก Database + Telegram Live Member แบบเรียลไทม์</i>",
    ])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 ดูโปรไฟล์ /user", callback_data=f"admin:view_user:{user.telegram_id}"),
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user.telegram_id}"),
            ],
            [
                InlineKeyboardButton(text="⚡ ปรับยอด & วันหมดอายุให้ตรง (Reconcile)", callback_data=f"admin:do_reconcile:{user.telegram_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 ตรวจสอบใหม่ (Refresh)", callback_data=f"admin:audit_user:{user.telegram_id}"),
            ],
        ]
    )

    return "\n".join(lines), keyboard


async def build_reconcile_preview_report(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """สร้างรายงานพรีวิวก่อนปรับยอด ตามสูตร: วันหมดอายุใหม่ = joined_at + วันซื้อ + วันแอดมิน + วันโบนัสเพื่อน + ทดลองฟรี"""
    results = await reconcile_all_users(session, only_active=False, commit=False)

    changed_list = [r for r in results if r.ref_stats_changed or r.expiry_changed or r.status_changed or (r.excess_ref_grants_deleted > 0)]
    active_list = [r for r in results if r.is_active or (r.status_old == SubStatus.ACTIVE.value)]

    lines = [
        "🔍 <b>[พรีวิวสรุปยอดก่อนปรับ] คำนวณวันหมดอายุตามเกณฑ์จริง</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "📐 <b>สูตรการคำนวณมาตรฐาน:</b>",
        "<code>วันหมดอายุใหม่ = วันที่เข้าครั้งแรก (joined_at) + วันซื้อ + วันแอดมินให้ + วันโบนัสเพื่อนจริง (หลังหักซ้ำ) + ทดลองฟรี</code>",
        "",
        "📊 <b>สถิติภาพรวม:</b>",
        f"• 👥 สมาชิกทั้งหมดที่สแกน: <b>{len(results)} คน</b>",
        f"• 🟢 สมาชิก Active: <b>{len(active_list)} คน</b>",
        f"• ⚠️ สมาชิกที่ตัวเลข<b>ไม่ตรงและต้องปรับแก้:</b> <b>{len(changed_list)} คน</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    display_targets = changed_list if changed_list else results
    if display_targets:
        lines.append("📋 <b>รายละเอียดแจกแจงตามสูตรรายบุคคล:</b>\n")
        for r in display_targets[:10]:
            lines.append(format_reconcile_formula(r))
            lines.append("")

        if len(display_targets) > 10:
            lines.append(f"<i>...และมีผู้ใช้อีก {len(display_targets) - 10} คน</i>\n")
    else:
        lines.append("✨ <i>ไม่พบข้อมูลผู้ใช้ในระบบ</i>\n")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>สามารถเลือกกดปุ่มด้านล่างเพื่อ 'ปรับเฉพาะบุคคล' หรือ 'ปรับทุกคนพร้อมกัน' ได้ทันทีครับ</i>")

    keyboard_buttons = []
    # สร้างปุ่มสำหรับปรับทีละคน (เฉพาะคนที่ตัวเลขไม่ตรง สูงสุด 5 คนแรก)
    for r in changed_list[:5]:
        u_label = f"⚡ ปรับ: {r.full_name[:12]} (ID: {r.user_id})"
        keyboard_buttons.append([
            InlineKeyboardButton(text=u_label, callback_data=f"admin:do_reconcile:{r.user_id}")
        ])

    if changed_list:
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"🚀 ปรับยอดทุกคนที่พบปัญหา ({len(changed_list)} คน)", callback_data="admin:confirm_reconcile_all")
        ])
    else:
        keyboard_buttons.append([
            InlineKeyboardButton(text="🚀 ยืนยัน Reconcile ทั้งระบบอีกครั้ง", callback_data="admin:confirm_reconcile_all")
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="🔄 ตรวจสอบและรีเฟรชพรีวิวใหม่", callback_data="admin:refresh_reconcile_preview")
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)


@router.message(Command("reconcile_preview", "reconcile_check", "reconcile_report"))
async def handle_admin_reconcile_preview_command(message: Message):
    """คำสั่งแอดมินสำหรับดูรายงานสรุปพรีวิวการคำนวณตามสูตรก่อนปรับยอด: /reconcile_preview"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    status_msg = await message.answer("⏳ <b>กำลังคำนวณและจัดทำรายงานพรีวิวตามสูตร...</b>", parse_mode="HTML")
    async with get_session() as session:
        resp_text, keyboard = await build_reconcile_preview_report(session)

    try:
        await status_msg.delete()
    except Exception:
        pass

    chunks = split_text_chunks(resp_text, max_chunk_size=3800)
    for i, chunk in enumerate(chunks):
        reply_kb = keyboard if i == len(chunks) - 1 else None
        await message.answer(text=chunk, reply_markup=reply_kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:refresh_reconcile_preview")
async def handle_admin_refresh_reconcile_preview_callback(callback: CallbackQuery):
    """Callback รีเฟรชหน้าพรีวิว Reconcile"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    await callback.answer("🔄 กำลังคำนวณพรีวิวใหม่...")
    async with get_session() as session:
        resp_text, keyboard = await build_reconcile_preview_report(session)

    chunks = split_text_chunks(resp_text, max_chunk_size=3800)
    for i, chunk in enumerate(chunks):
        reply_kb = keyboard if i == len(chunks) - 1 else None
        await callback.message.answer(text=chunk, reply_markup=reply_kb, parse_mode="HTML")


@router.message(Command("audit_user", "verify_user", "reconcile", "audit_sub"))
async def handle_admin_user_audit_command(message: Message, bot: Bot):
    """คำสั่งแอดมินตรวจสอบยอดและกระทบยอดเวลาสมาชิก: /audit_user <User ID หรือ @username> หรือ /reconcile เพื่อดูพรีวิวทุกคน"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        # หากไม่ระบุ User ID -> แสดงหน้ารายงานสรุปพรีวิวทุกคนทั้งระบบ
        status_msg = await message.answer("⏳ <b>กำลังคำนวณและจัดทำรายงานพรีวิวตามสูตร...</b>", parse_mode="HTML")
        async with get_session() as session:
            resp_text, keyboard = await build_reconcile_preview_report(session)
        try:
            await status_msg.delete()
        except Exception:
            pass
        chunks = split_text_chunks(resp_text, max_chunk_size=3800)
        for i, chunk in enumerate(chunks):
            reply_kb = keyboard if i == len(chunks) - 1 else None
            await message.answer(text=chunk, reply_markup=reply_kb, parse_mode="HTML")
        return

    query = args[1].strip()
    resp_text, keyboard = await build_user_audit_report(query=query, bot=bot)
    await message.answer(text=resp_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:audit_user:"))
async def handle_admin_audit_user_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่ม [🔍 ตรวจสอบยอด (Audit)]"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    resp_text, keyboard = await build_user_audit_report(query=uid_str, bot=bot)
    await callback.message.answer(text=resp_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:do_reconcile:"))
async def handle_admin_do_reconcile_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่ม [⚡ ปรับยอด & วันหมดอายุให้ตรง (Reconcile)] รายบุคคล"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    try:
        target_uid = int(uid_str)
    except ValueError:
        await callback.answer("❌ รหัสผู้ใช้ไม่ถูกต้อง", show_alert=True)
        return

    async with get_session() as session:
        result = await reconcile_user(session, target_uid, commit=True)

    if not result:
        await callback.answer("❌ ไม่พบข้อมูลผู้ใช้", show_alert=True)
        return

    await callback.answer("✅ ปรับยอดข้อมูลและวันหมดอายุเรียบร้อยแล้ว!", show_alert=True)

    # ส่งรายงานอัปเดตใหม่
    resp_text, keyboard = await build_user_audit_report(query=uid_str, bot=bot)
    notice = f"⚡ <b>[ดำเนินการ Reconcile สำเร็จสำหรับ User {target_uid}]</b>\n📝 <i>{html.escape(result.message)}</i>\n\n"
    await callback.message.answer(text=notice + resp_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "admin:confirm_reconcile_all")
async def handle_admin_confirm_reconcile_all_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่มยืนยันปรับยอดทุกคนพร้อมกัน"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    await callback.answer("⏳ กำลังดำเนินการปรับยอดทุกคน...", show_alert=False)

    async with get_session() as session:
        results = await reconcile_all_users(session, only_active=False, commit=True)

    changed_list = [r for r in results if r.ref_stats_changed or r.expiry_changed or r.status_changed or (r.excess_ref_grants_deleted > 0)]

    summary_lines = [
        "✅ <b>[Reconcile All Complete] กระทบยอดและปรับวันหมดอายุทั้งระบบเรียบร้อย</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 <b>ผู้ใช้ที่ตรวจสอบทั้งหมด:</b> {len(results)} คน",
        f"⚡ <b>ผู้ใช้ที่มีการปรับยอดแก้ไข:</b> <b>{len(changed_list)} คน</b>",
        "",
    ]

    if changed_list:
        summary_lines.append("📋 <b>รายชื่อผู้ใช้ที่ได้รับการปรับยอด:</b>")
        for r in changed_list[:20]:
            u_title = format_user_title(r.full_name, r.username, r.user_id)
            summary_lines.append(f"• {u_title}\n  └ <i>{html.escape(r.message)}</i>")
        if len(changed_list) > 20:
            summary_lines.append(f"• <i>...และอีก {len(changed_list) - 20} คน</i>")
    else:
        summary_lines.append("✨ <i>ข้อมูลผู้ใช้และวันหมดอายุของทุกคนถูกต้องตรงตามเกณฑ์แล้ว 100%</i>")

    summary_lines.append("━━━━━━━━━━━━━━━━━━━━")
    report_text = "\n".join(summary_lines)

    chunks = split_text_chunks(report_text, max_chunk_size=3800)
    for chunk in chunks:
        await callback.message.answer(text=chunk, parse_mode="HTML")


@router.message(Command("reconcile_all", "reconcile_users", "fix_all_expiry"))
async def handle_admin_reconcile_all_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับ Reconcile สถิติการชวนเพื่อนและวันหมดอายุของสมาชิกทุกคนในระบบ: /reconcile_all"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    status_msg = await message.answer("⏳ <b>กำลังเริ่มตรวจสอบและ Reconcile ผู้ใช้ทุกคนในฐานข้อมูล...</b>", parse_mode="HTML")

    async with get_session() as session:
        results = await reconcile_all_users(session, only_active=False, commit=True)

    changed_list = [r for r in results if r.ref_stats_changed or r.expiry_changed or r.status_changed or (r.excess_ref_grants_deleted > 0)]

    summary_lines = [
        "✅ <b>[Reconcile All Complete] กระทบยอดและปรับวันหมดอายุทั้งระบบเรียบร้อย</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 <b>ผู้ใช้ที่ตรวจสอบทั้งหมด:</b> {len(results)} คน",
        f"⚡ <b>ผู้ใช้ที่มีการปรับยอดแก้ไข:</b> <b>{len(changed_list)} คน</b>",
        "",
    ]

    if changed_list:
        summary_lines.append("📋 <b>รายชื่อผู้ใช้ที่ได้รับการปรับยอด:</b>")
        for r in changed_list[:20]:
            u_title = format_user_title(r.full_name, r.username, r.user_id)
            summary_lines.append(f"• {u_title}\n  └ <i>{html.escape(r.message)}</i>")
        if len(changed_list) > 20:
            summary_lines.append(f"• <i>...และอีก {len(changed_list) - 20} คน</i>")
    else:
        summary_lines.append("✨ <i>ข้อมูลผู้ใช้และวันหมดอายุของทุกคนถูกต้องตรงตามเกณฑ์แล้ว 100%</i>")

    summary_lines.append("━━━━━━━━━━━━━━━━━━━━")
    report_text = "\n".join(summary_lines)

    chunks = split_text_chunks(report_text, max_chunk_size=3800)
    for chunk in chunks:
        await message.answer(text=chunk, parse_mode="HTML")

    try:
        await status_msg.delete()
    except Exception:
        pass


@router.message(Command("audit", "check"))
async def handle_admin_audit_command(message: Message, bot: Bot):
    """คำสั่งตรวจสอบสถานะและสิทธิ์ของ Bot ใน Channel VIP และข้อมูลระบบ (หรือตรวจสอบผู้ใช้ถ้าใส่ User ID)"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) >= 2:
        query = args[1].strip()
        resp_text, keyboard = await build_user_audit_report(query=query, bot=bot)
        await message.answer(text=resp_text, reply_markup=keyboard, parse_mode="HTML")
        return

    status_lines = ["🔍 <b>ตรวจสอบสถานะและความพร้อมของระบบ (System Audit)</b>\n"]
    
    # 1. ตรวจสอบ Bot ใน Telegram Channels ทั้งหมด
    try:
        bot_info = await bot.get_me()
        chat_info = await bot.get_chat(chat_id=config.CHANNEL_ID)
        member_count = await bot.get_chat_member_count(chat_id=config.CHANNEL_ID)
        bot_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=bot_info.id)

        status_lines.append(f"📢 <b>Channel VIP หลัก:</b> {html.escape(chat_info.title or '')} (<code>{config.CHANNEL_ID}</code>)")
        status_lines.append(f"👥 <b>จำนวนสมาชิก:</b> {member_count} คน")
        
        is_admin = bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        status_lines.append(f"🤖 <b>สถานะบอท:</b> {'✅ Administrator' if is_admin else '❌ ไม่ได้เป็น Admin'}")

        if is_admin and hasattr(bot_member, "can_restrict_members"):
            can_ban = bot_member.can_restrict_members
            can_invite = bot_member.can_invite_users
            status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if can_ban else '❌ ขาดสิทธิ์ (สำคัญมาก!)'}")
            status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if can_invite else '❌ ขาดสิทธิ์'}")
    except Exception as e:
        status_lines.append(f"⚠️ <b>ตรวจสอบ Channel VIP หลักล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

    if config.SECONDARY_CHANNEL_ID:
        status_lines.append("")
        try:
            sec_chat_info = await bot.get_chat(chat_id=config.SECONDARY_CHANNEL_ID)
            sec_member_count = await bot.get_chat_member_count(chat_id=config.SECONDARY_CHANNEL_ID)
            sec_bot_member = await bot.get_chat_member(chat_id=config.SECONDARY_CHANNEL_ID, user_id=bot_info.id)

            status_lines.append(f"🌟 <b>Channel ใหม่ (Target):</b> {html.escape(sec_chat_info.title or '')} (<code>{config.SECONDARY_CHANNEL_ID}</code>)")
            status_lines.append(f"👥 <b>จำนวนสมาชิก:</b> {sec_member_count} คน")
            
            sec_is_admin = sec_bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
            status_lines.append(f"🤖 <b>สถานะบอท:</b> {'✅ Administrator' if sec_is_admin else '❌ ไม่ได้เป็น Admin'}")

            if sec_is_admin and hasattr(sec_bot_member, "can_restrict_members"):
                sec_can_ban = sec_bot_member.can_restrict_members
                sec_can_invite = sec_bot_member.can_invite_users
                status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if sec_can_ban else '❌ ขาดสิทธิ์'}")
                status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if sec_can_invite else '❌ ขาดสิทธิ์'}")
        except Exception as e:
            status_lines.append(f"⚠️ <b>ตรวจสอบ Channel ใหม่ล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

    status_lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    status_lines.append("💡 <i>พิมพ์ <code>/audit_user [User ID]</code> เพื่อตรวจสอบยอดและประวัติสมาชิกรายบุคคล\nหรือ <code>/summary</code> เพื่อดูรายชื่อสมาชิก Active</i>")

    await message.answer(text="\n".join(status_lines), parse_mode="HTML")


@router.message(Command("kick"))
async def handle_admin_kick_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับบังคับเตะผู้ใช้ออกจากทุก Target Channel: /kick <user_id>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("❌ <b>วิธีใช้งาน:</b> <code>/kick [User ID]</code>\nตัวอย่าง: <code>/kick 5125375696</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(args[1])
    except ValueError:
        await message.answer("❌ User ID ต้องเป็นตัวเลขเท่านั้น", parse_mode="HTML")
        return

    # ดำเนินการ Soft-kick จากทุก Channel (ทั้งห้องเดิมและห้องใหม่)
    kick_results = await kick_user_from_all_target_channels(bot, target_uid)
    kicked = len(kick_results["failed_channels"]) == 0 or len(kick_results["kicked_channels"]) > 0
    err_msg = "; ".join(f"{cid}: {err}" for cid, err in kick_results["errors"].items()) if kick_results["errors"] else ""

    # อัปเดตสถานะใน DB
    async with get_session() as session:
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == target_uid,
                Subscription.status.in_([SubStatus.ACTIVE.value, SubStatus.PENDING.value, SubStatus.KICK_FAILED.value, SubStatus.EXPIRED.value]),
            )
        )
        subs = (await session.execute(stmt)).scalars().all()
        for s in subs:
            s.status = SubStatus.KICKED.value
            session.add(s)
        await session.commit()

    if kicked:
        # ส่งข้อความแจ้งเตือนทาง DM ให้ผู้ใช้
        try:
            kick_dm = (
                "⚠️ <b>คุณถูกนำออกจาก Channel VIP โดยผู้ดูแลระบบ</b>\n\n"
                "สถานะสมาชิกของคุณถูกยกเลิกแล้วครับ\n"
                "หากต้องการสมัครสมาชิกใหม่หรือมีข้อสงสัย สามารถพิมพ์ <b>/start</b> เพื่อดูเมนูหรือติดต่อผู้ดูแลระบบได้ครับ"
            )
            await bot.send_message(chat_id=target_uid, text=kick_dm, parse_mode="HTML")
        except Exception as e:
            logger.debug(f"Could not send kick DM to user {target_uid}: {e}")

        await message.answer(f"✅ <b>ดำเนินการ Soft-Kick สำเร็จ!</b>\nนำ User ID <code>{target_uid}</code> ออกจาก Channel เรียบร้อยแล้ว (ส่งแจ้งเตือน DM แล้ว)", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ <b>เตะไม่สำเร็จ:</b> <code>{html.escape(err_msg)}</code>\n(แต่ได้อัปเดตสถานะในฐานข้อมูลเป็น KICKED แล้ว)", parse_mode="HTML")


@router.message(Command("add_vip"))
async def handle_admin_add_vip_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับเพิ่มสิทธิ์ VIP ให้ผู้ใช้ด้วยตนเอง (รองรับทั้งวันและชั่วโมง): /add_vip <User ID หรือ @username> [ระยะเวลา เช่น 30, 12h, 1d 6h]"""
    if not is_admin_chat(message.chat.id):
        is_admin_user = False
        if message.from_user:
            try:
                cm = await bot.get_chat_member(chat_id=config.ADMIN_GROUP_ID, user_id=message.from_user.id)
                if cm.status in ("creator", "administrator"):
                    is_admin_user = True
            except Exception:
                pass
        if not is_admin_user:
            return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/add_vip [User ID หรือ @username] [ระยะเวลา เช่น 30, 30d, 12h, 1d 12h]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/add_vip 5125375696 30</code> (เพิ่ม 30 วัน)\n"
            "• <code>/add_vip @numiruuna 12h</code> (เพิ่ม 12 ชั่วโมง)\n"
            "• <code>/add_vip @numiruuna 1d 12h</code> (เพิ่ม 1 วัน 12 ชั่วโมง)\n"
            "• <code>/add_vip 5125375696 30m</code> (เพิ่ม 30 นาที)",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    dur_text = args[2].strip() if len(args) >= 3 else "30"
    try:
        days, minutes, duration_label = parse_duration_input(dur_text, allow_zero=False)
    except ValueError as ve:
        await message.answer(f"❌ {ve}", parse_mode="HTML")
        return

    now = datetime.now(timezone.utc)
    is_stack_extension = False
    new_expires_at = None
    old_expires_at = None

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            if query.isdigit():
                target_uid = int(query)
                user, _ = await get_or_create_user(
                    session=session,
                    telegram_id=target_uid,
                    full_name=f"User {target_uid}",
                )
            else:
                await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id

        target_channel_id = get_user_target_channel_id(user)
        target_channel_label = get_channel_label(target_channel_id)

        # เก็บวันหมดอายุเดิมก่อนเพิ่ม
        sub_before = await session.get(Subscription, target_uid)
        old_expires_at = ensure_utc(sub_before.expires_at) if (sub_before and sub_before.expires_at) else None

        # ตรวจสอบสถานะจริงใน Channel เป้าหมาย
        is_in_channel = False
        try:
            chat_member = await bot.get_chat_member(chat_id=target_channel_id, user_id=target_uid)
            is_in_channel = chat_member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.RESTRICTED,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            )
            if chat_member.user:
                if chat_member.user.username:
                    user.username = chat_member.user.username
                if chat_member.user.full_name:
                    user.full_name = chat_member.user.full_name
                session.add(user)
        except Exception:
            is_in_channel = False

        grant = await grant_subscription(
            session,
            user_id=target_uid,
            days=days,
            minutes=minutes,
            source_label=f"VIP {duration_label} (Admin เพิ่ม)",
            grant_type=GrantType.ADMIN_GRANT.value,
            has_value=True,
            is_in_channel=is_in_channel,
        )
        is_stack_extension = grant.is_stack_extension
        new_expires_at = grant.new_expires_at
        if is_stack_extension:
            logger.info(f"Admin add_vip: Extended active sub for User {target_uid} by +{duration_label} in {target_channel_label}. New expires_at: {new_expires_at}")

        await session.commit()

    invite_url = "-"
    if not is_in_channel:
        try:
            await unban_user_in_channel(bot, target_channel_id, target_uid)
            invite_link_obj = await bot.create_chat_invite_link(
                chat_id=target_channel_id,
                member_limit=1,
                expire_date=now + timedelta(days=7),
                name=f"ManualVIP-{target_uid}",
            )
            invite_url = invite_link_obj.invite_link
        except Exception as e:
            logger.warning(f"Could not generate invite link for {target_channel_id}: {e}")

    # ส่ง DM หาผู้ใช้ (สำหรับ /add_vip)
    exp_thai = format_thai_datetime(new_expires_at) if new_expires_at else "-"
    old_exp_thai = format_thai_datetime(old_expires_at) if old_expires_at else "ไม่มีข้อมูลเดิม / สมาชิกใหม่"
    time_rem = format_time_remaining(new_expires_at) if new_expires_at else duration_label
    admin_display = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else (html.escape(message.from_user.full_name) if message.from_user else "Admin")
    admin_id_str = str(message.from_user.id) if message.from_user else "-"

    try:
        if is_stack_extension:
            dm_text = (
                f"🎉 <b>คุณได้รับการต่อเวลาสมาชิก VIP (+{duration_label}) จากทีมงานเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ <b>วันหมดอายุใหม่ของคุณ:</b> <code>{exp_thai} น.</code>\n"
                f"⏰ <b>เวลาคงเหลือรวม:</b> {time_rem}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>สามารถรับชมเนื้อหาใน {target_channel_label} ได้ต่อเนื่องทันทีครับ! 🚀</i>"
            )
        elif is_in_channel:
            dm_text = (
                f"🎉 <b>คุณได้รับสิทธิ์สมาชิก VIP ({duration_label}) จากแอดมินเรียบร้อยแล้ว!</b>\n\n"
                f"⏳ <b>หมดอายุวันที่:</b> <code>{exp_thai} น.</code>\n\n"
                f"💡 <i>คุณอยู่ใน {target_channel_label} อยู่แล้ว ใช้งานได้ทันทีโดยไม่ต้องกดลิงก์ใหม่ครับ!</i>"
            )
        else:
            dm_text = (
                f"🎉 <b>คุณได้รับสิทธิ์สมาชิก VIP ({duration_label}) จากแอดมินเรียบร้อยแล้ว!</b>\n\n"
                f"⏳ <b>หมดอายุวันที่:</b> <code>{exp_thai} น.</code>\n\n"
                f"🔗 <b>ลิงก์เข้าร่วม {target_channel_label}:</b>\n{invite_url}"
            )
        await bot.send_message(
            chat_id=target_uid,
            text=dm_text,
            parse_mode="HTML",
        )
    except Exception:
        pass

    user_header = format_user_title(user.full_name, user.username, target_uid)
    action_title = "ต่อเวลาสะสม VIP" if is_stack_extension else ("เพิ่มสิทธิ์ VIP (อยู่ในห้องแล้ว)" if is_in_channel else "เพิ่มสิทธิ์ VIP (ผู้ใช้ใหม่)")
    status_badge = format_subscription_status_display(grant.subscription)
    resp = (
        f"✅ <b>{action_title} สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"📱 <b>Channel:</b> <b>{target_channel_label}</b>\n"
        f"➕ <b>เวลาที่เพิ่ม:</b> <b>+{duration_label}</b>\n"
        f"⏳ <b>วันหมดอายุเดิม:</b> <code>{old_exp_thai} น.</code>\n"
        f"🎯 <b>วันหมดอายุใหม่:</b> <code>{exp_thai} น.</code> (<i>คงเหลือ {time_rem}</i>)\n"
        f"📊 <b>สถานะหลังปรับ:</b> <b>{status_badge}</b>\n"
        f"👑 <b>ดำเนินการโดย:</b> {admin_display} (<code>{admin_id_str}</code>)\n"
        f"📅 <b>เวลาบันทึก:</b> <code>{format_thai_datetime(now)} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not is_in_channel:
        resp += f"🔗 <b>Invite Link (7 วัน):</b> <code>{invite_url}</code>\n"
        resp += "📬 <i>ระบบได้ส่งข้อความ DM พร้อมลิงก์เชิญเข้าห้องให้ผู้ใช้แล้ว</i>"
    else:
        resp += "📬 <i>ระบบได้ส่งข้อความ DM แจ้งการต่อเวลาให้ผู้ใช้แล้ว</i>"

    await message.answer(resp, parse_mode="HTML")
    if not is_admin_chat(message.chat.id):
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=f"📢 <b>[Admin Log: /add_vip]</b>\n\n{resp}", parse_mode="HTML")
        except Exception:
            pass


@router.message(Command("deduct_vip", "reduce_vip", "minus_vip", "del_days", "remove_vip"))
async def handle_admin_deduct_vip_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับลดเวลาสมาชิก VIP โดยไม่มีการส่งข้อความหาผู้ใช้: /deduct_vip <User ID หรือ @username> <ระยะเวลา เช่น 7, 7d, 12h, 1d 12h>"""
    if not is_admin_chat(message.chat.id):
        is_admin_user = False
        if message.from_user:
            try:
                cm = await bot.get_chat_member(chat_id=config.ADMIN_GROUP_ID, user_id=message.from_user.id)
                if cm.status in ("creator", "administrator"):
                    is_admin_user = True
            except Exception:
                pass
        if not is_admin_user:
            return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/deduct_vip [User ID หรือ @username] [ระยะเวลาที่ต้องการลด เช่น 7, 7d, 12h, 1d 12h]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/deduct_vip 5125375696 7</code> (ลด 7 วัน)\n"
            "• <code>/deduct_vip @numiruuna 12h</code> (ลด 12 ชั่วโมง)\n"
            "• <code>/deduct_vip @numiruuna 1d 6h</code> (ลด 1 วัน 6 ชั่วโมง)\n\n"
            "🔇 <i>คำสั่งนี้จะปรับลดยอดเวลาในระบบทันที โดย<b>ไม่มีการส่งข้อความ DM ไปหาผู้ใช้</b></i>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    try:
        days_to_deduct, minutes_to_deduct, duration_label = parse_duration_input(args[2].strip(), allow_zero=False)
    except ValueError as ve:
        await message.answer(f"❌ {ve}", parse_mode="HTML")
        return

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return

        target_uid = user.telegram_id
        target_channel_id = get_user_target_channel_id(user)
        target_channel_label = get_channel_label(target_channel_id)

        sub = await session.get(Subscription, target_uid)
        if not sub or not sub.expires_at:
            await message.answer(
                f"❌ ผู้ใช้ {format_user_title(user.full_name, user.username, target_uid)} ยังไม่มีแพ็กเกจหรือวันหมดอายุในระบบ",
                parse_mode="HTML",
            )
            return

        old_expires_at = ensure_utc(sub.expires_at)
        new_expires_at = old_expires_at - timedelta(days=days_to_deduct, minutes=minutes_to_deduct)

        if new_expires_at <= now:
            sub_status_new = SubStatus.EXPIRED.value
            sub.source_label = f"หมดอายุ (Admin ปรับลด -{duration_label})"
        else:
            sub_status_new = SubStatus.ACTIVE.value

        sub.expires_at = new_expires_at
        sub.status = sub_status_new
        session.add(sub)

        # บันทึก Grant ประวัติการลดเวลา
        session.add(SubscriptionGrant(
            user_id=target_uid,
            days=-days_to_deduct,
            minutes=-minutes_to_deduct,
            source_label=f"Admin ลดเวลา (-{duration_label})",
            grant_type=GrantType.ADMIN_GRANT.value,
            has_value=False,
        ))

        await session.commit()

    old_exp_thai = format_thai_datetime(old_expires_at)
    new_exp_thai = format_thai_datetime(new_expires_at)
    time_rem = format_time_remaining(new_expires_at)
    user_header = format_user_title(user.full_name, user.username, target_uid)
    status_badge = format_subscription_status_display(sub)
    admin_display = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else (html.escape(message.from_user.full_name) if message.from_user else "Admin")
    admin_id_str = str(message.from_user.id) if message.from_user else "-"

    kick_line = ""
    if new_expires_at <= now:
        try:
            kick_res = await kick_user_from_all_target_channels(bot, target_uid)
            if len(kick_res["failed_channels"]) == 0:
                kick_line = "🚪 <b>การเตะออกจากห้อง:</b> นำออกจาก Channel สำเร็จ ✅\n"
            else:
                kick_line = "⚠️ <b>การเตะออกจากห้อง:</b> บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์บอท)\n"
        except Exception as e:
            kick_line = f"⚠️ <b>การเตะออกจากห้อง:</b> เกิดข้อผิดพลาด ({e})\n"

    resp = (
        "➖ <b>ปรับลดเวลาสมาชิก VIP สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"📱 <b>Channel:</b> <b>{target_channel_label}</b>\n"
        f"➖ <b>เวลาที่ปรับลด:</b> <b>-{duration_label}</b>\n"
        f"⏳ <b>วันหมดอายุเดิม:</b> <code>{old_exp_thai} น.</code>\n"
        f"🎯 <b>วันหมดอายุใหม่:</b> <code>{new_exp_thai} น.</code> (<i>คงเหลือ {time_rem}</i>)\n"
        f"📊 <b>สถานะหลังปรับ:</b> <b>{status_badge}</b>\n"
        f"{kick_line}"
        f"👑 <b>ดำเนินการโดย:</b> {admin_display} (<code>{admin_id_str}</code>)\n"
        f"📅 <b>เวลาบันทึก:</b> <code>{format_thai_datetime(now)} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔇 <i>หมายเหตุ: ปรับลดในระบบแล้ว (ไม่มีการส่ง DM ไปหาผู้ใช้)</i>"
    )
    await message.answer(resp, parse_mode="HTML")
    if not is_admin_chat(message.chat.id):
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=f"📢 <b>[Admin Log: /deduct_vip]</b>\n\n{resp}", parse_mode="HTML")
        except Exception:
            pass


@router.message(Command("set_vip", "set_days", "set_expiry", "override_vip"))
async def handle_admin_set_vip_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับปรับตั้งค่าเวลาคงเหลือของสมาชิก VIP โดยตรง (ไม่มีการส่ง DM หาผู้ใช้): /set_vip <User ID หรือ @username> <ระยะเวลา เช่น 30, 30d, 12h, 1d 12h, 0>"""
    if not is_admin_chat(message.chat.id):
        is_admin_user = False
        if message.from_user:
            try:
                cm = await bot.get_chat_member(chat_id=config.ADMIN_GROUP_ID, user_id=message.from_user.id)
                if cm.status in ("creator", "administrator"):
                    is_admin_user = True
            except Exception:
                pass
        if not is_admin_user:
            return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/set_vip [User ID หรือ @username] [ระยะเวลา เช่น 30, 30d, 12h, 1d 12h, 0]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/set_vip 5125375696 30</code> (ตั้งให้เหลือ 30 วันนับจากตอนนี้)\n"
            "• <code>/set_vip @numiruuna 12h</code> (ตั้งให้เหลือ 12 ชั่วโมงนับจากตอนนี้)\n"
            "• <code>/set_vip @numiruuna 1d 6h</code> (ตั้งให้เหลือ 1 วัน 6 ชั่วโมง)\n"
            "• <code>/set_vip @numiruuna 0</code> (ตั้งให้หมดอายุทันที)\n\n"
            "🔇 <i>คำสั่งนี้จะปรับตั้งค่ายอดเวลาในระบบทันที โดย<b>ไม่มีการส่งข้อความ DM ไปหาผู้ใช้</b></i>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    try:
        exact_days, exact_minutes, duration_label = parse_duration_input(args[2].strip(), allow_zero=True)
    except ValueError as ve:
        await message.answer(f"❌ {ve}", parse_mode="HTML")
        return

    now = datetime.now(timezone.utc)
    is_positive = (exact_days > 0 or exact_minutes > 0)

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            if query.isdigit():
                target_uid = int(query)
                user, _ = await get_or_create_user(
                    session=session,
                    telegram_id=target_uid,
                    full_name=f"User {target_uid}",
                )
            else:
                await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id

        target_channel_id = get_user_target_channel_id(user)
        target_channel_label = get_channel_label(target_channel_id)

        sub = await session.get(Subscription, target_uid)
        if not sub:
            sub = Subscription(
                user_id=target_uid,
                status=SubStatus.ACTIVE.value if is_positive else SubStatus.EXPIRED.value,
                joined_at=now,
                expires_at=now + timedelta(days=exact_days, minutes=exact_minutes) if is_positive else now,
                created_at=now,
                source_label=f"VIP {duration_label} (Admin กำหนดตรง)" if is_positive else "หมดอายุ (Admin กำหนด 0 วัน)",
            )
            session.add(sub)
            old_expires_at = None
        else:
            old_expires_at = ensure_utc(sub.expires_at) if sub.expires_at else None

        if is_positive:
            new_expires_at = now + timedelta(days=exact_days, minutes=exact_minutes)
            sub_status_new = SubStatus.ACTIVE.value
            sub.source_label = f"VIP {duration_label} (Admin กำหนดตรง)"
            if not sub.joined_at:
                sub.joined_at = now
        else:
            new_expires_at = now
            sub_status_new = SubStatus.EXPIRED.value
            sub.source_label = "หมดอายุ (Admin กำหนด 0 วัน)"

        sub.expires_at = new_expires_at
        sub.status = sub_status_new
        sub.pending_days = 0
        sub.pending_minutes = 0
        sub.pending_has_value = False
        sub.pending_since = None
        session.add(sub)

        # บันทึก Grant ประวัติการกำหนดเวลา
        session.add(SubscriptionGrant(
            user_id=target_uid,
            days=exact_days,
            minutes=exact_minutes,
            source_label=f"Admin กำหนดเวลาตรง ({duration_label})",
            grant_type=GrantType.ADMIN_GRANT.value,
            has_value=True if is_positive else False,
        ))

        await session.commit()

    old_exp_thai = format_thai_datetime(old_expires_at) if old_expires_at else "ไม่มีข้อมูลเดิม"
    new_exp_thai = format_thai_datetime(new_expires_at)
    time_rem = format_time_remaining(new_expires_at)
    user_header = format_user_title(user.full_name, user.username, target_uid)
    status_badge = format_subscription_status_display(sub)
    admin_display = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else (html.escape(message.from_user.full_name) if message.from_user else "Admin")
    admin_id_str = str(message.from_user.id) if message.from_user else "-"

    kick_line = ""
    if not is_positive:
        try:
            kick_res = await kick_user_from_all_target_channels(bot, target_uid)
            if len(kick_res["failed_channels"]) == 0:
                kick_line = "🚪 <b>การเตะออกจากห้อง:</b> นำออกจาก Channel สำเร็จ ✅\n"
            else:
                kick_line = "⚠️ <b>การเตะออกจากห้อง:</b> บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์บอท)\n"
        except Exception as e:
            kick_line = f"⚠️ <b>การเตะออกจากห้อง:</b> เกิดข้อผิดพลาด ({e})\n"

    resp = (
        "⚙️ <b>กำหนดเวลาสมาชิก VIP ตรงสำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"📱 <b>Channel:</b> <b>{target_channel_label}</b>\n"
        f"🎯 <b>ตั้งเวลาคงเหลือ:</b> <b>{duration_label}</b> (นับจากตอนนี้)\n"
        f"⏳ <b>วันหมดอายุเดิม:</b> <code>{old_exp_thai} น.</code>\n"
        f"🚀 <b>วันหมดอายุใหม่:</b> <code>{new_exp_thai} น.</code> (<i>คงเหลือ {time_rem}</i>)\n"
        f"📊 <b>สถานะหลังปรับ:</b> <b>{status_badge}</b>\n"
        f"{kick_line}"
        f"👑 <b>ดำเนินการโดย:</b> {admin_display} (<code>{admin_id_str}</code>)\n"
        f"📅 <b>เวลาบันทึก:</b> <code>{format_thai_datetime(now)} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔇 <i>หมายเหตุ: ปรับยอดในระบบแล้ว (ไม่มีการส่ง DM ไปหาผู้ใช้)</i>"
    )
    await message.answer(resp, parse_mode="HTML")
    if not is_admin_chat(message.chat.id):
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=f"📢 <b>[Admin Log: /set_vip]</b>\n\n{resp}", parse_mode="HTML")
        except Exception:
            pass


@router.message(Command("move_user", "move", "migrate_user"))
async def handle_admin_move_user_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับย้ายสมาชิกไปยัง Channel ใหม่ (Target Channel) พร้อมส่ง Invite Link 7 วัน: /move_user <User ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    if not config.SECONDARY_CHANNEL_ID:
        await message.answer(
            "⚠️ <b>ยังไม่ได้ตั้งค่า SECONDARY_CHANNEL_ID ใน .env</b>\n\n"
            "กรุณาระบุ Channel ID ของห้องใหม่ในไฟล์ .env (เช่น <code>SECONDARY_CHANNEL_ID=-1001234567890</code>) "
            "และรีสตาร์ทบอทก่อนใช้งานคำสั่งนี้ครับ 🙏",
            parse_mode="HTML",
        )
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/move_user [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/move_user 5125375696</code>\n"
            "• <code>/move_user @some_user</code>\n\n"
            "ℹ️ <i>คำสั่งนี้จะเปลี่ยน Channel ประจำตัวของผู้ใช้เป็น Channel ใหม่ และส่งลิงก์เชิญ (อายุ 7 วัน) ให้ทาง DM</i>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            if query.isdigit():
                target_uid = int(query)
                user, _ = await get_or_create_user(
                    session=session,
                    telegram_id=target_uid,
                    full_name=f"User {target_uid}",
                )
            else:
                await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id

        # บันทึกสถานะว่าย้ายไปยังห้องใหม่แล้ว
        user.is_moved_to_secondary = True
        user.assigned_channel = "SECONDARY"
        session.add(user)
        await session.commit()

    # ปลดแบนใน Channel ใหม่ก่อนสร้างลิงก์
    await unban_user_in_channel(bot, config.SECONDARY_CHANNEL_ID, target_uid)

    # สร้างลิงก์เชิญสำหรับ Channel ใหม่ (อายุ 7 วัน, ใช้งานได้ 1 ครั้ง)
    invite_expire = now + timedelta(days=7)
    invite_url = None
    try:
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=config.SECONDARY_CHANNEL_ID,
            member_limit=1,
            expire_date=invite_expire,
            name=f"Move-{target_uid}",
        )
        invite_url = invite_link_obj.invite_link
    except Exception as e:
        logger.error(f"Failed to generate move invite link for User {target_uid} in {config.SECONDARY_CHANNEL_ID}: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>ไม่สามารถสร้างลิงก์เชิญสำหรับ Channel ใหม่ ({config.SECONDARY_CHANNEL_ID}) ได้!</b>\n\n"
            f"สาเหตุ: <code>{html.escape(str(e))}</code>\n"
            "กรุณาตรวจสอบว่าบอทเป็น Administrator ใน Channel ใหม่ และมีสิทธิ์สร้างลิงก์เชิญ (Invite Users)",
            parse_mode="HTML",
        )
        return

    # ส่ง DM ให้ผู้ใช้
    user_header = format_user_title(user.full_name, user.username, target_uid)
    expire_thai = format_thai_datetime(invite_expire)
    target_channel_title = get_channel_label(config.SECONDARY_CHANNEL_ID)
    user_dm_sent = False

    user_move_text = (
        f"🎉 <b>คุณได้รับคำเชิญให้ย้ายเข้าสู่ {target_channel_title}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"แอดมินได้ส่งลิงก์เชิญพิเศษสำหรับคุณ เพื่อเข้าร่วม Channel VIP ห้องใหม่ (<b>{target_channel_title}</b>) เรียบร้อยแล้วครับ 🚀\n\n"
        f"🔗 <b>ลิงก์เชิญส่วนตัว (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
        f"⏳ <b>ลิงก์หมดอายุวันที่:</b> <code>{expire_thai} น.</code> (มีอายุ 7 วัน)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <i>ข้อควรทราบ: สิทธิ์และเวลาสมาชิกของคุณจะทำงานอย่างต่อเนื่องเหมือนเดิม กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ!</i>"
    )

    join_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 เข้าร่วม {target_channel_title} ตอนนี้", url=invite_url)]
        ]
    )

    try:
        await bot.send_message(
            chat_id=target_uid,
            text=user_move_text,
            reply_markup=join_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        user_dm_sent = True
    except Exception as e:
        logger.warning(f"Could not send move DM to User {target_uid}: {e}")

    try:
        await log_chat_message(
            user_id=target_uid,
            sender_role="BOT",
            message_text=f"[ระบบส่งลิงก์ย้าย {target_channel_title} (อายุ 7 วัน): {invite_url}]"
        )
    except Exception:
        pass

    admin_reply = (
        "🌟 <b>ย้ายสมาชิกไปยัง Channel ใหม่สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"📢 <b>Channel เป้าหมาย:</b> <b>{target_channel_title}</b> (<code>{config.SECONDARY_CHANNEL_ID}</code>)\n"
        f"⏳ <b>อายุลิงก์เชิญ:</b> 7 วัน (หมดอายุ: <code>{expire_thai} น.</code>)\n"
        f"🔗 <b>Invite Link:</b>\n<code>{invite_url}</code>\n"
        f"📨 <b>ส่งข้อความ DM หาผู้ใช้:</b> {'สำเร็จ ✅' if user_dm_sent else 'ไม่สำเร็จ (ผู้ใช้บล็อกบอท) ⚠️'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"ℹ️ <i>บันทึกสถานะผู้ใช้เป็น {target_channel_title} เรียบร้อย การซื้อ/ต่ออายุในอนาคตจะส่งลิงก์ห้องนี้ให้อัตโนมัติ</i>\n"
        "<i>เมื่อผู้ใช้กดเข้าร่วมห้อง ระบบจะส่งแจ้งเตือนเข้ากลุ่มแอดมินทันที</i>"
    )

    await message.answer(admin_reply, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("unmove_user", "move_user_back"))
async def handle_admin_unmove_user_command(message: Message):
    """คำสั่งแอดมินสำหรับย้ายผู้ใช้กลับสู่ Channel เดิม (Primary Channel): /unmove_user <User ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ <b>วิธีใช้งาน:</b> <code>/unmove_user [User ID หรือ @username]</code>", parse_mode="HTML")
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return

        user.is_moved_to_secondary = False
        user.assigned_channel = "PRIMARY"
        session.add(user)
        await session.commit()

    primary_title = get_channel_label(config.CHANNEL_ID)
    user_header = format_user_title(user.full_name, user.username, user.telegram_id)
    await message.answer(
        f"🔄 <b>ย้ายผู้ใช้กลับสู่ {primary_title} เรียบร้อย!</b>\n\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"📢 <b>Channel ประจำตัว:</b> <b>{primary_title}</b> (<code>{config.CHANNEL_ID}</code>)",
        parse_mode="HTML"
    )


@router.message(Command("reset_trial", "clear_trial"))
async def handle_admin_reset_trial_command(message: Message, bot: Bot):
    """คำสั่งแอดมินรีเซ็ตเฉพาะสิทธิ์ทดลองฟรี 15 นาที: /reset_trial <@username หรือ User ID>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/reset_trial [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/reset_trial 5125375696</code>\n"
            "• <code>/reset_trial @some_user</code>",
            parse_mode="HTML"
        )
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return

        target_uid = user.telegram_id
        user.trial_used = False
        session.add(user)

        # ปรับสถานะ Subscription trial เดิมเป็น EXPIRED
        sub = await session.get(Subscription, target_uid)
        is_trial_sub = bool(sub) and (
            (sub.status == SubStatus.ACTIVE.value and sub.is_trial_active)
            or (sub.status == SubStatus.PENDING.value and sub.pending_days == 0 and sub.pending_minutes > 0)
        )
        if is_trial_sub:
            sub.status = SubStatus.EXPIRED.value
            sub.pending_days = 0
            sub.pending_minutes = 0
            sub.pending_has_value = False
            sub.pending_since = None
            session.add(sub)

    # เตะออกจากทุก Channel หากกำลังใช้ trial
    if is_trial_sub:
        try:
            await kick_user_from_all_target_channels(bot, target_uid)
        except Exception:
            pass

    user_name = html.escape(user.full_name or f"User {target_uid}")
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"

    # ส่งข้อความแจ้งเตือนผู้ใช้พร้อมปุ่มทดลองใช้ใหม่ และปุ่มเข้าเมนูหลัก /start
    user_notify_text = (
        "🔄 <b>แอดมินได้ทำการรีเซ็ตสิทธิ์ทดลองใช้งานฟรี 15 นาทีให้คุณเรียบร้อยแล้ว</b>\n\n"
        "✨ คุณสามารถพิมพ์ <code>/start</code> หรือกดปุ่ม <b>'⏱️ ทดลองใช้ฟรี 15 นาที'</b> ด้านล่างนี้เพื่อเริ่มต้นทดลองใช้งานใหม่อีกครั้งได้ทันทีครับ"
    )
    trial_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱️ ทดลองใช้ฟรี 15 นาที (ทดลองใหม่)",
                    callback_data="menu:trial"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 เมนูหลัก /start",
                    callback_data="menu:main"
                )
            ]
        ]
    )
    dm_sent = False
    try:
        await bot.send_message(chat_id=target_uid, text=user_notify_text, reply_markup=trial_keyboard, parse_mode="HTML")
        await log_chat_message(user_id=target_uid, sender_role="BOT", message_text=user_notify_text)
        dm_sent = True
    except Exception as e:
        logger.error(f"Failed to notify user {target_uid} about trial reset: {e}")

    dm_status = "ส่งข้อความแจ้งเตือนผู้ใช้เรียบร้อยแล้ว ✅" if dm_sent else "ไม่สามารถส่ง DM แจ้งผู้ใช้ได้ (อาจบล็อกบอท) ⚠️"

    resp = (
        "🔄 <b>รีเซ็ตสิทธิ์ทดลองฟรี (Trial) สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"⏱️ <b>สถานะ Trial:</b> คืนสิทธิ์แล้ว (trial_used = False)\n"
        f"📩 <b>DM แจ้งเตือน:</b> {dm_status}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้สามารถกดรับสิทธิ์ทดลองฟรี 15 นาทีจากเมนู <code>/start</code> ได้อีกครั้งทันทีครับ</i>"
    )
    await message.answer(resp, parse_mode="HTML")


get_subscription_quota_and_label = subscription_status_label


def build_admin_user_action_keyboard(user: Optional[User], user_id: int) -> InlineKeyboardMarkup:
    """สร้าง Inline Keyboard ปุ่มลัดสำหรับจัดการสมาชิกในเมนู /user"""
    is_moved = False
    if user and config.SECONDARY_CHANNEL_ID:
        is_moved = getattr(user, "is_moved_to_secondary", False) or getattr(user, "assigned_channel", None) == "SECONDARY"

    sec_title = get_channel_label(config.SECONDARY_CHANNEL_ID) if config.SECONDARY_CHANNEL_ID else "BareLive V.2"
    pri_title = get_channel_label(config.CHANNEL_ID)

    buttons = [
        [
            InlineKeyboardButton(text="🔍 ตรวจสอบยอด (Audit)", callback_data=f"admin:audit_user:{user_id}"),
            InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user_id}"),
        ],
    ]

    if config.SECONDARY_CHANNEL_ID:
        if not is_moved:
            buttons.append([
                InlineKeyboardButton(text=f"🚀 ย้ายไป {sec_title} (ส่งลิงก์ 7 วัน)", callback_data=f"admin:quick_move:{user_id}"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton(text=f"🔗 ส่งลิงก์ {sec_title} ใหม่ (7 วัน)", callback_data=f"admin:quick_move:{user_id}"),
                InlineKeyboardButton(text=f"🔄 ย้ายกลับ {pri_title}", callback_data=f"admin:quick_unmove:{user_id}"),
            ])

    buttons.append([
        InlineKeyboardButton(text="🔄 รีเซ็ต Trial", callback_data=f"admin:reset_trial:{user_id}"),
        InlineKeyboardButton(text="🗑️ ลบประวัติทั้งหมด", callback_data=f"admin:confirm_reset_user:{user_id}"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("user", "check_user", "info"))
async def handle_admin_user_info_command(message: Message, bot: Bot):
    """คำสั่งแอดมินดูประวัติของผู้ใช้: /user <@username หรือ User ID>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/user [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/user 5125375696</code>\n"
            "• <code>/user @some_user</code>",
            parse_mode="HTML"
        )
        return

    query = args[1].strip().lstrip("@")
    
    async with get_session() as session:
        # ค้นหาด้วย User ID (ถ้าเป็นตัวเลข) หรือ username หรือ display name
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
            user = (await session.execute(user_stmt)).scalar_one_or_none()
        else:
            user_stmt = select(User).where(User.username.ilike(query))
            user = (await session.execute(user_stmt)).scalar_one_or_none()
            if not user:
                user_stmt = select(User).where(User.full_name.ilike(f"%{query}%"))
                user = (await session.execute(user_stmt)).scalars().first()

        if not user and query.isdigit():
            target_uid = int(query)
            try:
                cm = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid)
                tg_u = getattr(cm, "user", None)
                if tg_u:
                    user, _ = await get_or_create_user(
                        session=session,
                        telegram_id=target_uid,
                        username=tg_u.username,
                        full_name=tg_u.full_name,
                    )
            except Exception:
                pass

        if not user:
            await message.answer(f"❌ <b>ไม่พบข้อมูลผู้ใช้:</b> <code>{html.escape(query)}</code> ในระบบฐานข้อมูล", parse_mode="HTML")
            return

        # ดึงสถานะสมาชิกปัจจุบัน (1 แถวเดียว) + ประวัติการเติมวันล่าสุด (ledger)
        sub = await session.get(Subscription, user.telegram_id)
        grants_stmt = (
            select(SubscriptionGrant)
            .where(SubscriptionGrant.user_id == user.telegram_id)
            .order_by(SubscriptionGrant.id.desc())
            .limit(15)
        )
        grants = (await session.execute(grants_stmt)).scalars().all()
        total_grants_stmt = select(func.count(SubscriptionGrant.id)).where(SubscriptionGrant.user_id == user.telegram_id)
        total_grants_count = (await session.execute(total_grants_stmt)).scalar() or 0

        # ดึงประวัติ PaymentSlips
        slips_stmt = (
            select(PaymentSlip)
            .where(PaymentSlip.user_id == user.telegram_id)
            .order_by(PaymentSlip.id.desc())
        )
        slips = (await session.execute(slips_stmt)).scalars().all()

    target_channel_id = get_user_target_channel_id(user)
    target_channel_label = get_channel_label(target_channel_id)

    # ตรวจสอบสถานะในทุก Channel จริง พร้อมอัปเดตชื่อผู้ใช้ล่าสุดจาก Telegram
    in_channels, channel_status_map, tg_u = await check_user_presence_all_channels(bot, user.telegram_id)
    if tg_u:
        updated_meta = False
        if tg_u.full_name and user.full_name != tg_u.full_name:
            user.full_name = tg_u.full_name
            updated_meta = True
        if tg_u.username and user.username != tg_u.username:
            user.username = tg_u.username
            updated_meta = True
        if updated_meta:
            async with get_session() as session:
                db_u = (await session.execute(select(User).where(User.telegram_id == user.telegram_id))).scalar_one_or_none()
                if db_u:
                    db_u.full_name = user.full_name
                    db_u.username = user.username
                    session.add(db_u)

    channel_status_str = format_user_channel_presence(in_channels)

    # เวลาเข้า Channel ล่าสุด (ระบบใหม่เก็บสถานะปัจจุบันแถวเดียว ไม่มีประวัติการเข้าทุกครั้งอีกต่อไป)
    if sub and sub.joined_at:
        join_str = f"🚪 <b>เวลากดเข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(sub.joined_at)} น.</code>"
    else:
        join_str = "🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>"

    ref_by_str = f"<code>{user.referred_by_id}</code>" if user.referred_by_id else "<i>ไม่มี (เข้าเองโดยตรง)</i>"

    # คำนวณโควต้า PENDING ที่รอกดเข้าห้อง (จากแถว Subscription แถวเดียว)
    has_pending = bool(sub and sub.status == SubStatus.PENDING.value and ((sub.pending_days or 0) > 0 or (sub.pending_minutes or 0) > 0))
    summary_str = "0 วัน"
    if has_pending:
        parts = []
        if sub.pending_days > 0:
            parts.append(f"<b>{sub.pending_days} วัน ({sub.pending_days * 24} ชั่วโมง)</b>")
        if sub.pending_minutes > 0:
            parts.append(f"<b>{sub.pending_minutes} นาที</b>")
        summary_str = " + ".join(parts) if parts else "0 วัน"

    user_header = format_user_title(user.full_name, user.username, user.telegram_id)

    # คำนวณสรุปยอดสิทธิ์และเวลาคงเหลือปัจจุบัน (ตรงกับ /summary ทุกประการ)
    now = datetime.now(timezone.utc)
    latest_active_sub = sub if (sub and sub.status == SubStatus.ACTIVE.value and sub.expires_at and ensure_utc(sub.expires_at) > now) else None

    resp = [
        f"👤 <b>ข้อมูลผู้ใช้งาน:</b> {user_header}",
        f"🎯 <b>Channel ประจำตัว:</b> <b>{target_channel_label}</b> (<code>{target_channel_id}</code>)",
        f"📢 <b>สถานะใน Channel ปัจจุบัน:</b> <b>{channel_status_str}</b>",
        f"⏱️ <b>เคยใช้สิทธิ์ทดลองฟรี (Trial Used):</b> {'✅ เคยใช้แล้ว' if user.trial_used else '❌ ยังไม่เคยใช้'}",
        f"🎁 <b>สถิติ Referral:</b> ชวนสำเร็จ {user.referral_count or 0} คน | โบนัสสะสม {user.referral_bonus_days or 0} วัน",
        f"🔗 <b>สมัครผ่านผู้แนะนำ (Referred By):</b> {ref_by_str}",
        f"📅 <b>เข้าระบบบอทครั้งแรก:</b> <code>{format_thai_datetime(user.created_at)} น.</code>",
        join_str,
        "\n━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>สรุปยอดสิทธิ์และเวลาคงเหลือปัจจุบัน (Active Summary):</b>",
    ]

    if latest_active_sub:
        plan_title = latest_active_sub.source_label or "สมาชิก VIP"
        start_str = format_thai_datetime(latest_active_sub.joined_at)
        end_str = format_thai_datetime(latest_active_sub.expires_at)
        rem_str = format_remaining_time(latest_active_sub.expires_at)

        resp.extend([
            "• 🟢 <b>สถานะสิทธิ์:</b> <b>ACTIVE (กำลังใช้งาน)</b>",
            f"• 🏷️ <b>แพ็กเกจปัจจุบัน:</b> <b>{plan_title}</b>",
            f"• 🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_str} น.</code>",
            f"• 🔴 <b>เวลาหมดอายุ (End):</b> <code>{end_str} น.</code>",
            f"• ⏳ <b>เวลาคงเหลือสุทธิ (Remaining):</b> <b>{rem_str}</b>",
        ])
    elif has_pending:
        resp.extend([
            "• 🟡 <b>สถานะสิทธิ์:</b> <b>PENDING (ออกลิงก์แล้ว-รอกดเข้าห้อง)</b>",
            f"• ⏳ <b>โควต้ารอใช้งานรวม:</b> <b>{summary_str}</b> ({sub.source_label})",
            "• 💡 <i>(เวลาจะเริ่มนับถอยหลังทันทีที่ผู้ใช้กดลิงก์เข้าห้อง)</i>",
        ])
    else:
        if sub:
            last_exp = format_thai_datetime(sub.expires_at) if sub.expires_at else "-"
            resp.extend([
                "• ⚪ <b>สถานะสิทธิ์:</b> <b>EXPIRED (หมดอายุการใช้งานแล้ว)</b>",
                f"• ⏰ <b>หมดอายุไปเมื่อ:</b> <code>{last_exp} น.</code>",
                "• ⏳ <b>เวลาคงเหลือ:</b> <b>0 วัน</b>",
            ])
        else:
            resp.extend([
                "• ⚪ <b>สถานะสิทธิ์:</b> <i>ยังไม่มีประวัติการขอแพ็กเกจ</i>",
                "• ⏳ <b>เวลาคงเหลือ:</b> <b>0 วัน</b>",
            ])

    resp.extend([
        "\n━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>ประวัติการเติมวัน (Ledger) — แสดง {len(grants)}/{total_grants_count} รายการล่าสุด:</b>",
    ])

    if not grants:
        resp.append("<i>ไม่มีประวัติการเติมวัน</i>")
    else:
        for i, g in enumerate(grants, 1):
            created_thai = format_thai_datetime(g.created_at)
            amount_str = f"{g.days} วัน" if g.days > 0 else f"{g.minutes} นาที"
            value_badge = "" if g.has_value else " (ฟรี)"
            resp.append(
                f"\n<b>{i}. +{amount_str}</b> — {g.source_label}{value_badge}\n"
                f"   • 🏷️ <b>ประเภท:</b> {g.grant_type}\n"
                f"   • 🎟️ <b>เวลาเติม:</b> <code>{created_thai} น.</code>"
            )

    if slips:
        resp.append("\n━━━━━━━━━━━━━━━━━━━━")
        resp.append(f"💳 <b>ประวัติการชำระเงิน ({len(slips)} รายการ):</b>")
        for sl in slips:
            sl_created = format_thai_datetime(sl.created_at)
            method_badge = "🧧 ซอง TrueMoney" if getattr(sl, "payment_method", None) == "TRUEMONEY_ANGPAO" or (sl.file_id and str(sl.file_id).startswith("http")) else "💳 สแกน QR Code"
            resp.append(f"• #{sl.id} [{method_badge}] | สถานะ: <b>{sl.status}</b> | เวลาส่ง: <code>{sl_created} น.</code>")

    resp.append("\n━━━━━━━━━━━━━━━━━━━━")
    resp.append(f"📋 <b>คำสั่งด่วน (แตะเพื่อคัดลอก):</b>")
    resp.append(f"💬 ตอบกลับข้อความ: <code>/reply {user.telegram_id} </code>")
    resp.append(f"➕ เพิ่ม VIP (30 วัน): <code>/add_vip {user.telegram_id} 30</code>")

    is_moved = False
    if user and config.SECONDARY_CHANNEL_ID:
        is_moved = getattr(user, "is_moved_to_secondary", False) or getattr(user, "assigned_channel", None) == "SECONDARY"
    sec_title = get_channel_label(config.SECONDARY_CHANNEL_ID) if config.SECONDARY_CHANNEL_ID else "BareLive V.2"
    pri_title = get_channel_label(config.CHANNEL_ID)

    if config.SECONDARY_CHANNEL_ID:
        if not is_moved:
            resp.append(f"🚀 ย้ายไป {sec_title}: <code>/move_user {user.telegram_id}</code>")
        else:
            resp.append(f"🔄 ย้ายกลับ {pri_title}: <code>/unmove_user {user.telegram_id}</code>")

    resp.append(f"👢 เตะออกจากห้อง: <code>/kick {user.telegram_id}</code>")

    user_action_keyboard = build_admin_user_action_keyboard(user, user.telegram_id)

    await message.answer("\n".join(resp), reply_markup=user_action_keyboard, parse_mode="HTML")


@router.message(Command("chat", "history", "chat_history"))
async def handle_admin_chat_history_command(message: Message):
    """คำสั่งดูประวัติการสนทนาย้อนหลัง: /chat <@username หรือ User ID> [จำนวนข้อความ]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/chat [User ID หรือ @username] [จำนวนข้อความ (ค่าเริ่มต้น 15)]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/chat 5125375696</code>\n"
            "• <code>/chat @some_user 20</code>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    limit = 15
    if len(args) >= 3:
        try:
            limit = min(max(int(args[2]), 1), 50)
        except ValueError:
            limit = 15

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))

        user = (await session.execute(user_stmt)).scalar_one_or_none()
        if not user:
            await message.answer(f"❌ <b>ไม่พบข้อมูลผู้ใช้:</b> <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return

        chat_stmt = (
            select(ChatMessage)
            .where(ChatMessage.user_id == user.telegram_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        messages = (await session.execute(chat_stmt)).scalars().all()

    user_name = html.escape(user.full_name or f"User {user.telegram_id}")
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"

    lines = [
        f"💬 <b>ประวัติการสนทนาของ:</b> {user_name} ({user_handle})",
        f"🔢 <b>User ID:</b> <code>{user.telegram_id}</code>",
        f"📊 <b>แสดง:</b> {len(messages)} ข้อความล่าสุด (จากทั้งหมด)",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    if not messages:
        lines.append("📭 <i>ยังไม่มีประวัติการส่งข้อความใหม่ที่บันทึกไว้ในระบบ</i>")
        lines.append("(ระบบจะเริ่มบันทึกบทสนทนาใหม่นับตั้งแต่เริ่มเปิดใช้งานระบบนี้)")
    else:
        for msg in reversed(messages):
            t_str = format_thai_datetime(msg.created_at)
            role_icon = "👤" if msg.sender_role == "USER" else ("🤖" if msg.sender_role == "BOT" else "👑")
            role_label = "ผู้ใช้" if msg.sender_role == "USER" else ("บอท" if msg.sender_role == "BOT" else "แอดมิน")
            
            # Short time for cleaner view
            time_short = t_str[11:16] if len(t_str) >= 16 else t_str
            safe_content = html.escape(msg.message_text)
            lines.append(f"[{time_short} น.] {role_icon} <b>{role_label}:</b> {safe_content}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💡 <i>พิมพ์ <code>/reply {user.telegram_id} [ข้อความ]</code> เพื่อตอบกลับผู้ใช้</i>")

    await message.answer(text="\n".join(lines), parse_mode="HTML")


@router.message(Command("reply", "send"))
async def handle_admin_reply_command(message: Message, bot: Bot):
    """คำสั่งแอดมินส่งข้อความตอบกลับผู้ใช้ทาง DM: /reply <@username หรือ User ID> <ข้อความ>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/reply [User ID หรือ @username] [ข้อความที่ต้องการส่ง]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/reply 5125375696 สวัสดีครับ แอดมินกำลังตรวจสอบสลิปให้ครับ</code>\n"
            "• <code>/reply @some_user สวัสดีครับ มีอะไรให้ช่วยเหลือเพิ่มเติมมั้ยครับ</code>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    reply_text = args[2].strip()

    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))

        user = (await session.execute(user_stmt)).scalar_one_or_none()
        if not user:
            # If not in DB but is digits, try sending directly
            if query.isdigit():
                target_uid = int(query)
                user_name = f"User {target_uid}"
            else:
                await message.answer(f"❌ ไม่พบผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id
            user_name = html.escape(user.full_name or f"User {target_uid}")

    dm_msg = (
        "💬 <b>ข้อความจากทีมงานผู้ดูแลระบบ:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{html.escape(reply_text)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>คุณสามารถพิมพ์ข้อความตอบกลับในแชทนี้ได้ตลอดเวลาครับ</i>"
    )

    try:
        await bot.send_message(chat_id=target_uid, text=dm_msg, parse_mode="HTML")
        await log_chat_message(user_id=target_uid, sender_role="ADMIN", message_text=reply_text)
        await message.answer(
            f"✅ <b>ส่งข้อความไปยัง {user_name} (<code>{target_uid}</code>) สำเร็จ!</b>\n\n"
            f"📝 <b>ข้อความที่ส่ง:</b>\n<i>{html.escape(reply_text)}</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ <b>ส่งข้อความไม่สำเร็จ:</b> <code>{html.escape(str(e))}</code>\n(ผู้ใช้อาจบล็อกบอทหรือยังไม่เคยกด /start)", parse_mode="HTML")


@router.message(F.chat.id == config.ADMIN_GROUP_ID, F.reply_to_message)
async def handle_admin_swipe_reply(message: Message, bot: Bot):
    """จัดการเมื่อแอดมินปัดขวาตอบกลับ (Reply) ข้อความที่บอทส่งมาเพื่อตอบผู้ใช้อัตโนมัติ โดยไม่ต้องพิมพ์ /reply"""
    if not message.text:
        return

    # Check if the replied message is from the bot
    bot_user = await bot.get_me()
    if message.reply_to_message.from_user.id != bot_user.id:
        return

    # Look for User ID in the replied message caption or text
    text_to_search = message.reply_to_message.caption or message.reply_to_message.text or ""
    
    # regex matches: "User ID: </code>123456" or "User ID: 123456" etc.
    match = re.search(r"User ID:(?:</b>)?\s*(?:<code>)?(\d+)", text_to_search)
    if not match:
        return
        
    target_uid = int(match.group(1))
    reply_text = message.text

    dm_msg = (
        "💬 <b>ข้อความจากทีมงานผู้ดูแลระบบ:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{html.escape(reply_text)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>คุณสามารถพิมพ์ข้อความตอบกลับในแชทนี้ได้ตลอดเวลาครับ</i>"
    )

    try:
        await bot.send_message(chat_id=target_uid, text=dm_msg, parse_mode="HTML")
        await log_chat_message(user_id=target_uid, sender_role="ADMIN", message_text=reply_text)
        await message.reply(
            f"✅ <b>ตอบกลับข้อความไปยังผู้ใช้ (<code>{target_uid}</code>) สำเร็จ!</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply(f"❌ <b>ส่งข้อความไม่สำเร็จ:</b> <code>{html.escape(str(e))}</code>\n(ผู้ใช้อาจบล็อกบอทหรือยังไม่เคยกด /start)", parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:view_chat:"))
async def handle_admin_view_chat_callback(callback: CallbackQuery):
    """Callback เมื่อแอดมินกดปุ่ม [📜 ดูประวัติการคุย]"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    user_id = int(uid_str)
    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
        if not user:
            await callback.answer("❌ ไม่พบข้อมูลผู้ใช้นี้ในระบบ", show_alert=True)
            return

        chat_stmt = (
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.id.desc())
            .limit(15)
        )
        messages = (await session.execute(chat_stmt)).scalars().all()

    user_name = html.escape(user.full_name or f"User {user_id}")
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"

    lines = [
        f"💬 <b>ประวัติการสนทนาของ:</b> {user_name} ({user_handle})",
        f"🔢 <b>User ID:</b> <code>{user_id}</code>",
        f"📊 <b>แสดง:</b> {len(messages)} ข้อความล่าสุด",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    if not messages:
        lines.append("📭 <i>ยังไม่มีประวัติการส่งข้อความใหม่ที่บันทึกไว้ในระบบ</i>")
    else:
        for msg in reversed(messages):
            t_str = format_thai_datetime(msg.created_at)
            role_icon = "👤" if msg.sender_role == "USER" else ("🤖" if msg.sender_role == "BOT" else "👑")
            role_label = "ผู้ใช้" if msg.sender_role == "USER" else ("บอท" if msg.sender_role == "BOT" else "แอดมิน")
            time_short = t_str[11:16] if len(t_str) >= 16 else t_str
            safe_content = html.escape(msg.message_text)
            lines.append(f"[{time_short} น.] {role_icon} <b>{role_label}:</b> {safe_content}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📋 <b>แตะเพื่อคัดลอกคำสั่งตอบกลับ:</b>\n<code>/reply {user_id} </code>")

    await callback.message.answer(text="\n".join(lines), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:resolve_chat:"))
async def handle_admin_resolve_chat_callback(callback: CallbackQuery):
    """Callback เมื่อแอดมินกดปุ่ม [✅ ปิดจบการสนทนา] รายบุคคล"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    admin_user = callback.from_user
    admin_name = f"@{admin_user.username}" if admin_user.username else html.escape(admin_user.full_name)

    await log_chat_message(
        user_id=target_uid,
        sender_role="ADMIN",
        message_text=f"[แอดมิน {admin_name} ปิดจบการสนทนา / ทำเครื่องหมายว่าตอบแล้ว]"
    )

    await callback.answer(f"✅ ปิดจบการสนทนาของ User {target_uid} เรียบร้อย", show_alert=False)
    try:
        await callback.message.reply(
            f"✅ <b>ปิดจบการสนทนาเรียบร้อย!</b>\n"
            f"แอดมิน {admin_name} ได้ทำเครื่องหมายว่าตอบ/ปิดจบการสนทนาสำหรับ User ID <code>{target_uid}</code> แล้ว (ระบบจะไม่ส่งเตือนซ้ำ)",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "admin:resolve_all_chats")
async def handle_admin_resolve_all_chats_callback(callback: CallbackQuery):
    """Callback เมื่อแอดมินกดปุ่ม [✅ ปิดจบทั้งหมด]"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    admin_user = callback.from_user
    admin_name = f"@{admin_user.username}" if admin_user.username else html.escape(admin_user.full_name)

    resolved_count = 0
    async with get_session() as session:
        subq = (
            select(ChatMessage.user_id, func.max(ChatMessage.id).label("max_id"))
            .group_by(ChatMessage.user_id)
            .subquery()
        )
        stmt = (
            select(ChatMessage)
            .join(subq, ChatMessage.id == subq.c.max_id)
            .where(
                ChatMessage.sender_role == "USER",
                ~ChatMessage.message_text.startswith("/"),
                ~ChatMessage.message_text.startswith("[")
            )
        )
        unanswered_msgs = (await session.execute(stmt)).scalars().all()

        for msg in unanswered_msgs:
            session.add(ChatMessage(
                user_id=msg.user_id,
                sender_role="ADMIN",
                message_text=f"[แอดมิน {admin_name} ปิดจบการสนทนาทั้งหมด / ทำเครื่องหมายว่าตอบแล้ว]"
            ))
            resolved_count += 1
        await session.commit()

    await callback.answer(f"✅ ปิดจบการสนทนาที่ค้างอยู่ทั้งหมด ({resolved_count} คน) เรียบร้อย", show_alert=True)
    if resolved_count > 0:
        try:
            await callback.message.reply(
                f"✅ <b>ปิดจบการสนทนาทั้งหมดสำเร็จ!</b>\n"
                f"แอดมิน {admin_name} ได้ทำเครื่องหมายปิดจบข้อความที่ค้างอยู่ทั้งหมด {resolved_count} รายการเรียบร้อยแล้ว",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("close_chat", "resolve_chat", "done_chat", "end_chat"))
async def handle_admin_close_chat_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับปิดจบการสนทนาของ User: /close_chat <User ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/close_chat [User ID หรือ @username]</code>\n"
            "ตัวอย่าง: <code>/close_chat 5125375696</code>\n\n"
            "ℹ️ <i>คำสั่งนี้จะทำเครื่องหมายว่าการสนทนาได้รับการดูแลแล้ว เพื่อหยุดการแจ้งเตือนเตือนซ้ำข้อความค้างตอบ</i>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

        if not user:
            if query.isdigit():
                target_uid = int(query)
                user_header = f"User {target_uid}"
            else:
                await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id
            user_header = format_user_title(user.full_name, user.username, target_uid)

        admin_name = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else "Admin"
        session.add(ChatMessage(
            user_id=target_uid,
            sender_role="ADMIN",
            message_text=f"[แอดมิน {admin_name} ปิดจบการสนทนาด้วยคำสั่ง /close_chat]"
        ))
        await session.commit()

    await message.answer(
        f"✅ <b>ปิดจบการสนทนาสำเร็จ!</b>\n"
        f"👤 <b>ผู้ใช้:</b> {user_header}\n"
        "ℹ️ <i>ระบบทำเครื่องหมายว่าได้รับการดูแลแล้ว และจะไม่ส่งแจ้งเตือนเตือนซ้ำข้อความนี้อีกครับ</i>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:view_user:"))
async def handle_admin_view_user_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่ม [👤 ดูข้อมูลสมาชิก]"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    user_id = int(uid_str)
    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == user_id))).scalar_one_or_none()
        if not user:
            await callback.answer("❌ ไม่พบข้อมูลผู้ใช้ในระบบ", show_alert=True)
            return

        sub = await session.get(Subscription, user_id)

        slips_stmt = select(PaymentSlip).where(PaymentSlip.user_id == user_id).order_by(PaymentSlip.id.desc())
        slips = (await session.execute(slips_stmt)).scalars().all()

    in_channels, _, _ = await check_user_presence_all_channels(bot, user_id)
    channel_status_str = format_user_channel_presence(in_channels)

    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    
    if sub and sub.joined_at:
        join_str = f"🚪 <b>เวลากดเข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(sub.joined_at)} น.</code>"
    else:
        join_str = "🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>"

    resp = [
        f"👤 <b>ข้อมูลผู้ใช้งาน: {full_name_safe}</b> ({user_handle})",
        f"🔢 <b>Telegram ID:</b> <code>{user.telegram_id}</code>",
        f"⏱️ <b>เคยใช้สิทธิ์ทดลองฟรี (Trial Used):</b> {'✅ เคยใช้แล้ว' if user.trial_used else '❌ ยังไม่เคยใช้'}",
        f"📢 <b>สถานะใน Channel ปัจจุบัน:</b> {channel_status_str}",
        f"📅 <b>เข้าระบบบอทครั้งแรก:</b> <code>{format_thai_datetime(user.created_at)} น.</code>",
        join_str,
        "\n━━━━━━━━━━━━━━━━━━━━",
        "📦 <b>สถานะแพ็กเกจปัจจุบัน:</b>",
    ]

    if sub:
        plan_label, quota_str = get_subscription_quota_and_label(sub)
        expired_thai = format_thai_datetime(sub.expires_at) if sub.expires_at else "ยังไม่เริ่มนับ"
        resp.append(f"• <b>{plan_label}</b> ({sub.status}) | โควต้า: {quota_str} | หมดอายุ: <code>{expired_thai} น.</code>")
    else:
        resp.append("• <i>ยังไม่มีประวัติการขอแพ็กเกจ</i>")

    if slips:
        method_badge = "🧧 ซอง TrueMoney" if getattr(slips[0], "payment_method", None) == "TRUEMONEY_ANGPAO" or (slips[0].file_id and str(slips[0].file_id).startswith("http")) else "💳 สแกน QR Code"
        resp.append(f"\n💳 <b>รายการชำระล่าสุด:</b> #{slips[0].id} [{method_badge}] ({slips[0].status})")

    resp.append("\n━━━━━━━━━━━━━━━━━━━━")
    resp.append(f"📋 <b>คำสั่งด่วน (แตะเพื่อคัดลอก):</b>")
    resp.append(f"💬 ตอบกลับข้อความ: <code>/reply {user_id} </code>")
    resp.append(f"➕ เพิ่ม VIP (30 วัน): <code>/add_vip {user_id} 30</code>")

    is_moved = getattr(user, "is_moved_to_secondary", False) or getattr(user, "assigned_channel", "PRIMARY") == "SECONDARY"
    sec_title = get_channel_label(config.SECONDARY_CHANNEL_ID) if config.SECONDARY_CHANNEL_ID else "BareLive V.2"
    pri_title = get_channel_label(config.CHANNEL_ID)

    if config.SECONDARY_CHANNEL_ID:
        if not is_moved:
            resp.append(f"🚀 ย้ายไป {sec_title}: <code>/move_user {user_id}</code>")
        else:
            resp.append(f"🔄 ย้ายกลับ {pri_title}: <code>/unmove_user {user_id}</code>")

    resp.append(f"👢 เตะออกจากห้อง: <code>/kick {user_id}</code>")

    user_action_keyboard = build_admin_user_action_keyboard(user, user_id)

    await callback.message.answer("\n".join(resp), reply_markup=user_action_keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:quick_move:"))
async def handle_admin_quick_move_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่มลัด [🚀 ย้ายไป BareLive V.2] หรือ [🔗 ส่งลิงก์ V.2 ใหม่] ในเมนู /user"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    if not config.SECONDARY_CHANNEL_ID:
        await callback.answer("⚠️ ยังไม่ได้ตั้งค่า SECONDARY_CHANNEL_ID ใน .env", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target_uid))).scalar_one_or_none()
        if not user:
            user, _ = await get_or_create_user(session=session, telegram_id=target_uid, full_name=f"User {target_uid}")

        user.is_moved_to_secondary = True
        user.assigned_channel = "SECONDARY"
        session.add(user)
        await session.commit()

    # ปลดแบนใน Channel ใหม่ก่อนสร้างลิงก์
    await unban_user_in_channel(bot, config.SECONDARY_CHANNEL_ID, target_uid)

    # สร้างลิงก์เชิญสำหรับ Channel ใหม่ (อายุ 7 วัน, 1 ครั้ง)
    invite_expire = now + timedelta(days=7)
    invite_url = None
    target_channel_title = get_channel_label(config.SECONDARY_CHANNEL_ID)

    try:
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=config.SECONDARY_CHANNEL_ID,
            member_limit=1,
            expire_date=invite_expire,
            name=f"Move-{target_uid}",
        )
        invite_url = invite_link_obj.invite_link
    except Exception as e:
        logger.error(f"Failed to generate quick move invite link for User {target_uid}: {e}", exc_info=True)
        await callback.answer(f"❌ ไม่สามารถสร้างลิงก์เชิญสำหรับ {target_channel_title} ได้", show_alert=True)
        return

    # ส่ง DM หาผู้ใช้
    user_header = format_user_title(user.full_name, user.username, target_uid)
    expire_thai = format_thai_datetime(invite_expire)
    user_dm_sent = False

    user_move_text = (
        f"🎉 <b>คุณได้รับคำเชิญให้ย้ายเข้าสู่ {target_channel_title}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"แอดมินได้ส่งลิงก์เชิญพิเศษสำหรับคุณ เพื่อเข้าร่วม Channel VIP ห้องใหม่ (<b>{target_channel_title}</b>) เรียบร้อยแล้วครับ 🚀\n\n"
        f"🔗 <b>ลิงก์เชิญส่วนตัว (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
        f"⏳ <b>ลิงก์หมดอายุวันที่:</b> <code>{expire_thai} น.</code> (มีอายุ 7 วัน)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <i>ข้อควรทราบ: สิทธิ์และเวลาสมาชิกของคุณจะทำงานอย่างต่อเนื่องเหมือนเดิม กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ!</i>"
    )

    join_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 เข้าร่วม {target_channel_title} ตอนนี้", url=invite_url)]
        ]
    )

    try:
        await bot.send_message(
            chat_id=target_uid,
            text=user_move_text,
            reply_markup=join_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        user_dm_sent = True
    except Exception as e:
        logger.warning(f"Could not send move DM to User {target_uid}: {e}")

    try:
        await log_chat_message(
            user_id=target_uid,
            sender_role="BOT",
            message_text=f"[ระบบส่งลิงก์ย้าย {target_channel_title} (อายุ 7 วัน): {invite_url}]"
        )
    except Exception:
        pass

    admin_reply = (
        f"🌟 <b>ย้ายสมาชิกไปยัง {target_channel_title} สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"📢 <b>Channel เป้าหมาย:</b> <b>{target_channel_title}</b> (<code>{config.SECONDARY_CHANNEL_ID}</code>)\n"
        f"⏳ <b>อายุลิงก์เชิญ:</b> 7 วัน (หมดอายุ: <code>{expire_thai} น.</code>)\n"
        f"🔗 <b>Invite Link:</b>\n<code>{invite_url}</code>\n"
        f"📨 <b>ส่งข้อความ DM หาผู้ใช้:</b> {'สำเร็จ ✅' if user_dm_sent else 'ไม่สำเร็จ (ผู้ใช้บล็อกบอท) ⚠️'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"ℹ️ <i>บันทึกสถานะผู้ใช้เป็น {target_channel_title} เรียบร้อย การซื้อ/ต่ออายุในอนาคตจะส่งลิงก์ห้องนี้ให้อัตโนมัติ</i>"
    )

    await callback.message.answer(admin_reply, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer(f"✅ ย้ายไปยัง {target_channel_title} และส่งลิงก์ 7 วันแล้ว!", show_alert=True)


@router.callback_query(F.data.startswith("admin:quick_unmove:"))
async def handle_admin_quick_unmove_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่มลัด [🔄 ย้ายกลับ BareLive] ในเมนู /user"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target_uid))).scalar_one_or_none()
        if not user:
            await callback.answer("❌ ไม่พบข้อมูลผู้ใช้ในระบบ", show_alert=True)
            return

        user.is_moved_to_secondary = False
        user.assigned_channel = "PRIMARY"
        session.add(user)
        await session.commit()

    primary_title = get_channel_label(config.CHANNEL_ID)
    user_header = format_user_title(user.full_name, user.username, user.telegram_id)
    await callback.message.answer(
        f"🔄 <b>ย้ายผู้ใช้กลับสู่ {primary_title} เรียบร้อย!</b>\n\n"
        f"👤 <b>ผู้ใช้งาน:</b> {user_header}\n"
        f"📢 <b>Channel ประจำตัว:</b> <b>{primary_title}</b> (<code>{config.CHANNEL_ID}</code>)",
        parse_mode="HTML"
    )
    await callback.answer(f"✅ ย้ายกลับสู่ {primary_title} เรียบร้อยแล้ว", show_alert=True)


@router.callback_query(F.data.startswith("admin:reset_trial:"))
async def handle_admin_reset_trial_callback(callback: CallbackQuery, bot: Bot):
    """Callback เมื่อแอดมินกดปุ่ม [🔄 รีเซ็ต Trial]"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target_uid))).scalar_one_or_none()
        if not user:
            await callback.answer("❌ ไม่พบข้อมูลผู้ใช้ในระบบ", show_alert=True)
            return

        user.trial_used = False
        session.add(user)

        sub = await session.get(Subscription, target_uid)
        is_trial_sub = bool(sub) and (
            (sub.status == SubStatus.ACTIVE.value and sub.is_trial_active)
            or (sub.status == SubStatus.PENDING.value and sub.pending_days == 0 and sub.pending_minutes > 0)
        )
        if is_trial_sub:
            sub.status = SubStatus.EXPIRED.value
            sub.pending_days = 0
            sub.pending_minutes = 0
            sub.pending_has_value = False
            sub.pending_since = None
            session.add(sub)

    if is_trial_sub:
        try:
            await kick_user_from_all_target_channels(bot, target_uid)
        except Exception:
            pass

    user_name = html.escape(user.full_name or f"User {target_uid}")

    # ส่งข้อความแจ้งเตือนผู้ใช้พร้อมปุ่มทดลองใช้ใหม่ และปุ่มเข้าเมนูหลัก /start
    user_notify_text = (
        "🔄 <b>แอดมินได้ทำการรีเซ็ตสิทธิ์ทดลองใช้งานฟรี 15 นาทีให้คุณเรียบร้อยแล้ว</b>\n\n"
        "✨ คุณสามารถพิมพ์ <code>/start</code> หรือกดปุ่ม <b>'⏱️ ทดลองใช้ฟรี 15 นาที'</b> ด้านล่างนี้เพื่อเริ่มต้นทดลองใช้งานใหม่อีกครั้งได้ทันทีครับ"
    )
    trial_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱️ ทดลองใช้ฟรี 15 นาที (ทดลองใหม่)",
                    callback_data="menu:trial"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 เมนูหลัก /start",
                    callback_data="menu:main"
                )
            ]
        ]
    )
    dm_sent = False
    try:
        await bot.send_message(chat_id=target_uid, text=user_notify_text, reply_markup=trial_keyboard, parse_mode="HTML")
        await log_chat_message(user_id=target_uid, sender_role="BOT", message_text=user_notify_text)
        dm_sent = True
    except Exception as e:
        logger.error(f"Failed to notify user {target_uid} about trial reset: {e}")

    dm_status = "ส่งข้อความแจ้งเตือนผู้ใช้เรียบร้อยแล้ว ✅" if dm_sent else "ไม่สามารถส่ง DM แจ้งผู้ใช้ได้ (อาจบล็อกบอท) ⚠️"

    await callback.message.answer(
        f"🔄 <b>รีเซ็ตสิทธิ์ทดลองฟรี (Trial) สำหรับ {user_name} (<code>{target_uid}</code>) สำเร็จ!</b>\n"
        f"📩 <b>DM แจ้งเตือน:</b> {dm_status}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้สามารถกดรับสิทธิ์ทดลองฟรี 15 นาทีจากเมนู <code>/start</code> ได้อีกครั้งทันทีครับ</i>",
        parse_mode="HTML"
    )
    await callback.answer("✅ รีเซ็ตสิทธิ์ Trial เรียบร้อยแล้ว")


@router.callback_query(F.data.startswith("admin:confirm_reset_user:"))
async def handle_admin_confirm_reset_user_callback(callback: CallbackQuery):
    """Callback แสดงกล่องยืนยันการลบประวัติและผู้ใช้ทั้งหมด"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ ยืนยันลบประวัติและบัญชีทั้งหมด", callback_data=f"admin:do_reset_user:{target_uid}"),
            ],
            [
                InlineKeyboardButton(text="🔙 ยกเลิก", callback_data="admin:cancel_reset"),
            ]
        ]
    )

    await callback.message.answer(
        f"⚠️ <b>ยืนยันการรีเซ็ตและลบข้อมูลผู้ใช้ทั้งหมด (Factory Reset)?</b>\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n\n"
        "• ประวัติการคุย (Chat Messages) ทั้งหมดจะถูกลบ\n"
        "• สลิปการโอนเงิน (Payment Slips) ทั้งหมดจะถูกลบ\n"
        "• ประวัติการเติมวันและสิทธิ์สมาชิก (Subscriptions & Grants) ทั้งหมดจะถูกลบ\n"
        "• ผู้ใช้จะถูกเตะออกจากห้อง VIP ทันที และปลดแบนให้พร้อมเข้าใหม่\n"
        "• บัญชีจะถูกลบออกจากระบบ (กลายเป็น User ใหม่ 100% เหมือนไม่เคยเข้าใช้มาก่อน)",
        reply_markup=confirm_kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:do_reset_user:"))
async def handle_admin_do_reset_user_callback(callback: CallbackQuery, bot: Bot):
    """Callback ดำเนินการลบประวัติและผู้ใช้ทั้งหมดจริง (100% Factory Reset)"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    uid_str = callback.data.split(":")[-1]
    if not uid_str.isdigit():
        await callback.answer("❌ User ID ไม่ถูกต้อง")
        return

    target_uid = int(uid_str)
    async with get_session() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target_uid))).scalar_one_or_none()
        user_name = html.escape(user.full_name or f"User {target_uid}") if user else f"User {target_uid}"

        # 1. ลบประวัติแชททั้งหมด
        await session.execute(delete(ChatMessage).where(ChatMessage.user_id == target_uid))
        # 2. ลบสลิปชำระเงินทั้งหมด
        await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == target_uid))
        # 3. ลบประวัติการเติมวันทั้งหมด (Ledger)
        await session.execute(delete(SubscriptionGrant).where(SubscriptionGrant.user_id == target_uid))
        # 4. ลบสถานะสมาชิก Subscription
        await session.execute(delete(Subscription).where(Subscription.user_id == target_uid))
        # 5. เคลียร์ข้อมูลการชวนเพื่อนของผู้ใช้อื่นที่ถูกชวนโดย user คนนี้
        await session.execute(update(User).where(User.referred_by_id == target_uid).values(referred_by_id=None))
        await session.execute(update(SubscriptionGrant).where(SubscriptionGrant.referred_friend_id == target_uid).values(referred_friend_id=None))
        # 6. ลบแถว User ออกจากฐานข้อมูล
        await session.execute(delete(User).where(User.telegram_id == target_uid))
        await session.commit()

    # 7. เตะออกจากทุก Channel ทันที (หากเคยอยู่ในห้อง)
    await kick_user_from_all_target_channels(bot, target_uid)
    logger.info(f"Kicked User {target_uid} from all target channels on reset.")

    # 8. ปลดแบนจาก Blacklist ของทุก Channel ทันที เพื่อให้เป็นสถานะปกติที่สามารถรับลิงก์เชิญใหม่ได้ 100%
    await unban_user_in_all_target_channels(bot, target_uid)
    logger.info(f"Unbanned User {target_uid} in all target channels on reset.")

    resp_text = (
        f"🗑️ <b>ลบประวัติและรีเซ็ตบัญชี {user_name} (<code>{target_uid}</code>) สำเร็จแล้ว!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📭 ลบประวัติแชท, สลิป, แพ็กเกจ, ประวัติเติมวัน และบัญชีผู้ใช้ทั้งหมดแล้ว\n"
        "🚪 เตะออกจากห้อง VIP และปลดแบล็กลิสต์ใน Telegram ให้เรียบร้อยแล้ว\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้กลับเป็นผู้ใช้ใหม่ 100% เสมือนไม่เคยใช้งานมาก่อน สามารถพิมพ์ <code>/start</code> ในบอทเพื่อทดสอบรับสิทธิ์ทดลองหรือซื้อแพ็กเกจใหม่ได้ทันทีครับ</i>"
    )

    try:
        await callback.message.edit_text(text=resp_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text=resp_text, parse_mode="HTML")
    await callback.answer("✅ ลบและรีเซ็ตผู้ใช้เรียบร้อยแล้ว")


@router.message(Command("reset_user", "delete_user", "wipe_user", "clear_user"))
async def handle_admin_reset_user_command(message: Message):
    """คำสั่งแอดมินสำหรับลบประวัติและรีเซ็ตบัญชีผู้ใช้ทั้งหมด: /reset_user <User ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/reset_user [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/reset_user 5125375696</code>\n"
            "• <code>/reset_user @some_user</code>\n\n"
            "⚠️ <i>คำสั่งนี้จะเปิดหน้าต่างยืนยันการลบประวัติ แชท สลิป สิทธิ์ และเตะออกจากห้อง VIP</i>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

    if not user:
        if query.isdigit():
            target_uid = int(query)
            user_header = f"User {target_uid}"
        else:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return
    else:
        target_uid = user.telegram_id
        user_header = format_user_title(user.full_name, user.username, target_uid)

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ ยืนยันลบประวัติและบัญชีทั้งหมด", callback_data=f"admin:do_reset_user:{target_uid}"),
            ],
            [
                InlineKeyboardButton(text="🔙 ยกเลิก", callback_data="admin:cancel_reset"),
            ]
        ]
    )

    await message.answer(
        f"⚠️ <b>ยืนยันการรีเซ็ตและลบข้อมูลผู้ใช้ทั้งหมด (Factory Reset)?</b>\n"
        f"👤 <b>ผู้ใช้:</b> {user_header}\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n\n"
        "• ประวัติการคุย (Chat Messages) ทั้งหมดจะถูกลบ\n"
        "• สลิปการโอนเงิน (Payment Slips) ทั้งหมดจะถูกลบ\n"
        "• ประวัติการเติมวันและสิทธิ์สมาชิก (Subscriptions & Grants) ทั้งหมดจะถูกลบ\n"
        "• ผู้ใช้จะถูกเตะออกจากห้อง VIP ทันที และปลดแบนให้พร้อมเข้าใหม่\n"
        "• บัญชีจะถูกลบออกจากระบบ (กลายเป็น User ใหม่ 100% เหมือนไม่เคยเข้าใช้มาก่อน)",
        reply_markup=confirm_kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:cancel_reset")
async def handle_admin_cancel_reset_callback(callback: CallbackQuery):
    """Callback ยกเลิกการลบผู้ใช้"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("ยกเลิกการลบข้อมูลแล้ว")


USERS_PER_PAGE = 5


async def build_users_list_view(page: int = 1) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """สร้างรายงานสรุปรายชื่อและประวัติผู้ใช้งานทั้งหมดในระบบ พร้อมระบบแบ่งหน้า (Pagination)"""
    now = datetime.now(timezone.utc)
    
    async with get_session() as session:
        # 1. ดึงสถิติจำนวนรวม
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0
        total_active = (await session.execute(
            select(func.count(Subscription.user_id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )).scalar() or 0
        total_trial_used = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0
        total_kick_failed = (await session.execute(
            select(func.count(Subscription.user_id)).where(Subscription.status == SubStatus.KICK_FAILED.value)
        )).scalar() or 0

        if total_users == 0:
            return "ℹ️ <i>ขณะนี้ยังไม่มีข้อมูลผู้ใช้งานในระบบฐานข้อมูล</i>", None

        total_pages = max(1, (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
        page = max(1, min(page, total_pages))

        # 2. ดึงข้อมูล User ประจำหน้านี้ พร้อม Subscriptions และ Slips
        stmt = (
            select(User)
            .options(
                selectinload(User.subscription),
                selectinload(User.payment_slips),
            )
            .order_by(User.created_at.desc())
            .offset((page - 1) * USERS_PER_PAGE)
            .limit(USERS_PER_PAGE)
        )
        users = (await session.execute(stmt)).scalars().all()

    now_thai = format_thai_datetime(now)
    lines = [
        "📑 <b>รายงานประวัติผู้ใช้งานทั้งหมดในระบบ (Users History Audit)</b>",
        f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 <b>ผู้ใช้ทั้งหมด:</b> <b>{total_users} คน</b> | 🟢 <b>Active:</b> <b>{total_active} คน</b>",
        f"⏱️ <b>ใช้สิทธิ์ฟรี 15m แล้ว:</b> {total_trial_used} คน",
    ]
    if total_kick_failed > 0:
        lines.append(f"⚠️ <b>สมาชิกเตะไม่สำเร็จ (Kick Failed):</b> {total_kick_failed} คน")
    lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    start_index = (page - 1) * USERS_PER_PAGE + 1
    buttons = []
    
    for i, u in enumerate(users, start=start_index):
        u_header = format_user_title(u.full_name, u.username, u.telegram_id)
        trial_str = "เคยใช้แล้ว ✅" if u.trial_used else "ยังไม่เคยใช้ ⏱️"
        
        user_block = [
            f"<b>{i}.</b> {u_header}",
            f"   • สิทธิ์ฟรี: {trial_str} | 📅 <b>เข้าใช้บอทครั้งแรก:</b> <code>{format_thai_datetime(u.created_at)} น.</code>",
        ]

        # เวลากดเข้า Channel ล่าสุด
        if u.subscription and u.subscription.joined_at:
            user_block.append(f"   • 🚪 <b>เวลากดเข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(u.subscription.joined_at)} น.</code>")
        else:
            user_block.append("   • 🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>")

        # Subscription ปัจจุบัน
        latest_sub = u.subscription
        if latest_sub:
            plan_label, quota_str = get_subscription_quota_and_label(latest_sub)
            status_badge = {
                SubStatus.ACTIVE.value: "🟢 ACTIVE",
                SubStatus.PENDING.value: "🟡 PENDING (ออกลิงก์แล้ว-รอกดเข้า)",
                SubStatus.EXPIRED.value: "⚪ EXPIRED",
                SubStatus.KICKED.value: "🔴 KICKED",
                SubStatus.KICK_FAILED.value: "⚠️ KICK_FAILED",
            }.get(latest_sub.status, latest_sub.status)

            user_block.append(f"   • 📦 <b>สถานะล่าสุด:</b> {plan_label} [{status_badge}]")
            user_block.append(f"   • ⏳ <b>โควต้าระยะเวลา:</b> <b>{quota_str}</b>")
            if latest_sub.expires_at:
                user_block.append(f"   • ⏰ <b>เวลาหมดอายุ:</b> <code>{format_thai_datetime(latest_sub.expires_at)} น.</code>")
            else:
                user_block.append(f"   • ⏰ <b>เวลาหมดอายุ:</b> <i>ยังไม่เริ่มนับ (เริ่มนับเมื่อกดเข้าห้อง)</i>")
        else:
            user_block.append("   • 📦 <i>ยังไม่มีประวัติการขอแพ็กเกจ</i>")

        if u.payment_slips:
            user_block.append(f"   • 💳 สลิปชำระเงิน: {len(u.payment_slips)} รายการ (ล่าสุด: <b>{u.payment_slips[0].status}</b>)")

        user_block.append("")
        lines.extend(user_block)

        # เพิ่มปุ่มจัดการผู้ใช้รายบุคคล (UX improvement)
        btn_name = html.escape(u.full_name or f"User {u.telegram_id}")
        buttons.append([InlineKeyboardButton(text=f"👤 {i}. จัดการ {btn_name}", callback_data=f"admin:view_user:{u.telegram_id}")])

    lines.append(f"📄 <b>หน้า {page}/{total_pages}</b> (แสดงครั้งละ {USERS_PER_PAGE} คน)")
    lines.append("💡 <i>คลิกที่ปุ่มด้านล่างเพื่อจัดการผู้ใช้งานรายบุคคล</i>")

    # ปุ่มเปลี่ยนหน้าแบบ Interactive
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️ หน้าก่อน", callback_data=f"admin:users_page:{page-1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="admin:noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="หน้าถัดไป ▶️", callback_data=f"admin:users_page:{page+1}"))
    
    if nav_row:
        buttons.append(nav_row)
    
    buttons.append([
        InlineKeyboardButton(text="🔄 รีเฟรช", callback_data=f"admin:users_page:{page}")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), keyboard


@router.message(Command("users", "all_users", "user_list"))
async def handle_admin_users_command(message: Message):
    """คำสั่งดูประวัติผู้ใช้งานทั้งหมดในระบบ: /users [หน้าที่ต้องการดู]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    page = 1
    if len(args) >= 2 and args[1].isdigit():
        page = int(args[1])

    text, markup = await build_users_list_view(page=page)
    await message.answer(text=text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:users_page:"))
async def handle_admin_users_page_callback(callback: CallbackQuery):
    """จัดการการเปลี่ยนหน้าในรายงาน /users ผ่าน Inline Keyboard"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    page_str = callback.data.split(":")[-1]
    try:
        page = int(page_str)
    except ValueError:
        page = 1

    text, markup = await build_users_list_view(page=page)
    try:
        await callback.message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "admin:noop")
async def handle_admin_noop(callback: CallbackQuery):
    """Callback เปล่าสำหรับปุ่มแสดงเลขหน้า"""
    await callback.answer()


async def build_users_latest_view(limit: int = 10) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """สร้างรายงานสรุปรายชื่อผู้ใช้งานใหม่ล่าสุด N คน (Default 10 คน) แบบรวดเร็วและประหยัด Query"""
    now = datetime.now(timezone.utc)
    
    async with get_session() as session:
        # 1. ดึงสถิติจำนวนรวม
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0
        total_active = (await session.execute(
            select(func.count(Subscription.user_id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )).scalar() or 0
        total_trial_used = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0

        if total_users == 0:
            return "ℹ️ <i>ขณะนี้ยังไม่มีข้อมูลผู้ใช้งานในระบบฐานข้อมูล</i>", None

        # 2. ดึงข้อมูล User ล่าสุด 10 คน (เรียงจากใหม่สุดไปเก่า)
        stmt = (
            select(User)
            .options(
                selectinload(User.subscription),
                selectinload(User.payment_slips),
            )
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        users = (await session.execute(stmt)).scalars().all()

    now_thai = format_thai_datetime(now)
    lines = [
        f"⚡ <b>รายงานผู้ใช้งานสมัครใหม่ล่าสุด {len(users)} คน (Latest Users)</b>",
        f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 <b>ผู้ใช้ทั้งหมดในระบบ:</b> <b>{total_users} คน</b> | 🟢 <b>Active:</b> <b>{total_active} คน</b>",
        f"⏱️ <b>ใช้สิทธิ์ฟรี 15m แล้ว:</b> {total_trial_used} คน",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    buttons = []
    for i, u in enumerate(users, start=1):
        u_header = format_user_title(u.full_name, u.username, u.telegram_id)
        trial_str = "เคยใช้แล้ว ✅" if u.trial_used else "ยังไม่เคยใช้ ⏱️"
        
        user_block = [
            f"<b>{i}.</b> {u_header}",
            f"   • สิทธิ์ฟรี: {trial_str} | 📅 <b>เข้าใช้บอทครั้งแรก:</b> <code>{format_thai_datetime(u.created_at)} น.</code>",
        ]

        # เวลากดเข้า Channel ล่าสุด
        if u.subscription and u.subscription.joined_at:
            user_block.append(f"   • 🚪 <b>เวลากดเข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(u.subscription.joined_at)} น.</code>")
        else:
            user_block.append("   • 🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>")

        # Subscription ปัจจุบัน
        latest_sub = u.subscription
        if latest_sub:
            plan_label, quota_str = get_subscription_quota_and_label(latest_sub)
            status_badge = {
                SubStatus.ACTIVE.value: "🟢 ACTIVE",
                SubStatus.PENDING.value: "🟡 PENDING (รอกดเข้า)",
                SubStatus.EXPIRED.value: "⚪ EXPIRED",
                SubStatus.KICKED.value: "🔴 KICKED",
                SubStatus.KICK_FAILED.value: "⚠️ KICK_FAILED",
            }.get(latest_sub.status, latest_sub.status)

            user_block.append(f"   • 📦 <b>สถานะล่าสุด:</b> {plan_label} [{status_badge}]")
            user_block.append(f"   • ⏳ <b>โควต้าระยะเวลา:</b> <b>{quota_str}</b>")
            if latest_sub.expires_at:
                user_block.append(f"   • ⏰ <b>หมดอายุ:</b> <code>{format_thai_datetime(latest_sub.expires_at)} น.</code>")
            else:
                user_block.append(f"   • ⏰ <b>หมดอายุ:</b> <i>ยังไม่เริ่มนับ (เริ่มนับเมื่อกดเข้าห้อง)</i>")
        else:
            user_block.append("   • 📦 <i>ยังไม่มีประวัติการขอแพ็กเกจ</i>")

        if u.payment_slips:
            user_block.append(f"   • 💳 สลิปชำระเงิน: {len(u.payment_slips)} รายการ (ล่าสุด: <b>{u.payment_slips[0].status}</b>)")

        user_block.append("")
        lines.extend(user_block)

        btn_name = html.escape(u.full_name or f"User {u.telegram_id}")
        buttons.append([InlineKeyboardButton(text=f"👤 {i}. จัดการ {btn_name}", callback_data=f"admin:view_user:{u.telegram_id}")])

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ <i>แสดงเฉพาะ 10 สมาชิกใหม่ล่าสุดแบบรวดเร็ว | ดูทั้งหมดพร้อมเลื่อนหน้าใช้ /users</i>")

    buttons.append([
        InlineKeyboardButton(text="🔄 รีเฟรช", callback_data="admin_menu:users_latest"),
        InlineKeyboardButton(text="📑 ดูทั้งหมด (/users)", callback_data="admin:users_page:1"),
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), keyboard


@router.message(Command("users_lasted", "users_latest", "latest_users", "lasted_users"))
async def handle_admin_users_latest_command(message: Message):
    """คำสั่งดูรายชื่อผู้ใช้งานที่สมัครใหม่ล่าสุด 10 คน: /users_lasted หรือ /users_latest"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    text, markup = await build_users_latest_view(limit=10)
    await message.answer(text=text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:users_latest")
async def handle_admin_users_latest_callback(callback: CallbackQuery):
    """จัดการการกดดูหรือรีเฟรช 10 สมาชิกใหม่ล่าสุดผ่าน Inline Keyboard"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    text, markup = await build_users_latest_view(limit=10)
    try:
        await callback.message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()


async def build_top_referrals_view(page: int = 1, page_size: int = 10) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """สร้างรายงานอันดับผู้แนะนำเพื่อน (Top Referrers) เรียงจากยอดชวนเพื่อนมากไปน้อย"""
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # 1. สถิติภาพรวม Referral
        total_referrers = (await session.execute(
            select(func.count(User.telegram_id)).where(func.coalesce(User.referral_count, 0) > 0)
        )).scalar() or 0

        total_invited = (await session.execute(
            select(func.coalesce(func.sum(User.referral_count), 0)).where(func.coalesce(User.referral_count, 0) > 0)
        )).scalar() or 0

        total_bonus_days = (await session.execute(
            select(func.coalesce(func.sum(User.referral_bonus_days), 0)).where(func.coalesce(User.referral_bonus_days, 0) > 0)
        )).scalar() or 0

        if total_referrers == 0:
            return (
                "🏆 <b>รายงานอันดับผู้แนะนำเพื่อน (Top Referrals Leaderboard)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "ℹ️ <i>ขณะนี้ยังไม่มีผู้ใช้งานคนใดชวนเพื่อนสำเร็จในระบบ</i>",
                None,
            )

        # คำนวณหน้า
        total_pages = max(1, (total_referrers + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size

        # 2. ดึงผู้ใช้ที่ชวนเพื่อน เรียงจากมากไปน้อย
        stmt = (
            select(User)
            .options(selectinload(User.subscription))
            .where(func.coalesce(User.referral_count, 0) > 0)
            .order_by(User.referral_count.desc(), User.referral_bonus_days.desc(), User.created_at.asc())
            .offset(offset)
            .limit(page_size)
        )
        users = (await session.execute(stmt)).scalars().all()

        # แปลงข้อมูลผู้ใช้ภายใน Session ให้เสร็จสมบูรณ์ 100% ป้องกัน DetachedInstanceError
        user_rows = []
        for idx, u in enumerate(users, start=offset + 1):
            user_handle = f"@{u.username}" if u.username else "ไม่มี Username"
            full_name_safe = html.escape(u.full_name or f"User {u.telegram_id}")
            ref_count = u.referral_count or 0
            bonus_days = u.referral_bonus_days or 0
            u_id = u.telegram_id

            # ตรวจสอบสถานะ VIP (1 แถวเดียวต่อ user)
            u_sub = u.subscription
            is_active = bool(u_sub and u_sub.status == SubStatus.ACTIVE.value and u_sub.expires_at and ensure_utc(u_sub.expires_at) > now)
            is_pending = bool(u_sub and u_sub.status == SubStatus.PENDING.value and ((u_sub.pending_days or 0) > 0 or (u_sub.pending_minutes or 0) > 0))

            if is_active:
                exp_thai = format_thai_datetime(u_sub.expires_at)
                rem_str = format_time_remaining(u_sub.expires_at)
                vip_status_str = f"🟢 ACTIVE (หมดอายุ: <code>{exp_thai} น.</code> - เหลือ {rem_str})"
            elif is_pending:
                vip_status_str = f"🟡 PENDING (มีโควต้ารอกดเข้าสะสม <b>{u_sub.pending_days} วัน</b>)"
            else:
                vip_status_str = "⚪ EXPIRED (ไม่อยู่ในห้อง)"

            user_rows.append({
                "idx": idx,
                "uid": u_id,
                "name": full_name_safe,
                "handle": user_handle,
                "ref_count": ref_count,
                "bonus_days": bonus_days,
                "vip_status": vip_status_str,
            })

    now_thai = format_thai_datetime(now)
    lines = [
        "🏆 <b>อันดับผู้แนะนำเพื่อนสูงสุด (Top Referrals Leaderboard)</b>",
        f"📅 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>",
        f"📄 <b>หน้าที่:</b> {page}/{total_pages} (ทั้งหมด {total_referrers} คน)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>สถิติระบบชวนเพื่อนรวม (Overview):</b>",
        f"• 👥 สมาชิกที่เคยชวนเพื่อน: <b>{total_referrers} คน</b>",
        f"• 🤝 ยอดชวนเพื่อนสำเร็จรวม: <b>{total_invited} คน</b>",
        f"• 🎁 โบนัส VIP ที่แจกไปแล้ว: <b>{total_bonus_days} วัน ({total_bonus_days * 24} ชั่วโมง)</b>",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    buttons = []
    medals = ["🥇", "🥈", "🥉"]

    for row in user_rows:
        idx = row["idx"]
        medal = medals[idx - 1] if idx <= 3 else f"<b>#{idx}</b>"
        u_header = format_user_title(row['name'], row['handle'].lstrip("@") if row['handle'] != "ไม่มี Username" else None, row['uid'])
        user_block = [
            f"{medal} <b>อันดับ {idx}.</b> {u_header}",
            f"   • 👥 <b>ชวนเพื่อนสำเร็จ:</b> <b>{row['ref_count']} คน</b> | 🎁 <b>โบนัสสะสม:</b> <b>{row['bonus_days']} วัน</b>",
            f"   • 📦 <b>สถานะ VIP:</b> {row['vip_status']}",
            "",
        ]
        lines.extend(user_block)

        buttons.append([InlineKeyboardButton(text=f"👤 {idx}. จัดการ {row['name']} ({row['ref_count']} คน)", callback_data=f"admin:view_user:{row['uid']}")])

    # ปุ่มเปลี่ยนหน้า
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ ก่อนหน้า", callback_data=f"admin:top_refs_page:{page-1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="admin:noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="ถัดไป ➡️", callback_data=f"admin:top_refs_page:{page+1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton(text="🔄 รีเฟรช", callback_data=f"admin:top_refs_page:{page}"),
        InlineKeyboardButton(text="⚡ 10 ผู้ใช้ล่าสุด", callback_data="admin_menu:users_latest"),
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), keyboard


@router.message(Command("top_referrals", "top_referral", "top_refs", "referrals", "ref_ranking", "referral_leaderboard"))
async def handle_admin_top_referrals_command(message: Message):
    """คำสั่งดูอันดับผู้แนะนำเพื่อนสูงสุด (Leaderboard): /top_referrals [หน้าที่ต้องการดู]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    page = 1
    if len(args) >= 2 and args[1].isdigit():
        page = int(args[1])

    try:
        text, markup = await build_top_referrals_view(page=page)
        await message.answer(text=text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in top_referrals command: {e}", exc_info=True)
        await message.answer(f"❌ <b>เกิดข้อผิดพลาด:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:top_refs_page:"))
async def handle_admin_top_refs_page_callback(callback: CallbackQuery):
    """จัดการการเปลี่ยนหน้าในรายงาน /top_referrals ผ่าน Inline Keyboard"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    await callback.answer()
    page_str = callback.data.split(":")[-1]
    try:
        page = int(page_str)
    except ValueError:
        page = 1

    try:
        text, markup = await build_top_referrals_view(page=page)
        await callback.message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error editing top_referrals page: {e}", exc_info=True)


@router.callback_query(F.data == "admin_menu:top_referrals")
async def handle_admin_menu_top_referrals_callback(callback: CallbackQuery):
    """จัดการปุ่มลัด [🏆 อันดับชวนเพื่อน] ในเมนู Admin"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    await callback.answer()
    try:
        text, markup = await build_top_referrals_view(page=1)
        await callback.message.answer(text=text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error handling admin_menu:top_referrals: {e}", exc_info=True)
        await callback.message.answer(f"❌ <b>เกิดข้อผิดพลาดในการโหลดอันดับชวนเพื่อน:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


async def get_referral_status_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความสรุปสถานะระบบแนะนำเพื่อนและ Inline Keyboard สำหรับ Admin"""
    is_active = is_referral_active()
    status_str = "🟢 เปิดใช้งาน (Active) — แสดงปุ่มในเมนู /start และแจกโบนัส VIP เมื่อเพื่อนเข้าห้อง" if is_active else "🔴 ปิดใช้งาน (Disabled) — ซ่อนปุ่ม และไม่แจกโบนัสวันแม้มีคนเข้าผ่านลิงก์"

    async with get_session() as session:
        # สถิติรวม Referral ทั้งระบบ
        total_referrers = (await session.execute(
            select(func.count(User.telegram_id)).where(func.coalesce(User.referral_count, 0) > 0)
        )).scalar() or 0
        total_friends_joined = (await session.execute(
            select(func.coalesce(func.sum(User.referral_count), 0)).where(func.coalesce(User.referral_count, 0) > 0)
        )).scalar() or 0
        total_bonus_days = (await session.execute(
            select(func.coalesce(func.sum(User.referral_bonus_days), 0)).where(func.coalesce(User.referral_bonus_days, 0) > 0)
        )).scalar() or 0

    text = (
        "👥 <b>ระบบจัดการการแนะนำเพื่อน (Referral System Management)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>สถานะปัจจุบัน:</b> {status_str}\n\n"
        "📈 <b>สถิติรวมทั้งระบบ:</b>\n"
        f"• 👑 ผู้แนะนำทั้งหมด: <b>{total_referrers:,} คน</b>\n"
        f"• 👥 เพื่อนที่ชวนสำเร็จ: <b>{total_friends_joined:,} คน</b>\n"
        f"• 🏆 โบนัส VIP ที่แจกไปแล้ว: <b>{total_bonus_days:,} วัน</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>คำสั่งสำหรับควบคุมระบบ:</b>\n"
        "• <code>/referral_on</code> หรือ <code>/ref_on</code> — 🟢 เปิดใช้งานระบบชวนเพื่อน (แสดงปุ่มในเมนู /start)\n"
        "• <code>/referral_off</code> หรือ <code>/ref_off</code> — 🔴 ปิดใช้งานระบบชวนเพื่อน (ซ่อนปุ่ม และไม่ให้วันโบนัส)\n"
        "• <code>/top_refs</code> — 🏆 ดูตารางอันดับผู้ใช้ที่ชวนเพื่อนมากที่สุด (Leaderboard)\n"
        "• <code>/referral</code> — 🔄 ดูสถานะระบบแนะนำเพื่อน\n\n"
        "💡 <i>แตะปุ่มด่วนด้านล่างเพื่อเปิด/ปิดระบบได้ทันที:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 เปิดระบบชวนเพื่อน", callback_data="ref_action:on"),
            InlineKeyboardButton(text="🔴 ปิดระบบชวนเพื่อน", callback_data="ref_action:off"),
        ],
        [
            InlineKeyboardButton(text="🏆 ดูอันดับผู้ชวน (/top_refs)", callback_data="admin_menu:top_referrals"),
            InlineKeyboardButton(text="🔄 รีเฟรชสถานะ", callback_data="admin_menu:referral"),
        ],
        [
            InlineKeyboardButton(text="🔙 กลับสู่เมนูแอดมิน", callback_data="admin_menu:main"),
        ]
    ])
    return text, kb


async def show_referral_status(message_or_callback):
    """ส่งหรือแก้ไขข้อความแสดงสถานะระบบแนะนำเพื่อน"""
    text, kb = await get_referral_status_text_and_kb()
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message_or_callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_callback.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("referral", "ref", "referral_setting", "ref_setting", "referral_status", "ref_status"))
async def handle_referral_command(message: Message):
    """คำสั่งดูสถานะหรือควบคุมระบบแนะนำเพื่อน: /referral [on/off]"""
    if not is_admin_chat(message.chat.id):
        return
    args = (message.text or "").split()[1:]
    if not args:
        await show_referral_status(message)
        return

    subcmd = args[0].lower()
    if subcmd in ("on", "enable", "start", "open"):
        update_referral_settings(is_active=True)
        text, kb = await get_referral_status_text_and_kb()
        await message.answer(f"✅ <b>เปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    elif subcmd in ("off", "disable", "stop", "close"):
        update_referral_settings(is_active=False)
        text, kb = await get_referral_status_text_and_kb()
        await message.answer(f"❌ <b>ปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    else:
        await show_referral_status(message)


@router.message(Command("referral_on", "ref_on"))
async def handle_referral_on_command(message: Message):
    """คำสั่งเปิดใช้งานระบบแนะนำเพื่อน: /referral_on"""
    if not is_admin_chat(message.chat.id):
        return
    update_referral_settings(is_active=True)
    text, kb = await get_referral_status_text_and_kb()
    await message.answer(f"✅ <b>เปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("referral_off", "ref_off"))
async def handle_referral_off_command(message: Message):
    """คำสั่งปิดใช้งานระบบแนะนำเพื่อน: /referral_off"""
    if not is_admin_chat(message.chat.id):
        return
    update_referral_settings(is_active=False)
    text, kb = await get_referral_status_text_and_kb()
    await message.answer(f"❌ <b>ปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:referral")
async def handle_admin_menu_referral_callback(callback: CallbackQuery):
    """จัดการปุ่มลัด [👥 ระบบชวนเพื่อน] ในเมนู Admin"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    await show_referral_status(callback)


@router.callback_query(F.data.startswith("ref_action:"))
async def handle_ref_action_callback(callback: CallbackQuery):
    """จัดการ Quick Actions ปุ่มลัดเปิด/ปิด Referral"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "on":
        update_referral_settings(is_active=True)
        await callback.answer("✅ เปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว")
        text, kb = await get_referral_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    elif action == "off":
        update_referral_settings(is_active=False)
        await callback.answer("❌ ปิดใช้งานระบบแนะนำเพื่อนเรียบร้อยแล้ว")
        text, kb = await get_referral_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:main")
async def handle_admin_menu_main_callback(callback: CallbackQuery):
    """จัดการปุ่มลัดกลับสู่เมนูหลัก Admin"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    text, kb = get_admin_menu_text_and_kb()
    try:
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")


async def get_trial_status_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความสรุปสถานะระบบทดลองใช้งานฟรีและ Inline Keyboard สำหรับ Admin"""
    is_active = is_trial_active()
    status_str = f"🟢 เปิดใช้งาน (Active) — แสดงปุ่มทดลองฟรี {config.TRIAL_DURATION_MINUTES} นาที ในเมนู /start" if is_active else "🔴 ปิดใช้งาน (Disabled) — ซ่อนปุ่ม และปิดการขอรับสิทธิ์ทดลองฟรี"

    async with get_session() as session:
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0
        trial_used_count = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0
        never_trial_count = total_users - trial_used_count

    text = (
        "⏱️ <b>ระบบจัดการการทดลองใช้งานฟรี (Trial System Management)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>สถานะปัจจุบัน:</b> {status_str}\n"
        f"⏳ <b>ระยะเวลาทดลอง:</b> <b>{config.TRIAL_DURATION_MINUTES} นาที</b>\n\n"
        "📈 <b>สถิติผู้ใช้งาน:</b>\n"
        f"• 👥 ผู้ใช้ทั้งหมด: <b>{total_users:,} คน</b>\n"
        f"• ✅ เคยใช้สิทธิ์ทดลองแล้ว: <b>{trial_used_count:,} คน</b>\n"
        f"• ⏳ ยังไม่เคยใช้สิทธิ์: <b>{never_trial_count:,} คน</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>คำสั่งสำหรับควบคุมระบบ:</b>\n"
        "• <code>/trial_on</code> — 🟢 เปิดใช้งานระบบทดลองฟรี (แสดงปุ่มในเมนู /start)\n"
        "• <code>/trial_off</code> — 🔴 ปิดใช้งานระบบทดลองฟรี (ซ่อนปุ่ม และปิดรับสิทธิ์)\n"
        "• <code>/reset_trial [User ID หรือ @username]</code> — รีเซ็ตสิทธิ์ทดลองฟรีให้ผู้ใช้รายคน\n"
        "• <code>/trial</code> — 🔄 ดูสถานะระบบทดลองฟรี\n\n"
        "💡 <i>แตะปุ่มด่วนด้านล่างเพื่อเปิด/ปิดระบบได้ทันที:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 เปิดทดลองฟรี", callback_data="trial_action:on"),
            InlineKeyboardButton(text="🔴 ปิดทดลองฟรี", callback_data="trial_action:off"),
        ],
        [
            InlineKeyboardButton(text="🔄 รีเฟรชสถานะ", callback_data="admin_menu:trial"),
            InlineKeyboardButton(text="🔙 กลับสู่เมนูแอดมิน", callback_data="admin_menu:main"),
        ]
    ])
    return text, kb


async def show_trial_status(message_or_callback):
    """ส่งหรือแก้ไขข้อความแสดงสถานะระบบทดลองฟรี"""
    text, kb = await get_trial_status_text_and_kb()
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message_or_callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_callback.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("trial", "trial_status", "trial_setting"))
async def handle_trial_command(message: Message):
    """คำสั่งดูสถานะหรือควบคุมระบบทดลองฟรี: /trial [on/off]"""
    if not is_admin_chat(message.chat.id):
        return
    args = (message.text or "").split()[1:]
    if not args:
        await show_trial_status(message)
        return

    subcmd = args[0].lower()
    if subcmd in ("on", "enable", "start", "open"):
        update_trial_settings(is_active=True)
        text, kb = await get_trial_status_text_and_kb()
        await message.answer(f"✅ <b>เปิดใช้งานระบบทดลองใช้งานฟรีเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    elif subcmd in ("off", "disable", "stop", "close"):
        update_trial_settings(is_active=False)
        text, kb = await get_trial_status_text_and_kb()
        await message.answer(f"❌ <b>ปิดใช้งานระบบทดลองใช้งานฟรีเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    else:
        await show_trial_status(message)


@router.message(Command("trial_on"))
async def handle_trial_on_command(message: Message):
    """คำสั่งเปิดใช้งานระบบทดลองฟรี: /trial_on"""
    if not is_admin_chat(message.chat.id):
        return
    update_trial_settings(is_active=True)
    text, kb = await get_trial_status_text_and_kb()
    await message.answer(f"✅ <b>เปิดใช้งานระบบทดลองใช้งานฟรีเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("trial_off"))
async def handle_trial_off_command(message: Message):
    """คำสั่งปิดใช้งานระบบทดลองฟรี: /trial_off"""
    if not is_admin_chat(message.chat.id):
        return
    update_trial_settings(is_active=False)
    text, kb = await get_trial_status_text_and_kb()
    await message.answer(f"❌ <b>ปิดใช้งานระบบทดลองใช้งานฟรีเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:trial")
async def handle_admin_menu_trial_callback(callback: CallbackQuery):
    """จัดการปุ่มลัด [⏱️ ระบบทดลองฟรี] ในเมนู Admin"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    await show_trial_status(callback)


@router.callback_query(F.data.startswith("trial_action:"))
async def handle_trial_action_callback(callback: CallbackQuery):
    """จัดการ Quick Actions ปุ่มลัดเปิด/ปิด Trial"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "on":
        update_trial_settings(is_active=True)
        await callback.answer("✅ เปิดใช้งานระบบทดลองฟรีเรียบร้อยแล้ว")
        text, kb = await get_trial_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    elif action == "off":
        update_trial_settings(is_active=False)
        await callback.answer("❌ ปิดใช้งานระบบทดลองฟรีเรียบร้อยแล้ว")
        text, kb = await get_trial_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")


async def get_unanswered_reminder_status_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความและปุ่มสำหรับหน้าจัดการระบบแจ้งเตือนข้อความค้างตอบ (Unanswered DM Reminder)"""
    is_active = is_unanswered_dm_reminder_active()
    status_icon = "🟢" if is_active else "🔴"
    status_str = "เปิดใช้งาน (แจ้งเตือนทุก 10 นาทีเมื่อมีข้อความค้างตอบ)" if is_active else "ปิดใช้งาน (ไม่ส่งข้อความเตือนค้างตอบ)"

    text = (
        "🔔 <b>ระบบแจ้งเตือนข้อความค้างตอบ (Unanswered DM Reminder)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>สถานะปัจจุบัน:</b> {status_icon} <b>{status_str}</b>\n\n"
        "ℹ️ <b>คำอธิบายการทำงาน:</b>\n"
        "• เมื่อมีผู้ใช้ส่งข้อความ Direct Message หาบอท และแอดมินยังไม่ตอบเกิน 10 นาที\n"
        "• ระบบจะรวบรวมรายชื่อและส่งข้อความแจ้งเตือน <code>🚨 [แจ้งเตือนข้อความค้างตอบ]</code> เข้า Admin Group ทุกๆ 10 นาที\n"
        "• หากปิดใช้งาน ระบบจะไม่ส่งข้อความเตือนซ้ำนี้เข้ากลุ่มแอดมิน\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👉 <i>แตะปุ่มด้านล่างเพื่อเปิดหรือปิดการแจ้งเตือนได้ทันที:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 เปิดการแจ้งเตือน", callback_data="dm_reminder_action:on"),
            InlineKeyboardButton(text="🔴 ปิดการแจ้งเตือน", callback_data="dm_reminder_action:off"),
        ],
        [
            InlineKeyboardButton(text="🔄 รีเฟรชสถานะ", callback_data="admin_menu:unanswered_reminder"),
            InlineKeyboardButton(text="🔙 กลับสู่เมนูแอดมิน", callback_data="admin_menu:main"),
        ]
    ])
    return text, kb


async def show_unanswered_reminder_status(message_or_callback):
    """ส่งหรือแก้ไขข้อความแสดงสถานะระบบแจ้งเตือนข้อความค้างตอบ"""
    text, kb = await get_unanswered_reminder_status_text_and_kb()
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message_or_callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_callback.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("dm_reminder", "notify_unanswered", "unanswered_reminder", "toggle_unanswered", "reminder_setting"))
async def handle_dm_reminder_command(message: Message):
    """คำสั่งดูสถานะหรือควบคุมระบบแจ้งเตือนข้อความค้างตอบ: /dm_reminder [on/off]"""
    if not is_admin_chat(message.chat.id):
        return
    args = (message.text or "").split()[1:]
    if not args:
        await show_unanswered_reminder_status(message)
        return

    subcmd = args[0].lower()
    if subcmd in ("on", "enable", "start", "open"):
        update_unanswered_dm_reminder_setting(is_active=True)
        text, kb = await get_unanswered_reminder_status_text_and_kb()
        await message.answer(f"✅ <b>เปิดใช้งานการแจ้งเตือนข้อความค้างตอบเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    elif subcmd in ("off", "disable", "stop", "close"):
        update_unanswered_dm_reminder_setting(is_active=False)
        text, kb = await get_unanswered_reminder_status_text_and_kb()
        await message.answer(f"❌ <b>ปิดใช้งานการแจ้งเตือนข้อความค้างตอบเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    else:
        await show_unanswered_reminder_status(message)


@router.message(Command("dm_reminder_on", "notify_unanswered_on"))
async def handle_dm_reminder_on_command(message: Message):
    """คำสั่งเปิดใช้งานการแจ้งเตือนข้อความค้างตอบ: /dm_reminder_on"""
    if not is_admin_chat(message.chat.id):
        return
    update_unanswered_dm_reminder_setting(is_active=True)
    text, kb = await get_unanswered_reminder_status_text_and_kb()
    await message.answer(f"✅ <b>เปิดใช้งานการแจ้งเตือนข้อความค้างตอบเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("dm_reminder_off", "notify_unanswered_off"))
async def handle_dm_reminder_off_command(message: Message):
    """คำสั่งปิดใช้งานการแจ้งเตือนข้อความค้างตอบ: /dm_reminder_off"""
    if not is_admin_chat(message.chat.id):
        return
    update_unanswered_dm_reminder_setting(is_active=False)
    text, kb = await get_unanswered_reminder_status_text_and_kb()
    await message.answer(f"❌ <b>ปิดใช้งานการแจ้งเตือนข้อความค้างตอบเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:unanswered_reminder")
async def handle_admin_menu_unanswered_reminder_callback(callback: CallbackQuery):
    """จัดการปุ่มลัด [🔔 แจ้งเตือนข้อความค้างตอบ] ในเมนู Admin"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    await show_unanswered_reminder_status(callback)


@router.callback_query(F.data.startswith("dm_reminder_action:"))
async def handle_dm_reminder_action_callback(callback: CallbackQuery):
    """จัดการ Quick Actions ปุ่มลัดเปิด/ปิด การแจ้งเตือนข้อความค้างตอบ"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "on":
        update_unanswered_dm_reminder_setting(is_active=True)
        await callback.answer("✅ เปิดใช้งานการแจ้งเตือนข้อความค้างตอบแล้ว")
        text, kb = await get_unanswered_reminder_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    elif action == "off":
        update_unanswered_dm_reminder_setting(is_active=False)
        await callback.answer("❌ ปิดใช้งานการแจ้งเตือนข้อความค้างตอบแล้ว")
        text, kb = await get_unanswered_reminder_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("revoke_primary", "reset_primary_link", "revoke_channel_link"))
async def handle_revoke_primary_link(message: Message, bot: Bot):
    """คำสั่งสั่งเพิกถอนและสร้าง Primary Invite Link ใหม่ของ Channel: /revoke_primary"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    try:
        new_primary = await bot.export_chat_invite_link(chat_id=config.CHANNEL_ID)
        resp_text = (
            "🔄 <b>เพิกถอนลิงก์หลัก (Primary Link) เดิมของ Channel สำเร็จแล้ว!</b>\n\n"
            "❌ ลิงก์เดิมทั้งหมดถูกทำลายและไม่สามารถใช้งานได้อีกต่อไป\n"
            f"🔗 <b>ลิงก์หลักชุดใหม่:</b> <code>{new_primary}</code>\n\n"
            "⚠️ <i>แนะนำ: ห้ามแจกลิงก์หลักนี้ให้สมาชิกทั่วไป ให้ระบบบอทสร้างลิงก์แบบ 1 คน/1 ครั้งและมีวันหมดอายุเท่านั้นครับ</i>"
        )
        await message.answer(resp_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to revoke primary invite link: {e}", exc_info=True)
        await message.answer(
            f"❌ <b>ไม่สามารถเพิกถอนลิงก์ได้:</b> <code>{html.escape(str(e))}</code>\n\n"
            "👉 <i>กรุณาตรวจสอบว่าบอทมีสิทธิ์ 'Invite Users via Link / เชิญผู้ใช้ผ่านลิงก์' ใน Channel VIP หรือไม่</i>",
            parse_mode="HTML",
        )


@router.message(Command("revoke_link", "revoke"))
async def handle_revoke_specific_link(message: Message, bot: Bot):
    """คำสั่งเพิกถอนลิงก์เฉพาะเจาะจง: /revoke_link <url>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("⚠️ <b>รูปแบบคำสั่ง:</b> <code>/revoke_link [ลิงก์ที่ต้องการเพิกถอน]</code>", parse_mode="HTML")
        return

    link_to_revoke = args[1].strip()
    try:
        res = await bot.revoke_chat_invite_link(chat_id=config.CHANNEL_ID, invite_link=link_to_revoke)
        await message.answer(f"✅ <b>เพิกถอนลิงก์สำเร็จแล้ว:</b> <code>{res.invite_link}</code>\n(ลิงก์นี้ใช้งานไม่ได้แล้ว)", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ <b>เพิกถอนไม่สำเร็จ:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


def get_start_menu_content(trial_available: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความและปุ่มเมนูหลัก /start สำหรับส่งให้ผู้ใช้"""
    text = (
        "👋 <b>ยินดีต้อนรับสู่ระบบสมาชิก BareLive!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>สิทธิประโยชน์สำหรับสมาชิก VIP:</b>\n"
        "• 📺 รับชมการถ่ายทอดสดแบบ Exclusive ใน Channel VIP\n"
        "• 💬 พูดคุยแลกเปลี่ยนในกลุ่มแชทชุมชนฟรี\n"
        "• 🎁 <b>ใหม่! ชวนเพื่อนรับ VIP ฟรี +1 วัน/คน ไม่จำกัด!</b>\n"
        "• ⚡ ระบบอัตโนมัติตลอด 24 ชั่วโมง\n\n"
        "👇 <b>กรุณาเลือกรายการที่ต้องการด้านล่างได้เลยครับ:</b>"
    )
    keyboard = get_main_menu_keyboard(trial_available=trial_available)
    return text, keyboard


@router.message(Command("broadcast_count", "reach", "audience"))
async def handle_broadcast_count_command(message: Message):
    """คำสั่งตรวจสอบจำนวนผู้ใช้ทั้งหมดที่สามารถ Broadcast ได้: /broadcast_count"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        total_users = (await session.execute(select(func.count(User.telegram_id)))).scalar() or 0
        active_users = (await session.execute(
            select(func.count(Subscription.user_id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )).scalar() or 0
        trial_used_users = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0
        never_trial_users = total_users - trial_used_users

    resp = (
        "📢 <b>สถิติฐานผู้ใช้งานที่สามารถ Broadcast ได้ (Audience Reach)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>ผู้ใช้ทั้งหมดในระบบ:</b> <b>{total_users} คน</b>\n"
        f"🟢 <b>สมาชิก VIP Active ปัจจุบัน:</b> {active_users} คน\n"
        f"⏱️ <b>เคยใช้สิทธิ์ทดลองฟรีแล้ว:</b> {trial_used_users} คน\n"
        f"🎁 <b>ยังไม่เคยใช้สิทธิ์ทดลองฟรี:</b> {never_trial_users} คน\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>คำสั่งสำหรับการส่งข้อความ:</b>\n"
        "• <code>/broadcast_menu</code> — บรอดแคสต์เมนูหลัก /start ล่าสุด (พร้อมปุ่มชวนเพื่อน/โปรโมชั่น) ให้ทุกคน\n"
        "• <code>/broadcast [ข้อความ]</code> — บรอดแคสต์ข้อความข่าวสารหรือโปรโมชั่นให้ทุกคน\n"
        "• <code>/send_menu [User ID]</code> — ส่งเมนูหลัก /start ให้เฉพาะบุคคล\n"
        "• <code>/reply [User ID] [ข้อความ]</code> — ส่งข้อความหาเฉพาะบุคคล"
    )
    await message.answer(resp, parse_mode="HTML")


@router.message(Command("broadcast_menu", "broadcast_start", "bc_menu"))
async def handle_broadcast_menu_command(message: Message, bot: Bot):
    """คำสั่งบรอดแคสต์ส่ง Template เมนูหลัก /start ล่าสุดให้ผู้ใช้ทุกคนในฐานข้อมูล: /broadcast_menu"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    async with get_session() as session:
        users = (await session.execute(select(User).order_by(User.telegram_id))).scalars().all()

    total_count = len(users)
    if total_count == 0:
        await message.answer("❌ ไม่พบผู้ใช้ในระบบสำหรับ Broadcast", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"🚀 <b>กำลังเริ่ม Broadcast เมนูหลัก /start...</b>\n"
        f"👥 จำนวนเป้าหมาย: <b>{total_count} คน</b>\n"
        "⏳ กรุณารอสักครู่ ระบบกำลังทยอยส่งตาม Rate Limit...",
        parse_mode="HTML"
    )

    success_count = 0
    fail_count = 0

    for i, u in enumerate(users, 1):
        menu_text, menu_kb = get_start_menu_content(trial_available=not u.trial_used)
        sent = False
        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id=u.telegram_id,
                    text=menu_text,
                    reply_markup=menu_kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                success_count += 1
                sent = True
                break
            except TelegramRetryAfter as e:
                logger.warning(f"Flood limit in broadcast_menu. Sleeping for {e.retry_after}s (attempt {attempt+1}/3)...")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                logger.debug(f"Broadcast menu failed for user {u.telegram_id}: {e}")
                break

        if not sent:
            fail_count += 1

        # หน่วงเวลา 0.05 วินาที เพื่อป้องกัน Telegram Flood Limits
        await asyncio.sleep(0.05)

        # อัปเดตความคืบหน้าทุกๆ 50 คน
        if i % 50 == 0 or i == total_count:
            try:
                await status_msg.edit_text(
                    f"🚀 <b>กำลัง Broadcast เมนูหลัก /start ({i}/{total_count})...</b>\n"
                    f"✅ สำเร็จ: {success_count} คน\n"
                    f"❌ ไม่สำเร็จ (บล็อกบอท/ลบบัญชี): {fail_count} คน",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    final_report = (
        "🎉 <b>Broadcast เมนูหลัก /start เสร็จสมบูรณ์แล้ว!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>ผู้ใช้ทั้งหมด:</b> {total_count} คน\n"
        f"✅ <b>ส่งสำเร็จ:</b> <b>{success_count} คน</b>\n"
        f"❌ <b>ส่งไม่สำเร็จ (บล็อกบอท/ลบบัญชี):</b> {fail_count} คน\n"
        f"📅 <b>เวลาที่เสร็จสิ้น:</b> <code>{format_thai_datetime(datetime.now(timezone.utc))} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <i>ผู้ใช้ที่ได้รับข้อความจะเห็นเมนูเวอร์ชันล่าสุดพร้อมปุ่มชวนเพื่อนและเข้ากลุ่มฟรีทันที</i>"
    )
    try:
        await status_msg.edit_text(final_report, parse_mode="HTML")
    except Exception:
        await message.answer(final_report, parse_mode="HTML")


@router.message(Command("send_menu", "send_start"))
async def handle_send_menu_to_user_command(message: Message, bot: Bot):
    """คำสั่งส่ง Template เมนูหลัก /start ให้เฉพาะบุคคล: /send_menu <user_id หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/send_menu [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/send_menu 5125375696</code>\n"
            "• <code>/send_menu @username</code>",
            parse_mode="HTML"
        )
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        target_user = (await session.execute(user_stmt)).scalar_one_or_none()

    if not target_user:
        await message.answer(f"❌ <b>ไม่พบผู้ใช้:</b> <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
        return

    menu_text, menu_kb = get_start_menu_content(trial_available=not target_user.trial_used)
    try:
        await bot.send_message(
            chat_id=target_user.telegram_id,
            text=menu_text,
            reply_markup=menu_kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        user_handle = f"@{target_user.username}" if target_user.username else target_user.full_name
        await message.answer(
            f"✅ <b>ส่งเมนูหลัก /start ให้ผู้ใช้เรียบร้อยแล้ว!</b>\n"
            f"👤 <b>ผู้รับ:</b> {html.escape(user_handle)} (<code>{target_user.telegram_id}</code>)",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(
            f"❌ <b>ไม่สามารถส่ง DM ให้ผู้ใช้ได้:</b> <code>{html.escape(str(e))}</code>\n"
            "<i>(ผู้ใช้อาจจะบล็อกบอท หรือยังไม่เคยกดเริ่มคุยกับบอท)</i>",
            parse_mode="HTML"
        )


@router.message(Command("broadcast", "bc"))
async def handle_broadcast_custom_command(message: Message, bot: Bot):
    """คำสั่งบรอดแคสต์ข้อความกำหนดเอง หรือรูปภาพพร้อมแคปชันให้ทุกคน: /broadcast <ข้อความ>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    # ตรวจสอบข้อความที่จะ Broadcast
    broadcast_text = ""
    photo_file_id = None

    if message.reply_to_message:
        if message.reply_to_message.photo:
            photo_file_id = message.reply_to_message.photo[-1].file_id
            broadcast_text = message.reply_to_message.caption or ""
        elif message.reply_to_message.text:
            broadcast_text = message.reply_to_message.text

    # ถ้ามีข้อความตามหลังคำสั่ง /broadcast ให้ใช้ข้อความนั้น
    command_text_args = (message.text or "").split(maxsplit=1)
    if len(command_text_args) > 1:
        broadcast_text = command_text_args[1].strip()

    if not broadcast_text and not photo_file_id:
        await message.answer(
            "❌ <b>วิธีใช้งานคำสั่ง Broadcast:</b>\n\n"
            "1. <b>ส่งข้อความธรรมดา:</b>\n"
            "<code>/broadcast [ข้อความที่ต้องการส่งหาทุกคน]</code>\n\n"
            "2. <b>ส่งรูปภาพพร้อมข้อความ:</b>\n"
            "ส่งรูปเข้ากลุ่ม Admin แล้ว Reply รูปนั้นด้วยคำสั่ง <code>/broadcast [ข้อความแคปชัน]</code>",
            parse_mode="HTML"
        )
        return

    async with get_session() as session:
        users = (await session.execute(select(User).order_by(User.telegram_id))).scalars().all()

    total_count = len(users)
    if total_count == 0:
        await message.answer("❌ ไม่พบผู้ใช้ในระบบสำหรับ Broadcast", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"🚀 <b>กำลังเริ่ม Broadcast ข้อความไปยังผู้ใช้ {total_count} คน...</b>\n"
        "⏳ กรุณารอสักครู่...",
        parse_mode="HTML"
    )

    open_menu_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 เปิดเมนูหลัก VIP", callback_data="menu:main")]
        ]
    )

    success_count = 0
    fail_count = 0

    for i, u in enumerate(users, 1):
        sent = False
        for attempt in range(3):
            try:
                if photo_file_id:
                    await bot.send_photo(
                        chat_id=u.telegram_id,
                        photo=photo_file_id,
                        caption=broadcast_text,
                        reply_markup=open_menu_kb,
                        parse_mode="HTML",
                    )
                else:
                    await bot.send_message(
                        chat_id=u.telegram_id,
                        text=broadcast_text,
                        reply_markup=open_menu_kb,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
                success_count += 1
                sent = True
                break
            except TelegramRetryAfter as e:
                logger.warning(f"Flood limit in broadcast_custom. Sleeping for {e.retry_after}s (attempt {attempt+1}/3)...")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                logger.debug(f"Broadcast custom msg failed for user {u.telegram_id}: {e}")
                break

        if not sent:
            fail_count += 1

        await asyncio.sleep(0.05)

        if i % 50 == 0 or i == total_count:
            try:
                await status_msg.edit_text(
                    f"🚀 <b>กำลัง Broadcast ({i}/{total_count})...</b>\n"
                    f"✅ สำเร็จ: {success_count} คน\n"
                    f"❌ ไม่สำเร็จ (บล็อก/ลบบัญชี): {fail_count} คน",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    final_report = (
        "🎉 <b>Broadcast ข้อความเสร็จสมบูรณ์แล้ว!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>ผู้ใช้ทั้งหมด:</b> {total_count} คน\n"
        f"✅ <b>ส่งสำเร็จ:</b> <b>{success_count} คน</b>\n"
        f"❌ <b>ส่งไม่สำเร็จ:</b> {fail_count} คน\n"
        f"📅 <b>เวลาที่เสร็จสิ้น:</b> <code>{format_thai_datetime(datetime.now(timezone.utc))} น.</code>"
    )
    try:
        await status_msg.edit_text(final_report, parse_mode="HTML")
    except Exception:
        await message.answer(final_report, parse_mode="HTML")


@router.message(Command("deep_scan"))
async def handle_admin_deep_scan_command(message: Message, bot: Bot):
    """
    คำสั่ง /deep_scan
    เช็ค User ทุกคนใน Database ว่าถ้าไม่มีแพ็กเกจ ACTIVE หรือ PENDING
    แต่ตัวยังอยู่ใน Channel จะทำการเตะออกทันที
    """
    if not message.from_user or message.chat.id != config.ADMIN_GROUP_ID:
        return

    await message.answer("🔄 <b>กำลังเริ่มกระบวนการ Deep Scan...</b>\nระบบจะกวาดล้างผู้ใช้ที่หมดอายุแต่ยังค้างอยู่ในห้อง (อาจใช้เวลาหลายนาที กรุณารอจนกว่าจะเสร็จสิ้น)", parse_mode="HTML")

    kicked_count = 0
    error_count = 0
    admin_skip = 0
    scanned_count = 0

    try:
        async with get_session() as session:
            # ดึง User ID ทั้งหมดที่เคยใช้งานบอท
            stmt = select(User.telegram_id)
            users = (await session.execute(stmt)).scalars().all()

            for user_id in users:
                scanned_count += 1
                # 1. เช็คว่ามี ACTIVE หรือ PENDING อยู่ไหม
                active_stmt = (
                    select(Subscription)
                    .where(
                        Subscription.user_id == user_id,
                        Subscription.status.in_([SubStatus.ACTIVE.value, SubStatus.PENDING.value]),
                    )
                )
                has_active = (await session.execute(active_stmt)).scalar_one_or_none()

                if has_active:
                    continue

                # 2. ถ้าไม่มี ACTIVE/PENDING ให้ลองดึงสถานะจากทุก Target Channel
                kicked_any = False
                target_cids = get_all_target_channel_ids()

                for cid in target_cids:
                    try:
                        chat_member = await bot.get_chat_member(chat_id=cid, user_id=user_id)
                    except TelegramBadRequest as e:
                        continue
                    except TelegramRetryAfter as e:
                        from asyncio import sleep as asyncio_sleep
                        await asyncio_sleep(e.retry_after)
                        continue
                    except Exception:
                        continue

                    # 3. ถ้าอยู่ในห้อง แต่ไม่ใช่ Admin ให้เตะ!
                    if chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                        try:
                            await bot.ban_chat_member(chat_id=cid, user_id=user_id, revoke_messages=False)
                            await bot.unban_chat_member(chat_id=cid, user_id=user_id, only_if_banned=True)
                            kicked_any = True
                        except Exception as e:
                            error_count += 1
                    elif chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        admin_skip += 1

                if kicked_any:
                    kicked_count += 1
                    latest_sub = await session.get(Subscription, user_id)
                    if latest_sub:
                        latest_sub.status = SubStatus.KICKED.value
                        session.add(latest_sub)
                        await session.commit()
                
                # หน่วงเวลาเล็กน้อยป้องกัน Rate Limit จาก Telegram
                from asyncio import sleep as asyncio_sleep
                await asyncio_sleep(0.1)

        report_msg = (
            "✅ <b>Deep Scan เสร็จสิ้น!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 ตรวจสอบทั้งหมด: <b>{scanned_count}</b> บัญชี\n"
            f"👢 เตะคนที่ค้างสำเร็จ: <b>{kicked_count}</b> คน\n"
            f"⚠️ เตะไม่สำเร็จ (Error): <b>{error_count}</b> ครั้ง\n"
            f"🛡️ ข้ามแอดมิน: <b>{admin_skip}</b> ครั้ง\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await message.answer(report_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in deep_scan: {e}", exc_info=True)
        await message.answer("❌ เกิดข้อผิดพลาดระหว่างทำ Deep Scan กรุณาลองใหม่อีกครั้ง")


def get_kick_all_v1_confirmation_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความและปุ่มยืนยันสำหรับคำสั่งเตะทุกคนออกจาก Channel V.1"""
    channel_name = get_channel_label(config.CHANNEL_ID)
    text = (
        "⚠️ <b>ยืนยันการเตะสมาชิกทุกคนออกจาก Channel V.1?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Channel เป้าหมาย (V.1):</b> <b>{channel_name}</b> (<code>{config.CHANNEL_ID}</code>)\n\n"
        "🚨 <b>ผลกระทบของคำสั่งนี้:</b>\n"
        "• ระบบจะตรวจสอบและสั่งเตะสมาชิกทั่วไปทุกคนออกจาก Channel V.1 (และกลุ่มพูดคุยที่เชื่อมโยง)\n"
        "• ระบบจะทำการ <b>ปลดแบนทันที</b> (Soft-kick) เพื่อไม่ให้สมาชิกติด Blacklist ใน Telegram\n"
        "• บัญชีผู้ดูแลระบบ (Admin) และผู้สร้างห้อง (Creator) จะ <b>ไม่ถูกเตะ</b> (ระบบข้ามให้อัตโนมัติ)\n"
        "• สมาชิกใน Channel V.2 (BareLive V.2) จะ <b>ไม่ได้รับผลกระทบใดๆ</b> ทั้งสิ้น\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👉 <i>กรุณากดยืนยันด้านล่างหากต้องการดำเนินการทันที</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ ยืนยันเตะทุกคนออกจาก V.1", callback_data="admin:do_kick_all_v1"),
            ],
            [
                InlineKeyboardButton(text="🔙 ยกเลิก", callback_data="admin:cancel_kick_all_v1"),
            ]
        ]
    )
    return text, kb


@router.message(Command("kick_all_v1", "kick_v1_all", "kick_channel_v1", "kick_v1", "wipe_v1"))
async def handle_kick_all_v1_command(message: Message):
    """คำสั่งแอดมินสำหรับเตะสมาชิกทุกคนออกจาก Channel V.1: /kick_all_v1"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    text, kb = get_kick_all_v1_confirmation_text_and_kb()
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:confirm_kick_all_v1")
async def handle_admin_confirm_kick_all_v1_callback(callback: CallbackQuery):
    """Callback เปิดหน้าต่างยืนยันเตะทุกคนออกจาก V.1 จากปุ่มใน Admin Menu"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    text, kb = get_kick_all_v1_confirmation_text_and_kb()
    await callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:cancel_kick_all_v1")
async def handle_admin_cancel_kick_all_v1_callback(callback: CallbackQuery):
    """Callback ยกเลิกการเตะสมาชิกออกจาก V.1"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer("ยกเลิกคำสั่งเรียบร้อย")
    try:
        await callback.message.edit_text("❌ <b>ยกเลิกการเตะสมาชิกออกจาก Channel V.1 เรียบร้อยแล้ว</b>", parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data == "admin:do_kick_all_v1")
async def handle_admin_do_kick_all_v1_callback(callback: CallbackQuery, bot: Bot):
    """Callback เริ่มกระบวนการเตะทุกคนออกจาก Channel V.1"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else html.escape(callback.from_user.full_name)
    await callback.answer("🚀 กำลังเริ่มเตะสมาชิกออกจาก V.1...")

    status_msg = await callback.message.edit_text(
        "🔄 <b>กำลังเริ่มกระบวนการเตะทุกคนออกจาก Channel V.1...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
        "⏳ กรุณารอสักครู่ ระบบกำลังทยอยตรวจสอบสมาชิกและดำเนินการ...",
        parse_mode="HTML"
    )

    v1_channel_id = config.CHANNEL_ID
    linked_chat_id = None
    try:
        ch_info = await bot.get_chat(chat_id=v1_channel_id)
        linked_chat_id = getattr(ch_info, "linked_chat_id", None)
    except Exception:
        pass

    scanned_count = 0
    kicked_v1_count = 0
    kicked_linked_count = 0
    admin_skip = 0
    error_count = 0

    try:
        async with get_session() as session:
            stmt = select(User.telegram_id)
            user_ids = (await session.execute(stmt)).scalars().all()
            total_users = len(user_ids)

            for i, uid in enumerate(user_ids, 1):
                scanned_count += 1
                is_admin = False

                # 1. ตรวจสอบใน Channel V.1
                try:
                    chat_member = await bot.get_chat_member(chat_id=v1_channel_id, user_id=uid)
                    if chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        is_admin = True
                        admin_skip += 1
                    elif chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                        try:
                            await bot.ban_chat_member(chat_id=v1_channel_id, user_id=uid, revoke_messages=False)
                            await bot.unban_chat_member(chat_id=v1_channel_id, user_id=uid, only_if_banned=True)
                            kicked_v1_count += 1
                        except Exception as e:
                            logger.debug(f"Failed to kick user {uid} from V1 channel: {e}")
                            error_count += 1
                except TelegramBadRequest:
                    pass
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    pass

                # 2. ตรวจสอบใน Linked Discussion Group (ถ้ามี)
                if linked_chat_id and not is_admin:
                    try:
                        grp_member = await bot.get_chat_member(chat_id=linked_chat_id, user_id=uid)
                        if grp_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                            await bot.ban_chat_member(chat_id=linked_chat_id, user_id=uid, revoke_messages=False)
                            await bot.unban_chat_member(chat_id=linked_chat_id, user_id=uid, only_if_banned=True)
                            kicked_linked_count += 1
                    except Exception:
                        pass

                # 3. อัปเดตสถานะ Subscription ใน DB สำหรับคนที่อยู่ V1
                sub = await session.get(Subscription, uid)
                u = await session.get(User, uid)
                if u and (not getattr(u, "is_moved_to_secondary", False) and getattr(u, "assigned_channel", "PRIMARY") == "PRIMARY"):
                    if sub and sub.status in (SubStatus.ACTIVE.value, SubStatus.PENDING.value):
                        sub.status = SubStatus.KICKED.value
                        session.add(sub)

                await asyncio.sleep(0.08)

                if i % 25 == 0 or i == total_users:
                    try:
                        await status_msg.edit_text(
                            "🔄 <b>กำลังดำเนินการเตะสมาชิกออกจาก Channel V.1...</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"📊 <b>ความคืบหน้า:</b> {i}/{total_users} บัญชี\n"
                            f"👢 <b>เตะออกจาก V.1 สำเร็จ:</b> <b>{kicked_v1_count}</b> คน\n"
                            + (f"💬 <b>เตะออกจากกลุ่มสนทนา:</b> {kicked_linked_count} คน\n" if linked_chat_id else "")
                            + f"🛡️ <b>ข้ามแอดมิน:</b> {admin_skip} คน\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "⏳ <i>กรุณารอสักครู่จนกว่าระบบจะทำงานเสร็จสิ้น...</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            await session.commit()

        final_report = (
            "🎉 <b>ดำเนินการเตะสมาชิกทุกคนออกจาก Channel V.1 เสร็จสมบูรณ์!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📢 <b>Channel เป้าหมาย (V.1):</b> <b>{get_channel_label(v1_channel_id)}</b> (<code>{v1_channel_id}</code>)\n"
            f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
            f"🔍 <b>ตรวจสอบทั้งหมด:</b> <b>{scanned_count}</b> บัญชี\n"
            f"👢 <b>เตะออกจาก Channel V.1 สำเร็จ:</b> <b>{kicked_v1_count}</b> คน\n"
            + (f"💬 <b>เตะออกจากกลุ่มสนทนาที่เชื่อมโยง:</b> <b>{kicked_linked_count}</b> คน\n" if linked_chat_id else "")
            + f"🛡️ <b>ข้ามแอดมิน/ผู้สร้าง:</b> <b>{admin_skip}</b> คน\n"
            f"⚠️ <b>ข้อผิดพลาด:</b> <b>{error_count}</b> ครั้ง\n"
            f"📅 <b>เวลาที่เสร็จสิ้น:</b> <code>{format_thai_datetime(datetime.now(timezone.utc))} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <i>ระบบได้ทำการ Soft-kick และปลดแบนใน Telegram ให้ทุกคนเรียบร้อยแล้ว ไม่ติด Blacklist ครับ</i>"
        )
        try:
            await status_msg.edit_text(final_report, parse_mode="HTML")
        except Exception:
            await callback.message.answer(final_report, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in kick_all_v1: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"❌ <b>เกิดข้อผิดพลาดในการเตะสมาชิก V.1:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        except Exception:
            await callback.message.answer(f"❌ <b>เกิดข้อผิดพลาด:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


async def get_kick_all_chat_confirmation_text_and_kb(
    bot: Bot, explicit_target: Optional[Union[str, int]] = None
) -> tuple[str, InlineKeyboardMarkup, Optional[int]]:
    """สร้างข้อความและปุ่มยืนยันสำหรับคำสั่งเตะทุกคนออกจากห้องพูดคุย (Community/Discussion Chat)"""
    chat_id = await get_discussion_chat_id(bot, explicit_target)
    chat_title = "ห้องพูดคุยชุมชน"
    if chat_id:
        try:
            ch = await bot.get_chat(chat_id)
            if ch and ch.title:
                chat_title = ch.title
        except Exception:
            pass

    if not chat_id:
        text = (
            "⚠️ <b>ไม่พบข้อมูลห้องพูดคุยในระบบ</b>\n\n"
            "📝 <b>วิธีระบุห้องพูดคุย (เลือกวิธีใดวิธีหนึ่ง):</b>\n"
            "1. <b>ระบุในคำสั่งโดยตรง:</b>\n"
            "   • <code>/kick_all_chat [Chat ID หรือ @username]</code>\n"
            "   <i>ตัวอย่าง: <code>/kick_all_chat -1001234567890</code> หรือ <code>/kick_all_chat @barelivechat</code></i>\n\n"
            "2. <b>หรือตั้งค่าห้องถาวร:</b>\n"
            "   • <code>/set_chat_group [Chat ID หรือ @username]</code>\n\n"
            "3. <b>หรือดึงบอทเข้ากลุ่มพูดคุยแล้วตั้งสิทธิ์ Admin ให้บอทครับ 🙏</b>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 กลับสู่เมนูแอดมิน", callback_data="admin_menu:main")]
            ]
        )
        return text, kb, None

    text = (
        "⚠️ <b>ยืนยันการเตะสมาชิกทุกคนออกจากห้องพูดคุย (Chat Group)?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>กลุ่มเป้าหมาย:</b> <b>{html.escape(chat_title)}</b> (<code>{chat_id}</code>)\n\n"
        "🚨 <b>ผลกระทบของคำสั่งนี้:</b>\n"
        "• ระบบจะตรวจสอบและสั่งเตะสมาชิกทั่วไปทุกคนออกจากกลุ่มพูดคุย\n"
        "• ระบบจะทำการ <b>ปลดแบนทันที</b> (Soft-kick) เพื่อไม่ให้สมาชิกติด Blacklist ใน Telegram\n"
        "• บัญชีผู้ดูแลระบบ (Admin) และผู้สร้างกลุ่ม (Creator) จะ <b>ไม่ถูกเตะ</b>\n"
        "• สมาชิกใน Channel VIP จะไม่ถูกเตะออกจาก Channel (เตะเฉพาะในห้องแชทเท่านั้น)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👉 <i>กรุณากดยืนยันด้านล่างหากต้องการดำเนินการทันที</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ ยืนยันเตะทุกคนออกจากห้องแชท", callback_data=f"admin:do_kick_all_chat:{chat_id}"),
            ],
            [
                InlineKeyboardButton(text="🔙 ยกเลิก", callback_data="admin:cancel_kick_all_chat"),
            ]
        ]
    )
    return text, kb, chat_id


@router.message(Command("kick_all_chat", "kick_chat_all", "kick_discussion_all", "kick_group_all", "wipe_chat"))
async def handle_kick_all_chat_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับเตะสมาชิกทุกคนออกจากห้องพูดคุย: /kick_all_chat [Chat ID หรือ @username]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    args = (message.text or "").split(maxsplit=1)
    explicit_target = args[1].strip() if len(args) > 1 else None
    text, kb, _ = await get_kick_all_chat_confirmation_text_and_kb(bot, explicit_target)
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("set_chat_group", "set_discussion_group", "set_chat"))
async def handle_set_chat_group_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับบันทึก Chat ID ของห้องพูดคุย: /set_chat_group <Chat ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        saved_id = get_saved_chat_group_id()
        current_str = f"<code>{saved_id}</code>" if saved_id else "ยังไม่ได้ตั้งค่า"
        await message.answer(
            f"⚙️ <b>การตั้งค่าห้องพูดคุย (Community Chat Group)</b>\n\n"
            f"💬 <b>สถานะปัจจุบัน:</b> {current_str}\n\n"
            "📝 <b>วิธีตั้งค่าใหม่:</b>\n"
            "• <code>/set_chat_group [Chat ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/set_chat_group -1001234567890</code>\n"
            "• <code>/set_chat_group @barelivechat</code>",
            parse_mode="HTML"
        )
        return

    target_raw = args[1].strip()
    chat_id = await resolve_chat_group(bot, target_raw)
    if not chat_id:
        await message.answer(
            f"❌ <b>ไม่สามารถค้นหาห้อง <code>{html.escape(target_raw)}</code> ได้</b>\n\n"
            "💡 <i>คำแนะนำ: กรุณาตรวจสอบว่าพิมพ์ @username หรือ Chat ID ถูกต้อง และดึงบอทเข้ากลุ่มแล้วตั้งสิทธิ์ Admin เรียบร้อยแล้วครับ</i>",
            parse_mode="HTML"
        )
        return

    save_chat_group_id(chat_id)
    chat_title = "ห้องพูดคุย"
    try:
        ch = await bot.get_chat(chat_id)
        if ch and ch.title:
            chat_title = ch.title
    except Exception:
        pass

    await message.answer(
        f"✅ <b>บันทึกห้องพูดคุยเรียบร้อยแล้ว!</b>\n"
        f"💬 <b>ชื่อกลุ่ม:</b> <b>{html.escape(chat_title)}</b>\n"
        f"🔢 <b>Chat ID:</b> <code>{chat_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>สามารถใช้คำสั่ง <code>/kick_all_chat</code> หรือ <code>/kick_chat</code> ได้ทันทีครับ</i>",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin:confirm_kick_all_chat")
async def handle_admin_confirm_kick_all_chat_callback(callback: CallbackQuery, bot: Bot):
    """Callback เปิดหน้าต่างยืนยันเตะทุกคนออกจากห้องแชท จากปุ่มใน Admin Menu"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    text, kb, _ = await get_kick_all_chat_confirmation_text_and_kb(bot)
    await callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:cancel_kick_all_chat")
async def handle_admin_cancel_kick_all_chat_callback(callback: CallbackQuery):
    """Callback ยกเลิกการเตะสมาชิกออกจากห้องแชท"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer("ยกเลิกคำสั่งเรียบร้อย")
    try:
        await callback.message.edit_text("❌ <b>ยกเลิกการเตะสมาชิกออกจากห้องพูดคุยเรียบร้อยแล้ว</b>", parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:do_kick_all_chat"))
async def handle_admin_do_kick_all_chat_callback(callback: CallbackQuery, bot: Bot):
    """Callback เริ่มกระบวนการเตะทุกคนออกจากห้องพูดคุย"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    target_cid = None
    if ":" in callback.data:
        parts = callback.data.split(":")
        if len(parts) >= 3 and parts[2].lstrip("-").isdigit():
            target_cid = int(parts[2])

    chat_id = target_cid or await get_discussion_chat_id(bot)
    if not chat_id:
        await callback.answer("❌ ไม่พบข้อมูลห้องพูดคุย", show_alert=True)
        return

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else html.escape(callback.from_user.full_name)
    await callback.answer("🚀 กำลังเริ่มเตะสมาชิกออกจากห้องพูดคุย...")

    chat_title = "ห้องพูดคุยชุมชน"
    try:
        ch = await bot.get_chat(chat_id)
        if ch and ch.title:
            chat_title = ch.title
    except Exception:
        pass

    # ตรวจสอบสิทธิ์ Ban Users ของบอทก่อนดำเนินการ
    try:
        bot_user = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_user.id)
        can_restrict = getattr(bot_member, "can_restrict_members", False) or bot_member.status == ChatMemberStatus.CREATOR
        if not can_restrict:
            await callback.message.edit_text(
                f"❌ <b>บอทไม่มีสิทธิ์ Ban Users ในกลุ่ม {html.escape(chat_title)}!</b>\n\n"
                "กรุณาไปที่แอป Telegram ➔ แก้ไขกลุ่ม ➔ ผู้ดูแลระบบ (Administrators) ➔ เลือกบอท ➔ <b>เปิดสิทธิ์ 'Ban Users / แบนผู้ใช้'</b> ให้บอทก่อนครับ 🙏",
                parse_mode="HTML"
            )
            return
    except Exception as e:
        logger.warning(f"Could not verify bot rights in group {chat_id}: {e}")

    initial_member_count = 0
    try:
        initial_member_count = await bot.get_chat_member_count(chat_id)
    except Exception:
        pass

    status_msg = await callback.message.edit_text(
        "🔄 <b>กำลังเริ่มกระบวนการเตะทุกคนออกจากห้องพูดคุย...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 <b>ห้องเป้าหมาย:</b> <b>{html.escape(chat_title)}</b> (<code>{chat_id}</code>)\n"
        f"👥 <b>สมาชิกปัจจุบันในกลุ่ม:</b> <b>{initial_member_count:,}</b> คน\n"
        f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
        "⏳ กรุณารอสักครู่ ระบบกำลังทยอยตรวจสอบสมาชิกและดำเนินการ...",
        parse_mode="HTML"
    )

    scanned_count = 0
    kicked_count = 0
    admin_skip = 0
    error_count = 0

    try:
        async with get_session() as session:
            stmt = select(User.telegram_id)
            user_ids = (await session.execute(stmt)).scalars().all()
            total_users = len(user_ids)

            for i, uid in enumerate(user_ids, 1):
                scanned_count += 1
                try:
                    chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
                    if chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        admin_skip += 1
                    elif chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                        try:
                            await bot.ban_chat_member(chat_id=chat_id, user_id=uid, revoke_messages=False)
                            await bot.unban_chat_member(chat_id=chat_id, user_id=uid, only_if_banned=True)
                            kicked_count += 1
                        except Exception as e:
                            logger.debug(f"Failed to kick user {uid} from discussion chat: {e}")
                            error_count += 1
                except TelegramBadRequest:
                    pass
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    pass

                await asyncio.sleep(0.08)

                if i % 25 == 0 or i == total_users:
                    try:
                        await status_msg.edit_text(
                            "🔄 <b>กำลังดำเนินการเตะสมาชิกออกจากห้องพูดคุย...</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"💬 <b>ห้องเป้าหมาย:</b> <b>{html.escape(chat_title)}</b> (<code>{chat_id}</code>)\n"
                            f"📊 <b>ความคืบหน้า:</b> {i}/{total_users} บัญชี\n"
                            f"👢 <b>เตะออกจากห้องแชทสำเร็จ:</b> <b>{kicked_count}</b> คน\n"
                            f"🛡️ <b>ข้ามแอดมิน:</b> {admin_skip} คน\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "⏳ <i>กรุณารอสักครู่จนกว่าระบบจะทำงานเสร็จสิ้น...</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        final_member_count = 0
        try:
            final_member_count = await bot.get_chat_member_count(chat_id)
        except Exception:
            pass

        final_report = (
            "🎉 <b>ดำเนินการเตะสมาชิกทุกคนออกจากห้องพูดคุยเสร็จสมบูรณ์!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 <b>ห้องเป้าหมาย:</b> <b>{html.escape(chat_title)}</b> (<code>{chat_id}</code>)\n"
            f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
            f"👥 <b>สมาชิกก่อนดำเนินการ:</b> <b>{initial_member_count:,}</b> คน\n"
            f"🔍 <b>ตรวจสอบบัญชีในฐานข้อมูล:</b> <b>{scanned_count:,}</b> บัญชี\n"
            f"👢 <b>เตะออกจากห้องแชทสำเร็จ:</b> <b>{kicked_count:,}</b> คน\n"
            f"🛡️ <b>ข้ามแอดมิน/ผู้สร้าง:</b> <b>{admin_skip}</b> คน\n"
            f"👥 <b>สมาชิกคงเหลือในกลุ่ม:</b> <b>{final_member_count:,}</b> คน (รวมแอดมิน)\n"
            f"⚠️ <b>ข้อผิดพลาด:</b> <b>{error_count}</b> ครั้ง\n"
            f"📅 <b>เวลาที่เสร็จสิ้น:</b> <code>{format_thai_datetime(datetime.now(timezone.utc))} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )
        if final_member_count > (admin_skip + 1):
            final_report += (
                "💡 <b>คำแนะนำเพิ่มเติม:</b>\n"
                f"เนื่องจากยังมีสมาชิกคงเหลือ {final_member_count - admin_skip - 1} คน (เป็นผู้ที่กดเข้ากลุ่มเองโดยตรง ไม่เคยเริ่มใช้งานบอท จึงไม่มีบันทึก User ID ในฐานข้อมูล)\n"
                "👉 <b>วิธีจัดการ:</b>\n"
                "1. ใช้คำสั่ง <code>/kick_chat [User ID หรือ @username]</code> เพื่อเตะรายคน\n"
                "2. พิมพ์ <code>/chat_info</code> เพื่อดูสิทธิ์บอทและรายชื่อแอดมินในกลุ่ม\n"
                "3. หรือในแอป Telegram เข้าไปที่ Edit Group ➔ Permissions ➔ ปิด Send Messages หรือ Revoke ลิงก์กลุ่มเดิมครับ"
            )
        else:
            final_report += "✨ <i>สมาชิกทั่วไปถูกเตะออกจากกลุ่มพูดคุยเรียบร้อยแล้ว ไม่ติด Blacklist ครับ</i>"

        try:
            await status_msg.edit_text(final_report, parse_mode="HTML")
        except Exception:
            await callback.message.answer(final_report, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in kick_all_chat: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"❌ <b>เกิดข้อผิดพลาดในการเตะสมาชิกห้องแชท:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        except Exception:
            await callback.message.answer(f"❌ <b>เกิดข้อผิดพลาด:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@router.message(Command("chat_info", "audit_chat", "group_info", "check_chat"))
async def handle_chat_info_command(message: Message, bot: Bot):
    """ตรวจสอบสถานะ สิทธิ์บอท และจำนวนสมาชิกในห้องพูดคุย: /chat_info [Chat ID หรือ @username]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    parts = (message.text or "").split()
    explicit_group = parts[1].strip() if len(parts) >= 2 else None
    chat_id = await get_discussion_chat_id(bot, explicit_group)
    if not chat_id:
        await message.answer("⚠️ ไม่พบข้อมูลห้องพูดคุยในระบบ กรุณาใช้คำสั่ง <code>/set_chat_group [Chat ID หรือ @username]</code> ครับ", parse_mode="HTML")
        return

    try:
        ch = await bot.get_chat(chat_id)
        chat_title = ch.title or "ห้องพูดคุย"
        member_count = await bot.get_chat_member_count(chat_id)

        bot_user = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_user.id)

        is_admin = bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        can_restrict = getattr(bot_member, "can_restrict_members", False) or bot_member.status == ChatMemberStatus.CREATOR
        can_delete = getattr(bot_member, "can_delete_messages", False) or bot_member.status == ChatMemberStatus.CREATOR

        admins = await bot.get_chat_administrators(chat_id)
        admin_names = []
        for adm in admins:
            if not adm.user.is_bot:
                uname = f"@{adm.user.username}" if adm.user.username else html.escape(adm.user.full_name)
                admin_names.append(uname)

        info_text = (
            "📊 <b>ข้อมูลห้องพูดคุย (Chat Group Info)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 <b>ชื่อกลุ่ม:</b> <b>{html.escape(chat_title)}</b>\n"
            f"🔢 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"👥 <b>จำนวนสมาชิกทั้งหมด:</b> <b>{member_count:,}</b> คน\n"
            f"👑 <b>จำนวนแอดมิน (คน):</b> {len(admin_names)} คน ({', '.join(admin_names) if admin_names else 'ไม่มี'})\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>สถานะและสิทธิ์ของบอทในกลุ่ม:</b>\n"
            f"• สถานะ: {'🟢 เป็น Admin' if is_admin else '🔴 ไม่ใช่ Admin (ต้องตั้งบอทเป็น Admin)'}\n"
            f"• สิทธิ์แบน/เตะสมาชิก (Ban Users): {'✅ มีสิทธิ์' if can_restrict else '❌ ไม่มีสิทธิ์ (จำเป็นต้องเปิด!)'}\n"
            f"• สิทธิ์ลบข้อความ (Delete Messages): {'✅ มีสิทธิ์' if can_delete else '❌ ไม่มีสิทธิ์'}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )
        if not can_restrict:
            info_text += "⚠️ <b>คำเตือน:</b> บอทยังไม่มีสิทธิ์ <b>Ban Users</b> ในกลุ่มนี้ ทำให้บอทไม่สามารถเตะสมาชิกได้ กรุณาไปที่ Edit Group ➔ Administrators ➔ บอท ➔ เปิดสิทธิ์ Ban Users ครับ"
        else:
            info_text += "✨ <i>บอทมีสิทธิ์พร้อมสำหรับการเตะและดูแลกลุ่มเรียบร้อยครับ</i>"

        await message.answer(info_text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ <b>ไม่สามารถดึงข้อมูลกลุ่มได้:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@router.message(Command("kick_chat", "kick_group"))
async def handle_admin_kick_chat_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับเตะผู้ใช้รายคนออกจากห้องพูดคุย: /kick_chat <User ID หรือ @username> [Chat ID หรือ @group]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/kick_chat [User ID หรือ @username] [Chat ID ของกลุ่ม (ถ้ามี)]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/kick_chat 5125375696</code>\n"
            "• <code>/kick_chat @username</code>\n"
            "• <code>/kick_chat 5125375696 -1001234567890</code>",
            parse_mode="HTML"
        )
        return

    explicit_group = parts[2].strip() if len(parts) >= 3 else None
    chat_id = await get_discussion_chat_id(bot, explicit_group)
    if not chat_id:
        await message.answer(
            "⚠️ <b>ไม่พบข้อมูลห้องพูดคุยในระบบ</b>\n"
            "สามารถระบุ Chat ID ของกลุ่มต่อท้ายได้ เช่น <code>/kick_chat 5125375696 -1001234567890</code> ครับ",
            parse_mode="HTML"
        )
        return

    query = parts[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

    if not user:
        if query.isdigit():
            target_uid = int(query)
            user_name = f"User {target_uid}"
        else:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return
    else:
        target_uid = user.telegram_id
        user_name = html.escape(user.full_name or f"User {target_uid}")

    try:
        cm = await bot.get_chat_member(chat_id=chat_id, user_id=target_uid)
        if cm.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.answer(f"⚠️ ไม่สามารถเตะ {user_name} ได้ เนื่องจากเป็น Administrator / Creator ของกลุ่ม", parse_mode="HTML")
            return
    except Exception:
        pass

    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=target_uid, revoke_messages=False)
        await bot.unban_chat_member(chat_id=chat_id, user_id=target_uid, only_if_banned=True)
        await message.answer(
            f"✅ <b>เตะผู้ใช้ออกจากห้องพูดคุยสำเร็จ!</b>\n"
            f"👤 <b>ผู้ใช้:</b> {user_name} (<code>{target_uid}</code>)\n"
            f"💬 <b>ห้องพูดคุย ID:</b> <code>{chat_id}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <i>ทำการ Soft-kick และปลดแบนใน Telegram ให้เรียบร้อยแล้ว</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>เตะไม่สำเร็จ:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


def get_clean_non_v2_dms_confirmation_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความและปุ่มยืนยันสำหรับคำสั่งลบข้อความ DM ทั้งหมดของทุกคนที่ไม่ใช่สมาชิก V.2"""
    text = (
        "⚠️ <b>ยืนยันการลบข้อความของบอทใน DM ของทุกคนที่ไม่ใช่สมาชิก V.2?</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>กลุ่มเป้าหมายที่จะถูกลบข้อความ:</b>\n"
        "• สมาชิกห้องเดิม V.1 ทั้งหมด\n"
        "• ผู้ใช้ที่ยังไม่ได้สมัครสมาชิก / ผู้ใช้ที่เคยกด /start หรือทักแชทเข้ามาทั้งหมด\n\n"
        "✨ <b>กลุ่มที่จะไม่ได้รับผลกระทบ (ปลอดภัย 100%):</b>\n"
        "• <b>สมาชิกห้อง BareLive V.2 (สมาชิกปัจจุบัน)</b> จะไม่ถูกลบข้อความใดๆ\n"
        "• <b>กลุ่ม Admin</b> จะไม่ได้รับผลกระทบ\n\n"
        "🚨 <b>ผลลัพธ์:</b>\n"
        "• บอทจะทำการทยอยลบข้อความทุกอย่างที่บอทเคยส่งไป (เมนู, ราคา, QR Code, ลิงก์เชิญ, ข้อความต้อนรับ ฯลฯ)\n"
        "• ผู้ใช้กลุ่มเป้าหมายจะมองไม่เห็นข้อความของบอทอีกต่อไป\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👉 <i>กรุณากดยืนยันด้านล่างหากต้องการเริ่มการกวาดล้างข้อความทันที</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧹 ยืนยันลบข้อความ DM ทุกคนที่ไม่ใช่ V.2", callback_data="admin:do_clean_non_v2_dms"),
            ],
            [
                InlineKeyboardButton(text="🔙 ยกเลิก", callback_data="admin:cancel_clean_non_v2_dms"),
            ]
        ]
    )
    return text, kb


@router.message(Command("clean_non_v2_chat", "clean_all_v1_dms", "clean_all_dms", "wipe_all_dms", "clean_dms"))
async def handle_clean_non_v2_chat_command(message: Message):
    """คำสั่งแอดมินสำหรับลบข้อความ DM ของผู้ใช้ที่ไม่ใช่สมาชิก V.2 ทุกคน: /clean_non_v2_chat"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    text, kb = get_clean_non_v2_dms_confirmation_text_and_kb()
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:confirm_clean_non_v2_dms")
async def handle_admin_confirm_clean_non_v2_dms_callback(callback: CallbackQuery):
    """Callback เปิดหน้าต่างยืนยันลบข้อความ DM จากปุ่มใน Admin Menu"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    text, kb = get_clean_non_v2_dms_confirmation_text_and_kb()
    await callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:cancel_clean_non_v2_dms")
async def handle_admin_cancel_clean_non_v2_dms_callback(callback: CallbackQuery):
    """Callback ยกเลิกการลบข้อความ DM"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer("ยกเลิกคำสั่งเรียบร้อย")
    try:
        await callback.message.edit_text("❌ <b>ยกเลิกการลบข้อความ DM เรียบร้อยแล้ว</b>", parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data == "admin:do_clean_non_v2_dms")
async def handle_admin_do_clean_non_v2_dms_callback(callback: CallbackQuery, bot: Bot):
    """Callback เริ่มกระบวนการกวาดล้างข้อความ DM ของทุกคนที่ไม่ใช่สมาชิก V.2"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else html.escape(callback.from_user.full_name)
    await callback.answer("🚀 กำลังเริ่มกระบวนการลบข้อความ DM...")

    status_msg = await callback.message.edit_text(
        "🔄 <b>กำลังเริ่มกระบวนการลบข้อความ DM ของทุกคนที่ไม่ใช่สมาชิก V.2...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
        "⏳ กรุณารอสักครู่ ระบบกำลังค้นหารายชื่อผู้ใช้และทยอยดำเนินการ...",
        parse_mode="HTML"
    )

    scanned_count = 0
    cleaned_user_count = 0
    skipped_v2_count = 0
    blocked_count = 0
    total_msgs_deleted = 0
    error_count = 0

    try:
        async with get_session() as session:
            stmt = select(User)
            all_users = (await session.execute(stmt)).scalars().all()
            total_users = len(all_users)

            for i, user in enumerate(all_users, 1):
                scanned_count += 1
                uid = user.telegram_id

                # ตรวจสอบว่าเป็นสมาชิก V.2 หรือไม่
                if is_user_v2_member(user):
                    skipped_v2_count += 1
                    continue

                # ดำเนินการลบข้อความใน DM
                try:
                    del_count, success, detail = await clean_user_chat_messages(bot, uid)
                    if success:
                        cleaned_user_count += 1
                        total_msgs_deleted += del_count
                    elif detail == "BLOCKED_BOT":
                        blocked_count += 1
                    else:
                        error_count += 1
                except Exception as e:
                    logger.debug(f"Error cleaning DM for user {uid}: {e}")
                    error_count += 1

                await asyncio.sleep(0.06)

                if i % 15 == 0 or i == total_users:
                    try:
                        await status_msg.edit_text(
                            "🔄 <b>กำลังดำเนินการลบข้อความ DM ของทุกคนที่ไม่ใช่ V.2...</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"📊 <b>ความคืบหน้า:</b> {i}/{total_users} บัญชี\n"
                            f"🧹 <b>ล้างแชทสำเร็จ:</b> <b>{cleaned_user_count}</b> คน\n"
                            f"🗑️ <b>ยอดข้อความที่ลบแล้ว:</b> <b>{total_msgs_deleted:,}</b> ข้อความ\n"
                            f"🛡️ <b>ข้ามสมาชิก V.2 (ปลอดภัย):</b> {skipped_v2_count} คน\n"
                            f"🚫 <b>บล็อกบอท/ติดต่อไม่ได้:</b> {blocked_count} คน\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "⏳ <i>กรุณารอสักครู่จนกว่าระบบจะทำงานเสร็จสิ้น...</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        final_report = (
            "🎉 <b>ดำเนินการกวาดล้างข้อความ DM ของทุกคนที่ไม่ใช่สมาชิก V.2 เสร็จสมบูรณ์!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>ผู้สั่งการ:</b> {admin_name}\n"
            f"🔍 <b>ตรวจสอบผู้ใช้ทั้งหมด:</b> <b>{scanned_count}</b> บัญชี\n"
            f"🧹 <b>ล้างข้อความสำเร็จ:</b> <b>{cleaned_user_count}</b> คน\n"
            f"🗑️ <b>จำนวนข้อความที่ลบทั้งหมด:</b> <b>{total_msgs_deleted:,}</b> ข้อความ\n"
            f"🛡️ <b>ข้ามสมาชิก V.2 (คงข้อความไว้):</b> <b>{skipped_v2_count}</b> คน\n"
            f"🚫 <b>ผู้ใช้บล็อกบอท:</b> <b>{blocked_count}</b> คน\n"
            f"⚠️ <b>ข้อผิดพลาดอื่นๆ:</b> <b>{error_count}</b> ครั้ง\n"
            f"📅 <b>เวลาที่เสร็จสิ้น:</b> <code>{format_thai_datetime(datetime.now(timezone.utc))} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <i>ข้อความ เมนู และปุ่มกดทั้งหมดของบอทในแชทของผู้ใช้ที่ไม่ใช่ V.2 ถูกลบออกเรียบร้อยแล้วครับ</i>"
        )
        try:
            await status_msg.edit_text(final_report, parse_mode="HTML")
        except Exception:
            await callback.message.answer(final_report, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error in clean_non_v2_dms: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"❌ <b>เกิดข้อผิดพลาดในการลบข้อความ DM:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        except Exception:
            await callback.message.answer(f"❌ <b>เกิดข้อผิดพลาด:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")


@router.message(Command("clean_chat", "clean_user", "wipe_user_chat"))
async def handle_admin_clean_chat_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับลบข้อความของบอทใน DM ของผู้ใช้รายคน: /clean_chat <User ID หรือ @username>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/clean_chat [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/clean_chat 5125375696</code>\n"
            "• <code>/clean_chat @username</code>",
            parse_mode="HTML"
        )
        return

    query = args[1].strip().lstrip("@")
    async with get_session() as session:
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
        user = (await session.execute(user_stmt)).scalar_one_or_none()

    if not user:
        if query.isdigit():
            target_uid = int(query)
            user_name = f"User {target_uid}"
        else:
            await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
            return
    else:
        target_uid = user.telegram_id
        user_name = html.escape(user.full_name or f"User {target_uid}")

    del_count, success, detail = await clean_user_chat_messages(bot, target_uid)
    if success:
        await message.answer(
            f"✅ <b>ลบข้อความของบอทใน DM สำเร็จ!</b>\n"
            f"👤 <b>ผู้ใช้:</b> {user_name} (<code>{target_uid}</code>)\n"
            f"🗑️ <b>ลบข้อความทั้งหมด:</b> <b>{del_count:,}</b> ข้อความ\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <i>ข้อความของบอทถูกลบออกจากหน้าจอแชทของผู้ใช้เรียบร้อยแล้ว</i>",
            parse_mode="HTML"
        )
    else:
        if detail == "BLOCKED_BOT":
            await message.answer(f"⚠️ ไม่สามารถลบได้เนื่องจากผู้ใช้ <code>{target_uid}</code> บล็อกบอทไว้", parse_mode="HTML")
        else:
            await message.answer(f"❌ <b>ลบไม่สำเร็จ:</b> <code>{html.escape(detail)}</code>", parse_mode="HTML")


def get_payment_methods_status_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความและปุ่มจัดการช่องทางการชำระเงิน"""
    pp_active = is_promptpay_active()
    tmn_active = is_truemoney_active()

    text = (
        "💳 <b>ตั้งค่าช่องทางการชำระเงิน (Payment Methods)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📲 <b>สแกน QR Code (PromptPay):</b> {'🟢 เปิดใช้งาน (Active)' if pp_active else '🔴 ปิดใช้งาน (Disabled)'}\n"
        f"🧧 <b>ซองของขวัญ TrueMoney:</b> {'🟢 เปิดใช้งาน (Active)' if tmn_active else '🔴 ปิดใช้งาน (Disabled)'}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>เมื่อปิด QR Code บอทจะไม่แสดงตัวเลือกสแกน QR Code ให้ผู้ใช้ และจะรับชำระเฉพาะซองของขวัญ TrueMoney เท่านั้น</i>\n"
        "👉 <i>แตะปุ่มด้านล่างเพื่อเปิดหรือปิดช่องทางที่ต้องการได้ทันทีครับ</i>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 ปิดชำระผ่าน QR Code" if pp_active else "🟢 เปิดชำระผ่าน QR Code",
                    callback_data="pay_method_action:promptpay_off" if pp_active else "pay_method_action:promptpay_on"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔴 ปิดซอง TrueMoney" if tmn_active else "🟢 เปิดซอง TrueMoney",
                    callback_data="pay_method_action:truemoney_off" if tmn_active else "pay_method_action:truemoney_on"
                ),
            ],
            [
                InlineKeyboardButton(text="🔙 กลับเมนูแอดมิน", callback_data="admin_menu:back_to_main"),
            ]
        ]
    )
    return text, kb


@router.message(Command("promptpay", "qr", "qr_payment", "promptpay_setting"))
async def handle_promptpay_command(message: Message):
    """คำสั่งเปิด/ปิด/ดูสถานะการชำระเงินผ่าน QR Code: /promptpay [on/off]"""
    if not is_admin_chat(message.chat.id):
        return

    parts = (message.text or "").split()
    if len(parts) >= 2:
        subcmd = parts[1].strip().lower()
        if subcmd in ("on", "enable", "start", "open"):
            update_promptpay_setting(is_active=True)
            text, kb = get_payment_methods_status_text_and_kb()
            await message.answer(f"✅ <b>เปิดใช้งานการชำระเงินผ่าน QR Code เรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
            return
        elif subcmd in ("off", "disable", "stop", "close"):
            update_promptpay_setting(is_active=False)
            text, kb = get_payment_methods_status_text_and_kb()
            await message.answer(f"❌ <b>ปิดใช้งานการชำระเงินผ่าน QR Code เรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
            return

    text, kb = get_payment_methods_status_text_and_kb()
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("promptpay_on", "qr_on"))
async def handle_promptpay_on_command(message: Message):
    """คำสั่งเปิดใช้งานการชำระเงินผ่าน QR Code: /promptpay_on"""
    if not is_admin_chat(message.chat.id):
        return
    update_promptpay_setting(is_active=True)
    text, kb = get_payment_methods_status_text_and_kb()
    await message.answer(f"✅ <b>เปิดใช้งานการชำระเงินผ่าน QR Code เรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("promptpay_off", "qr_off"))
async def handle_promptpay_off_command(message: Message):
    """คำสั่งปิดใช้งานการชำระเงินผ่าน QR Code: /promptpay_off"""
    if not is_admin_chat(message.chat.id):
        return
    update_promptpay_setting(is_active=False)
    text, kb = get_payment_methods_status_text_and_kb()
    await message.answer(f"❌ <b>ปิดใช้งานการชำระเงินผ่าน QR Code เรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("payment_methods", "payment_settings", "pay_setting"))
async def handle_payment_methods_command(message: Message):
    """คำสั่งดูและจัดการช่องทางการชำระเงินทั้งหมด: /payment_methods"""
    if not is_admin_chat(message.chat.id):
        return
    text, kb = get_payment_methods_status_text_and_kb()
    await message.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:payment_methods")
async def handle_admin_menu_payment_methods_callback(callback: CallbackQuery):
    """จัดการปุ่มลัด [💳 จัดการช่องทางชำระเงิน] ในเมนู Admin"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    text, kb = get_payment_methods_status_text_and_kb()
    await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pay_method_action:"))
async def handle_pay_method_action_callback(callback: CallbackQuery):
    """จัดการ Quick Actions ปุ่มลัดเปิด/ปิด ช่องทางชำระเงิน"""
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "promptpay_on":
        update_promptpay_setting(is_active=True)
        await callback.answer("✅ เปิดใช้งานชำระเงินผ่าน QR Code แล้ว")
    elif action == "promptpay_off":
        update_promptpay_setting(is_active=False)
        await callback.answer("❌ ปิดใช้งานชำระเงินผ่าน QR Code แล้ว")
    elif action == "truemoney_on":
        update_truemoney_setting(is_active=True)
        await callback.answer("✅ เปิดใช้งานชำระเงินผ่าน TrueMoney แล้ว")
    elif action == "truemoney_off":
        update_truemoney_setting(is_active=False)
        await callback.answer("❌ ปิดใช้งานชำระเงินผ่าน TrueMoney แล้ว")

    text, kb = get_payment_methods_status_text_and_kb()
    try:
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass





