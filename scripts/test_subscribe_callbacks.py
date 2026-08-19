import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.models.schema import PlanType, PLAN_DETAILS, get_dynamic_plan_info, format_plan_duration
from bot.handlers.payment import handle_subscribe_plan_button, get_payment_method_keyboard
from bot.handlers.user_menu import handle_menu_main, get_main_menu_keyboard

async def test_callbacks():
    print("\n--- Testing get_dynamic_plan_info & format_plan_duration ---")
    for key, plan in PLAN_DETAILS.items():
        info = get_dynamic_plan_info(key)
        dur = format_plan_duration(info)
        print(f"Plan {key}: name={info['name']}, price={info['price']}, duration={dur}")
        assert info["price"] > 0 or key in (PlanType.REFERRAL_VIP.value, PlanType.PROMOTION.value)

    print("\n--- Testing handle_subscribe_plan_button with various callback_data ---")
    mock_state = MagicMock()
    mock_state.clear = AsyncMock()

    plans_to_test = [
        "menu:subscribe:VIP_12H",
        "menu:subscribe:VIP_1D",
        "menu:subscribe:VIP_3D",
        "menu:subscribe:VIP_10D",
        "menu:subscribe:VIP_30D",
        "menu:subscribe:PROMOTION",
        "menu:subscribe_30d",
    ]

    for cb_data in plans_to_test:
        mock_cb = MagicMock()
        mock_cb.data = cb_data
        mock_cb.from_user.id = 123456
        mock_cb.message.photo = None
        mock_cb.message.document = None
        mock_cb.message.edit_text = AsyncMock()
        mock_cb.message.answer = AsyncMock()
        mock_cb.answer = AsyncMock()

        try:
            await handle_subscribe_plan_button(mock_cb, mock_state)
            print(f"Callback {cb_data}: SUCCESS ✅")
            assert mock_cb.answer.call_count == 1, f"callback.answer was not called for {cb_data}"
            assert mock_cb.message.edit_text.call_count == 1, f"edit_text was not called for {cb_data}"
            sent_text = mock_cb.message.edit_text.call_args[1]["text"]
            kb = mock_cb.message.edit_text.call_args[1]["reply_markup"]
            print(f"  -> Reply markup rows: {len(kb.inline_keyboard)}")
        except Exception as e:
            print(f"Callback {cb_data}: FAILED with error: {e} ❌")
            raise e

    print("\n🎉 ALL CALLBACK TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(test_callbacks())
