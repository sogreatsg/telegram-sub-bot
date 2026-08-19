import logging
import html
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User
from bot.services.database import get_session
from bot.services.promotion import get_promotion_settings, update_promotion

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="promotion_admin")


class PromoSettingStates(StatesGroup):
    waiting_for_days = State()
    waiting_for_price = State()


class PromoBroadcastStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_target = State()


def get_promotion_status_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    """สร้างข้อความสรุปสถานะโปรโมชั่นและ Inline Keyboard สำหรับ Quick Actions"""
    settings = get_promotion_settings()
    is_active = bool(settings.get("is_active"))
    status_str = "🟢 เปิดใช้งาน (Active)" if is_active else "🔴 ปิดใช้งาน (Inactive)"
    days = settings.get("days", 0)
    price = settings.get("price", 0)
    qr = settings.get("qr_filename", "-")

    text = (
        "🎁 <b>ระบบจัดการโปรโมชั่น (Promotion Management)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>สถานะปัจจุบัน:</b> {status_str}\n"
        f"⏳ <b>ระยะเวลา:</b> <b>{days} วัน</b>\n"
        f"💰 <b>ราคาแพ็กเกจ:</b> <b>{price:,} บาท</b>\n"
        f"🖼️ <b>QR Code:</b> <code>{qr}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>แตะคำสั่งด้านล่างเพื่อสั่งการได้ทันที:</b>\n"
        "• /promotion_on — 🟢 เปิดใช้งานโปรโมชั่นให้ผู้ใช้เห็นในเมนู\n"
        "• /promotion_off — 🔴 ปิดใช้งานโปรโมชั่น\n"
        "• /promotion_setting — ⚙️ ตั้งค่าจำนวนวันและราคาใหม่\n"
        "• /promo_broadcast — 📢 บรอดแคสต์แจ้งโปรโมชั่นหาผู้ใช้\n"
        "• /promotion — 🔄 เช็คสถานะโปรโมชั่นปัจจุบัน\n\n"
        "💡 <i>หรือแตะปุ่มด่วนด้านล่างนี้ได้เลยครับ:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 เปิดโปรโมชั่น", callback_data="promo_action:on"),
            InlineKeyboardButton(text="🔴 ปิดโปรโมชั่น", callback_data="promo_action:off"),
        ],
        [
            InlineKeyboardButton(text="⚙️ ตั้งค่าโปรโมชั่นใหม่", callback_data="promo_action:setting"),
            InlineKeyboardButton(text="📢 บรอดแคสต์แจ้งโปรฯ", callback_data="promo_action:broadcast"),
        ],
        [
            InlineKeyboardButton(text="🔄 รีเฟรชสถานะ", callback_data="admin_menu:promotion"),
        ]
    ])
    return text, kb


async def show_promotion_status(message_or_callback, bot: Optional[Bot] = None):
    """ส่งหรือแก้ไขข้อความแสดงสถานะโปรโมชั่น"""
    text, kb = get_promotion_status_text_and_kb()
    if isinstance(message_or_callback, CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message_or_callback.message.answer(text=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_callback.answer(text=text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("promotion", "promo"))
async def handle_promotion_command(message: Message, state: FSMContext):
    """คำสั่งดูสถานะหรือควบคุมโปรโมชั่น: /promotion [on/off/setting]"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()[1:]
    if not args:
        await show_promotion_status(message)
        return

    subcmd = args[0].lower()
    if subcmd in ("on", "enable", "start"):
        update_promotion(is_active=True)
        text, kb = get_promotion_status_text_and_kb()
        await message.answer(f"✅ <b>เปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    elif subcmd in ("off", "disable", "stop"):
        update_promotion(is_active=False)
        text, kb = get_promotion_status_text_and_kb()
        await message.answer(f"❌ <b>ปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")
    elif subcmd in ("setting", "set", "config"):
        await message.answer("⚙️ <b>กรุณาพิมพ์จำนวนวันสำหรับโปรโมชั่นนี้</b> (เช่น 3, 10, 30 หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
        await state.set_state(PromoSettingStates.waiting_for_days)
    else:
        await show_promotion_status(message)


@router.message(Command("promotion_on", "promo_on"))
async def handle_promotion_on_command(message: Message):
    """คำสั่งเปิดใช้งานโปรโมชั่น: /promotion_on"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    update_promotion(is_active=True)
    text, kb = get_promotion_status_text_and_kb()
    await message.answer(f"✅ <b>เปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("promotion_off", "promo_off"))
async def handle_promotion_off_command(message: Message):
    """คำสั่งปิดใช้งานโปรโมชั่น: /promotion_off"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    update_promotion(is_active=False)
    text, kb = get_promotion_status_text_and_kb()
    await message.answer(f"❌ <b>ปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!</b>\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.message(Command("promotion_setting", "promo_setting", "promotion_set", "promo_set"))
async def handle_promotion_setting_command(message: Message, state: FSMContext):
    """คำสั่งเริ่มตั้งค่าโปรโมชั่น: /promotion_setting"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    await message.answer("⚙️ <b>กรุณาพิมพ์จำนวนวันสำหรับโปรโมชั่นนี้</b> (เช่น 3, 10, 30 หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
    await state.set_state(PromoSettingStates.waiting_for_days)


@router.callback_query(F.data.startswith("promo_action:"))
async def handle_promo_action_callback(callback: CallbackQuery, state: FSMContext):
    """จัดการ Quick Actions ปุ่มลัดโปรโมชั่น"""
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "on":
        update_promotion(is_active=True)
        await callback.answer("✅ เปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว")
        text, kb = get_promotion_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    elif action == "off":
        update_promotion(is_active=False)
        await callback.answer("❌ ปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว")
        text, kb = get_promotion_status_text_and_kb()
        await callback.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")
    elif action == "setting":
        await callback.answer()
        await callback.message.answer("⚙️ <b>กรุณาพิมพ์จำนวนวันสำหรับโปรโมชั่นนี้</b> (เช่น 3, 10, 30 หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
        await state.set_state(PromoSettingStates.waiting_for_days)
    elif action == "broadcast":
        await callback.answer()
        await callback.message.answer("📢 <b>กรุณาพิมพ์ข้อความโปรโมชั่นที่คุณต้องการส่งหาผู้ใช้</b>\n(รองรับ HTML format หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
        await state.set_state(PromoBroadcastStates.waiting_for_message)


@router.message(PromoSettingStates.waiting_for_days)
async def process_promo_days(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ ยกเลิกการตั้งค่าโปรโมชั่นแล้ว")
        return

    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ จำนวนวันไม่ถูกต้อง กรุณาพิมพ์ตัวเลขจำนวนวันใหม่ (เช่น 3, 10, 30 หรือพิมพ์ /cancel):")
        return

    await state.update_data(promo_days=days)

    # 3 options matching existing QR codes
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="300 บาท", callback_data="promoset:300")],
        [InlineKeyboardButton(text="500 บาท", callback_data="promoset:500")],
        [InlineKeyboardButton(text="1,000 บาท", callback_data="promoset:1000")],
    ])
    await message.answer(f"⏳ <b>กำหนด {days} วัน เรียบร้อย</b>\nกรุณาเลือกราคาสำหรับโปรโมชั่น (จำกัด 3 ราคาตาม QR Code ที่มี):", reply_markup=kb, parse_mode="HTML")
    await state.set_state(PromoSettingStates.waiting_for_price)


@router.callback_query(F.data.startswith("promoset:"), PromoSettingStates.waiting_for_price)
async def process_promo_price(callback: CallbackQuery, state: FSMContext):
    price_str = callback.data.split(":")[1]
    price = int(price_str)

    qr_map = {
        300: "qr_300.png",
        500: "qr_500.png",
        1000: "qr_1000.png"
    }

    data = await state.get_data()
    days = data.get("promo_days", 0)

    update_promotion(days=days, price=price, qr_filename=qr_map.get(price, ""))
    # Defaults to False on setting change to be safe
    update_promotion(is_active=False)

    finish_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 เปิดใช้งานโปรโมชั่นทันที", callback_data="promo_action:on"),
        ],
        [
            InlineKeyboardButton(text="📋 ดูสถานะโปรโมชั่น", callback_data="admin_menu:promotion"),
        ]
    ])

    await callback.message.edit_text(
        f"✅ <b>ตั้งค่าโปรโมชั่นสำเร็จ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>จำนวนวัน:</b> {days} วัน\n"
        f"💰 <b>ราคา:</b> {price:,} บาท\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ <i>สถานะโปรโมชั่นถูกรีเซ็ตเป็น 'ปิด' กรุณาแตะ /promotion_on หรือกดปุ่มด้านล่างเพื่อเปิดใช้งาน</i>",
        reply_markup=finish_kb,
        parse_mode="HTML"
    )
    await state.clear()


@router.message(Command("promo_broadcast", "promotion_broadcast", "broadcast_promo", "promobc"))
async def handle_promo_broadcast(message: Message, state: FSMContext):
    """คำสั่งบรอดแคสต์โปรโมชั่น: /promo_broadcast"""
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    await message.answer("📢 <b>กรุณาพิมพ์ข้อความโปรโมชั่นที่คุณต้องการส่งหาผู้ใช้</b>\n(รองรับ HTML format หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
    await state.set_state(PromoBroadcastStates.waiting_for_message)


@router.message(PromoBroadcastStates.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ ยกเลิกการบรอดแคสต์แล้ว")
        return

    await state.update_data(broadcast_msg=message.text)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 ส่งให้ผู้ใช้ทั้งหมด", callback_data="promobc:all")],
        [InlineKeyboardButton(text="⏳ ส่งเฉพาะผู้ใช้ที่หมดอายุ/ไม่แอคทีฟ", callback_data="promobc:inactive")],
        [InlineKeyboardButton(text="👤 ส่งให้ผู้ใช้คนเดียว (ระบุ User ID)", callback_data="promobc:single")],
        [InlineKeyboardButton(text="❌ ยกเลิก", callback_data="promobc:cancel")],
    ])
    await message.answer("🎯 <b>กรุณาเลือกกลุ่มผู้ใช้ที่คุณต้องการส่งข้อความ:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(PromoBroadcastStates.waiting_for_target)


@router.callback_query(F.data.startswith("promobc:"), PromoBroadcastStates.waiting_for_target)
async def process_broadcast_target(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split(":")[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ ยกเลิกการบรอดแคสต์แล้ว")
        return

    data = await state.get_data()
    msg_text = data.get("broadcast_msg", "")

    if action == "single":
        await callback.message.edit_text("👤 <b>กรุณาพิมพ์ User ID ของผู้ใช้ที่ต้องการส่ง</b> (หรือพิมพ์ /cancel เพื่อยกเลิก):", parse_mode="HTML")
        return

    await callback.message.edit_text("⏳ กำลังเตรียมส่งข้อความ...")

    async with get_session() as session:
        if action == "all":
            users = (await session.execute(select(User))).scalars().all()
        else:  # inactive
            from bot.models.schema import Subscription, SubStatus
            active_users_stmt = select(Subscription.user_id).where(Subscription.status == SubStatus.ACTIVE.value)
            active_user_ids = (await session.execute(active_users_stmt)).scalars().all()
            stmt = select(User).where(User.telegram_id.notin_(active_user_ids))
            users = (await session.execute(stmt)).scalars().all()

    success = 0
    fail = 0
    for u in users:
        try:
            await bot.send_message(u.telegram_id, msg_text, parse_mode="HTML")
            success += 1
        except Exception:
            fail += 1

    await state.clear()
    await callback.message.answer(f"✅ <b>บรอดแคสต์เสร็จสิ้น!</b>\n━━━━━━━━━━━━━━━━━━━━\n• ส่งสำเร็จ: <b>{success} คน</b>\n• ส่งไม่สำเร็จ: <b>{fail} คน</b>", parse_mode="HTML")


@router.message(PromoBroadcastStates.waiting_for_target)
async def process_broadcast_single_user(message: Message, state: FSMContext, bot: Bot):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ ยกเลิกการส่งแล้ว")
        return

    target_uid = message.text.strip()
    if not target_uid.isdigit():
        await message.answer("❌ User ID ไม่ถูกต้อง (ต้องเป็นตัวเลขเท่านั้น หรือพิมพ์ /cancel เพื่อยกเลิก):")
        return

    data = await state.get_data()
    msg_text = data.get("broadcast_msg", "")

    try:
        await bot.send_message(int(target_uid), msg_text, parse_mode="HTML")
        await message.answer(f"✅ <b>ส่งข้อความไปยัง User ID <code>{target_uid}</code> สำเร็จ!</b>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ <b>ส่งข้อความไม่สำเร็จ:</b> <code>{html.escape(str(e))}</code>", parse_mode="HTML")

    await state.clear()


@router.callback_query(F.data == "admin_menu:promotion")
async def handle_admin_menu_promotion_callback(callback: CallbackQuery):
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer("❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น", show_alert=True)
        return
    await callback.answer()
    await show_promotion_status(callback)
