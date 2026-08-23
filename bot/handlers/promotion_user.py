import logging
import html
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from bot.models.schema import PlanType, User
from bot.services.promotion import get_promotion_settings
from bot.services.chat_logger import log_chat_message
from bot.services.database import get_session
from bot.services.channel_service import is_user_v2_member

logger = logging.getLogger(__name__)
router = Router(name="promotion_user")

PROMO_KEYWORDS = {"promo", "promotion", "promotions", "โปร", "โปรโมชั่น", "ขอโปร", "ดูโปร", "โปรพิเศษ"}


@router.message(F.chat.type == "private", Command("promo", "promotion", "promotions", "โปรโมชั่น", "โปร"))
@router.message(F.chat.type == "private", F.text.lower().in_(PROMO_KEYWORDS))
async def handle_user_promo(message: Message, state: FSMContext):
    """ส่งข้อมูลโปรโมชั่นให้ผู้ใช้เมื่อพิมพ์ /promo หรือพิมพ์คำว่า promo ในแชทส่วนตัว (เฉพาะสมาชิก V.2)"""
    if not message.from_user:
        return

    user_id = message.from_user.id
    async with get_session() as session:
        user = await session.get(User, user_id)
        if not is_user_v2_member(user):
            return
    settings = get_promotion_settings()
    is_active = bool(settings.get("is_active", False))

    if not is_active:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 ดูแพ็กเกจปกติทั้งหมด", callback_data="menu:main")]
        ])
        await message.answer(
            "😢 <b>ขณะนี้ยังไม่มีโปรโมชั่นพิเศษเปิดใช้งานครับ</b>\n\n"
            "คุณสามารถกดปุ่มด้านล่างเพื่อเลือกดูแพ็กเกจสมาชิก VIP ปกติได้เลยครับ 🙏",
            reply_markup=kb,
            parse_mode="HTML",
        )
        try:
            await log_chat_message(user_id=user_id, sender_role="USER", message_text=message.text or "/promo")
        except Exception:
            pass
        return

    days = settings.get("days", 0)
    price = settings.get("price", 0)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 รับสิทธิ์โปรโมชั่น {days} วัน ({price:,} บาท)", callback_data=f"menu:subscribe:{PlanType.PROMOTION.value}")],
        [InlineKeyboardButton(text="🔙 กลับเมนูหลัก", callback_data="menu:main")],
    ])

    await message.answer(
        f"🎉 <b>โปรโมชั่นพิเศษมาแล้ว!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>แพ็กเกจ:</b> VIP จำนวน <b>{days} วัน</b>\n"
        f"💰 <b>ราคาพิเศษเพียง:</b> <b>{price:,} บาท</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👉 <i>แตะปุ่มด้านล่างเพื่อรับสิทธิ์และชำระเงินได้ทันทีครับ! 🚀</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    try:
        await log_chat_message(user_id=user_id, sender_role="USER", message_text=message.text or "/promo")
    except Exception:
        pass
