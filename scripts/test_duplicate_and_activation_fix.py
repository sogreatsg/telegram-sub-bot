import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.models.schema import User, Subscription, PaymentSlip, SlipStatus, SubStatus, PlanType, GrantType, PLAN_DETAILS
from bot.services.database import get_session, init_db
from bot.services.subscription import grant_subscription, activate_pending_subscription, subscription_status_label
from bot.handlers.payment import process_truemoney_submission
from bot.handlers.admin import handle_admin_approve
from sqlalchemy import select

async def test_all_fixes():
    print("\n--- [INIT DB] ---")
    await init_db()
    config = get_settings()

    import time
    base_id = int(time.time() * 100) % 100000000
    test_user_id = base_id + 1
    trial_user_id = base_id + 2
    fresh_user_id = base_id + 3
    submit_uid = base_id + 4

    print("\n--- [TEST 1] Paid 12-Hour VIP for User with trial_used=True ---")
    async with get_session() as session:
        # Create user with trial_used = True (already used trial previously)
        user = User(telegram_id=test_user_id, username="vip12h_user", full_name="VIP 12H User", trial_used=True)
        session.add(user)
        await session.flush()

        # Grant 12 hours (days=0, minutes=720, has_value=True)
        grant = await grant_subscription(
            session,
            user_id=test_user_id,
            days=0,
            minutes=720,
            source_label="สมาชิก ⚡ VIP 12 ชั่วโมง",
            grant_type=GrantType.PURCHASE.value,
            has_value=True,
            is_trial=False,
            is_in_channel=False,
        )
        assert grant.is_new_pending is True
        assert grant.subscription.pending_days == 0
        assert grant.subscription.pending_minutes == 720
        assert grant.subscription.pending_has_value is True
        print("  1.1 Grant 12H Pending: PASS ✅ (pending_minutes=720, has_value=True)")

        # Now simulate user entering the channel -> activate_pending_subscription
        activated_grant = await activate_pending_subscription(session, user_id=test_user_id)
        assert activated_grant is not None, "FAILED: 12-hour VIP was rejected as trial!"
        assert activated_grant.subscription.status == SubStatus.ACTIVE.value
        assert activated_grant.subscription.is_trial_active is False
        assert activated_grant.subscription.pending_minutes == 0
        now = datetime.now(timezone.utc)
        diff_hours = (activated_grant.subscription.expires_at.replace(tzinfo=timezone.utc) - now).total_seconds() / 3600
        assert 11.9 <= diff_hours <= 12.1, f"Expected ~12 hours, got {diff_hours}"
        print(f"  1.2 Activate 12H VIP: PASS ✅ (status=ACTIVE, is_trial_active=False, hours_granted={diff_hours:.2f}h)")

    print("\n--- [TEST 2] Free Trial for User with trial_used=True (Should be Rejected) ---")
    async with get_session() as session:
        trial_user = User(telegram_id=trial_user_id, username="trial_abuser", full_name="Trial Abuser", trial_used=True)
        session.add(trial_user)
        await session.flush()

        # Grant trial (has_value=False, is_trial=True)
        await grant_subscription(
            session,
            user_id=trial_user_id,
            days=0,
            minutes=15,
            source_label="⏱️ ทดลองใช้งานฟรี 15 นาที",
            grant_type=GrantType.TRIAL.value,
            has_value=False,
            is_trial=True,
            is_in_channel=False,
        )

        # Try to activate
        rejected = await activate_pending_subscription(session, user_id=trial_user_id)
        assert rejected is None, "FAILED: Trial abuser was allowed to activate trial!"
        print("  2.1 Reject Repeated Trial: PASS ✅ (returned None, trial abuse prevented)")

    print("\n--- [TEST 3] Free Trial for Fresh User with trial_used=False (Should Succeed) ---")
    async with get_session() as session:
        fresh_user = User(telegram_id=fresh_user_id, username="fresh_trial", full_name="Fresh Trial", trial_used=False)
        session.add(fresh_user)
        await session.flush()

        await grant_subscription(
            session,
            user_id=fresh_user_id,
            days=0,
            minutes=15,
            source_label="⏱️ ทดลองใช้งานฟรี 15 นาที",
            grant_type=GrantType.TRIAL.value,
            has_value=False,
            is_trial=True,
            is_in_channel=False,
        )

        trial_act = await activate_pending_subscription(session, user_id=fresh_user_id)
        assert trial_act is not None
        assert trial_act.subscription.status == SubStatus.ACTIVE.value
        assert trial_act.subscription.is_trial_active is True
        # Check user.trial_used is now True
        u = await session.get(User, fresh_user_id)
        assert u.trial_used is True
        print("  3.1 Activate Fresh Trial: PASS ✅ (status=ACTIVE, is_trial_active=True, user.trial_used=True)")

    print("\n--- [TEST 4] TrueMoney Angpao Submission Deduplication & Lock ---")
    angpao_url = f"https://gift.truemoney.com/campaign/?v=testdup_{submit_uid}"
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={"plan_type": PlanType.VIP_12H.value})
    mock_state.clear = AsyncMock()

    mock_msg1 = MagicMock()
    mock_msg1.from_user.id = submit_uid
    mock_msg1.from_user.username = "test_sub"
    mock_msg1.from_user.first_name = "Test"
    mock_msg1.from_user.full_name = "Test Sub"
    mock_msg1.answer = AsyncMock()

    # Submit 1
    await process_truemoney_submission(mock_msg1, mock_state, mock_bot, angpao_url)
    assert mock_bot.send_message.call_count == 1
    assert "ได้รับลิงก์ซองของขวัญ" in mock_msg1.answer.call_args[0][0]
    print("  4.1 First Angpao Submission: PASS ✅ (forwarded to admin group)")

    # Submit 2 (Immediate duplicate)
    mock_msg2 = MagicMock()
    mock_msg2.from_user.id = submit_uid
    mock_msg2.from_user.username = "test_sub"
    mock_msg2.from_user.first_name = "Test"
    mock_msg2.from_user.full_name = "Test Sub"
    mock_msg2.answer = AsyncMock()

    await process_truemoney_submission(mock_msg2, mock_state, mock_bot, angpao_url)
    # mock_bot.send_message should STILL be 1 (NOT 2)
    assert mock_bot.send_message.call_count == 1, f"Expected 1 admin message, got {mock_bot.send_message.call_count}"
    assert "คุณได้ส่งลิงก์ซองของขวัญนี้เข้าระบบไว้แล้ว" in mock_msg2.answer.call_args[0][0]
    print("  4.2 Duplicate Angpao Submission: PASS ✅ (blocked duplicate admin forward)")

    print("\n--- [TEST 5] Unban Before Invite Link Generation on Approval ---")
    mock_bot.get_chat_member = AsyncMock()
    mock_chat_member = MagicMock()
    mock_chat_member.status = "left" # not in channel
    mock_bot.get_chat_member.return_value = mock_chat_member
    mock_bot.unban_chat_member = AsyncMock()
    mock_invite = MagicMock()
    mock_invite.invite_link = "https://t.me/+testlink123"
    mock_bot.create_chat_invite_link = AsyncMock(return_value=mock_invite)

    # Get the slip ID created in Test 4
    async with get_session() as session:
        slip = (await session.execute(
            select(PaymentSlip).where(PaymentSlip.user_id == submit_uid)
        )).scalars().first()
        slip_id = slip.id

    mock_cb = MagicMock()
    mock_cb.data = f"admin:approve:{slip_id}"
    mock_cb.from_user.id = 1001
    mock_cb.from_user.username = "admin1"
    mock_cb.from_user.full_name = "Admin 1"
    mock_cb.message.chat.id = config.ADMIN_GROUP_ID
    mock_cb.message.caption = "Test Caption"
    mock_cb.message.text = None
    mock_cb.message.edit_caption = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_admin_approve(mock_cb, mock_bot)
    assert mock_bot.unban_chat_member.call_count >= 1, "FAILED: unban_chat_member was not called before invite link!"
    unban_kwargs = mock_bot.unban_chat_member.call_args[1]
    assert unban_kwargs.get("only_if_banned") is True
    assert unban_kwargs.get("user_id") == submit_uid
    print("  5.1 Auto-Unban Before Invite Link: PASS ✅ (unban_chat_member called with only_if_banned=True)")

    print("\n🎉 ALL FIX VERIFICATION TESTS PASSED 100%!")

if __name__ == "__main__":
    asyncio.run(test_all_fixes())
