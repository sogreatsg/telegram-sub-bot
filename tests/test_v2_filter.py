import sys
import os

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.models.schema import User
from bot.services.channel_service import is_user_v2_member, get_user_target_channel_id

def test_v2_checks():
    u_v1 = User(telegram_id=1001, username="v1_user", assigned_channel="PRIMARY", is_moved_to_secondary=False)
    u_v2_moved = User(telegram_id=1002, username="v2_moved", assigned_channel="PRIMARY", is_moved_to_secondary=True)
    u_v2_channel = User(telegram_id=1003, username="v2_channel", assigned_channel="SECONDARY", is_moved_to_secondary=False)
    u_v3_channel = User(telegram_id=1005, username="v3_channel", assigned_channel="TERTIARY", is_moved_to_secondary=False)
    u_new = User(telegram_id=1004, username="new_user")

    assert is_user_v2_member(u_v1) is False, "V1 user should be False"
    assert is_user_v2_member(u_v2_moved) is True, "V2 moved user should be True"
    assert is_user_v2_member(u_v2_channel) is True, "V2 assigned user should be True"
    assert is_user_v2_member(u_v3_channel) is True, "V3 assigned user should be True"
    assert is_user_v2_member(None) is False, "None user should be False"
    assert is_user_v2_member(u_new) is False, "New user without V2/V3 should be False"

    print("All is_user_v2_member assertions passed!")

if __name__ == "__main__":
    test_v2_checks()
