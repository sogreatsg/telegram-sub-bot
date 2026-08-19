import asyncio
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.models.schema import User, Subscription, SubStatus
from bot.services.database import get_session, init_db
from bot.services.scheduler import check_expiring_soon_subscriptions
from bot.handlers.user_menu import handle_menu_main, handle_my_status

async def test_expiration_warning():
    print("\n--- [INIT DB] ---")
    await init_db()

    from sqlalchemy import text
    async with get_session() as session:
        await session.execute(text("DELETE FROM subscriptions;"))
        await session.execute(text("DELETE FROM users;"))
        await session.commit()

    base_id = int(time.time() * 100) % 100000000
    user_12h_early = base_id + 10
    user_12h_due = base_id + 20
    user_30d_due_24h = base_id + 30
    user_30d_due_1h = base_id + 40

    now = datetime.now(timezone.utc)

    print("\n--- [SETUP TEST USERS] ---")
    async with get_session() as session:
        # User 1: 12H VIP with 11 hours remaining -> Should NOT warn
        u1 = User(telegram_id=user_12h_early, username="u12h_early", full_name="12H Early User")
        session.add(u1)
        sub1 = Subscription(
            user_id=user_12h_early,
            status=SubStatus.ACTIVE.value,
            joined_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=11),
            source_label="สมาชิก ⚡ VIP 12 ชั่วโมง",
            warned_1d=False,
            warned_1h=False,
            is_trial_active=False,
        )
        session.add(sub1)

        # User 2: 12H VIP with 45 minutes remaining -> Should get 1-hour WARN
        u2 = User(telegram_id=user_12h_due, username="u12h_due", full_name="12H Due User")
        session.add(u2)
        sub2 = Subscription(
            user_id=user_12h_due,
            status=SubStatus.ACTIVE.value,
            joined_at=now - timedelta(hours=11, minutes=15),
            expires_at=now + timedelta(minutes=45),
            source_label="สมาชิก ⚡ VIP 12 ชั่วโมง",
            warned_1d=False,
            warned_1h=False,
            is_trial_active=False,
        )
        session.add(sub2)

        # User 3: 30D VIP with 20 hours remaining -> Should get 24-hour WARN
        u3 = User(telegram_id=user_30d_due_24h, username="u30d_24h", full_name="30D 24h Due User")
        session.add(u3)
        sub3 = Subscription(
            user_id=user_30d_due_24h,
            status=SubStatus.ACTIVE.value,
            joined_at=now - timedelta(days=29, hours=4),
            expires_at=now + timedelta(hours=20),
            source_label="สมาชิก 🥇 VIP 30 วัน",
            warned_1d=False,
            warned_1h=False,
            is_trial_active=False,
        )
        session.add(sub3)

        # User 4: 30D VIP with 45 minutes remaining (already got 24h warning) -> Should get 1-hour WARN
        u4 = User(telegram_id=user_30d_due_1h, username="u30d_1h", full_name="30D 1h Due User")
        session.add(u4)
        sub4 = Subscription(
            user_id=user_30d_due_1h,
            status=SubStatus.ACTIVE.value,
            joined_at=now - timedelta(days=29, hours=23, minutes=15),
            expires_at=now + timedelta(minutes=45),
            source_label="สมาชิก 🥇 VIP 30 วัน",
            warned_1d=True,
            warned_1h=False,
            is_trial_active=False,
        )
        session.add(sub4)

        await session.commit()

    print("\n--- [TEST 1] Run check_expiring_soon_subscriptions ---")
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    await check_expiring_soon_subscriptions(mock_bot)

    # Verify mock_bot sent messages
    # User 1 (12H early) should NOT be warned
    # User 2 (12H due, 45m left) SHOULD be warned (1h warn)
    # User 3 (30D due, 20h left) SHOULD be warned (24h warn)
    # User 4 (30D due, 45m left) SHOULD be warned (1h warn)
    assert mock_bot.send_message.call_count == 3, f"Expected 3 warnings, got {mock_bot.send_message.call_count}"

    sent_targets = [call[1]["chat_id"] for call in mock_bot.send_message.call_args_list]
    assert user_12h_early not in sent_targets, "FAILED: 12H Early user was warned prematurely!"
    assert user_12h_due in sent_targets, "FAILED: 12H Due user was not warned!"
    assert user_30d_due_24h in sent_targets, "FAILED: 30D 24h user was not warned!"
    assert user_30d_due_1h in sent_targets, "FAILED: 30D 1h user was not warned!"
    print("  1.1 12H plan filtering: PASS ✅ (11h left skipped, 45m left got 1-hour warning)")
    print("  1.2 30D plan 24h warning: PASS ✅ (20h left got 24-hour warning)")
    print("  1.3 30D plan 1h warning: PASS ✅ (45m left got 1-hour warning even after 24h warning was sent)")

    # Verify warning flags in database
    async with get_session() as session:
        s1 = await session.get(Subscription, user_12h_early)
        s2 = await session.get(Subscription, user_12h_due)
        s3 = await session.get(Subscription, user_30d_due_24h)
        s4 = await session.get(Subscription, user_30d_due_1h)
        assert s1.warned_1d is False and s1.warned_1h is False
        assert s2.warned_1h is True
        assert s3.warned_1d is True and s3.warned_1h is False
        assert s4.warned_1d is True and s4.warned_1h is True
        print("  1.4 Database warning flags: PASS ✅ (s1: 0/0, s2: 1h, s3: 24h, s4: 24h+1h)")

    print("\n--- [TEST 2] Verify Notification Button Callbacks (No Spinner Hang) ---")
    mock_state = MagicMock()
    mock_state.clear = AsyncMock()

    # Test menu:packages callback
    cb_packages = MagicMock()
    cb_packages.data = "menu:packages"
    cb_packages.from_user.id = user_12h_due
    cb_packages.from_user.username = "u12h_due"
    cb_packages.from_user.first_name = "12H"
    cb_packages.from_user.full_name = "12H Due User"
    cb_packages.message.edit_text = AsyncMock()
    cb_packages.answer = AsyncMock()

    await handle_menu_main(cb_packages, mock_state)
    assert cb_packages.answer.call_count == 1, "FAILED: callback.answer() was not called for menu:packages!"
    assert cb_packages.message.edit_text.call_count == 1
    print("  2.1 Callback menu:packages: PASS ✅ (answered and rendered main menu)")

    # Test menu:status callback
    cb_status = MagicMock()
    cb_status.data = "menu:status"
    cb_status.from_user.id = user_12h_due
    cb_status.from_user.username = "u12h_due"
    cb_status.from_user.first_name = "12H"
    cb_status.from_user.full_name = "12H Due User"
    cb_status.message.edit_text = AsyncMock()
    cb_status.answer = AsyncMock()

    await handle_my_status(cb_status, mock_state)
    assert cb_status.answer.call_count == 1, "FAILED: callback.answer() was not called for menu:status!"
    assert cb_status.message.edit_text.call_count == 1
    print("  2.2 Callback menu:status: PASS ✅ (answered and rendered status view)")

    # Test menu:my_status callback
    cb_my_status = MagicMock()
    cb_my_status.data = "menu:my_status"
    cb_my_status.from_user.id = user_12h_due
    cb_my_status.from_user.username = "u12h_due"
    cb_my_status.from_user.first_name = "12H"
    cb_my_status.from_user.full_name = "12H Due User"
    cb_my_status.message.edit_text = AsyncMock()
    cb_my_status.answer = AsyncMock()

    await handle_my_status(cb_my_status, mock_state)
    assert cb_my_status.answer.call_count == 1, "FAILED: callback.answer() was not called for menu:my_status!"
    assert cb_my_status.message.edit_text.call_count == 1
    print("  2.3 Callback menu:my_status: PASS ✅ (answered and rendered status view)")

    print("\n🎉 ALL 2-TIER EXPIRATION WARNING & BUTTON TESTS PASSED 100%!")

if __name__ == "__main__":
    asyncio.run(test_expiration_warning())
