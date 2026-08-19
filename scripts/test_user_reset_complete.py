import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.models.schema import User, Subscription, SubscriptionGrant, PaymentSlip, ChatMessage, SubStatus, SlipStatus, GrantType
from bot.services.database import get_session, init_db, get_or_create_user
from bot.handlers.admin import handle_admin_do_reset_user_callback, handle_admin_reset_user_command
from sqlalchemy import select, func

async def test_reset_user_complete():
    print("\n--- [INIT DB] ---")
    await init_db()

    import time
    test_uid = int(time.time() * 1000) % 100000000
    print(f"\n--- [SETUP] Populating full history for User {test_uid} ---")
    async with get_session() as session:
        # 1. User
        user = User(
            telegram_id=test_uid,
            username="reset_tester",
            full_name="Reset Tester",
            trial_used=True,
            referral_count=3,
            referral_bonus_days=3,
        )
        session.add(user)

        # 2. Subscription
        sub = Subscription(
            user_id=test_uid,
            status=SubStatus.ACTIVE.value,
            expires_at=datetime.now(timezone.utc),
            joined_at=datetime.now(timezone.utc),
            source_label="VIP 30 วัน",
        )
        session.add(sub)

        # 3. Subscription Grants
        session.add(SubscriptionGrant(
            user_id=test_uid,
            days=30,
            minutes=0,
            source_label="VIP 30 วัน",
            grant_type=GrantType.PURCHASE.value,
        ))
        session.add(SubscriptionGrant(
            user_id=test_uid,
            days=1,
            minutes=0,
            source_label="โบนัสชวนเพื่อน",
            grant_type=GrantType.REFERRAL_BONUS.value,
        ))

        # 4. Payment Slips
        session.add(PaymentSlip(
            user_id=test_uid,
            file_id="slip_photo_123",
            status=SlipStatus.APPROVED.value,
        ))

        # 5. Chat Messages
        session.add(ChatMessage(
            user_id=test_uid,
            sender_role="USER",
            message_text="สวัสดีครับ ขอสอบถาม",
        ))
        session.add(ChatMessage(
            user_id=test_uid,
            sender_role="BOT",
            message_text="ยินดีต้อนรับครับ",
        ))

        await session.commit()
    print("  User history populated successfully ✅")

    print("\n--- [EXECUTE RESET] Running handle_admin_do_reset_user_callback ---")
    mock_bot = MagicMock()
    mock_bot.ban_chat_member = AsyncMock()
    mock_bot.unban_chat_member = AsyncMock()

    mock_cb = MagicMock()
    mock_cb.data = f"admin:do_reset_user:{test_uid}"
    mock_cb.from_user.id = 1001
    mock_cb.message.chat.id = -1001923058869
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    from bot.config import get_settings
    config = get_settings()
    mock_cb.message.chat.id = config.ADMIN_GROUP_ID

    await handle_admin_do_reset_user_callback(mock_cb, mock_bot)

    print("\n--- [VERIFY DELETION] Checking all DB tables ---")
    async with get_session() as session:
        # Check User
        u = (await session.execute(select(User).where(User.telegram_id == test_uid))).scalar_one_or_none()
        assert u is None, "FAILED: User still exists in users table!"
        print("  1. User table: DELETED ✅")

        # Check Subscription
        s = (await session.execute(select(Subscription).where(Subscription.user_id == test_uid))).scalar_one_or_none()
        assert s is None, "FAILED: Subscription still exists in subscriptions table!"
        print("  2. Subscriptions table: DELETED ✅")

        # Check Grants
        g_count = (await session.execute(select(func.count(SubscriptionGrant.id)).where(SubscriptionGrant.user_id == test_uid))).scalar()
        assert g_count == 0, f"FAILED: {g_count} grants still remain in subscription_grants table!"
        print("  3. SubscriptionGrants table: DELETED ✅ (0 rows)")

        # Check Payment Slips
        p_count = (await session.execute(select(func.count(PaymentSlip.id)).where(PaymentSlip.user_id == test_uid))).scalar()
        assert p_count == 0, f"FAILED: {p_count} slips still remain in payment_slips table!"
        print("  4. PaymentSlips table: DELETED ✅ (0 rows)")

        # Check Chat Messages
        c_count = (await session.execute(select(func.count(ChatMessage.id)).where(ChatMessage.user_id == test_uid))).scalar()
        assert c_count == 0, f"FAILED: {c_count} messages still remain in chat_messages table!"
        print("  5. ChatMessages table: DELETED ✅ (0 rows)")

    print("\n--- [VERIFY TELEGRAM ACTIONS] Checking Kick & Unban ---")
    assert mock_bot.ban_chat_member.call_count == 1, "FAILED: ban_chat_member was not called!"
    assert mock_bot.unban_chat_member.call_count == 1, "FAILED: unban_chat_member was not called!"
    assert mock_bot.unban_chat_member.call_args[1].get("only_if_banned") is True
    print("  6. Telegram Channel Kick & Unban: PASS ✅ (banned to kick, then unbanned to clear blacklist)")

    print("\n--- [VERIFY FRESH USER START] User returns to bot after reset ---")
    async with get_session() as session:
        fresh_u, created = await get_or_create_user(
            session=session,
            telegram_id=test_uid,
            username="reset_tester",
            full_name="Reset Tester",
        )
        assert created is True, "FAILED: User was not treated as brand new!"
        assert fresh_u.trial_used is False, "FAILED: trial_used was not False!"
        assert fresh_u.referral_count == 0
        assert fresh_u.referral_bonus_days == 0
        print("  7. Fresh /start Simulation: PASS ✅ (created=True, trial_used=False, 100% brand new user)")

    print("\n🎉 ALL USER RESET TESTS PASSED 100%!")

if __name__ == "__main__":
    asyncio.run(test_reset_user_complete())
