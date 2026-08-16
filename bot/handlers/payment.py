import logging
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command

from bot.config import get_settings
from bot.models.schema import PaymentSlip, SlipStatus, PlanType, PLAN_DETAILS
from bot.services.database import get_session, get_or_create_user
from bot.services.chat_logger import log_chat_message

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="payment")

from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

class PaymentStates(StatesGroup):
    """FSM states สำหรับกระบวนการส่งสลิปโอนเงิน"""
    waiting_for_slip = State()


def get_payment_keyboard() -> InlineKeyboardMarkup:
    """สร้างปุ่มยกเลิกการส่งสลิป"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ ยกเลิก", callback_data="payment:cancel")]
        ]
    )


def get_admin_slip_keyboard(slip_id: int, user_id: int) -> InlineKeyboardMarkup:
    """สร้างปุ่มอนุมัติและปฏิเสธสำหรับแอดมินในกลุ่ม Admin"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ อนุมัติ",
                    callback_data=f"admin:approve:{slip_id}",
                ),
                InlineKeyboardButton(
                    text="❌ ปฏิเสธ",
                    callback_data=f"admin:reject:{slip_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👤 ดูข้อมูลสมาชิก",
                    callback_data=f"admin:view_user:{user_id}",
                ),
                InlineKeyboardButton(
                    text="📜 ดูประวัติการคุย",
                    callback_data=f"admin:view_chat:{user_id}",
                ),
            ]
        ]
    )


@router.callback_query(F.data.startswith("menu:subscribe"))
async def handle_subscribe_plan_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """เริ่มขั้นตอนการสมัครสมาชิก VIP ตามแพ็กเกจที่เลือก พร้อมส่งรูป QR Code PromptPay"""
    if not callback.from_user or not callback.message:
        return

    # ระบุประเภทแพ็กเกจจาก Callback data
    plan_key = PlanType.VIP_30D.value
    if ":" in callback.data:
        parts = callback.data.split(":")
        if len(parts) >= 3:
            plan_key = parts[2]
        elif callback.data == "menu:subscribe_30d":
            plan_key = PlanType.VIP_30D.value

    plan_info = PLAN_DETAILS.get(plan_key, PLAN_DETAILS[PlanType.VIP_30D.value])

    await state.set_state(PaymentStates.waiting_for_slip)
    await state.update_data(plan_type=plan_key)

    caption_text = (
        f"💳 <b>สมัครสมาชิก {plan_info['badge']} (ราคา {plan_info['price']:,} บาท)</b>\n\n"
        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วันเต็ม\n"
        "📲 <b>สแกน QR Code พร้อมเพย์ด้านบนเพื่อชำระเงิน</b>\n"
        f"• ยอดชำระ: <b>{plan_info['price']:,} บาท</b>\n"
        "• สแกนจ่ายผ่านแอปธนาคารได้ทันที\n\n"
        "📸 <b>ขั้นตอนถัดไป:</b>\n"
        "เมื่อโอนเงินเรียบร้อยแล้ว กรุณาส่งรูปสลิปเข้ามาในแชทนี้ได้เลยครับ\n\n"
        "💡 <i>คำแนะนำ: คุณสามารถพิมพ์ /cancel ได้ตลอดเวลาหากต้องการยกเลิก</i>"
    )

    # ค้นหารูป QR Code สำหรับแพ็กเกจนี้ หรือรูปเริ่มต้น
    qr_candidates = [
        Path(f"bot/assets/{plan_info.get('qr_filename', '')}"),
        Path("bot/assets/qr_payment.png"),
        Path(config.PAYMENT_QR_PATH),
    ]
    qr_path = next((p for p in qr_candidates if p.exists()), None)

    if qr_path:
        try:
            qr_photo = FSInputFile(str(qr_path))
            await callback.message.answer_photo(
                photo=qr_photo,
                caption=caption_text,
                reply_markup=get_payment_keyboard(),
                parse_mode="HTML",
            )
            try:
                await callback.message.delete()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to send QR image: {e}", exc_info=True)
            await callback.message.answer(
                text=caption_text,
                reply_markup=get_payment_keyboard(),
                parse_mode="HTML",
            )
    else:
        await callback.message.answer(
            text=caption_text,
            reply_markup=get_payment_keyboard(),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data == "payment:cancel")
async def handle_payment_cancel_callback(callback: CallbackQuery, state: FSMContext):
    """ยกเลิกการส่งสลิปจากปุ่ม Inline Button"""
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "❌ ยกเลิกการทำรายการเรียบร้อยแล้ว\nพิมพ์ /start เพื่อเปิดเมนูหลักอีกครั้ง",
            parse_mode="HTML",
        )
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("ยกเลิกรายการแล้ว")


@router.message(Command("cancel"))
async def handle_cancel_command(message: Message, state: FSMContext):
    """จัดการคำสั่ง /cancel ในระหว่างขั้นตอน FSM"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("ไม่มีรายการที่กำลังดำเนินการอยู่ครับ พิมพ์ /start เพื่อเปิดเมนูหลัก", parse_mode="HTML")
        return

    await state.clear()
    await message.answer("❌ ยกเลิกการทำรายการเรียบร้อยแล้ว พิมพ์ /start เพื่อเปิดเมนูหลัก", parse_mode="HTML")


@router.message(PaymentStates.waiting_for_slip, F.photo)
async def handle_payment_slip_photo(message: Message, state: FSMContext, bot: Bot):
    """จัดการรูปภาพสลิปที่ผู้ใช้ส่งมา และส่งต่อไปยังกลุ่ม Admin เพื่อตรวจสอบ (เวลาไทย)"""
    if not message.from_user or not message.photo:
        return

    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)
    plan_info = PLAN_DETAILS.get(plan_key, PLAN_DETAILS[PlanType.VIP_30D.value])

    telegram_user = message.from_user
    photo = message.photo[-1]
    file_id = photo.file_id

    # 1. บันทึกข้อมูลสลิปลงฐานข้อมูล
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name or telegram_user.first_name,
        )

        slip = PaymentSlip(
            user_id=user.telegram_id,
            file_id=file_id,
            plan_type=plan_key,
            status=SlipStatus.PENDING.value,
        )
        session.add(slip)
        await session.flush()
        slip_id = slip.id

    # 2. ล้างสถานะ FSM
    await state.clear()

    # 3. ส่งข้อความยืนยันให้ผู้ใช้
    await message.answer(
        f"✅ <b>ได้รับสลิปการโอนเงินสำหรับ {plan_info['badge']} เรียบร้อยแล้ว!</b>\n\n"
        "ระบบได้ส่งสลิปให้ทีมงานแอดมินเพื่อตรวจสอบความถูกต้องเรียบร้อยแล้วครับ\n"
        "เมื่อได้รับการอนุมัติ คุณจะได้รับลิงก์เชิญเข้า Channel VIP ในแชทนี้ทันที\n\n"
        "ขอบคุณที่ร่วมเป็นสมาชิก VIP ครับ!",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=telegram_user.id, sender_role="USER", message_text=f"[ส่งรูปภาพสลิปโอนเงิน #{slip_id} ({plan_info['badge']})]")

    # 4. ส่งต่อไปยังกลุ่ม Admin (เวลาไทย)
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    full_name_safe = html.escape(telegram_user.full_name or telegram_user.first_name)
    submitted_time_thai = format_thai_datetime(slip.created_at)

    admin_caption = (
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่!</b>\n\n"
        f"🆔 <b>รหัสสลิป:</b> <code>#{slip_id}</code>\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วัน\n"
        f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n\n"
        "👉 <b>กรุณาตรวจสอบสลิปและเลือกการดำเนินการด้านล่าง:</b>"
    )

    try:
        await bot.send_photo(
            chat_id=config.ADMIN_GROUP_ID,
            photo=file_id,
            caption=admin_caption,
            reply_markup=get_admin_slip_keyboard(slip_id, telegram_user.id),
            parse_mode="HTML",
        )
        logger.info(f"Payment slip #{slip_id} from user {telegram_user.id} forwarded to Admin Group {config.ADMIN_GROUP_ID}")
    except Exception as e:
        logger.error(
            f"Failed to forward payment slip #{slip_id} to Admin Group {config.ADMIN_GROUP_ID}: {e}",
            exc_info=True,
        )


@router.message(PaymentStates.waiting_for_slip, F.document)
async def handle_payment_slip_document(message: Message, state: FSMContext, bot: Bot):
    """จัดการกรณีผู้ใช้ส่งสลิปเป็นไฟล์รูปภาพ (Document) (เวลาไทย)"""
    if not message.from_user or not message.document:
        return

    doc = message.document
    mime = doc.mime_type or ""
    if not mime.startswith("image/"):
        await message.answer(
            "⚠️ กรุณาอัปโหลดไฟล์รูปภาพของสลิปโอนเงิน (JPG, PNG, WEBP) หรือพิมพ์ /cancel เพื่อยกเลิก",
            parse_mode="HTML",
        )
        return

    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)
    plan_info = PLAN_DETAILS.get(plan_key, PLAN_DETAILS[PlanType.VIP_30D.value])

    file_id = doc.file_id
    telegram_user = message.from_user

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name or telegram_user.first_name,
        )

        slip = PaymentSlip(
            user_id=user.telegram_id,
            file_id=file_id,
            plan_type=plan_key,
            status=SlipStatus.PENDING.value,
        )
        session.add(slip)
        await session.flush()
        slip_id = slip.id

    await state.clear()

    await message.answer(
        f"✅ <b>ได้รับไฟล์สลิปการโอนเงินสำหรับ {plan_info['badge']} เรียบร้อยแล้ว!</b>\n\n"
        "ระบบได้ส่งสลิปให้ทีมงานแอดมินเพื่อตรวจสอบความถูกต้องเรียบร้อยแล้วครับ\n"
        "เมื่อได้รับการอนุมัติ คุณจะได้รับลิงก์เชิญเข้า Channel VIP ทางแชทนี้ทันที",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=telegram_user.id, sender_role="USER", message_text=f"[ส่งไฟล์เอกสารสลิป #{slip_id} ({plan_info['badge']})]")

    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    full_name_safe = html.escape(telegram_user.full_name or telegram_user.first_name)
    submitted_time_thai = format_thai_datetime(slip.created_at)

    admin_caption = (
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่ (ไฟล์รูป)!</b>\n\n"
        f"🆔 <b>รหัสสลิป:</b> <code>#{slip_id}</code>\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วัน\n"
        f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n\n"
        "👉 <b>กรุณาตรวจสอบสลิปและเลือกการดำเนินการด้านล่าง:</b>"
    )

    try:
        await bot.send_document(
            chat_id=config.ADMIN_GROUP_ID,
            document=file_id,
            caption=admin_caption,
            reply_markup=get_admin_slip_keyboard(slip_id, telegram_user.id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to forward document slip #{slip_id} to Admin Group: {e}", exc_info=True)


@router.message(F.chat.type == "private", F.photo | F.document)
async def handle_general_user_media(message: Message, bot: Bot):
    """จัดการรูปภาพหรือไฟล์ที่ผู้ใช้ส่งมานอกขั้นตอนการชำระเงิน (ส่งต่อเป็นข้อความ Support ให้แอดมิน)"""
    if not message.from_user:
        return

    telegram_user = message.from_user
    user_id = telegram_user.id
    user_name = html.escape(telegram_user.full_name or telegram_user.first_name)
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    time_now = format_thai_datetime(datetime.now(timezone.utc))
    caption = message.caption or ""

    media_type = "รูปภาพ" if message.photo else "ไฟล์เอกสาร"
    await log_chat_message(user_id=user_id, sender_role="USER", message_text=f"[{media_type}] {caption}")

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user_id}"),
                InlineKeyboardButton(text="👤 ดูข้อมูลสมาชิก", callback_data=f"admin:view_user:{user_id}"),
            ],
        ]
    )

    admin_alert = (
        f"📷 <b>มีผู้ใช้ส่ง{media_type} (Direct Message)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
    )
    if caption:
        admin_alert += f"📝 <b>คำบรรยาย:</b> <i>{html.escape(caption)}</i>\n"
    admin_alert += (
        f"📅 <b>เวลา:</b> <code>{time_now} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>แตะข้อความด้านล่างเพื่อคัดลอกคำสั่งตอบกลับ:</b>\n"
        f"<code>/reply {user_id} </code>"
    )

    try:
        if message.photo:
            await bot.send_photo(
                chat_id=config.ADMIN_GROUP_ID,
                photo=message.photo[-1].file_id,
                caption=admin_alert,
                reply_markup=admin_keyboard,
                parse_mode="HTML",
            )
        elif message.document:
            await bot.send_document(
                chat_id=config.ADMIN_GROUP_ID,
                document=message.document.file_id,
                caption=admin_alert,
                reply_markup=admin_keyboard,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Failed to forward user media to Admin Group: {e}")

    await message.answer(
        f"💬 <b>ระบบได้รับ{media_type}ของคุณเรียบร้อยแล้วครับ</b>\n\n"
        "ทีมงานแอดมินได้รับข้อมูลเรียบร้อยแล้วและจะติดต่อกลับโดยเร็วที่สุดครับ\n"
        "💡 <i>หากคุณต้องการส่งสลิปสมัครสมาชิก VIP กรุณาพิมพ์ /start แล้วกดเลือกแพ็กเกจก่อนส่งสลิปครับ</i>",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=user_id, sender_role="BOT", message_text=f"💬 ระบบได้รับ{media_type}ของคุณเรียบร้อยแล้วครับ")


@router.message(PaymentStates.waiting_for_slip, ~F.text.startswith("/"))
async def handle_invalid_slip_input(message: Message, bot: Bot):
    """แจ้งเตือนหากผู้ใช้ส่งข้อความที่ไม่ใช่รูปภาพเข้ามา และส่งต่อให้แอดมินรับทราบ"""
    if not message.from_user:
        return

    telegram_user = message.from_user
    user_id = telegram_user.id
    msg_text = message.text or (f"[{message.content_type}]" if message.content_type else "[ข้อความ/ไฟล์]")

    # 1. บันทึกข้อความของผู้ใช้
    await log_chat_message(user_id=user_id, sender_role="USER", message_text=msg_text)

    # 2. ส่งต่อเข้ากลุ่มแอดมิน
    user_name = html.escape(telegram_user.full_name or telegram_user.first_name)
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    time_now = format_thai_datetime(datetime.now(timezone.utc))

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user_id}"),
                InlineKeyboardButton(text="👤 ดูข้อมูลสมาชิก", callback_data=f"admin:view_user:{user_id}"),
            ],
        ]
    )

    admin_alert = (
        "💬 <b>มีข้อความจากผู้ใช้ (ระหว่างรอสลิปโอนเงิน)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📝 <b>ข้อความ:</b>\n<i>{html.escape(msg_text)}</i>\n"
        f"📅 <b>เวลา:</b> <code>{time_now} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>แตะข้อความด้านล่างเพื่อคัดลอกคำสั่งตอบกลับ:</b>\n"
        f"<code>/reply {user_id} </code>"
    )
    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_alert,
            reply_markup=admin_keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to forward slip question to Admin Group: {e}")

    # 3. ตอบกลับผู้ใช้
    await message.answer(
        "⚠️ กรุณาส่งรูปภาพหรือสกรีนช็อตของสลิปการโอนเงินครับ\n"
        "หากต้องการยกเลิก ให้พิมพ์คำสั่ง /cancel",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=user_id, sender_role="BOT", message_text="⚠️ กรุณาส่งรูปภาพหรือสกรีนช็อตของสลิปการโอนเงินครับ")
