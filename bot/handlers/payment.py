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
from bot.models.schema import PaymentSlip, SlipStatus
from bot.services.database import get_session, get_or_create_user
from bot.services.chat_logger import log_chat_message

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="payment")

BANGKOK_TZ = timezone(timedelta(hours=7))


def format_thai_datetime(dt: datetime) -> str:
    """แปลงเวลาเป็นเวลาไทย (UTC+7) รูปแบบ วัน/เดือน/ปี ชั่วโมง:นาที:วินาที"""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    thai_dt = dt.astimezone(BANGKOK_TZ)
    return thai_dt.strftime("%d/%m/%Y %H:%M:%S")


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


def get_admin_slip_keyboard(slip_id: int) -> InlineKeyboardMarkup:
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
            ]
        ]
    )


@router.callback_query(F.data == "menu:subscribe_30d")
async def handle_subscribe_30d_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """เริ่มขั้นตอนการสมัครสมาชิก VIP พร้อมส่งรูป QR Code PromptPay 300 บาท"""
    if not callback.from_user or not callback.message:
        return

    await state.set_state(PaymentStates.waiting_for_slip)

    qr_path = Path(config.PAYMENT_QR_PATH)
    
    caption_text = (
        "💳 <b>สมัครสมาชิก VIP (ราคา 300 บาท)</b>\n\n"
        "📲 <b>สแกน QR Code พร้อมเพย์ด้านบนเพื่อชำระเงิน</b>\n"
        "• ยอดชำระ: <b>300 บาท</b>\n"
        "• สแกนจ่ายผ่านแอปธนาคารได้ทันที ไม่ต้องกรอกเลขบัญชีเอง\n\n"
        "📸 <b>ขั้นตอนถัดไป:</b>\n"
        "เมื่อโอนเงินเรียบร้อยแล้ว กรุณาส่งรูปสลิปเข้ามาในแชทนี้ได้เลยครับ\n\n"
        "💡 <i>คำแนะนำ: คุณสามารถพิมพ์ /cancel ได้ตลอดเวลาหากต้องการยกเลิก</i>"
    )

    if qr_path.exists():
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
            status=SlipStatus.PENDING.value,
        )
        session.add(slip)
        await session.flush()
        slip_id = slip.id

    # 2. ล้างสถานะ FSM
    await state.clear()

    # 3. ส่งข้อความยืนยันให้ผู้ใช้
    await message.answer(
        "✅ <b>ได้รับสลิปการโอนเงินเรียบร้อยแล้ว!</b>\n\n"
        "ระบบได้ส่งสลิปให้ทีมงานแอดมินเพื่อตรวจสอบความถูกต้องเรียบร้อยแล้วครับ\n"
        "เมื่อได้รับการอนุมัติ คุณจะได้รับลิงก์เชิญเข้า Channel VIP ในแชทนี้ทันที\n\n"
        "ขอบคุณที่ร่วมเป็นสมาชิก VIP ครับ!",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=telegram_user.id, sender_role="USER", message_text=f"[ส่งรูปภาพสลิปโอนเงิน #{slip_id}]")

    # 4. ส่งต่อไปยังกลุ่ม Admin (เวลาไทย)
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    full_name_safe = html.escape(telegram_user.full_name or telegram_user.first_name)
    submitted_time_thai = format_thai_datetime(slip.created_at)

    admin_caption = (
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่!</b>\n\n"
        f"🆔 <b>รหัสสลิป:</b> <code>#{slip_id}</code>\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่ขอ:</b> สมาชิก VIP (300 บาท)\n"
        f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n\n"
        "👉 <b>กรุณาตรวจสอบสลิปและเลือกการดำเนินการด้านล่าง:</b>"
    )

    try:
        await bot.send_photo(
            chat_id=config.ADMIN_GROUP_ID,
            photo=file_id,
            caption=admin_caption,
            reply_markup=get_admin_slip_keyboard(slip_id),
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
            status=SlipStatus.PENDING.value,
        )
        session.add(slip)
        await session.flush()
        slip_id = slip.id

    await state.clear()

    await message.answer(
        "✅ <b>ได้รับไฟล์สลิปการโอนเงินเรียบร้อยแล้ว!</b>\n\n"
        "ระบบได้ส่งสลิปให้ทีมงานแอดมินเพื่อตรวจสอบความถูกต้องเรียบร้อยแล้วครับ\n"
        "เมื่อได้รับการอนุมัติ คุณจะได้รับลิงก์เชิญเข้า Channel VIP ทางแชทนี้ทันที",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=telegram_user.id, sender_role="USER", message_text=f"[ส่งไฟล์เอกสารสลิป #{slip_id}]")

    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    full_name_safe = html.escape(telegram_user.full_name or telegram_user.first_name)
    submitted_time_thai = format_thai_datetime(slip.created_at)

    admin_caption = (
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่ (ไฟล์รูป)!</b>\n\n"
        f"🆔 <b>รหัสสลิป:</b> <code>#{slip_id}</code>\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่ขอ:</b> สมาชิก VIP (300 บาท)\n"
        f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n\n"
        "👉 <b>กรุณาตรวจสอบสลิปและเลือกการดำเนินการด้านล่าง:</b>"
    )

    try:
        await bot.send_document(
            chat_id=config.ADMIN_GROUP_ID,
            document=file_id,
            caption=admin_caption,
            reply_markup=get_admin_slip_keyboard(slip_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to forward document slip #{slip_id} to Admin Group: {e}", exc_info=True)


@router.message(PaymentStates.waiting_for_slip)
async def handle_invalid_slip_input(message: Message):
    """แจ้งเตือนหากผู้ใช้ส่งข้อความที่ไม่ใช่รูปภาพเข้ามา"""
    await message.answer(
        "⚠️ กรุณาส่งรูปภาพหรือสกรีนช็อตของสลิปการโอนเงินครับ\n"
        "หากต้องการยกเลิก ให้พิมพ์คำสั่ง /cancel",
        parse_mode="HTML",
    )
