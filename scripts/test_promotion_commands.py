import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.services.promotion import get_promotion_settings, update_promotion
from bot.handlers.promotion_admin import (
    handle_promotion_command,
    handle_promotion_on_command,
    handle_promotion_off_command,
    handle_promotion_setting_command,
    handle_promo_action_callback,
    get_promotion_status_text_and_kb,
)

async def test_promotion():
    config = get_settings()
    
    print("\n--- [TEST 1] Testing get_promotion_status_text_and_kb ---")
    update_promotion(days=15, price=500, is_active=True)
    text, kb = get_promotion_status_text_and_kb()
    print("Promotion Status Preview:\n", text)
    assert "/promotion_on" in text
    assert "/promotion_off" in text
    assert "/promotion_setting" in text
    assert "/promo_broadcast" in text
    assert len(kb.inline_keyboard) == 3
    print("Status text & keyboard: PASS ✅")

    print("\n--- [TEST 2] Testing /promotion_off command ---")
    mock_msg = MagicMock()
    mock_msg.chat.id = config.ADMIN_GROUP_ID
    mock_msg.answer = AsyncMock()
    
    await handle_promotion_off_command(mock_msg)
    settings = get_promotion_settings()
    assert settings.get("is_active") is False
    assert mock_msg.answer.call_count == 1
    print("/promotion_off: PASS (is_active=False) ✅")

    print("\n--- [TEST 3] Testing /promotion_on command ---")
    mock_msg.answer.reset_mock()
    await handle_promotion_on_command(mock_msg)
    settings = get_promotion_settings()
    assert settings.get("is_active") is True
    assert mock_msg.answer.call_count == 1
    print("/promotion_on: PASS (is_active=True) ✅")

    print("\n--- [TEST 4] Testing Callback promo_action:off and promo_action:on ---")
    mock_cb = MagicMock()
    mock_cb.message.chat.id = config.ADMIN_GROUP_ID
    mock_cb.data = "promo_action:off"
    mock_cb.answer = AsyncMock()
    mock_cb.message.edit_text = AsyncMock()
    mock_state = MagicMock()

    await handle_promo_action_callback(mock_cb, mock_state)
    assert get_promotion_settings().get("is_active") is False
    assert mock_cb.message.edit_text.call_count == 1

    mock_cb.data = "promo_action:on"
    mock_cb.message.edit_text.reset_mock()
    await handle_promo_action_callback(mock_cb, mock_state)
    assert get_promotion_settings().get("is_active") is True
    assert mock_cb.message.edit_text.call_count == 1
    print("promo_action Callbacks: PASS ✅")

    print("\n--- [TEST 5] Testing Legacy space commands (/promotion on / off) ---")
    mock_msg.text = "/promotion off"
    mock_msg.answer.reset_mock()
    await handle_promotion_command(mock_msg, mock_state)
    assert get_promotion_settings().get("is_active") is False

    mock_msg.text = "/promotion on"
    mock_msg.answer.reset_mock()
    await handle_promotion_command(mock_msg, mock_state)
    assert get_promotion_settings().get("is_active") is True
    print("Legacy space commands: PASS ✅")

    print("\n--- [TEST 6] Testing Setting Promotion with 100 Baht ---")
    from bot.handlers.promotion_admin import process_promo_price
    mock_cb.data = "promoset:100"
    mock_state.get_data = AsyncMock(return_value={"promo_days": 1})
    mock_state.clear = AsyncMock()
    mock_cb.message.edit_text.reset_mock()

    await process_promo_price(mock_cb, mock_state)
    settings = get_promotion_settings()
    assert settings.get("price") == 100
    assert settings.get("days") == 1
    assert settings.get("qr_filename") == "qr_100.png"
    assert mock_cb.message.edit_text.call_count == 1
    print("100 Baht promo setting & qr_100.png mapping: PASS ✅")

    print("\n==============================================")
    print("    ALL PROMOTION COMMAND TESTS PASSED!       ")
    print("==============================================\n")

if __name__ == "__main__":
    asyncio.run(test_promotion())
