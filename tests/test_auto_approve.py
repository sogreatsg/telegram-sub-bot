import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select, delete

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubscriptionGrant, PaymentSlip, SlipStatus, SubStatus, PlanType
from bot.services.database import init_db, get_session, get_or_create_user
from bot.services.payment_settings import update_auto_approve_setting, PAYMENT_SETTINGS_FILE
from bot.handlers.payment import process_truemoney_submission
from bot.handlers.admin import handle_admin_reject_auto

async def cleanup_test_data(user_ids):
    async with get_session() as session:
        for uid in user_ids:
            await session.execute(delete(SubscriptionGrant).where(SubscriptionGrant.user_id == uid))
            await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == uid))
            await session.execute(delete(Subscription).where(Subscription.user_id == uid))
            await session.execute(delete(User).where(User.telegram_id == uid))

async def run_async_tests():
    # 1. Setup DB
    await init_db()

    config = get_settings()
    test_user_ids = [999111222, 999333444]
    await cleanup_test_data(test_user_ids)

    # Backup payment settings
    backup_content = None
    if os.path.exists(PAYMENT_SETTINGS_FILE):
        with open(PAYMENT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            backup_content = f.read()

    try:
        # TEST 1: Auto-Approve Enabled (User sends TrueMoney link -> Auto-approved immediately)
        update_auto_approve_setting(True)

        user_id = 999111222
        user_name = "TestAutoUser"

        # Pre-create user as V.2 member
        async with get_session() as session:
            v2_user, _ = await get_or_create_user(session, user_id, user_name, "Test Auto")
            v2_user.assigned_channel = "SECONDARY"
            v2_user.is_moved_to_secondary = True
            session.add(v2_user)

        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.username = user_name
        mock_user.first_name = "Test"
        mock_user.full_name = "Test Auto"

        mock_msg = MagicMock()
        mock_msg.from_user = mock_user
        mock_msg.answer = AsyncMock()

        mock_state = MagicMock()
        mock_state.get_data = AsyncMock(return_value={"plan_type": PlanType.VIP_30D.value})
        mock_state.clear = AsyncMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        mock_bot.get_chat_member = AsyncMock(side_effect=Exception("Not in channel"))
        mock_invite_obj = MagicMock()
        mock_invite_obj.invite_link = "https://t.me/+mockInviteLink123"
        mock_bot.create_chat_invite_link = AsyncMock(return_value=mock_invite_obj)

        angpao_url_1 = "https://gift.truemoney.com/campaign/?v=testvoucher123"

        await process_truemoney_submission(
            message=mock_msg,
            state=mock_state,
            bot=mock_bot,
            angpao_url=angpao_url_1,
        )

        # Verify DB records
        async with get_session() as session:
            db_slip = (await session.execute(
                select(PaymentSlip).where(PaymentSlip.user_id == user_id, PaymentSlip.file_id == angpao_url_1)
            )).scalars().first()
            assert db_slip is not None, "PaymentSlip should be created"
            assert db_slip.status == SlipStatus.APPROVED.value, f"Expected APPROVED but got {db_slip.status}"
            slip_id = db_slip.id

            db_sub = await session.get(Subscription, user_id)
            assert db_sub is not None, "Subscription should exist"
            assert db_sub.status == SubStatus.PENDING.value, "Status should be PENDING (waiting to join channel)"
            assert db_sub.pending_days == 30, f"Expected 30 pending days but got {db_sub.pending_days}"

        # Verify User DM received invite link
        assert mock_msg.answer.called
        user_sent_text = mock_msg.answer.call_args[0][0]
        assert "อนุมัติอัตโนมัติ" in user_sent_text
        assert "30 วัน" in user_sent_text

        # Verify Admin notification
        assert mock_bot.send_message.called
        admin_call_kwargs = mock_bot.send_message.call_args[1]
        assert admin_call_kwargs["chat_id"] == config.ADMIN_GROUP_ID
        assert "AUTO-APPROVED" in admin_call_kwargs["text"]
        admin_kb = admin_call_kwargs["reply_markup"]
        all_cb_data = [btn.callback_data for row in admin_kb.inline_keyboard for btn in row]
        assert f"admin:reject_auto:{slip_id}" in all_cb_data

        print("Test 1: Auto-approval flow passed successfully!")

        # TEST 2: Admin rejects the auto-approved slip retroactively
        mock_callback = MagicMock()
        mock_admin_user = MagicMock()
        mock_admin_user.id = 111111
        mock_admin_user.username = "AdminUser"
        mock_admin_user.full_name = "Super Admin"
        mock_callback.from_user = mock_admin_user

        mock_cb_msg = MagicMock()
        mock_cb_msg.chat.id = config.ADMIN_GROUP_ID
        mock_cb_msg.caption = "Original Admin Caption"
        mock_cb_msg.text = None
        mock_cb_msg.edit_caption = AsyncMock()
        mock_cb_msg.edit_text = AsyncMock()
        mock_callback.message = mock_cb_msg
        mock_callback.data = f"admin:reject_auto:{slip_id}"
        mock_callback.answer = AsyncMock()

        await handle_admin_reject_auto(mock_callback, mock_bot)

        # Verify DB after rejection
        async with get_session() as session:
            db_slip_after = await session.get(PaymentSlip, slip_id)
            assert db_slip_after.status == SlipStatus.REJECTED.value, "Slip status should be REJECTED"

            db_sub_after = await session.get(Subscription, user_id)
            assert db_sub_after.pending_days == 0, f"Pending days should be revoked (0), got {db_sub_after.pending_days}"

        assert mock_cb_msg.edit_caption.called
        assert "ปฏิเสธและยกเลิกสิทธิ์ย้อนหลังแล้ว" in mock_cb_msg.edit_caption.call_args[1]["caption"]
        print("Test 2: Auto-approved slip rejection passed successfully!")

        # TEST 3: Auto-Approve Disabled (Standard Pending Flow)
        update_auto_approve_setting(False)

        user_id_2 = 999333444
        async with get_session() as session:
            v2_user_2, _ = await get_or_create_user(session, user_id_2, "TestPendingUser", "Pending User")
            v2_user_2.assigned_channel = "SECONDARY"
            v2_user_2.is_moved_to_secondary = True
            session.add(v2_user_2)

        mock_user_2 = MagicMock()
        mock_user_2.id = user_id_2
        mock_user_2.username = "TestPendingUser"
        mock_user_2.first_name = "Pending"
        mock_user_2.full_name = "Pending User"

        mock_msg_2 = MagicMock()
        mock_msg_2.from_user = mock_user_2
        mock_msg_2.answer = AsyncMock()

        mock_state_2 = MagicMock()
        mock_state_2.get_data = AsyncMock(return_value={"plan_type": PlanType.VIP_30D.value})
        mock_state_2.clear = AsyncMock()

        angpao_url_2 = "https://gift.truemoney.com/campaign/?v=pendingvoucher456"

        await process_truemoney_submission(
            message=mock_msg_2,
            state=mock_state_2,
            bot=mock_bot,
            angpao_url=angpao_url_2,
        )

        async with get_session() as session:
            db_slip_2 = (await session.execute(
                select(PaymentSlip).where(PaymentSlip.user_id == user_id_2, PaymentSlip.file_id == angpao_url_2)
            )).scalars().first()
            assert db_slip_2 is not None
            assert db_slip_2.status == SlipStatus.PENDING.value, f"Expected PENDING when auto-approve is OFF, got {db_slip_2.status}"

        print("Test 3: Standard pending flow when auto-approve is disabled passed successfully!")

    finally:
        # Cleanup
        await cleanup_test_data(test_user_ids)
        # Restore backup
        if backup_content is not None:
            with open(PAYMENT_SETTINGS_FILE, "w", encoding="utf-8") as f:
                f.write(backup_content)
        else:
            if os.path.exists(PAYMENT_SETTINGS_FILE):
                os.remove(PAYMENT_SETTINGS_FILE)

if __name__ == "__main__":
    asyncio.run(run_async_tests())
