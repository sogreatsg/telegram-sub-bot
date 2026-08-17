import logging
import html
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

async def show_promotion_status(message: Message):
    settings = get_promotion_settings()
    status = "🟢 เปิดใช้งาน" if settings.get("is_active") else "🔴 ปิดใช้งาน"
    days = settings.get("days", 0)
    price = settings.get("price", 0)
    await message.answer(
        f"🎯 <b>ระบบโปรโมชั่น</b>\n\n"
        f"สถานะ: {status}\n"
        f"จำนวนวัน: {days} วัน\n"
        f"ราคา: {price} บาท\n\n"
        "คำสั่งที่ใช้งานได้:\n"
        "• `/promotion setting` - ตั้งค่าโปรโมชั่นใหม่\n"
        "• `/promotion on` - เปิดใช้งาน\n"
        "• `/promotion off` - ปิดใช้งาน\n"
        "• `/promo_broadcast` - ส่งข้อความแจ้งเตือนโปรโมชั่น",
        parse_mode="HTML"
    )

@router.message(Command("promotion"))
async def handle_promotion_command(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return

    args = (message.text or "").split()[1:]
    
    if not args:
        await show_promotion_status(message)
        return

    subcmd = args[0].lower()
    if subcmd == "on":
        update_promotion(is_active=True)
        await message.answer("✅ เปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!")
    elif subcmd == "off":
        update_promotion(is_active=False)
        await message.answer("❌ ปิดใช้งานโปรโมชั่นเรียบร้อยแล้ว!")
    elif subcmd == "setting":
        await message.answer("กรุณาพิมพ์จำนวนวันสำหรับโปรโมชั่นนี้ (เช่น 3, 10, 30):")
        await state.set_state(PromoSettingStates.waiting_for_days)

@router.message(PromoSettingStates.waiting_for_days)
async def process_promo_days(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ จำนวนวันไม่ถูกต้อง กรุณาพิมพ์ตัวเลขจำนวนวันใหม่:")
        return

    await state.update_data(promo_days=days)
    
    # 3 options matching existing QR codes
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="300 บาท", callback_data="promoset:300")],
        [InlineKeyboardButton(text="500 บาท", callback_data="promoset:500")],
        [InlineKeyboardButton(text="1000 บาท", callback_data="promoset:1000")],
    ])
    await message.answer("กรุณาเลือกราคาสำหรับโปรโมชั่น (จำกัด 3 ราคาตาม QR Code ที่มี):", reply_markup=kb)
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
    
    await callback.message.edit_text(
        f"✅ <b>ตั้งค่าโปรโมชั่นสำเร็จ!</b>\n"
        f"จำนวน: {days} วัน\n"
        f"ราคา: {price} บาท\n\n"
        f"⚠️ <i>สถานะโปรโมชั่นถูกรีเซ็ตเป็น 'ปิด' กรุณาพิมพ์ <code>/promotion on</code> เพื่อเปิดใช้งาน</i>",
        parse_mode="HTML"
    )
    await state.clear()

@router.message(Command("promo_broadcast"))
async def handle_promo_broadcast(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    await message.answer("กรุณาพิมพ์ข้อความโปรโมชั่นที่คุณต้องการส่งหาผู้ใช้\n(รองรับ HTML format, สามารถยกเลิกได้โดยพิมพ์ /cancel)")
    await state.set_state(PromoBroadcastStates.waiting_for_message)

@router.message(PromoBroadcastStates.waiting_for_message)
async def process_broadcast_message(message: Message, state: FSMContext):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("ยกเลิกแล้ว")
        return
        
    await state.update_data(broadcast_msg=message.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ส่งให้ผู้ใช้ทั้งหมด", callback_data="promobc:all")],
        [InlineKeyboardButton(text="ส่งเฉพาะผู้ใช้ที่หมดอายุ/ไม่แอคทีฟ", callback_data="promobc:inactive")],
        [InlineKeyboardButton(text="ส่งให้ผู้ใช้คนเดียว (ระบุ User ID)", callback_data="promobc:single")],
        [InlineKeyboardButton(text="❌ ยกเลิก", callback_data="promobc:cancel")],
    ])
    await message.answer("กรุณาเลือกกลุ่มผู้ใช้ที่คุณต้องการส่งข้อความ:", reply_markup=kb)
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
        await callback.message.edit_text("กรุณาพิมพ์ User ID ของผู้ใช้ที่ต้องการส่ง:")
        # We can hijack waiting_for_target to accept user ID since it's a message
        return
        
    await callback.message.edit_text("⏳ กำลังเตรียมส่งข้อความ...")
    
    async with get_session() as session:
        if action == "all":
            users = (await session.execute(select(User))).scalars().all()
        else: # inactive
            # For simplicity, we just fetch all and check if they have active subs
            # A better query could be written, but for a bot it's okay
            from bot.models.schema import Subscription, SubStatus
            # Users who don't have ACTIVE subscriptions
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
    await callback.message.answer(f"✅ บรอดแคสต์เสร็จสิ้น!\nส่งสำเร็จ: {success}\nส่งไม่สำเร็จ: {fail}")

@router.message(PromoBroadcastStates.waiting_for_target)
async def process_broadcast_single_user(message: Message, state: FSMContext, bot: Bot):
    if message.chat.id != config.ADMIN_GROUP_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("ยกเลิกแล้ว")
        return
        
    target_uid = message.text.strip()
    if not target_uid.isdigit():
        await message.answer("User ID ไม่ถูกต้อง (ต้องเป็นตัวเลข)")
        return
        
    data = await state.get_data()
    msg_text = data.get("broadcast_msg", "")
    
    try:
        await bot.send_message(int(target_uid), msg_text, parse_mode="HTML")
        await message.answer("✅ ส่งข้อความสำเร็จ!")
    except Exception as e:
        await message.answer(f"❌ ส่งข้อความไม่สำเร็จ: {e}")
        
    await state.clear()


@router.callback_query(F.data == 'admin_menu:promotion')
async def handle_admin_menu_promotion_callback(callback: CallbackQuery):
    if callback.message.chat.id != config.ADMIN_GROUP_ID:
        await callback.answer('❌ คำสั่งนี้สำหรับกลุ่ม Admin เท่านั้น', show_alert=True)
        return
    await callback.answer()
    await show_promotion_status(callback.message)
