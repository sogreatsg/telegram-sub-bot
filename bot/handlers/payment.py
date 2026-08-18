import logging
import html
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
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
from bot.models.schema import PaymentSlip, SlipStatus, PlanType, PLAN_DETAILS, get_dynamic_plan_info
from bot.services.database import get_session, get_or_create_user
from bot.services.chat_logger import log_chat_message
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="payment")


class PaymentStates(StatesGroup):
    """FSM states สำหรับกระบวนการชำระเงิน"""
    waiting_for_slip = State()       # รอส่งรูป/ไฟล์สลิป PromptPay
    waiting_for_truemoney = State()  # รอส่งลิงก์ซองของขวัญ TrueMoney (ซองแดง)


def extract_truemoney_url(text: str) -> Optional[str]:
    """สกัดและจัดรูปแบบ URL ซองของขวัญ TrueMoney จากข้อความ"""
    if not text:
        return None
    # match full http/https url
    match = re.search(r'https?://(?:(?:gift|tmn)\.truemoney\.com|tmn\.app\.link)/[^\s]+', text, re.IGNORECASE)
    if match:
        return match.group(0).rstrip('.,;()[]{}')
    # match without http://
    match_no_http = re.search(r'(?:gift|tmn)\.truemoney\.com/[^\s]+', text, re.IGNORECASE)
    if match_no_http:
        return "https://" + match_no_http.group(0).rstrip('.,;()[]{}')
    return None


def get_payment_method_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    """สร้างปุ่มเลือกช่องทางการชำระเงิน (สแกน QR Code หรือ ซองของขวัญ TrueMoney)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📲 สแกน QR Code",
                    callback_data=f"payment:method:promptpay:{plan_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧧 ส่งซองของขวัญ TrueMoney (ซองแดง)",
                    callback_data=f"payment:method:truemoney:{plan_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 กลับสู่เมนูหลัก",
                    callback_data="menu:main",
                ),
            ],
        ]
    )


def get_payment_cancel_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    """สร้างปุ่มเปลี่ยนวิธีชำระและปุ่มยกเลิก"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 เปลี่ยนวิธีชำระเงิน",
                    callback_data=f"menu:subscribe:{plan_key}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ ยกเลิก",
                    callback_data="payment:cancel",
                ),
            ],
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
async def handle_subscribe_plan_button(callback: CallbackQuery, state: FSMContext):
    """แสดงหน้าจอเลือกวิธีชำระเงิน (สแกน QR Code หรือ ซองของขวัญ TrueMoney) สำหรับแพ็กเกจที่เลือก"""
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

    plan_info = get_dynamic_plan_info(plan_key)
    await state.clear()

    method_text = (
        f"💳 <b>เลือกช่องทางชำระเงิน — {plan_info['badge']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ยอดชำระ:</b> <b>{plan_info['price']:,} บาท</b>\n"
        f"⏳ <b>ระยะเวลาสมาชิก:</b> <b>{plan_info['days']} วันเต็ม</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌟 <b>กรุณาเลือกช่องทางที่คุณต้องการชำระเงิน:</b>\n\n"
        "1️⃣ <b>📲 สแกน QR Code</b>\n"
        "   • สแกน QR Code และส่งรูปสลิปโอนเงินเข้ามาในแชท\n\n"
        "2️⃣ <b>🧧 ซองของขวัญ TrueMoney (ซองแดง)</b>\n"
        "   • สร้างซองของขวัญ TrueMoney ตามยอดที่ระบุ และส่งลิงก์เข้ามาในแชท"
    )

    # ส่งหรือแก้ไขข้อความตามความเหมาะสม
    if callback.message.photo or callback.message.document:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text=method_text,
            reply_markup=get_payment_method_keyboard(plan_key),
            parse_mode="HTML",
        )
    else:
        try:
            await callback.message.edit_text(
                text=method_text,
                reply_markup=get_payment_method_keyboard(plan_key),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                text=method_text,
                reply_markup=get_payment_method_keyboard(plan_key),
                parse_mode="HTML",
            )

    await callback.answer()


@router.callback_query(F.data.startswith("payment:method:promptpay:"))
async def handle_payment_method_promptpay(callback: CallbackQuery, state: FSMContext):
    """เริ่มขั้นตอนการชำระเงินด้วย QR Code"""
    if not callback.from_user or not callback.message:
        return

    plan_key = callback.data.split(":")[-1]
    plan_info = get_dynamic_plan_info(plan_key)

    await state.set_state(PaymentStates.waiting_for_slip)
    await state.update_data(plan_type=plan_key, payment_method="PROMPTPAY")

    caption_text = (
        f"💳 <b>สมัครสมาชิก {plan_info['badge']} (ราคา {plan_info['price']:,} บาท)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วันเต็ม\n"
        "📲 <b>สแกน QR Code ด้านบนเพื่อชำระเงิน</b>\n"
        f"• ยอดชำระ: <b>{plan_info['price']:,} บาท</b>\n"
        "• สแกนจ่ายผ่านแอปธนาคารได้ทุกธนาคารทันที\n\n"
        "📸 <b>ขั้นตอนถัดไป:</b>\n"
        "เมื่อโอนเงินเรียบร้อยแล้ว กรุณาส่งรูปสลิปเข้ามาในแชทนี้ได้เลยครับ\n\n"
        "💡 <i>คำแนะนำ: คุณสามารถกด 'เปลี่ยนวิธีชำระ' หรือพิมพ์ /cancel เพื่อยกเลิกได้ครับ</i>"
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
                reply_markup=get_payment_cancel_keyboard(plan_key),
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
                reply_markup=get_payment_cancel_keyboard(plan_key),
                parse_mode="HTML",
            )
    else:
        await callback.message.answer(
            text=caption_text,
            reply_markup=get_payment_cancel_keyboard(plan_key),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data.startswith("payment:method:truemoney:"))
async def handle_payment_method_truemoney(callback: CallbackQuery, state: FSMContext):
    """เริ่มขั้นตอนการชำระเงินด้วยซองของขวัญ TrueMoney (ซองแดง)"""
    if not callback.from_user or not callback.message:
        return

    plan_key = callback.data.split(":")[-1]
    plan_info = get_dynamic_plan_info(plan_key)

    await state.set_state(PaymentStates.waiting_for_truemoney)
    await state.update_data(plan_type=plan_key, payment_method="TRUEMONEY_ANGPAO")

    truemoney_text = (
        f"🧧 <b>ชำระเงินผ่านซองของขวัญ TrueMoney (ซองแดง)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>แพ็กเกจ:</b> <b>{plan_info['badge']}</b>\n"
        f"💰 <b>ยอดเงินที่ต้องสร้างซอง:</b> <b>{plan_info['price']:,} บาท</b> (ระบุยอดให้ตรงเท่านั้น)\n"
        f"⏳ <b>ระยะเวลา:</b> <b>{plan_info['days']} วันเต็ม</b>\n"
        f"👥 <b>การตั้งค่าซอง:</b> สุ่มยอดเงินเท่ากัน / จำนวนผู้รับ <b>1 คน</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>วิธีสร้างและส่งซองของขวัญ TrueMoney:</b>\n"
        f"1. เปิดแอป <b>TrueMoney Wallet</b> บนมือถือ\n"
        f"2. เลือกเมนู <b>'โอน/ถอน'</b> ➔ เลือก <b>'ส่งซองของขวัญ' (ซองแดง)</b>\n"
        f"3. กรอกยอดเงิน <b>{plan_info['price']:,} บาท</b>\n"
        f"4. เลือกประเภท <b>'แบ่งจำนวนเงินเท่ากัน'</b> และใส่จำนวนคนรับ <b>1 คน</b>\n"
        f"5. กดยืนยันสร้างซอง และ <b>คัดลอกลิงก์ซองของขวัญ</b>\n"
        f"6. <b>ส่งหรือวาง (Paste) ลิงก์ซองของขวัญเข้ามาในแชทนี้</b> ได้เลยครับ\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>คำแนะนำ: คุณสามารถกด 'เปลี่ยนวิธีชำระ' หรือพิมพ์ /cancel เพื่อยกเลิกได้ครับ</i>"
    )

    if callback.message.photo or callback.message.document:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text=truemoney_text,
            reply_markup=get_payment_cancel_keyboard(plan_key),
            parse_mode="HTML",
        )
    else:
        try:
            await callback.message.edit_text(
                text=truemoney_text,
                reply_markup=get_payment_cancel_keyboard(plan_key),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                text=truemoney_text,
                reply_markup=get_payment_cancel_keyboard(plan_key),
                parse_mode="HTML",
            )

    await callback.answer()


@router.callback_query(F.data == "payment:cancel")
async def handle_payment_cancel_callback(callback: CallbackQuery, state: FSMContext):
    """ยกเลิกการทำรายการชำระเงินจากปุ่ม Inline Button"""
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


async def process_truemoney_submission(
    message: Message,
    state: FSMContext,
    bot: Bot,
    angpao_url: str,
):
    """ฟังก์ชันประมวลผลการส่งลิงก์ซองของขวัญ TrueMoney และส่งต่อไปยังกลุ่ม Admin (เฉพาะเมื่อเลือกแพ็กเกจแล้ว)"""
    telegram_user = message.from_user
    if not telegram_user:
        return

    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)
    plan_info = get_dynamic_plan_info(plan_key)

    # 1. บันทึกลงฐานข้อมูล
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name or telegram_user.first_name,
        )

        slip = PaymentSlip(
            user_id=user.telegram_id,
            file_id=angpao_url,
            plan_type=plan_key,
            payment_method="TRUEMONEY_ANGPAO",
            status=SlipStatus.PENDING.value,
        )
        session.add(slip)
        await session.flush()
        slip_id = slip.id

    # 2. ล้างสถานะ FSM
    await state.clear()

    # 3. แจ้งผู้ใช้
    await message.answer(
        f"✅ <b>ได้รับลิงก์ซองของขวัญ TrueMoney สำหรับ {plan_info['badge']} เรียบร้อยแล้ว!</b>\n\n"
        f"🔗 <b>ลิงก์ที่ส่ง:</b> <code>{html.escape(angpao_url)}</code>\n\n"
        "ระบบได้ส่งลิงก์ให้ทีมงานแอดมินเพื่อกดรับและตรวจสอบยอดเงินเรียบร้อยแล้วครับ\n"
        "เมื่อได้รับการอนุมัติ คุณจะได้รับลิงก์เชิญเข้า Channel VIP ในแชทนี้ทันที\n\n"
        "ขอบคุณที่ร่วมเป็นสมาชิก VIP ครับ!",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await log_chat_message(
        user_id=telegram_user.id,
        sender_role="USER",
        message_text=f"[ส่งลิงก์ซองของขวัญ TrueMoney #{slip_id} ({plan_info['badge']}): {angpao_url}]"
    )

    # 4. ส่งต่อไปยังกลุ่ม Admin
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    full_name_safe = html.escape(telegram_user.full_name or telegram_user.first_name)
    submitted_time_thai = format_thai_datetime(slip.created_at)

    admin_text = (
        "🧧 <b>มีการชำระเงินใหม่ผ่าน ซองของขวัญ TrueMoney!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
        f"🆔 <b>รหัสรายการ:</b> <code>#{slip_id}</code>\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
        f"⏳ <b>ระยะเวลา:</b> {plan_info['days']} วัน\n"
        f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>ลิงก์ซองของขวัญ (TrueMoney Angpao):</b>\n"
        f"👉 <a href=\"{angpao_url}\">{html.escape(angpao_url)}</a>\n\n"
        f"📋 <b>แตะเพื่อคัดลอกลิงก์:</b>\n"
        f"<code>{angpao_url}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 <b>กรุณากดรับซองเพื่อตรวจสอบยอดเงิน ({plan_info['price']:,} บาท) แล้วกดอนุมัติด้านล่าง:</b>"
    )

    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_text,
            reply_markup=get_admin_slip_keyboard(slip_id, telegram_user.id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"TrueMoney Angpao #{slip_id} from user {telegram_user.id} forwarded to Admin Group {config.ADMIN_GROUP_ID}")
    except Exception as e:
        logger.error(
            f"Failed to forward TrueMoney Angpao #{slip_id} to Admin Group {config.ADMIN_GROUP_ID}: {e}",
            exc_info=True,
        )


@router.message(PaymentStates.waiting_for_truemoney, F.text, ~F.text.startswith("/"))
async def handle_truemoney_angpao_input(message: Message, state: FSMContext, bot: Bot):
    """จัดการข้อความที่ผู้ใช้ส่งมาระหว่างรอลิงก์ซองของขวัญ TrueMoney"""
    if not message.from_user or not message.text:
        return

    text_input = message.text.strip()
    angpao_url = extract_truemoney_url(text_input)

    if angpao_url:
        await process_truemoney_submission(message=message, state=state, bot=bot, angpao_url=angpao_url)
        return

    # กรณีส่งข้อความธรรมดาที่ไม่ใช่ลิงก์ TrueMoney
    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)

    await log_chat_message(user_id=message.from_user.id, sender_role="USER", message_text=text_input)
    await message.answer(
        "⚠️ <b>ไม่พบลิงก์ซองของขวัญ TrueMoney ที่ถูกต้อง</b>\n\n"
        "กรุณาส่งลิงก์ซองของขวัญ เช่น:\n"
        "<code>https://gift.truemoney.com/campaign/?v=...</code>\n\n"
        "💡 <i>คุณสามารถกดปุ่มด้านล่างเพื่อเปลี่ยนวิธีชำระเงิน หรือพิมพ์ /cancel เพื่อยกเลิกครับ</i>",
        reply_markup=get_payment_cancel_keyboard(plan_key),
        parse_mode="HTML",
    )


@router.message(PaymentStates.waiting_for_slip, F.photo)
@router.message(PaymentStates.waiting_for_truemoney, F.photo)
async def handle_payment_slip_photo(message: Message, state: FSMContext, bot: Bot):
    """จัดการรูปภาพสลิปที่ผู้ใช้ส่งมา และส่งต่อไปยังกลุ่ม Admin เพื่อตรวจสอบ (เวลาไทย)"""
    if not message.from_user or not message.photo:
        return

    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)
    plan_info = get_dynamic_plan_info(plan_key)

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
            payment_method="PROMPTPAY",
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
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
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
@router.message(PaymentStates.waiting_for_truemoney, F.document)
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
    plan_info = get_dynamic_plan_info(plan_key)

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
            payment_method="PROMPTPAY",
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
        "🔔 <b>มีการส่งสลิปชำระเงินใหม่ (ไฟล์รูป)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
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


@router.message(PaymentStates.waiting_for_slip, F.text, ~F.text.startswith("/"))
async def handle_slip_text_input(message: Message, state: FSMContext, bot: Bot):
    """จัดการข้อความตัวอักษรระหว่างรอสลิป (หากเป็นลิงก์ TrueMoney จะรับเป็นซองของขวัญอัตโนมัติ)"""
    if not message.from_user or not message.text:
        return

    text_input = message.text.strip()
    angpao_url = extract_truemoney_url(text_input)

    if angpao_url:
        # ผู้ใช้ส่งลิงก์ซองของขวัญ TrueMoney มาในหน้ารอสลิป -> ประมวลผลเป็นซองของขวัญทันที
        await process_truemoney_submission(message=message, state=state, bot=bot, angpao_url=angpao_url)
        return

    telegram_user = message.from_user
    user_id = telegram_user.id
    msg_text = message.text

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
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n"
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
    fsm_data = await state.get_data()
    plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)

    await message.answer(
        "⚠️ <b>กรุณาส่งรูปภาพสลิปการโอนเงิน หรือลิงก์ซองของขวัญ TrueMoney ครับ</b>\n\n"
        "💡 <i>คุณสามารถกดปุ่มด้านล่างเพื่อเปลี่ยนวิธีชำระเงิน หรือพิมพ์ /cancel เพื่อยกเลิกครับ</i>",
        reply_markup=get_payment_cancel_keyboard(plan_key),
        parse_mode="HTML",
    )
    await log_chat_message(user_id=user_id, sender_role="BOT", message_text="⚠️ กรุณาส่งรูปภาพสลิปการโอนเงิน หรือลิงก์ซองของขวัญ TrueMoney ครับ")


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
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n"
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
        "💡 <i>หากคุณต้องการสมัครสมาชิก VIP กรุณาพิมพ์ /start แล้วกดเลือกแพ็กเกจก่อนส่งสลิปหรือลิงก์ซองของขวัญครับ</i>",
        parse_mode="HTML",
    )
    await log_chat_message(user_id=user_id, sender_role="BOT", message_text=f"💬 ระบบได้รับ{media_type}ของคุณเรียบร้อยแล้วครับ")
