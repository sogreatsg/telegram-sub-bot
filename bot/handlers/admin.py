import logging
import html
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.filters import Command
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import PaymentSlip, Subscription, SlipStatus, SubStatus, PlanType
from bot.services.database import get_session
from bot.services.scheduler import build_active_members_report

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="admin")

BANGKOK_TZ = timezone(timedelta(hours=7))


def format_thai_datetime(dt: datetime) -> str:
    """แปลงเวลาเป็นเวลาไทย (UTC+7) รูปแบบ วัน/เดือน/ปี ชั่วโมง:นาที:วินาที"""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    thai_dt = dt.astimezone(BANGKOK_TZ)
    return thai_dt.strftime("%d/%m/%Y %H:%M:%S")


@router.callback_query(F.data.startswith("admin:approve:"))
async def handle_admin_approve(callback: CallbackQuery, bot: Bot):
    """จัดการเมื่อ Admin กดยืนยัน/อนุมัติสลิปสำหรับสมาชิก VIP (เวลาไทย)"""
    if not callback.from_user:
        return

    admin_user = callback.from_user
    slip_id_str = callback.data.split(":")[-1]
    
    try:
        slip_id = int(slip_id_str)
    except ValueError:
        await callback.answer("❌ รหัสสลิปไม่ถูกต้อง", show_alert=True)
        return

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

        # 2. สร้างรายการ Subscription ใหม่แบบ PENDING
        subscription = Subscription(
            user_id=slip.user_id,
            plan_type=PlanType.MONTHLY_30D.value,
            status=SubStatus.PENDING.value,
        )
        session.add(subscription)
        await session.flush()
        target_user_id = slip.user_id

    # 3. สร้างลิงก์เชิญแบบ 1 ครั้งให้ผู้ใช้
    try:
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=config.CHANNEL_ID,
            member_limit=1,
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

    # 4. ส่งลิงก์เชิญพร้อมปุ่มกดเข้าร่วมให้ผู้ใช้ทาง DM
    paid_min = config.PAID_DURATION_MINUTES
    plan_desc = f"{paid_min // 1440} วัน" if paid_min >= 1440 else f"{paid_min} นาที (โหมดทดสอบ)"

    user_dm_sent = False
    try:
        user_message = (
            "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว!</b>\n\n"
            f"แพ็กเกจ <b>สมาชิก VIP ({plan_desc})</b> ของคุณพร้อมใช้งานแล้วครับ\n\n"
            f"🔗 <b>ลิงก์เชิญเข้า Channel ส่วนตัว (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
            "📌 <b>ข้อควรทราบ:</b>\n"
            "• ลิงก์นี้สามารถใช้งานได้เพียง 1 ครั้งเท่านั้น\n"
            f"• <b>ระยะเวลาสมาชิก {plan_desc} จะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel</b>\n\n"
            "กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ! 🚀"
        )
        join_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 เข้าร่วม Channel VIP ตอนนี้", url=invite_url)]
            ]
        )
        await bot.send_message(
            chat_id=target_user_id,
            text=user_message,
            reply_markup=join_keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        user_dm_sent = True
        logger.info(f"Sent approval DM with invite link and button to User ID={target_user_id}")
    except Exception as e:
        logger.warning(f"Could not send approval DM to User ID={target_user_id}: {e}")

    # 5. แก้ไขข้อความในกลุ่ม Admin (เวลาไทย)
    admin_name = f"@{admin_user.username}" if admin_user.username else html.escape(admin_user.full_name)
    timestamp_thai = format_thai_datetime(datetime.now(timezone.utc))
    
    current_caption = callback.message.caption or "" if callback.message else ""
    updated_caption = (
        f"{current_caption}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>อนุมัติแล้ว</b> โดย {admin_name} (<code>{admin_user.id}</code>)\n"
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

    await callback.answer("✅ อนุมัติสลิปและส่งลิงก์เชิญให้ผู้ใช้เรียบร้อยแล้ว")


@router.callback_query(F.data.startswith("admin:reject:"))
async def handle_admin_reject(callback: CallbackQuery, bot: Bot):
    """จัดการเมื่อ Admin กดปฏิเสธสลิป (เวลาไทย)"""
    if not callback.from_user:
        return

    admin_user = callback.from_user
    slip_id_str = callback.data.split(":")[-1]

    try:
        slip_id = int(slip_id_str)
    except ValueError:
        await callback.answer("❌ รหัสสลิปไม่ถูกต้อง", show_alert=True)
        return

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

    # 1. ส่งข้อความแจ้งผู้ใช้ทาง DM
    try:
        rejection_message = (
            "❌ <b>แจ้งเตือนผลการตรวจสอบสลิปโอนเงิน</b>\n\n"
            "ทีมงานไม่สามารถยืนยันสลิปการโอนเงินสำหรับสมาชิก VIP ของคุณได้ครับ\n\n"
            "สาเหตุที่เป็นไปได้:\n"
            "• ยอดเงินที่โอนไม่ตรงกับค่าบริการ (300 บาท)\n"
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


@router.message(Command("report", "summary"))
async def handle_admin_report_command(message: Message):
    """คำสั่งดูรายงานสรุปสมาชิก Active ปัจจุบัน (สำหรับแอดมิน)"""
    # ตรวจสอบว่าเป็นคำสั่งจากกลุ่ม Admin หรือแอดมิน
    if message.chat.id != config.ADMIN_GROUP_ID and (not message.from_user):
        return

    report_text = await build_active_members_report()
    await message.answer(text=report_text, parse_mode="HTML")
