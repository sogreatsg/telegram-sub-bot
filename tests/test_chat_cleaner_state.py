import sys
import os
import asyncio

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.services.chat_cleaner_state import (
    get_clean_state,
    save_clean_state,
    get_user_checkpoint,
    is_user_clean_completed,
    update_user_checkpoint,
    update_session_state,
    reset_all_clean_checkpoints,
    get_clean_status_summary,
)

def test_chat_cleaner_state():
    # 1. Reset
    reset_all_clean_checkpoints()
    state = get_clean_state()
    assert state["users"] == {}
    assert state["session"]["is_running"] is False

    # 2. Update user in progress
    update_user_checkpoint(
        user_id=8869252777,
        max_id=19064,
        scanned_down_to_id=14200,
        deleted_count=14,
        status="IN_PROGRESS",
        username="sh",
        full_name="sh",
        deleted_ids=[16233, 16218, 15794],
    )
    assert not is_user_clean_completed(8869252777)
    cp = get_user_checkpoint(8869252777)
    assert cp is not None
    assert cp["scanned_down_to_id"] == 14200
    assert cp["deleted_count"] == 14
    assert len(cp["deleted_ids"]) == 3

    # 3. Update another user to completed
    update_user_checkpoint(
        user_id=111222333,
        max_id=500,
        scanned_down_to_id=0,
        deleted_count=10,
        status="COMPLETED",
        username="user_completed",
        full_name="Test User Completed",
    )
    assert is_user_clean_completed(111222333)

    # 4. Update session
    update_session_state(
        is_running=True,
        total_users=100,
        current_user_index=15,
        current_user_id=8869252777,
        current_user_name="sh",
        total_msgs_deleted=24,
    )

    # 5. Check summary
    summary = get_clean_status_summary()
    assert summary["total_tracked_users"] == 2
    assert summary["completed_count"] == 1
    assert summary["in_progress_count"] == 1
    assert summary["total_deleted_msgs"] == 24
    assert summary["session"]["is_running"] is True

    # 6. Cleanup
    reset_all_clean_checkpoints()
    print("Chat cleaner state persistence test passed successfully!")

if __name__ == "__main__":
    test_chat_cleaner_state()
