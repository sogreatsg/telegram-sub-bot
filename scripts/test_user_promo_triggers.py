import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.models.schema import PlanType
from bot.services.promotion import get_promotion_settings, update_promotion
from bot.handlers.promotion_user import handle_user_promo
from bot.handlers.payment import handle_subscribe_plan_button

async def test_user_promo():
    config = get_settings()
    print("\n--- [TEST 1] Testing User /promo when Promotion is ACTIVE ---")
    update_promotion(is_active=True, days=15, price=500, qr_filename="qr_500.png")
    
    mock_state = MagicMock()
    mock_state.clear = AsyncMock()

    user_inputs = ["/promo", "/promotion", "/โปรโมชั่น", "/โปร", "promo", "PROMO", "โปร", "โปรโมชั่น", "ขอโปร", "ดูโปร"]
    for inp in user_inputs:
        mock_msg = MagicMock()
        mock_msg.text = inp
        mock_msg.chat.type = "private"
        mock_msg.chat.id = 999888
        mock_msg.from_user.id = 999888
        mock_msg.answer = AsyncMock()

        await handle_user_promo(mock_msg, mock_state)
        assert mock_msg.answer.call_count == 1, f"Bot did not answer for input: {inp}"
        sent_text = mock_msg.answer.call_args[0][0]
        sent_kb = mock_msg.answer.call_args[1]["reply_markup"]
        assert "โปรโมชั่นพิเศษมาแล้ว" in sent_text
        assert "15 วัน" in sent_text
        assert "500 บาท" in sent_text
        assert len(sent_kb.inline_keyboard) == 2
        assert sent_kb.inline_keyboard[0][0].callback_data == f"menu:subscribe:{PlanType.PROMOTION.value}"
        print(f"  Input {inp!r} -> Successfully answered with promo text & buttons! ✅")

    print("\n--- [TEST 2] Testing User /promo when Promotion is INACTIVE ---")
    update_promotion(is_active=False)
    for inp in ["/promo", "promo", "โปร"]:
        mock_msg = MagicMock()
        mock_msg.text = inp
        mock_msg.chat.type = "private"
        mock_msg.chat.id = 999888
        mock_msg.from_user.id = 999888
        mock_msg.answer = AsyncMock()

        await handle_user_promo(mock_msg, mock_state)
        assert mock_msg.answer.call_count == 1
        sent_text = mock_msg.answer.call_args[0][0]
        sent_kb = mock_msg.answer.call_args[1]["reply_markup"]
        assert "ยังไม่มีโปรโมชั่นพิเศษ" in sent_text
        assert sent_kb.inline_keyboard[0][0].callback_data == "menu:main"
        print(f"  Input {inp!r} (Inactive) -> Successfully answered with fallback notice & main menu button! ✅")

    print("\n--- [TEST 3] Testing clicking the Promo Button (menu:subscribe:PROMOTION) ---")
    update_promotion(is_active=True, days=7, price=300, qr_filename="qr_300.png")
    mock_cb = MagicMock()
    mock_cb.data = f"menu:subscribe:{PlanType.PROMOTION.value}"
    mock_cb.from_user.id = 999888
    mock_cb.message.photo = None
    mock_cb.message.document = None
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.message.answer = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_subscribe_plan_button(mock_cb, mock_state)
    assert mock_cb.answer.call_count == 1
    assert mock_cb.message.edit_text.call_count == 1
    edit_text = mock_cb.message.edit_text.call_args[1]["text"]
    assert "300 บาท" in edit_text
    assert "7 วัน" in edit_text
    print("  Promo Button Click -> Successfully opened payment methods without crash! ✅")

    print("\n🎉 ALL USER PROMO TRIGGER TESTS PASSED 100%!")

if __name__ == "__main__":
    asyncio.run(test_user_promo())
