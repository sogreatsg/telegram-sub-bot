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

import asyncio
from collections import defaultdict
from sqlalchemy import select
from bot.config import get_settings
from bot.models.schema import PaymentSlip, SlipStatus, PlanType, GrantType, PLAN_DETAILS, get_dynamic_plan_info, format_plan_duration, User
from bot.services.database import get_session, get_or_create_user
from bot.services.chat_logger import log_chat_message
from bot.services.channel_service import is_user_v2_member, get_user_target_channel_id, get_channel_label, unban_user_in_channel
from bot.services.payment_settings import is_promptpay_active, is_truemoney_active, is_auto_approve_active
from bot.services.subscription import grant_subscription, parse_plan_days
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime, format_remaining_time
from aiogram.enums import ChatMemberStatus

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="payment")

# Concurrency lock ป้องกันการส่งสลิปหรือซองของขวัญซ้ำซ้อนในเวลาเดียวกัน
submission_locks = defaultdict(asyncio.Lock)


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
    """สร้างปุ่มเลือกช่องทางการชำระเงินตามที่เปิดใช้งาน (สแกน QR Code หรือ ซองของขวัญ TrueMoney)"""
    buttons = []
    if is_promptpay_active():
        buttons.append([
            InlineKeyboardButton(
                text="📲 สแกน QR Code",
                callback_data=f"payment:method:promptpay:{plan_key}",
            )
        ])
    if is_truemoney_active():
        buttons.append([
            InlineKeyboardButton(
                text="🧧 ส่งซองของขวัญ TrueMoney (ซองแดง)",
                callback_data=f"payment:method:truemoney:{plan_key}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="🔙 กลับสู่เมนูหลัก",
            callback_data="menu:main",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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


def get_admin_auto_approved_slip_keyboard(slip_id: int, user_id: int) -> InlineKeyboardMarkup:
    """สร้างปุ่มสำหรับรายการที่ระบบอนุมัติอัตโนมัติ (Auto-Approved) ในกลุ่ม Admin"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 ดูข้อมูลสมาชิก",
                    callback_data=f"admin:view_user:{user_id}",
                ),
                InlineKeyboardButton(
                    text="📜 ดูประวัติการคุย",
                    callback_data=f"admin:view_chat:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ ปฏิเสธ/ยกเลิกรายการนี้",
                    callback_data=f"admin:reject_auto:{slip_id}",
                ),
            ]
        ]
    )



@router.callback_query(F.data.startswith("menu:subscribe"))
async def handle_subscribe_plan_button(callback: CallbackQuery, state: FSMContext):
    """แสดงหน้าจอเลือกวิธีชำระเงิน (สแกน QR Code หรือ ซองของขวัญ TrueMoney) สำหรับแพ็กเกจที่เลือก (เฉพาะสมาชิก V.2)"""
    if not callback.from_user or not callback.message:
        return

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )
        if not is_user_v2_member(user):
            await callback.answer()
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
    duration_str = format_plan_duration(plan_info)
    await state.clear()

    has_qr = is_promptpay_active()
    has_tmn = is_truemoney_active()

    if not has_qr and not has_tmn:
        await callback.answer("⚠️ ระบบรับชำระเงินปิดปรับปรุงชั่วคราว", show_alert=True)
        disabled_text = (
            f"⚠️ <b>ระบบชำระเงินปิดปรับปรุงชั่วคราว — {plan_info['badge']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ขณะนี้ระบบรับชำระเงินปิดปรับปรุงชั่วคราว ขออภัยในความไม่สะดวกครับ\n"
            "กรุณาติดต่อแอดมินหรือลองใหม่อีกครั้งในภายหลังครับ 🙏"
        )
        await callback.message.edit_text(
            text=disabled_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 กลับสู่เมนูหลัก", callback_data="menu:main")]]),
            parse_mode="HTML",
        )
        return

    method_text = (
        f"💳 <b>เลือกช่องทางชำระเงิน — {plan_info['badge']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ยอดชำระ:</b> <b>{plan_info['price']:,} บาท</b>\n"
        f"⏳ <b>ระยะเวลาสมาชิก:</b> <b>{duration_str}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌟 <b>กรุณาเลือกช่องทางที่คุณต้องการชำระเงิน:</b>\n\n"
    )

    if has_qr and has_tmn:
        method_text += (
            "1️⃣ <b>📲 สแกน QR Code</b>\n"
            "   • ขอรับ QR Code รายครั้งและส่งรูปสลิปโอนเงินเข้ามาในแชท\n\n"
            "2️⃣ <b>🧧 ซองของขวัญ TrueMoney (ซองแดง)</b>\n"
            "   • สร้างซองของขวัญ TrueMoney ตามยอดที่ระบุ และส่งลิงก์เข้ามาในแชท"
        )
    elif has_tmn and not has_qr:
        method_text += (
            "🧧 <b>ซองของขวัญ TrueMoney (ซองแดง)</b>\n"
            "   • สร้างซองของขวัญ TrueMoney ตามยอดที่ระบุ และส่งลิงก์เข้ามาในแชท\n\n"
            "⚠️ <i>(ขณะนี้ระบบปิดรับชำระผ่าน QR Code ชั่วคราว)</i>"
        )
    else:
        method_text += (
            "📲 <b>สแกน QR Code</b>\n"
            "   • ขอรับ QR Code รายครั้งและส่งรูปสลิปโอนเงินเข้ามาในแชท"
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
async def handle_payment_method_promptpay(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """เริ่มขั้นตอนการชำระเงินด้วย QR Code (เฉพาะสมาชิก V.2) — แจ้งเตือนแอดมินสร้าง QR Code รายครั้ง"""
    if not callback.from_user or not callback.message:
        return

    plan_key = callback.data.split(":")[-1]
    if not is_promptpay_active():
        await callback.answer("⚠️ ระบบชำระเงินผ่าน QR Code ปิดให้บริการชั่วคราว", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "⚠️ <b>ระบบชำระเงินผ่าน QR Code ปิดให้บริการชั่วคราว</b>\n\n"
                    "ขณะนี้ระบบรับชำระผ่าน QR Code ปิดปรับปรุงชั่วคราว กรุณาเลือกชำระผ่านซองของขวัญ TrueMoney แทนครับ 🙏",
                    reply_markup=get_payment_method_keyboard(plan_key),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )
        if not is_user_v2_member(user):
            await callback.answer()
            return

    plan_info = get_dynamic_plan_info(plan_key)
    duration_str = format_plan_duration(plan_info)

    await state.set_state(PaymentStates.waiting_for_slip)
    await state.update_data(plan_type=plan_key, payment_method="PROMPTPAY")

    # 1. ข้อความแจ้งเตือนฝั่งผู้ใช้ใน DM
    user_waiting_text = (
        f"💳 <b>สมัครสมาชิก {plan_info['badge']} (ราคา {plan_info['price']:,} บาท)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <b>กรุณารอระบบสร้าง QR Code สำหรับชำระเงินสักครู่...</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ยอดชำระ:</b> <b>{plan_info['price']:,} บาท</b>\n"
        f"⏳ <b>ระยะเวลา:</b> <b>{duration_str}</b>\n\n"
        "🔔 <i>ระบบได้ส่งคำขอไปยังแอดมินเพื่อสร้าง QR Code สำหรับการชำระเงินของคุณแล้วครับ (QR Code แบบใช้ครั้งเดียวต่อรอบ)</i>\n"
        "📸 <i>เมื่อแอดมินส่งรูป QR Code มาในแชทนี้แล้ว คุณสามารถสแกนชำระเงินและส่งรูปสลิปเข้ามาได้ทันทีครับ 🙏</i>\n\n"
        "💡 <i>คำแนะนำ: คุณสามารถกด 'เปลี่ยนวิธีชำระ' หรือพิมพ์ /cancel เพื่อยกเลิกได้ครับ</i>"
    )

    if callback.message.photo or callback.message.document:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text=user_waiting_text,
            reply_markup=get_payment_cancel_keyboard(plan_key),
            parse_mode="HTML",
        )
    else:
        try:
            await callback.message.edit_text(
                text=user_waiting_text,
                reply_markup=get_payment_cancel_keyboard(plan_key),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                text=user_waiting_text,
                reply_markup=get_payment_cancel_keyboard(plan_key),
                parse_mode="HTML",
            )

    await callback.answer("แจ้งแอดมินสร้าง QR Code เรียบร้อยแล้ว")

    # 2. บันทึกข้อความลง Log
    await log_chat_message(
        user_id=callback.from_user.id,
        sender_role="USER",
        message_text=f"[กดขอ QR Code ชำระเงิน: {plan_info['badge']} ({plan_info['price']:,} บาท)]"
    )

    # 3. ส่งการแจ้งเตือนไปยังกลุ่ม Admin พร้อมแท็กแอดมินให้สร้าง QR Code รายครั้ง
    user_handle = f"@{callback.from_user.username}" if callback.from_user.username else "ไม่มี Username"
    full_name_safe = html.escape(callback.from_user.full_name or callback.from_user.first_name)
    req_time_thai = format_thai_datetime(datetime.now(timezone.utc))

    admin_alert_text = (
        "📲 <b>มีคำขอสร้าง QR Code ชำระเงินใหม่!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{callback.from_user.id}</code>\n"
        f"📦 <b>แพ็กเกจที่เลือก:</b> <b>{plan_info['badge']}</b>\n"
        f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
        f"💰 <b>ยอดเงินที่ต้องสร้าง QR:</b> <b>{plan_info['price']:,} บาท</b>\n"
        f"📅 <b>เวลาที่ขอ:</b> <code>{req_time_thai} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👉 <b>กรุณาสร้าง QR Code แบบครั้งเดียว (ยอด {plan_info['price']:,} บาท) แล้วส่งให้ผู้ใช้:</b>\n\n"
        "📝 <b>วิธีส่ง QR Code ให้ผู้ใช้:</b>\n"
        "1️⃣ <b>ปัดขวาตอบกลับ (Reply) ข้อความนี้ พร้อมแนบรูป QR Code</b>\n"
        f"2️⃣ หรือส่งรูปภาพพร้อมแคปชั่น <code>/reply {callback.from_user.id}</code>\n"
        f"3️⃣ หรือพิมพ์ <code>/reply {callback.from_user.id} [ข้อความ]</code>"
    )

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 ดูข้อมูลสมาชิก", callback_data=f"admin:view_user:{callback.from_user.id}"),
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{callback.from_user.id}"),
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_alert_text,
            reply_markup=admin_keyboard,
            parse_mode="HTML",
        )
        logger.info(f"QR generation request for User {callback.from_user.id} ({plan_info['badge']}) forwarded to Admin Group {config.ADMIN_GROUP_ID}")
    except Exception as e:
        logger.error(f"Failed to forward QR generation request to Admin Group: {e}", exc_info=True)


@router.callback_query(F.data.startswith("payment:method:truemoney:"))
async def handle_payment_method_truemoney(callback: CallbackQuery, state: FSMContext):
    """เริ่มขั้นตอนการชำระเงินด้วยซองของขวัญ TrueMoney (ซองแดง) (เฉพาะสมาชิก V.2)"""
    if not callback.from_user or not callback.message:
        return

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )
        if not is_user_v2_member(user):
            await callback.answer()
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
        f"⏳ <b>ระยะเวลา:</b> <b>{format_plan_duration(plan_info)}</b>\n"
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
    """ยกเลิกการทำรายการชำระเงินจากปุ่ม Inline Button (เฉพาะสมาชิก V.2)"""
    await state.clear()
    if not callback.from_user:
        return

    async with get_session() as session:
        user = await session.get(User, callback.from_user.id)
        if not is_user_v2_member(user):
            await callback.answer()
            return

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
    """จัดการคำสั่ง /cancel ในระหว่างขั้นตอน FSM (เฉพาะสมาชิก V.2)"""
    if not message.from_user:
        return

    async with get_session() as session:
        user = await session.get(User, message.from_user.id)
        if not is_user_v2_member(user):
            await state.clear()
            return

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
    """ฟังก์ชันประมวลผลการส่งลิงก์ซองของขวัญ TrueMoney และส่งต่อไปยังกลุ่ม Admin (รองรับระบบอนุมัติอัตโนมัติ Auto-Approve)"""
    telegram_user = message.from_user
    if not telegram_user:
        return

    async with submission_locks[telegram_user.id]:
        fsm_data = await state.get_data()
        plan_key = fsm_data.get("plan_type", PlanType.VIP_30D.value)
        plan_info = get_dynamic_plan_info(plan_key)
        auto_approved = is_auto_approve_active()
        now = datetime.now(timezone.utc)

        is_stack_extension = False
        is_in_channel = False
        new_expires_at = None
        target_channel_id = None
        target_channel_label = ""
        invite_url = None

        # 1. บันทึกลงฐานข้อมูล (ตรวจสอบรายการซ้ำก่อน)
        async with get_session() as session:
            dup_stmt = select(PaymentSlip).where(
                PaymentSlip.user_id == telegram_user.id,
                PaymentSlip.file_id == angpao_url,
            )
            existing_dup = (await session.execute(dup_stmt)).scalars().first()
            if existing_dup:
                await state.clear()
                dup_status_msg = "ได้รับการอนุมัติเรียบร้อยแล้ว" if existing_dup.status == SlipStatus.APPROVED.value else "กำลังรอการตรวจสอบ"
                await message.answer(
                    f"ℹ️ <b>คุณได้ส่งลิงก์ซองของขวัญนี้เข้าระบบไว้แล้วครับ (รายการ #{existing_dup.id})</b>\n\n"
                    f"สถานะปัจจุบัน: <b>{dup_status_msg}</b> ขอบคุณครับ 🙏",
                    parse_mode="HTML",
                )
                return

            user, _ = await get_or_create_user(
                session=session,
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                full_name=telegram_user.full_name or telegram_user.first_name,
            )

            slip_status = SlipStatus.APPROVED.value if auto_approved else SlipStatus.PENDING.value
            slip = PaymentSlip(
                user_id=user.telegram_id,
                file_id=angpao_url,
                plan_type=plan_key,
                payment_method="TRUEMONEY_ANGPAO",
                status=slip_status,
                admin_id=None,
            )
            session.add(slip)
            await session.flush()
            slip_id = slip.id

            if auto_approved:
                # ระบบ Auto-Approve: ทำการ Grant Subscription ทันที
                additional_days, additional_minutes = parse_plan_days(plan_key)
                grant_type_value = GrantType.PROMOTION.value if plan_key == PlanType.PROMOTION.value else GrantType.PURCHASE.value

                target_channel_id = get_user_target_channel_id(user)
                target_channel_label = get_channel_label(target_channel_id)

                # ตรวจสอบสถานะจริงใน Channel เป้าหมาย
                try:
                    chat_member = await bot.get_chat_member(chat_id=target_channel_id, user_id=telegram_user.id)
                    is_in_channel = chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
                except Exception:
                    is_in_channel = False

                grant = await grant_subscription(
                    session,
                    user_id=telegram_user.id,
                    days=additional_days,
                    minutes=additional_minutes,
                    source_label=f"สมาชิก {plan_info['badge']}",
                    grant_type=grant_type_value,
                    has_value=True,
                    is_in_channel=is_in_channel,
                )
                is_stack_extension = grant.is_stack_extension
                new_expires_at = grant.new_expires_at

        # 2. ล้างสถานะ FSM
        await state.clear()

    # 3. แจ้งผู้ใช้
    is_v2 = is_user_v2_member(user)
    plan_desc = format_plan_duration(plan_info)

    if auto_approved:
        # กรณีอนุมัติอัตโนมัติ (Auto-Approve) -> ส่งข้อความอนุมัติและลิงก์เข้าแชแนลให้ผู้ใช้ทันที
        if is_v2:
            if is_stack_extension and new_expires_at:
                exp_thai = format_thai_datetime(new_expires_at)
                time_rem = format_remaining_time(new_expires_at)
                user_message = (
                    "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว (อนุมัติอัตโนมัติ ⚡)!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 <b>แพ็กเกจที่ซื้อเพิ่ม:</b> {plan_info['badge']} (+{plan_desc})\n"
                    "⏳ <b>ระบบได้ต่อเวลาสะสมให้คุณเรียบร้อยแล้ว!</b>\n"
                    f"📅 <b>วันหมดอายุใหม่ของคุณ:</b> <code>{exp_thai} น.</code>\n"
                    f"⏰ <b>เวลาคงเหลือรวม:</b> {time_rem}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"💡 <i>คุณสามารถรับชมเนื้อหาใน {target_channel_label} ได้ต่อเนื่องทันทีโดยไม่ต้องกดเข้าห้องใหม่ครับ! 🚀</i>"
                )
                try:
                    await message.answer(user_message, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"Could not send auto-approve stacked DM to User {telegram_user.id}: {e}")

            elif is_in_channel:
                exp_thai = format_thai_datetime(new_expires_at) if new_expires_at else "-"
                user_message = (
                    "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว (อนุมัติอัตโนมัติ ⚡)!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 <b>แพ็กเกจ:</b> <b>{plan_info['badge']} ({plan_desc})</b> เปิดใช้งานให้ทันทีแล้วครับ\n"
                    f"📅 <b>วันหมดอายุ:</b> <code>{exp_thai} น.</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"💡 <i>คุณอยู่ใน {target_channel_label} อยู่แล้ว สามารถใช้งานต่อได้ทันทีโดยไม่ต้องกดลิงก์ใหม่ครับ! 🚀</i>"
                )
                try:
                    await message.answer(user_message, parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"Could not send auto-approve in-channel DM to User {telegram_user.id}: {e}")

            else:
                # ปลดแบนผู้ใช้ใน Channel ก่อนสร้างลิงก์เสมอ
                await unban_user_in_channel(bot, target_channel_id, telegram_user.id)

                # สร้างลิงก์เชิญแบบ 1 ครั้งให้ผู้ใช้
                try:
                    invite_link_obj = await bot.create_chat_invite_link(
                        chat_id=target_channel_id,
                        member_limit=1,
                        expire_date=now + timedelta(days=7),
                        name=f"VIP-{telegram_user.id}",
                    )
                    invite_url = invite_link_obj.invite_link
                except Exception as e:
                    logger.error(f"Failed to generate auto-approve invite link for user {telegram_user.id} in {target_channel_id}: {e}", exc_info=True)
                    invite_url = None

                if invite_url:
                    user_message = (
                        "🎉 <b>การชำระเงินได้รับการอนุมัติเรียบร้อยแล้ว (อนุมัติอัตโนมัติ ⚡)!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"📦 <b>แพ็กเกจ:</b> <b>{plan_info['badge']} ({plan_desc})</b> พร้อมใช้งานแล้วครับ\n\n"
                        f"📢 <b>ห้องสำหรับสมาชิก:</b> <b>{target_channel_label}</b>\n\n"
                        "📌 <b>ข้อควรทราบ:</b>\n"
                        "• สามารถกดปุ่มเข้าร่วมได้เพียง 1 ครั้งเท่านั้น\n"
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
                        sent_msg = await message.answer(
                            user_message,
                            reply_markup=join_keyboard,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                        mid = getattr(sent_msg, "message_id", None)
                        if isinstance(mid, int):
                            async with get_session() as session:
                                u_save = await session.get(User, telegram_user.id)
                                if u_save:
                                    u_save.last_invite_msg_id = mid
                                    session.add(u_save)
                                    await session.commit()
                    except Exception as e:
                        logger.warning(f"Could not send auto-approve invite DM to User {telegram_user.id}: {e}")
                else:
                    await message.answer(
                        f"✅ <b>ได้รับลิงก์ซองของขวัญ TrueMoney สำหรับ {plan_info['badge']} เรียบร้อยแล้ว!</b>\n\n"
                        "ระบบกำลังดำเนินการออกลิงก์เชิญ หากยังไม่ได้รับลิงก์กรุณาติดต่อแอดมินครับ 🙏",
                        parse_mode="HTML",
                    )

        await log_chat_message(
            user_id=telegram_user.id,
            sender_role="USER",
            message_text=f"[ส่งลิงก์ซองของขวัญ TrueMoney #{slip_id} ({plan_info['badge']}) (อนุมัติอัตโนมัติ ⚡): {angpao_url}]"
        )
    else:
        # กรณีไม่ได้เปิด Auto-Approve (รอแอดมินอนุมัติ)
        if is_v2:
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

    if auto_approved:
        admin_text = (
            "🧧 <b>มีการชำระเงินใหม่ผ่าน ซองของขวัญ TrueMoney!</b>\n"
            "⚡ <b>[อนุมัติอัตโนมัติ / AUTO-APPROVED]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
            f"🆔 <b>รหัสรายการ:</b> <code>#{slip_id}</code>\n"
            f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
            f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
            f"⏳ <b>ระยะเวลา:</b> {plan_desc}\n"
            f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>ลิงก์ซองของขวัญ (TrueMoney Angpao):</b>\n"
            f"👉 <a href=\"{angpao_url}\">{html.escape(angpao_url)}</a>\n\n"
            f"📋 <b>แตะเพื่อคัดลอกลิงก์:</b>\n"
            f"<code>{angpao_url}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ <b>สถานะ:</b> บอทอนุมัติและส่งลิงก์เข้าแชแนลให้ผู้ใช้แล้ว ✅\n"
            f"👉 <b>แอดมินกรุณากดรับซองเพื่อตรวจสอบยอดเงินย้อนหลัง ({plan_info['price']:,} บาท)</b>\n"
            "<i>(หากตรวจสอบแล้วซองไม่ถูกต้อง สามารถกดปุ่มปฏิเสธด้านล่างเพื่อตัดสิทธิ์ได้ทันที)</i>"
        )
        admin_markup = get_admin_auto_approved_slip_keyboard(slip_id, telegram_user.id)
    else:
        admin_text = (
            "🧧 <b>มีการชำระเงินใหม่ผ่าน ซองของขวัญ TrueMoney!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n\n"
            f"🆔 <b>รหัสรายการ:</b> <code>#{slip_id}</code>\n"
            f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{telegram_user.id}</code>\n"
            f"📦 <b>แพ็กเกจที่ขอ:</b> <b>{plan_info['badge']} ({plan_info['price']:,} บาท)</b>\n"
            f"⏳ <b>ระยะเวลา:</b> {plan_desc}\n"
            f"📅 <b>เวลาที่ส่ง:</b> <code>{submitted_time_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>ลิงก์ซองของขวัญ (TrueMoney Angpao):</b>\n"
            f"👉 <a href=\"{angpao_url}\">{html.escape(angpao_url)}</a>\n\n"
            f"📋 <b>แตะเพื่อคัดลอกลิงก์:</b>\n"
            f"<code>{angpao_url}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 <b>กรุณากดรับซองเพื่อตรวจสอบยอดเงิน ({plan_info['price']:,} บาท) แล้วกดอนุมัติด้านล่าง:</b>"
        )
        admin_markup = get_admin_slip_keyboard(slip_id, telegram_user.id)

    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_text,
            reply_markup=admin_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"TrueMoney Angpao #{slip_id} (auto_approved={auto_approved}) from user {telegram_user.id} forwarded to Admin Group {config.ADMIN_GROUP_ID}")
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

    # กรณีส่งข้อความธรรมดาที่ไม่ใช่ลิงก์ TrueMoney (เฉพาะสมาชิก V.2)
    async with get_session() as session:
        user = await session.get(User, message.from_user.id)
        if not is_user_v2_member(user):
            return

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

    async with submission_locks[telegram_user.id]:
        # 1. บันทึกข้อมูลสลิปลงฐานข้อมูล (ตรวจรายการซ้ำ)
        async with get_session() as session:
            dup_stmt = select(PaymentSlip).where(
                PaymentSlip.user_id == telegram_user.id,
                PaymentSlip.file_id == file_id,
                PaymentSlip.status == SlipStatus.PENDING.value,
            )
            existing_dup = (await session.execute(dup_stmt)).scalars().first()
            if existing_dup:
                await state.clear()
                user = await session.get(User, telegram_user.id)
                if is_user_v2_member(user):
                    await message.answer(
                        f"ℹ️ <b>คุณได้ส่งสลิปนี้เข้าระบบไว้แล้วครับ (รายการ #{existing_dup.id})</b>\n\n"
                        "ทีมงานแอดมินกำลังดำเนินการตรวจสอบความถูกต้องครับ ขอบคุณครับ 🙏",
                        parse_mode="HTML",
                    )
                return

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

    # 3. ส่งข้อความยืนยันให้ผู้ใช้ (เฉพาะสมาชิก V.2)
    if is_user_v2_member(user):
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
        f"⏳ <b>ระยะเวลา:</b> {format_plan_duration(plan_info)}\n"
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

    async with submission_locks[telegram_user.id]:
        # 1. บันทึกข้อมูลสลิปลงฐานข้อมูล (ตรวจรายการซ้ำ)
        async with get_session() as session:
            dup_stmt = select(PaymentSlip).where(
                PaymentSlip.user_id == telegram_user.id,
                PaymentSlip.file_id == file_id,
                PaymentSlip.status == SlipStatus.PENDING.value,
            )
            existing_dup = (await session.execute(dup_stmt)).scalars().first()
            if existing_dup:
                await state.clear()
                user = await session.get(User, telegram_user.id)
                if is_user_v2_member(user):
                    await message.answer(
                        f"ℹ️ <b>คุณได้ส่งสลิปนี้เข้าระบบไว้แล้วครับ (รายการ #{existing_dup.id})</b>\n\n"
                        "ทีมงานแอดมินกำลังดำเนินการตรวจสอบความถูกต้องครับ ขอบคุณครับ 🙏",
                        parse_mode="HTML",
                    )
                return

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

    # 3. ส่งข้อความยืนยันให้ผู้ใช้ (เฉพาะสมาชิก V.2)
    if is_user_v2_member(user):
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
        f"⏳ <b>ระยะเวลา:</b> {format_plan_duration(plan_info)}\n"
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

    # 3. ตอบกลับผู้ใช้ (เฉพาะสมาชิก V.2)
    async with get_session() as session:
        user = await session.get(User, user_id)
        if is_user_v2_member(user):
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

    is_v2 = False
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=user_id,
            username=telegram_user.username,
            full_name=telegram_user.full_name or telegram_user.first_name,
        )
        is_v2 = is_user_v2_member(user)

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

    assigned = getattr(user, "assigned_channel", None) if user else None
    if assigned == "TERTIARY" or getattr(user, "is_moved_to_tertiary", False):
        room_label = "🟢 BareLive V.3 (ห้องใหม่)"
    elif is_v2:
        room_label = "🟢 BareLive V.2 (ห้องใหม่)"
    else:
        room_label = "🔵 BareLive V.1 (ห้องเดิม)"

    admin_alert = (
        f"📷 <b>มีผู้ใช้ส่ง{media_type} (Direct Message)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📣 <b>แท็กแอดมิน:</b> {config.ADMIN_MENTION}\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📌 <b>ห้องสมาชิก:</b> <code>{room_label}</code>\n"
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

    # ส่งข้อความตอบกลับเฉพาะสมาชิก V.2
    if is_v2:
        await message.answer(
            f"💬 <b>ระบบได้รับ{media_type}ของคุณเรียบร้อยแล้วครับ</b>\n\n"
            "ทีมงานแอดมินได้รับข้อมูลเรียบร้อยแล้วและจะติดต่อกลับโดยเร็วที่สุดครับ\n"
            "💡 <i>หากคุณต้องการสมัครสมาชิก VIP กรุณาพิมพ์ /start แล้วกดเลือกแพ็กเกจก่อนส่งสลิปหรือลิงก์ซองของขวัญครับ</i>",
            parse_mode="HTML",
        )
        await log_chat_message(user_id=user_id, sender_role="BOT", message_text=f"💬 ระบบได้รับ{media_type}ของคุณเรียบร้อยแล้วครับ")
