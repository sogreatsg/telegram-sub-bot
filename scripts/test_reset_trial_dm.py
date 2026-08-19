import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, ChatMessage
from bot.services.database import get_session, init_db
from bot.handlers.admin import handle_admin_reset_trial_command, handle_admin_reset_trial_callback
from sqlalchemy import select

async def run_tests():
    print("=== [1] INIT DATABASE ===")
    await init_db()
    config = get_settings()

    # -------------------------------------------------------------
    # Test 1: handle_admin_reset_trial_command (/reset_trial)
    # -------------------------------------------------------------
    test_uid_1 = int(time.time() * 1000) % 100000000
    print(f"\n=== [2] TEST COMMAND: /reset_trial for User {test_uid_1} ===")
    async with get_session() as session:
        user1 = User(
            telegram_id=test_uid_1,
            username=f"test_user_cmd_{test_uid_1}",
            full_name="Trial Command Tester",
            trial_used=True,
        )
        session.add(user1)
        sub1 = Subscription(
            user_id=test_uid_1,
            status=SubStatus.ACTIVE.value,
            is_trial_active=True,
            expires_at=datetime.now(timezone.utc),
            joined_at=datetime.now(timezone.utc),
            source_label="ทดลองใช้ฟรี 15 นาที",
        )
        session.add(sub1)
        await session.commit()

    mock_bot = MagicMock()
    mock_bot.ban_chat_member = AsyncMock()
    mock_bot.unban_chat_member = AsyncMock()
    mock_bot.send_message = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.chat.id = config.ADMIN_GROUP_ID
    mock_msg.text = f"/reset_trial {test_uid_1}"
    mock_msg.answer = AsyncMock()

    await handle_admin_reset_trial_command(mock_msg, mock_bot)

    # Verify DB state
    async with get_session() as session:
        u = await session.get(User, test_uid_1)
        s = await session.get(Subscription, test_uid_1)
        assert u.trial_used is False, "user.trial_used should be False"
        assert s.status == SubStatus.EXPIRED.value, "subscription should be EXPIRED"

        chat_msgs = (await session.execute(
            select(ChatMessage).where(ChatMessage.user_id == test_uid_1)
        )).scalars().all()
        assert len(chat_msgs) > 0, "ChatMessage log should be created in DB"

    # Verify DM sent to user
    mock_bot.send_message.assert_called_once()
    call_args = mock_bot.send_message.call_args
    assert call_args.kwargs['chat_id'] == test_uid_1, f"DM must be sent to {test_uid_1}"
    dm_text = call_args.kwargs['text']
    assert "/start" in dm_text or "15 นาที" in dm_text, f"DM text must mention /start or 15 นาที: {dm_text}"
    kb = call_args.kwargs['reply_markup']
    assert any(btn.callback_data == "menu:trial" for row in kb.inline_keyboard for btn in row), "Keyboard must have menu:trial button"
    print("  Command reset trial test passed! (DM sent to user, DB updated) ✅")

    # -------------------------------------------------------------
    # Test 2: handle_admin_reset_trial_callback (admin:reset_trial:<uid>)
    # -------------------------------------------------------------
    test_uid_2 = test_uid_1 + 1
    print(f"\n=== [3] TEST CALLBACK: admin:reset_trial for User {test_uid_2} ===")
    async with get_session() as session:
        user2 = User(
            telegram_id=test_uid_2,
            username=f"test_user_cb_{test_uid_2}",
            full_name="Trial Callback Tester",
            trial_used=True,
        )
        session.add(user2)
        sub2 = Subscription(
            user_id=test_uid_2,
            status=SubStatus.ACTIVE.value,
            is_trial_active=True,
            expires_at=datetime.now(timezone.utc),
            joined_at=datetime.now(timezone.utc),
            source_label="ทดลองใช้ฟรี 15 นาที",
        )
        session.add(sub2)
        await session.commit()

    mock_bot_cb = MagicMock()
    mock_bot_cb.ban_chat_member = AsyncMock()
    mock_bot_cb.unban_chat_member = AsyncMock()
    mock_bot_cb.send_message = AsyncMock()

    mock_cb = MagicMock()
    mock_cb.data = f"admin:reset_trial:{test_uid_2}"
    mock_cb.from_user.id = 1001
    mock_cb.message.chat.id = config.ADMIN_GROUP_ID
    mock_cb.message.answer = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_admin_reset_trial_callback(mock_cb, mock_bot_cb)

    # Verify DB state
    async with get_session() as session:
        u2 = await session.get(User, test_uid_2)
        s2 = await session.get(Subscription, test_uid_2)
        assert u2.trial_used is False, "user.trial_used should be False"
        assert s2.status == SubStatus.EXPIRED.value, "subscription should be EXPIRED"

        chat_msgs_2 = (await session.execute(
            select(ChatMessage).where(ChatMessage.user_id == test_uid_2)
        )).scalars().all()
        assert len(chat_msgs_2) > 0, "ChatMessage log should be created in DB"

    # Verify DM sent to user
    mock_bot_cb.send_message.assert_called_once()
    call_args_2 = mock_bot_cb.send_message.call_args
    assert call_args_2.kwargs['chat_id'] == test_uid_2, f"DM must be sent to {test_uid_2}"
    dm_text_2 = call_args_2.kwargs['text']
    assert "/start" in dm_text_2 or "15 นาที" in dm_text_2, f"DM text must mention /start or 15 นาที: {dm_text_2}"
    kb_2 = call_args_2.kwargs['reply_markup']
    assert any(btn.callback_data == "menu:trial" for row in kb_2.inline_keyboard for btn in row), "Keyboard must have menu:trial button"
    print("  Callback reset trial test passed! (DM sent to user, DB updated) ✅")

    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! 🎉")

if __name__ == "__main__":
    asyncio.run(run_tests())
