import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from bot.models.schema import PlanType
from bot.services.promotion import get_promotion_settings

# We reuse the logic from user_menu or payment for subscribing
# but wait, payment.py listens to menu:subscribe:xxx callback.
# So we can just answer with a text and an inline keyboard that sends menu:subscribe:PROMOTION

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router(name="promotion_user")

@router.message(Command("promo"))
async def handle_user_promo(message: Message, state: FSMContext):
    if not message.from_user:
        return
        
    settings = get_promotion_settings()
    is_active = settings.get("is_active", False)
    
    if not is_active:
        await message.answer("เสียใจด้วยน้าา ช่วงนี้ยังไม่มีโปรโมชั่น 😢")
        return
        
    days = settings.get("days", 0)
    price = settings.get("price", 0)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 รับสิทธิ์โปรโมชั่น {days} วัน ({price} บาท)", callback_data=f"menu:subscribe:{PlanType.PROMOTION.value}")]
    ])
    
    await message.answer(
        f"🎉 <b>โปรโมชั่นพิเศษมาแล้ว!</b>\n\n"
        f"แพ็กเกจ VIP จำนวน <b>{days} วัน</b> ในราคาเพียง <b>{price} บาท</b>\n\n"
        f"กดปุ่มด้านล่างเพื่อรับสิทธิ์และโอนเงินได้เลยครับ!",
        reply_markup=kb,
        parse_mode="HTML"
    )
