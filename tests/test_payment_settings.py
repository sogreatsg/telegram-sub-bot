import sys
import os
import shutil

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.services.payment_settings import (
    is_promptpay_active,
    update_promptpay_setting,
    is_truemoney_active,
    update_truemoney_setting,
    get_payment_settings,
    PAYMENT_SETTINGS_FILE
)
from bot.handlers.payment import get_payment_method_keyboard

def test_payment_settings():
    # Save backup if exists
    backup_content = None
    if os.path.exists(PAYMENT_SETTINGS_FILE):
        with open(PAYMENT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            backup_content = f.read()

    try:
        # 1. Test PromptPay toggle
        update_promptpay_setting(False)
        assert is_promptpay_active() is False, "PromptPay should be False after turning off"
        kb = get_payment_method_keyboard("vip_30d")
        all_cb_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert not any("promptpay" in cb for cb in all_cb_data), "PromptPay button should NOT appear when disabled"
        assert any("truemoney" in cb for cb in all_cb_data), "TrueMoney button should appear"

        update_promptpay_setting(True)
        assert is_promptpay_active() is True, "PromptPay should be True after turning on"
        kb2 = get_payment_method_keyboard("vip_30d")
        all_cb_data2 = [btn.callback_data for row in kb2.inline_keyboard for btn in row]
        assert any("promptpay" in cb for cb in all_cb_data2), "PromptPay button should appear when enabled"

        # 2. Test TrueMoney toggle
        update_truemoney_setting(False)
        assert is_truemoney_active() is False, "TrueMoney should be False after turning off"
        kb3 = get_payment_method_keyboard("vip_30d")
        all_cb_data3 = [btn.callback_data for row in kb3.inline_keyboard for btn in row]
        assert not any("truemoney" in cb for cb in all_cb_data3), "TrueMoney button should NOT appear when disabled"
        assert any("promptpay" in cb for cb in all_cb_data3), "PromptPay button should appear"

        update_truemoney_setting(True)
        assert is_truemoney_active() is True

        print("Payment settings test passed successfully!")

    finally:
        # Restore backup
        if backup_content is not None:
            with open(PAYMENT_SETTINGS_FILE, "w", encoding="utf-8") as f:
                f.write(backup_content)
        else:
            if os.path.exists(PAYMENT_SETTINGS_FILE):
                os.remove(PAYMENT_SETTINGS_FILE)

if __name__ == "__main__":
    test_payment_settings()
