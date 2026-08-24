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
    reset_for_incremental_rescan,
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
    assert cp["deleted_count"] == 14

    # 3. Update to completed
    update_user_checkpoint(
        user_id=8869252777,
        max_id=19064,
        scanned_down_to_id=0,
        deleted_count=14,
        status="COMPLETED",
        username="sh",
        full_name="sh",
    )
    assert is_user_clean_completed(8869252777)
    # Check max_id comparison
    assert is_user_clean_completed(8869252777, current_max_id=19064)
    assert not is_user_clean_completed(8869252777, current_max_id=19070)

    # 4. Incremental reset test
    reset_for_incremental_rescan()
    cp = get_user_checkpoint(8869252777)
    assert cp["status"] == "INCREMENTAL_READY"
    assert cp["last_scanned_max_id"] == 19064

    # 5. Cleanup
    reset_all_clean_checkpoints()
    print("Chat cleaner state persistence and incremental delta test passed successfully!")

if __name__ == "__main__":
    test_chat_cleaner_state()
