import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.models.schema import Base, User, ChatMessage
import bot.services.scheduler as sched_module
from bot.services.scheduler import check_unanswered_user_dms_reminder

async def test_unanswered_dms_reminder():
    test_db = BASE_DIR / "data" / "test_unanswered_dms.db"
    if test_db.exists():
        test_db.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        async with async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Monkeypatch get_session
    sched_module.get_session = mock_get_session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    now = datetime.now(timezone.utc)

    print("\n--- [TEST 1] Empty state -> Should NOT send any reminder ---")
    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 0
    print("Empty state: PASS (0 messages sent) ✅")

    print("\n--- [TEST 2] Add User A (recent msg, 100s ago) -> Should NOT send reminder (< 600s) ---")
    async with async_session() as session:
        u_a = User(telegram_id=1001, username="user_a", full_name="User Alpha", created_at=now)
        msg_a = ChatMessage(id=1, user_id=1001, sender_role="USER", message_text="สนใจแพ็กเกจ 30 วันครับ", created_at=now - timedelta(seconds=100))
        session.add_all([u_a, msg_a])
        await session.commit()

    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 0
    print("Recent message (<600s): PASS (0 messages sent) ✅")

    print("\n--- [TEST 3] Add User B (overdue msg, 650s ago) and User C (overdue msg, 700s ago) ---")
    async with async_session() as session:
        u_b = User(telegram_id=1002, username="user_b", full_name="User Beta", created_at=now)
        msg_b = ChatMessage(id=2, user_id=1002, sender_role="USER", message_text="ขอสอบถามเรื่องโอนเงินครับ", created_at=now - timedelta(seconds=650))

        u_c = User(telegram_id=1003, username="user_c", full_name="User Gamma", created_at=now)
        msg_c = ChatMessage(id=3, user_id=1003, sender_role="USER", message_text="ลิงก์ซองแดงเปิดไม่ติดครับ", created_at=now - timedelta(seconds=700))

        session.add_all([u_b, msg_b, u_c, msg_c])
        await session.commit()

    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 1, "Expected 1 aggregated reminder message"
    sent_text = mock_bot.send_message.call_args[1]["text"]
    print("Aggregated Alert Message Preview:\n", sent_text)

    assert "User Beta" in sent_text
    assert "User Gamma" in sent_text
    assert "User Alpha" not in sent_text  # Alpha was only 100s ago
    print("Aggregated Overdue Reminder: PASS ✅")

    print("\n--- [TEST 3.1] Run check_unanswered_user_dms_reminder immediately (0s later) ---")
    mock_bot.send_message.reset_mock()
    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 0, "Expected 0 messages due to 10-minute throttle cooldown"
    print("Throttle Verification (0s later): PASS (0 messages sent) ✅")

    print("\n--- [TEST 3.2] Fast-forward 650 seconds -> Should send repeat reminder ---")
    # Simulate 650 seconds passing for last_reminded timestamp
    for msg_id in sched_module._dm_last_reminded:
        sched_module._dm_last_reminded[msg_id] = now - timedelta(seconds=650)

    mock_bot.send_message.reset_mock()
    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 1, "Expected 1 repeat reminder message after cooldown"
    print("Repeat Reminder after 10m: PASS ✅")

    print("\n--- [TEST 4] Admin replies to User B -> User B removed from reminder ---")
    async with async_session() as session:
        admin_reply_b = ChatMessage(id=4, user_id=1002, sender_role="ADMIN", message_text="สวัสดีครับ สามารถโอนผ่านพร้อมเพย์ได้เลยครับ", created_at=now)
        session.add(admin_reply_b)
        await session.commit()

    # Fast-forward cooldown for remaining overdue users
    for msg_id in sched_module._dm_last_reminded:
        sched_module._dm_last_reminded[msg_id] = now - timedelta(seconds=650)

    mock_bot.send_message.reset_mock()
    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 1
    sent_text_2 = mock_bot.send_message.call_args[1]["text"]
    assert "User Gamma" in sent_text_2
    assert "User Beta" not in sent_text_2  # Beta was answered by ADMIN
    print("Replied User Filter: PASS ✅")

    print("\n--- [TEST 5] Admin clicks Resolve for User C -> All resolved, NO reminders sent ---")
    async with async_session() as session:
        admin_resolve_c = ChatMessage(id=5, user_id=1003, sender_role="ADMIN", message_text="[แอดมินปิดจบการสนทนา]", created_at=now)
        session.add(admin_resolve_c)
        await session.commit()

    mock_bot.send_message.reset_mock()
    await check_unanswered_user_dms_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 0, "Expected 0 messages after all are resolved"
    print("All Resolved -> 0 Reminders: PASS ✅")

    await engine.dispose()
    if test_db.exists():
        test_db.unlink()

    print("\n==============================================")
    print("  ALL UNANSWERED DMS REMINDER TESTS PASSED!   ")
    print("==============================================\n")

if __name__ == "__main__":
    asyncio.run(test_unanswered_dms_reminder())
