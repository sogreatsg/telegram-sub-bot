import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.services.notification_settings import (
    get_notification_settings,
    save_notification_settings,
    is_unanswered_dm_reminder_active,
    update_unanswered_dm_reminder_setting,
)

def test_notification_settings_toggle():
    # Save original setting
    original = is_unanswered_dm_reminder_active()

    try:
        # Test toggle OFF
        update_unanswered_dm_reminder_setting(False)
        assert is_unanswered_dm_reminder_active() is False, "Setting should be False after turning OFF"

        # Test toggle ON
        update_unanswered_dm_reminder_setting(True)
        assert is_unanswered_dm_reminder_active() is True, "Setting should be True after turning ON"

        print("All notification settings assertion tests passed successfully!")
    finally:
        # Restore original
        update_unanswered_dm_reminder_setting(original)

if __name__ == "__main__":
    test_notification_settings_toggle()
