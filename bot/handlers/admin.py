import logging
import html
import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from bot.config import get_settings
from bot.models.schema import User, PaymentSlip, Subscription, ChatMessage, SlipStatus, SubStatus, PlanType, PLAN_DETAILS, get_dynamic_plan_info
from bot.services.database import get_session, get_or_create_user
from bot.services.scheduler import build_active_members_report, sync_pending_members
from bot.services.chat_logger import log_chat_message
from bot.handlers.user_menu import get_main_menu_keyboard
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime, ensure_utc

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="admin")



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


@router.message(Command("version"))
async def handle_admin_version_command(message: Message):
    """(Admin) เช็คเวอร์ชันปัจจุบันของบอท"""
    import os
    try:
        commit_hash = os.environ.get("BOT_APP_VERSION", "Unknown")
        commit_date = os.environ.get("BOT_APP_DATE", "Unknown")
            
        text = (
            f"🤖 <b>Bot Version Info</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 <b>Commit:</b> <code>{commit_hash}</code>\n"
            f"📅 <b>Date:</b> {commit_date}\n\n"
            f"<i>อัปเดตล่าสุด: ฟีเจอร์ Swipe to Reply & เมนู Inline Users</i>"
        )
    except Exception as e:
        text = f"🤖 <b>Bot Version Info</b>\nไม่สามารถอ่านเวอร์ชันได้: <code>{str(e)}</code>"
        
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
            requested_plan = getattr(slip, "plan_type", None) or PlanType.VIP_30D.value

            plan_info = get_dynamic_plan_info(requested_plan)
            additional_days = plan_info["days"]

            # 2. ตรวจสอบว่าผู้ใช้อยู่ใน Channel และมี ACTIVE Subscription หรือไม่
            active_stmt = (
                select(Subscription)
                .where(
                    Subscription.user_id == target_user_id,
                    Subscription.status == SubStatus.ACTIVE.value,
                    Subscription.expires_at > now,
                )
                .order_by(Subscription.id.desc())
            )
            active_sub = (await session.execute(active_stmt)).scalar_one_or_none()

            # ตรวจสอบสถานะจริงใน Channel
            is_in_channel = False
            try:
                chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=target_user_id)
                is_in_channel = chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
            except Exception:
                is_in_channel = False

            if active_sub and is_in_channel:
                # === กรณีต่อเวลาสะสม (Day Stacking) ===
                current_exp = ensure_utc(active_sub.expires_at)
                base_time = max(current_exp, now) if current_exp else now
                new_expires_at = base_time + timedelta(days=additional_days)
                active_sub.expires_at = new_expires_at
                active_sub.plan_type = requested_plan
                session.add(active_sub)
                is_stack_extension = True
                logger.info(
                    f"Approve slip #{slip_id}: Extended active sub #{active_sub.id} for User {target_user_id} by +{additional_days} days. "
                    f"New expires_at: {new_expires_at}"
                )
            else:
                # === กรณีต้องส่งลิงก์เชิญใหม่ ===
                subscription = Subscription(
                    user_id=target_user_id,
                    plan_type=requested_plan,
                    status=SubStatus.PENDING.value,
                )
                session.add(subscription)
                await session.flush()

        plan_info = get_dynamic_plan_info(requested_plan)
        plan_badge = plan_info["badge"]
        plan_desc = f"{plan_info['days']} วัน"
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
                "💡 <i>คุณสามารถรับชมเนื้อหาใน Channel VIP ได้ต่อเนื่องทันทีโดยไม่ต้องกดเข้าห้องใหม่ครับ! 🚀</i>"
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

        else:
            # สร้างลิงก์เชิญแบบ 1 ครั้งให้ผู้ใช้
            try:
                invite_link_obj = await bot.create_chat_invite_link(
                    chat_id=config.CHANNEL_ID,
                    member_limit=1,
                    expire_date=now + timedelta(days=7),
                    name=f"VIP-{target_user_id}",
                )
                invite_url = invite_link_obj.invite_link
            except Exception as e:
                logger.error(f"Failed to generate invite link for approved user {target_user_id}: {e}", exc_info=True)
                await callback.answer(
                    "⚠️ ไม่สามารถสร้างลิงก์เชิญได้! กรุณาตรวจสอบว่าบอทมีสิทธิ์สร้างลิงก์ใน Channel",
                    show_alert=True,
                )
                return

            user_message = (
                "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>แพ็กเกจ:</b> <b>{plan_badge} ({plan_desc})</b> พร้อมใช้งานแล้วครับ\n\n"
                f"🔗 <b>ลิงก์เชิญเข้า Channel ส่วนตัว (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
                "📌 <b>ข้อควรทราบ:</b>\n"
                "• ลิงก์นี้สามารถใช้งานได้เพียง 1 ครั้งเท่านั้น\n"
                f"• <b>ระยะเวลาสมาชิก {plan_desc} จะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ! 🚀"
            )
            join_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🚀 เข้าร่วม Channel VIP ตอนนี้", url=invite_url)]
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
        current_caption = callback.message.caption or "" if callback.message else ""

        action_label = f"✅ <b>อนุมัติแล้ว (ต่อเวลาสะสม +{plan_desc})</b>" if is_stack_extension else f"✅ <b>อนุมัติแล้ว (ออกลิงก์เชิญใหม่)</b>"
        expiry_note = f"\n⏳ หมดอายุใหม่: <code>{format_thai_datetime(new_expires_at)} น.</code>" if is_stack_extension and new_expires_at else ""

        updated_caption = (
            f"{current_caption}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{action_label} โดย {admin_name} (<code>{admin_user.id}</code>){expiry_note}\n"
            f"📅 <code>{timestamp_thai} น.</code>\n"
            f"📨 ส่ง DM: {'สำเร็จ ✅' if user_dm_sent else 'ไม่สำเร็จ (ผู้ใช้บล็อกบอท) ⚠️'}"
        )

        try:
            if callback.message:
                await callback.message.edit_caption(
                    caption=updated_caption,
                    reply_markup=None,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to update admin message caption: {e}")

        await callback.answer(f"✅ อนุมัติสลิปเรียบร้อย ({'ต่อเวลาสะสม' if is_stack_extension else 'ส่งลิงก์เชิญ'})")
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
                "❌ <b>แจ้งเตือนผลการตรวจสอบสลิปโอนเงิน</b>\n\n"
                "ทีมงานไม่สามารถยืนยันสลิปการโอนเงินสำหรับสมาชิก VIP ของคุณได้ครับ\n\n"
                "สาเหตุที่เป็นไปได้:\n"
                f"• ยอดเงินที่โอนไม่ตรงกับค่าบริการ ({plan_price_str})\n"
                "• รูปภาพสลิปไม่ชัดเจนหรือไม่สามารถอ่านข้อมูลได้\n"
                "• วันที่หรือเวลาในสลิปไม่ถูกต้อง\n\n"
                "👉 กรุณาพิมพ์ /start เพื่อส่งสลิปใหม่อีกครั้ง หรือติดต่อแอดมินหากมีข้อสงสัยครับ"
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

        current_caption = callback.message.caption or "" if callback.message else ""
        updated_caption = (
            f"{current_caption}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ <b>ปฏิเสธแล้ว</b> โดย {admin_name} (<code>{admin_user.id}</code>)\n"
            f"📅 <code>{timestamp_thai} น.</code>"
        )

        try:
            if callback.message:
                await callback.message.edit_caption(
                    caption=updated_caption,
                    reply_markup=None,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"Failed to update admin message caption: {e}")

        await callback.answer("❌ ปฏิเสธสลิปเรียบร้อยแล้ว")
    except Exception as e:
        logger.error(f"Failed to reject slip #{slip_id}: {e}", exc_info=True)
        await callback.answer(f"❌ เกิดข้อผิดพลาด: {e}", show_alert=True)


@router.message(Command("admin", "admin_help", "help_admin"))
async def handle_admin_menu_command(message: Message):
    """คำสั่งแสดงเมนูคำสั่งแอดมินทั้งหมด: /admin (เฉพาะใน Admin Group เท่านั้น)"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    admin_menu_text = (
        "👑 <b>เมนูคำสั่งผู้ดูแลระบบ (Admin Panel & Commands)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 <b>1. ตรวจสอบสมาชิก & รายงาน:</b>\n"
        "• <code>/summary</code> หรือ <code>/report</code> — ดูสรุปสมาชิก Active ปัจจุบัน พร้อมเปรียบเทียบยอดสมาชิกใน Channel จริง\n"
        "• <code>/users</code> หรือ <code>/users [หน้า]</code> — ดูประวัติผู้ใช้งานย้อนหลังทั้งหมดในระบบ พร้อมปุ่มเลื่อนหน้า\n"
        "• <code>/user [User ID หรือ @username]</code> — ดูประวัติเจาะลึกเฉพาะราย (เวลาออกลิงก์ 15m, เวลากดเข้าห้อง, เวลาหมดอายุ, สลิปโอนเงิน)\n\n"
        "💬 <b>2. ดูประวัติการคุย & ตอบกลับผู้ใช้:</b>\n"
        "• <code>/chat [User ID หรือ @username] [จำนวน]</code> — ดูประวัติการสนทนาย้อนหลังระหว่าง User กับ Bot\n"
        "• <code>/reply [User ID หรือ @username] [ข้อความ]</code> — ส่งข้อความตอบกลับผู้ใช้ทาง DM ในนามทีมงานแอดมิน\n\n"
        "📢 <b>3. บรอดแคสต์ & ส่งข้อความหาผู้ใช้:</b>\n"
        "• <code>/broadcast_count</code> — ตรวจสอบยอดผู้ใช้ทั้งหมดที่สามารถบรอดแคสต์ไปหาได้\n"
        "• <code>/broadcast_menu</code> — บรอดแคสต์เมนูหลัก /start ล่าสุด (พร้อมปุ่มชวนเพื่อน/โปรโมชั่น) ให้ผู้ใช้ทุกคน\n"
        "• <code>/broadcast [ข้อความ]</code> (หรือ Reply รูป) — บรอดแคสต์ข้อความข่าวสารหรือโปรโมชั่นให้ทุกคน\n"
        "• <code>/send_menu [User ID หรือ @username]</code> — ส่ง Template เมนูหลัก /start ล่าสุดให้เฉพาะบุคคล\n\n"
        "🔄 <b>4. ซิงค์และกู้คืนสมาชิกตกหล่น:</b>\n"
        "• <code>/sync</code> หรือ <code>/sync_channel</code> — ตรวจเช็คผู้ใช้ที่ค้าง PENDING ทั้งหมด หากพบว่าอยู่ใน Channel แล้วจะเปิดใช้งาน ACTIVE และเริ่มนับเวลาให้ทันที (พร้อมเตะคนที่หมดเวลาแล้ว)\n"
        "• <code>/deep_scan</code> — สแกนผู้ใช้ทั้งหมดในระบบแบบเจาะลึก หากพบว่ามีคนหมดอายุแต่ยังค้างอยู่ในห้องจะกวาดล้างเตะออกทันที\n\n"
        "⚙️ <b>5. ตรวจสอบระบบ & สิทธิ์บอท:</b>\n"
        "• <code>/audit</code> หรือ <code>/check</code> — ตรวจสอบสิทธิ์ของ Bot ใน Channel VIP (สิทธิ์ Ban Users, สิทธิ์สร้าง Invite Links) และสถานะการเชื่อมต่อ\n"
        "• <code>/revoke_primary</code> — สั่งเพิกถอนและสร้าง Primary Link ใหม่ของ Channel\n"
        "• <code>/revoke_link [Link]</code> — สั่งเพิกถอนลิงก์เชิญ (Invite Link) เฉพาะเจาะจง\n"
        "• <code>/version</code> — ตรวจสอบเวอร์ชันและ Uptime ของบอท\n\n"
        "🛠️ <b>6. จัดการสมาชิกใน Channel:</b>\n"
        "• <code>/kick [User ID]</code> — สั่งเตะ (Soft-Kick) ผู้ใช้ออกจาก Channel VIP ทันที พร้อมอัปเดตสถานะในระบบ\n"
        "• <code>/add_vip [User ID] [จำนวนวัน เช่น 30]</code> — เพิ่ม/ต่อเวลาสะสม VIP ให้ผู้ใช้ด้วยตนเอง\n\n"
        "🗑️ <b>7. รีเซ็ต & ลบประวัติผู้ใช้ (สำหรับทดสอบระบบ):</b>\n"
        "• <code>/reset_user [User ID หรือ @username]</code> — ลบประวัติแชท สลิป สิทธิ์ และบัญชีผู้ใช้ทั้งหมด พร้อมเตะออกจากห้อง VIP เพื่อให้เริ่มใหม่เป็นผู้ใช้ใหม่ 100%\n"
        "• <code>/reset_trial [User ID หรือ @username]</code> — รีเซ็ตเฉพาะสิทธิ์ทดลองใช้ฟรี 15 นาที ให้ผู้ใช้กดรับสิทธิ์ใหม่ได้ทันที\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>สามารถกดปุ่มลัดด้านล่างเพื่อใช้งานเมนูหลักได้ทันทีครับ</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 สรุปสมาชิก Active", callback_data="admin_menu:summary"),
                InlineKeyboardButton(text="📢 ยอด Broadcast", callback_data="admin_menu:broadcast_count"),
            ],
            [
                InlineKeyboardButton(text="🔄 ซิงค์สมาชิกค้าง (/sync)", callback_data="admin_menu:sync"),
                InlineKeyboardButton(text="🔍 Audit สิทธิ์บอท", callback_data="admin_menu:audit"),
            ],
            [
                InlineKeyboardButton(text="📑 ประวัติผู้ใช้ทั้งหมด (/users)", callback_data="admin:users_page:1"),
            ],
        ]
    )

    await message.answer(text=admin_menu_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "admin_menu:summary")
async def handle_admin_menu_summary_callback(callback: CallbackQuery, bot: Bot):
    """ปุ่มลัดสำหรับเปิดรายงาน Active Summary"""
    if not callback.from_user or not callback.message:
        return
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    report_text = await build_active_members_report(bot=bot)
    await callback.message.answer(text=report_text, parse_mode="HTML")
    await callback.answer()


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

        status_lines.append(f"📢 <b>Channel:</b> {html.escape(chat_info.title or '')} (<code>{config.CHANNEL_ID}</code>)")
        status_lines.append(f"👥 <b>จำนวนสมาชิกใน Telegram:</b> {member_count} คน")
        
        is_admin = bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        status_lines.append(f"🤖 <b>สถานะบอทใน Channel:</b> {'✅ Administrator' if is_admin else '❌ ไม่ได้เป็น Admin'}")

        if is_admin and hasattr(bot_member, "can_restrict_members"):
            can_ban = bot_member.can_restrict_members
            can_invite = bot_member.can_invite_users
            status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if can_ban else '❌ ขาดสิทธิ์ (สำคัญมาก!)'}")
            status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if can_invite else '❌ ขาดสิทธิ์'}")
    except Exception as e:
        status_lines.append(f"⚠️ <b>ตรวจสอบ Channel ล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

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
            select(func.count(Subscription.id)).where(
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


@router.message(Command("report", "summary"))
async def handle_admin_report_command(message: Message, bot: Bot):
    """คำสั่งดูรายงานสรุปสมาชิก Active ปัจจุบัน พร้อมเปรียบเทียบ Channel Member จริง (เฉพาะใน Admin Group)"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    report_text = await build_active_members_report(bot=bot)
    await message.answer(text=report_text, parse_mode="HTML")


@router.message(Command("audit", "check"))
async def handle_admin_audit_command(message: Message, bot: Bot):
    """คำสั่งตรวจสอบสถานะและสิทธิ์ของ Bot ใน Channel VIP และข้อมูลระบบ"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    status_lines = ["🔍 <b>ตรวจสอบสถานะและความพร้อมของระบบ (System Audit)</b>\n"]
    
    # 1. ตรวจสอบ Bot ใน Telegram Channel
    try:
        bot_info = await bot.get_me()
        chat_info = await bot.get_chat(chat_id=config.CHANNEL_ID)
        member_count = await bot.get_chat_member_count(chat_id=config.CHANNEL_ID)
        bot_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=bot_info.id)

        status_lines.append(f"📢 <b>Channel:</b> {html.escape(chat_info.title or '')} (<code>{config.CHANNEL_ID}</code>)")
        status_lines.append(f"👥 <b>จำนวนสมาชิกใน Telegram:</b> {member_count} คน")
        
        is_admin = bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        status_lines.append(f"🤖 <b>สถานะบอทใน Channel:</b> {'✅ Administrator' if is_admin else '❌ ไม่ได้เป็น Admin'}")

        if is_admin and hasattr(bot_member, "can_restrict_members"):
            can_ban = bot_member.can_restrict_members
            can_invite = bot_member.can_invite_users
            status_lines.append(f"   • สิทธิ์เตะ/แบน (Ban Users): {'✅ มีสิทธิ์' if can_ban else '❌ ขาดสิทธิ์ (สำคัญมาก!)'}")
            status_lines.append(f"   • สิทธิ์สร้างลิงก์เชิญ: {'✅ มีสิทธิ์' if can_invite else '❌ ขาดสิทธิ์'}")
    except Exception as e:
        status_lines.append(f"⚠️ <b>ตรวจสอบ Channel ล้มเหลว:</b> <code>{html.escape(str(e))}</code>")

    status_lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    status_lines.append("💡 <i>พิมพ์ <code>/summary</code> เพื่อดูรายชื่อสมาชิก Active\nหรือ <code>/kick [User ID]</code> เพื่อสั่งเตะสมาชิกออกจากห้อง</i>")

    await message.answer(text="\n".join(status_lines), parse_mode="HTML")


@router.message(Command("kick"))
async def handle_admin_kick_command(message: Message, bot: Bot):
    """คำสั่งแอดมินสำหรับบังคับเตะผู้ใช้ออกจาก Channel: /kick <user_id>"""
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

    # ดำเนินการ Soft-kick
    kicked = False
    err_msg = ""
    try:
        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, revoke_messages=False)
        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, only_if_banned=True)
        kicked = True
    except Exception as e:
        err_msg = str(e)
        logger.error(f"Manual kick failed for User {target_uid}: {e}")

    # อัปเดตสถานะใน DB
    async with get_session() as session:
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == target_uid,
                Subscription.status.in_([SubStatus.ACTIVE.value, SubStatus.PENDING.value, SubStatus.KICK_FAILED.value]),
            )
        )
        subs = (await session.execute(stmt)).scalars().all()
        for s in subs:
            s.status = SubStatus.KICKED.value
            session.add(s)

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
    """คำสั่งแอดมินสำหรับเพิ่มสิทธิ์ VIP ให้ผู้ใช้ด้วยตนเอง (รองรับสะสมวัน): /add_vip <user_id> [จำนวนวัน]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("❌ <b>วิธีใช้งาน:</b> <code>/add_vip [User ID] [จำนวนวัน เช่น 30]</code>\nตัวอย่าง: <code>/add_vip 5125375696 30</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(args[1])
        days = int(args[2]) if len(args) >= 3 else 30
    except ValueError:
        await message.answer("❌ User ID และจำนวนวันต้องเป็นตัวเลขเท่านั้น", parse_mode="HTML")
        return

    now = datetime.now(timezone.utc)
    is_stack_extension = False
    new_expires_at = None
    sub_id = None

    # ตรวจสอบสถานะจริงใน Channel
    is_in_channel = False
    try:
        chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid)
        is_in_channel = chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        is_in_channel = False

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=target_uid,
            username=None,
            full_name=f"User {target_uid}",
        )

        active_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == target_uid,
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.id.desc())
        )
        active_sub = (await session.execute(active_stmt)).scalar_one_or_none()

        if active_sub and is_in_channel:
            current_exp = ensure_utc(active_sub.expires_at)
            base_time = max(current_exp, now) if current_exp else now
            new_expires_at = base_time + timedelta(days=days)
            active_sub.expires_at = new_expires_at
            session.add(active_sub)
            sub_id = active_sub.id
            is_stack_extension = True
            logger.info(f"Admin add_vip: Extended active sub #{sub_id} for User {target_uid} by +{days} days. New expires_at: {new_expires_at}")
        else:
            new_expires_at = now + timedelta(days=days)
            subscription = Subscription(
                user_id=target_uid,
                plan_type=f"MANUAL_VIP_{days}D",
                joined_at=now if is_in_channel else None,
                expires_at=new_expires_at if is_in_channel else None,
                status=SubStatus.ACTIVE.value if is_in_channel else SubStatus.PENDING.value,
            )
            session.add(subscription)
            await session.flush()
            sub_id = subscription.id

    invite_url = "-"
    if not (is_stack_extension and is_in_channel):
        # สร้าง invite link ให้
        try:
            invite_link_obj = await bot.create_chat_invite_link(
                chat_id=config.CHANNEL_ID,
                member_limit=1,
                expire_date=now + timedelta(days=7),
                name=f"ManualVIP-{target_uid}",
            )
            invite_url = invite_link_obj.invite_link
        except Exception as e:
            logger.warning(f"Could not generate invite link: {e}")

    # ส่ง DM หาผู้ใช้
    exp_thai = format_thai_datetime(new_expires_at) if new_expires_at else "-"
    time_rem = format_time_remaining(new_expires_at) if new_expires_at else f"{days} วัน"

    try:
        if is_stack_extension:
            dm_text = (
                f"🎉 <b>คุณได้รับการต่อเวลาสมาชิก VIP (+{days} วัน) จากทีมงานเรียบร้อยแล้ว!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ <b>วันหมดอายุใหม่ของคุณ:</b> <code>{exp_thai} น.</code>\n"
                f"⏰ <b>เวลาคงเหลือรวม:</b> {time_rem}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>สามารถรับชมเนื้อหาใน Channel VIP ได้ต่อเนื่องทันทีครับ! 🚀</i>"
            )
        else:
            dm_text = (
                f"🎉 <b>คุณได้รับสิทธิ์สมาชิก VIP ({days} วัน) จากแอดมินเรียบร้อยแล้ว!</b>\n\n"
                f"⏳ <b>หมดอายุวันที่:</b> <code>{exp_thai} น.</code>\n\n"
                f"🔗 <b>ลิงก์เข้าร่วม Channel:</b>\n{invite_url}"
            )
        await bot.send_message(
            chat_id=target_uid,
            text=dm_text,
            parse_mode="HTML",
        )
    except Exception:
        pass

    if is_stack_extension:
        resp = (
            "✅ <b>ต่อเวลาสะสม VIP สำเร็จ!</b>\n\n"
            f"👤 <b>User ID:</b> <code>{target_uid}</code>\n"
            f"➕ <b>เพิ่มเวลา:</b> +{days} วัน\n"
            f"⏳ <b>วันหมดอายุใหม่:</b> <code>{exp_thai} น.</code> (คงเหลือ {time_rem})\n"
            f"🆔 <b>Sub ID:</b> <code>#{sub_id}</code>\n"
            "ℹ️ <i>ผู้ใช้อยู่ในห้องอยู่แล้ว ระบบต่อเวลาให้โดยไม่ต้องกดเข้าใหม่</i>"
        )
    else:
        resp = (
            "✅ <b>เพิ่มสิทธิ์ VIP เรียบร้อยแล้ว!</b>\n\n"
            f"👤 <b>User ID:</b> <code>{target_uid}</code>\n"
            f"📅 <b>ระยะเวลา:</b> {days} วัน (หมดอายุ: <code>{exp_thai} น.</code>)\n"
            f"🆔 <b>Sub ID:</b> <code>#{sub_id}</code>\n"
            f"🔗 <b>Invite Link:</b> <code>{invite_url}</code>"
        )
    await message.answer(resp, parse_mode="HTML")


@router.message(Command("reset_user", "delete_user", "del_user", "clear_user"))
async def handle_admin_reset_user_command(message: Message, bot: Bot):
    """คำสั่งแอดมินลบประวัติและบัญชีผู้ใช้ทั้งหมด (Factory Reset): /reset_user <@username หรือ User ID>"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ <b>วิธีใช้งาน:</b> <code>/reset_user [User ID หรือ @username]</code>\n"
            "ตัวอย่าง:\n"
            "• <code>/reset_user 5125375696</code>\n"
            "• <code>/reset_user @some_user</code>\n\n"
            "⚠️ <i>คำสั่งนี้จะลบประวัติการแชท สลิป สิทธิ์แพ็กเกจ และเตะออกจากห้อง VIP ทันที เพื่อให้ผู้ใช้เริ่มใหม่เหมือนเพิ่งเข้าบอทครั้งแรก</i>",
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
                user_handle = "ไม่มีในระบบ"
            else:
                await message.answer(f"❌ ไม่พบข้อมูลผู้ใช้ <code>{html.escape(query)}</code> ในระบบ", parse_mode="HTML")
                return
        else:
            target_uid = user.telegram_id
            user_name = html.escape(user.full_name or f"User {target_uid}")
            user_handle = f"@{user.username}" if user.username else "ไม่มี Username"

        # ลบข้อมูลทั้งหมดจากฐานข้อมูล
        await session.execute(delete(ChatMessage).where(ChatMessage.user_id == target_uid))
        await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == target_uid))
        await session.execute(delete(Subscription).where(Subscription.user_id == target_uid))
        await session.execute(delete(User).where(User.telegram_id == target_uid))

    # เตะออกจาก Channel (Soft-kick) เพื่อให้ลิงก์เก่าไม่ค้าง และผู้ใช้ออกจากห้องจริง
    channel_kicked = False
    try:
        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, revoke_messages=False)
        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, only_if_banned=True)
        channel_kicked = True
    except Exception as e:
        logger.debug(f"Soft-kick during reset for {target_uid}: {e}")

    resp = (
        "🗑️ <b>รีเซ็ตและลบข้อมูลผู้ใช้สำเร็จ (Factory Reset)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"🚪 <b>สถานะ Channel:</b> {'เตะออกจากห้อง VIP เรียบร้อย ✅' if channel_kicked else 'ไม่อยู่ในห้อง / ปลดสิทธิ์แล้ว'}\n"
        "📭 <b>สถานะ Database:</b> ลบประวัติแชท, สลิป, แพ็กเกจ และบัญชีผู้ใช้ทั้งหมดแล้ว\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้นี้กลับเป็นผู้ใช้ใหม่ 100% สามารถพิมพ์ <code>/start</code> ในบอทเพื่อทดสอบรับสิทธิ์ทดลองฟรี หรือสมัครแพ็กเกจใหม่ได้ทันทีครับ</i>"
    )
    await message.answer(resp, parse_mode="HTML")


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
        trial_subs = (await session.execute(
            select(Subscription).where(
                Subscription.user_id == target_uid,
                Subscription.plan_type == PlanType.TRIAL_15M.value,
                Subscription.status.in_([SubStatus.ACTIVE.value, SubStatus.PENDING.value])
            )
        )).scalars().all()

        for s in trial_subs:
            s.status = SubStatus.EXPIRED.value
            session.add(s)

    # เตะออกจาก Channel หากกำลังใช้ trial
    if trial_subs:
        try:
            await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, revoke_messages=False)
            await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, only_if_banned=True)
        except Exception:
            pass

    user_name = html.escape(user.full_name or f"User {target_uid}")
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"

    resp = (
        "🔄 <b>รีเซ็ตสิทธิ์ทดลองฟรี (Trial) สำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{target_uid}</code>\n"
        f"⏱️ <b>สถานะ Trial:</b> คืนสิทธิ์แล้ว (trial_used = False)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้สามารถกดรับสิทธิ์ทดลองฟรี 15 นาทีจากเมนู <code>/start</code> ได้อีกครั้งทันทีครับ</i>"
    )
    await message.answer(resp, parse_mode="HTML")

    # ส่งข้อความแจ้งเตือนผู้ใช้พร้อมปุ่มทดลองใช้ใหม่
    user_notify_text = (
        "🔄 <b>แอดมินได้ทำการรีเซ็ตสิทธิ์ทดลองใช้งานให้คุณแล้ว</b>\n\n"
        "คุณสามารถกดปุ่ม <b>'ทดลองใหม่'</b> ด้านล่างนี้เพื่อรับสิทธิ์และออกลิงก์ทดลองใช้ฟรี 15 นาทีได้เลยค่ะ"
    )
    trial_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱️ ทดลองใช้ฟรี 15 นาที (ทดลองใหม่)",
                    callback_data="menu:trial"
                )
            ]
        ]
    )
    try:
        await bot.send_message(chat_id=target_uid, text=user_notify_text, reply_markup=trial_keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to notify user {target_uid} about trial reset: {e}")


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
        # ค้นหาด้วย User ID (ถ้าเป็นตัวเลข) หรือ username
        if query.isdigit():
            user_stmt = select(User).where(User.telegram_id == int(query))
        else:
            user_stmt = select(User).where(User.username.ilike(query))
            
        user = (await session.execute(user_stmt)).scalar_one_or_none()
        
        if not user:
            await message.answer(f"❌ <b>ไม่พบข้อมูลผู้ใช้:</b> <code>{html.escape(query)}</code> ในระบบฐานข้อมูล", parse_mode="HTML")
            return

        # ดึงประวัติ Subscriptions ทั้งหมดของผู้ใช้นี้
        subs_stmt = (
            select(Subscription)
            .where(Subscription.user_id == user.telegram_id)
            .order_by(Subscription.id.desc())
        )
        subs = (await session.execute(subs_stmt)).scalars().all()
        
        # ดึงประวัติ PaymentSlips
        slips_stmt = (
            select(PaymentSlip)
            .where(PaymentSlip.user_id == user.telegram_id)
            .order_by(PaymentSlip.id.desc())
        )
        slips = (await session.execute(slips_stmt)).scalars().all()

    # ตรวจสอบสถานะใน Channel จริง
    channel_status_str = "ไม่ทราบสถานะ"
    try:
        chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user.telegram_id)
        status_map = {
            ChatMemberStatus.CREATOR: "👑 เจ้าของห้อง (Creator)",
            ChatMemberStatus.ADMINISTRATOR: "🛡️ ผู้ดูแล (Admin)",
            ChatMemberStatus.MEMBER: "🟢 อยู่ใน Channel (Member)",
            ChatMemberStatus.LEFT: "⚪ ออกจากห้องไปแล้ว (Left)",
            ChatMemberStatus.KICKED: "🔴 ถูกแบน/เตะออก (Kicked/Banned)",
            ChatMemberStatus.RESTRICTED: "🟡 ถูกจำกัดสิทธิ์ (Restricted)",
        }
        channel_status_str = status_map.get(chat_member.status, chat_member.status)
    except Exception as e:
        channel_status_str = f"ไม่อยู่ใน Channel / ตรวจสอบไม่ได้ ({e})"

    # จัดรูปแบบข้อความ
    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    
    # ประวัติการเข้า Channel ทั้งหมด
    joined_times = [s.joined_at for s in subs if s.joined_at is not None]
    if joined_times:
        earliest_join = min(joined_times)
        latest_join = max(joined_times)
        if earliest_join != latest_join:
            join_str = f"🚪 <b>เข้า Channel ครั้งแรก:</b> <code>{format_thai_datetime(earliest_join)} น.</code>\n🚪 <b>เข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(latest_join)} น.</code>"
        else:
            join_str = f"🚪 <b>เวลากดเข้า Channel:</b> <code>{format_thai_datetime(latest_join)} น.</code>"
    else:
        join_str = "🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>"

    ref_by_str = f"<code>{user.referred_by_id}</code>" if user.referred_by_id else "<i>ไม่มี (เข้าเองโดยตรง)</i>"
    resp = [
        f"👤 <b>ข้อมูลผู้ใช้งาน: {full_name_safe}</b> ({user_handle})",
        f"🔢 <b>Telegram ID:</b> <code>{user.telegram_id}</code>",
        f"⏱️ <b>เคยใช้สิทธิ์ทดลองฟรี (Trial Used):</b> {'✅ เคยใช้แล้ว' if user.trial_used else '❌ ยังไม่เคยใช้'}",
        f"🎁 <b>สถิติ Referral:</b> ชวนสำเร็จ {user.referral_count or 0} คน | โบนัสสะสม {user.referral_bonus_days or 0} วัน",
        f"🔗 <b>สมัครผ่านผู้แนะนำ (Referred By):</b> {ref_by_str}",
        f"📢 <b>สถานะใน Channel ปัจจุบัน:</b> {channel_status_str}",
        f"📅 <b>เข้าระบบบอทครั้งแรก:</b> <code>{format_thai_datetime(user.created_at)} น.</code>",
        join_str,
        "\n━━━━━━━━━━━━━━━━━━━━",
        "📦 <b>ประวัติการขอแพ็กเกจ/สิทธิ์ (Subscriptions):</b>",
    ]

    if not subs:
        resp.append("<i>ไม่มีประวัติการขอแพ็กเกจ</i>")
    else:
        for i, s in enumerate(subs, 1):
            created_thai = format_thai_datetime(s.created_at)
            joined_thai = format_thai_datetime(s.joined_at)
            expired_thai = format_thai_datetime(s.expires_at)
            
            if s.plan_type == PlanType.TRIAL_15M.value:
                plan_label = "ทดลองใช้ 15 นาที"
            elif s.plan_type in PLAN_DETAILS:
                plan_label = get_dynamic_plan_info(s.plan_type)["badge"]
            elif s.plan_type.startswith("MANUAL_VIP_"):
                plan_label = s.plan_type.replace("MANUAL_VIP_", "VIP ").replace("D", " วัน")
            else:
                plan_label = s.plan_type

            status_badge = {
                SubStatus.ACTIVE.value: "🟢 ACTIVE (กำลังใช้งาน)",
                SubStatus.PENDING.value: "🟡 PENDING (ออกลิงก์แล้ว-รอกดเข้า)",
                SubStatus.EXPIRED.value: "⚪ EXPIRED (หมดอายุ)",
                SubStatus.KICKED.value: "🔴 KICKED (เตะออกจากห้องแล้ว)",
                SubStatus.KICK_FAILED.value: "⚠️ KICK_FAILED (เตะไม่สำเร็จ)",
            }.get(s.status, s.status)

            sub_info = (
                f"\n<b>{i}. [#{s.id}] {plan_label}</b> — {status_badge}\n"
                f"   • 🎟️ <b>เวลาออกลิงก์/สร้าง:</b> <code>{created_thai} น.</code>\n"
                f"   • 🚪 <b>เวลากดเข้า Channel:</b> <code>{joined_thai} น.</code>\n"
                f"   • ⏰ <b>เวลาหมดอายุ:</b> <code>{expired_thai} น.</code>"
            )
            resp.append(sub_info)

    if slips:
        resp.append("\n━━━━━━━━━━━━━━━━━━━━")
        resp.append(f"💳 <b>ประวัติส่งสลิปโอนเงิน ({len(slips)} รายการ):</b>")
        for sl in slips:
            sl_created = format_thai_datetime(sl.created_at)
            resp.append(f"• สลิป #{sl.id} | สถานะ: <b>{sl.status}</b> | เวลาส่ง: <code>{sl_created} น.</code>")

    resp.append("\n━━━━━━━━━━━━━━━━━━━━")
    resp.append(f"📋 <b>คำสั่งด่วน (แตะเพื่อคัดลอก):</b>")
    resp.append(f"💬 ตอบกลับข้อความ: <code>/reply {user.telegram_id} </code>")
    resp.append(f"➕ เพิ่ม VIP (30 วัน): <code>/add_vip {user.telegram_id} 30</code>")
    resp.append(f"👢 เตะออกจากห้อง: <code>/kick {user.telegram_id}</code>")

    user_action_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user.telegram_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 รีเซ็ต Trial", callback_data=f"admin:reset_trial:{user.telegram_id}"),
                InlineKeyboardButton(text="🗑️ ลบประวัติทั้งหมด", callback_data=f"admin:confirm_reset_user:{user.telegram_id}"),
            ],
        ]
    )

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

        subs_stmt = select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id.desc())
        subs = (await session.execute(subs_stmt)).scalars().all()

        slips_stmt = select(PaymentSlip).where(PaymentSlip.user_id == user_id).order_by(PaymentSlip.id.desc())
        slips = (await session.execute(slips_stmt)).scalars().all()

    channel_status_str = "ไม่ทราบสถานะ"
    try:
        chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
        status_map = {
            ChatMemberStatus.CREATOR: "👑 เจ้าของห้อง (Creator)",
            ChatMemberStatus.ADMINISTRATOR: "🛡️ ผู้ดูแล (Admin)",
            ChatMemberStatus.MEMBER: "🟢 อยู่ใน Channel (Member)",
            ChatMemberStatus.LEFT: "⚪ ออกจากห้องไปแล้ว (Left)",
            ChatMemberStatus.KICKED: "🔴 ถูกแบน/เตะออก (Kicked/Banned)",
            ChatMemberStatus.RESTRICTED: "🟡 ถูกจำกัดสิทธิ์ (Restricted)",
        }
        channel_status_str = status_map.get(chat_member.status, chat_member.status)
    except Exception as e:
        channel_status_str = f"ไม่อยู่ใน Channel ({e})"

    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    
    joined_times = [s.joined_at for s in subs if s.joined_at is not None]
    if joined_times:
        earliest_join = min(joined_times)
        latest_join = max(joined_times)
        if earliest_join != latest_join:
            join_str = f"🚪 <b>เข้า Channel ครั้งแรก:</b> <code>{format_thai_datetime(earliest_join)} น.</code>\n🚪 <b>เข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(latest_join)} น.</code>"
        else:
            join_str = f"🚪 <b>เวลากดเข้า Channel:</b> <code>{format_thai_datetime(latest_join)} น.</code>"
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
        f"📦 <b>ประวัติการขอแพ็กเกจ ({len(subs)} รายการ):</b>",
    ]

    for i, s in enumerate(subs[:3], 1):
        expired_thai = format_thai_datetime(s.expires_at)
        if s.plan_type == PlanType.TRIAL_15M.value:
            plan_label = "ทดลองใช้ 15 นาที"
        elif s.plan_type in PLAN_DETAILS:
            plan_label = get_dynamic_plan_info(s.plan_type)["badge"]
        elif s.plan_type.startswith("MANUAL_VIP_"):
            plan_label = s.plan_type.replace("MANUAL_VIP_", "VIP ").replace("D", " วัน")
        else:
            plan_label = s.plan_type
        resp.append(f"• [#{s.id}] <b>{plan_label}</b> ({s.status}) | หมดอายุ: <code>{expired_thai} น.</code>")

    if slips:
        resp.append(f"\n💳 <b>สลิปล่าสุด:</b> #{slips[0].id} ({slips[0].status})")

    resp.append("\n━━━━━━━━━━━━━━━━━━━━")
    resp.append("\n━━━━━━━━━━━━━━━━━━━━")
    resp.append(f"📋 <b>คำสั่งด่วน (แตะเพื่อคัดลอก):</b>")
    resp.append(f"💬 ตอบกลับข้อความ: <code>/reply {user_id} </code>")
    resp.append(f"➕ เพิ่ม VIP (30 วัน): <code>/add_vip {user_id} 30</code>")
    resp.append(f"👢 เตะออกจากห้อง: <code>/kick {user_id}</code>")

    user_action_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user_id}"),
            ],
            [
                InlineKeyboardButton(text="🔄 รีเซ็ต Trial", callback_data=f"admin:reset_trial:{user_id}"),
                InlineKeyboardButton(text="🗑️ ลบประวัติทั้งหมด", callback_data=f"admin:confirm_reset_user:{user_id}"),
            ],
        ]
    )

    await callback.message.answer("\n".join(resp), reply_markup=user_action_keyboard, parse_mode="HTML")
    await callback.answer()


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

        trial_subs = (await session.execute(
            select(Subscription).where(
                Subscription.user_id == target_uid,
                Subscription.plan_type == PlanType.TRIAL_15M.value,
                Subscription.status.in_([SubStatus.ACTIVE.value, SubStatus.PENDING.value])
            )
        )).scalars().all()

        for s in trial_subs:
            s.status = SubStatus.EXPIRED.value
            session.add(s)

    if trial_subs:
        try:
            await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, revoke_messages=False)
            await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, only_if_banned=True)
        except Exception:
            pass

    user_name = html.escape(user.full_name or f"User {target_uid}")
    await callback.message.answer(
        f"🔄 <b>รีเซ็ตสิทธิ์ทดลองฟรี (Trial) สำหรับ {user_name} (<code>{target_uid}</code>) สำเร็จ!</b>\n"
        "✨ <i>ผู้ใช้สามารถกดรับสิทธิ์ทดลองฟรี 15 นาทีจากเมนู /start ได้อีกครั้งทันทีครับ</i>",
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
        "• สิทธิ์สมาชิก (Subscriptions) ทั้งหมดจะถูกลบ\n"
        "• ผู้ใช้จะถูกเตะออกจากห้อง VIP ทันที\n"
        "• บัญชีจะถูกลบออกจากระบบ (กลายเป็น User ใหม่ 100%)",
        reply_markup=confirm_kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:do_reset_user:"))
async def handle_admin_do_reset_user_callback(callback: CallbackQuery, bot: Bot):
    """Callback ดำเนินการลบประวัติและผู้ใช้ทั้งหมดจริง"""
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

        await session.execute(delete(ChatMessage).where(ChatMessage.user_id == target_uid))
        await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == target_uid))
        await session.execute(delete(Subscription).where(Subscription.user_id == target_uid))
        await session.execute(delete(User).where(User.telegram_id == target_uid))

    try:
        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, revoke_messages=False)
        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=target_uid, only_if_banned=True)
    except Exception:
        pass

    resp_text = (
        f"🗑️ <b>ลบประวัติและรีเซ็ตบัญชี {user_name} (<code>{target_uid}</code>) สำเร็จแล้ว!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📭 ลบประวัติแชท, สลิป, แพ็กเกจ และบัญชีผู้ใช้ทั้งหมดแล้ว\n"
        "🚪 เตะออกจากห้อง VIP เรียบร้อยแล้ว (หากเคยอยู่)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <i>ผู้ใช้กลับเป็นผู้ใช้ใหม่ 100% สามารถพิมพ์ <code>/start</code> ในบอทเพื่อทดสอบรับลิงก์ใหม่ได้ทันทีครับ</i>"
    )

    try:
        await callback.message.edit_text(text=resp_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text=resp_text, parse_mode="HTML")
    await callback.answer("✅ ลบและรีเซ็ตผู้ใช้เรียบร้อยแล้ว")


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
            select(func.count(Subscription.id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )).scalar() or 0
        total_trial_used = (await session.execute(
            select(func.count(User.telegram_id)).where(User.trial_used == True)
        )).scalar() or 0
        total_kick_failed = (await session.execute(
            select(func.count(Subscription.id)).where(Subscription.status == SubStatus.KICK_FAILED.value)
        )).scalar() or 0

        if total_users == 0:
            return "ℹ️ <i>ขณะนี้ยังไม่มีข้อมูลผู้ใช้งานในระบบฐานข้อมูล</i>", None

        total_pages = max(1, (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
        page = max(1, min(page, total_pages))

        # 2. ดึงข้อมูล User ประจำหน้านี้ พร้อม Subscriptions และ Slips
        stmt = (
            select(User)
            .options(
                selectinload(User.subscriptions),
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
        user_handle = f"@{u.username}" if u.username else "ไม่มี Username"
        full_name_safe = html.escape(u.full_name or "ไม่ระบุชื่อ")
        trial_str = "เคยใช้แล้ว ✅" if u.trial_used else "ยังไม่เคยใช้ ⏱️"
        
        user_block = [
            f"<b>{i}. {full_name_safe}</b> ({user_handle})",
            f"   • 🔢 <b>User ID:</b> <code>{u.telegram_id}</code> | สิทธิ์ฟรี: {trial_str}",
            f"   • 📅 <b>เข้าใช้บอทครั้งแรก:</b> <code>{format_thai_datetime(u.created_at)} น.</code>",
        ]

        # ตรวจสอบประวัติการกดเข้า Channel ทั้งหมด
        joined_times = [s.joined_at for s in u.subscriptions if s.joined_at is not None]
        if joined_times:
            earliest_join = min(joined_times)
            latest_join = max(joined_times)
            if earliest_join != latest_join:
                user_block.append(f"   • 🚪 <b>เข้า Channel ครั้งแรก:</b> <code>{format_thai_datetime(earliest_join)} น.</code>")
                user_block.append(f"   • 🚪 <b>เข้า Channel ล่าสุด:</b> <code>{format_thai_datetime(latest_join)} น.</code>")
            else:
                user_block.append(f"   • 🚪 <b>เวลากดเข้า Channel:</b> <code>{format_thai_datetime(latest_join)} น.</code>")
        else:
            user_block.append("   • 🚪 <b>เวลากดเข้า Channel:</b> <i>ยังไม่เคยกดเข้าห้อง</i>")

        # Subscription ล่าสุด
        latest_sub = u.subscriptions[0] if u.subscriptions else None
        if latest_sub:
            if latest_sub.plan_type == PlanType.TRIAL_15M.value:
                plan_label = "ทดลองใช้ 15 นาที"
            elif latest_sub.plan_type in PLAN_DETAILS:
                plan_label = get_dynamic_plan_info(latest_sub.plan_type)["badge"]
            elif latest_sub.plan_type.startswith("MANUAL_VIP_"):
                plan_label = latest_sub.plan_type.replace("MANUAL_VIP_", "VIP ").replace("D", " วัน")
            else:
                plan_label = latest_sub.plan_type

            status_badge = {
                SubStatus.ACTIVE.value: "🟢 ACTIVE",
                SubStatus.PENDING.value: "🟡 PENDING (ออกลิงก์แล้ว-รอกดเข้า)",
                SubStatus.EXPIRED.value: "⚪ EXPIRED",
                SubStatus.KICKED.value: "🔴 KICKED",
                SubStatus.KICK_FAILED.value: "⚠️ KICK_FAILED",
            }.get(latest_sub.status, latest_sub.status)

            user_block.append(f"   • 📦 <b>สถานะล่าสุด:</b> {plan_label} [{status_badge}]")
            user_block.append(f"   • 🎟️ <b>เวลาออกลิงก์ล่าสุด:</b> <code>{format_thai_datetime(latest_sub.created_at)} น.</code>")
            if latest_sub.expires_at:
                user_block.append(f"   • ⏰ <b>เวลาหมดอายุ:</b> <code>{format_thai_datetime(latest_sub.expires_at)} น.</code>")
        else:
            user_block.append("   • 📦 <i>ยังไม่มีประวัติการขอแพ็กเกจ</i>")

        if u.payment_slips:
            user_block.append(f"   • 💳 สลิปชำระเงิน: {len(u.payment_slips)} รายการ (ล่าสุด: <b>{u.payment_slips[0].status}</b>)")

        user_block.append("")
        lines.extend(user_block)
        
        # เพิ่มปุ่มจัดการผู้ใช้รายบุคคล (UX improvement)
        buttons.append([InlineKeyboardButton(text=f"👤 {i}. จัดการ {full_name_safe}", callback_data=f"admin:view_user:{u.telegram_id}")])

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
            select(func.count(Subscription.id)).where(
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

    now = datetime.now(timezone.utc)

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

                # 2. ถ้าไม่มี ACTIVE/PENDING ให้ลองดึงสถานะจาก Telegram
                try:
                    chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
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
                        await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, revoke_messages=False)
                        await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id, only_if_banned=True)
                        kicked_count += 1
                        
                        # อัปเดต Sub ล่าสุดให้เป็น KICKED
                        sub_stmt = select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id.desc())
                        latest_sub = (await session.execute(sub_stmt)).scalars().first()
                        if latest_sub:
                            latest_sub.status = SubStatus.KICKED.value
                            session.add(latest_sub)
                            await session.commit()
                            
                    except Exception as e:
                        error_count += 1
                elif chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                    admin_skip += 1
                
                # หน่วงเวลาเล็กน้อยป้องกัน Rate Limit จาก Telegram
                from asyncio import sleep as asyncio_sleep
                await asyncio_sleep(0.1)

        report_msg = (
            "✅ <b>Deep Scan เสร็จสิ้น!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 ตรวจสอบทั้งหมด: <b>{scanned_count}</b> บัญชี\n"
            f"👢 เตะคนที่ค้างสำเร็จ: <b>{kicked_count}</b> คน\n"
            f"⚠️ เตะไม่สำเร็จ (Error): <b>{error_count}</b> คน\n"
            f"🛡️ ข้ามแอดมิน: <b>{admin_skip}</b> คน\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await message.answer(report_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in deep_scan: {e}", exc_info=True)
        await message.answer("❌ เกิดข้อผิดพลาดระหว่างทำ Deep Scan กรุณาลองใหม่อีกครั้ง")
