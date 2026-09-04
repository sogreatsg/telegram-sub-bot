import sys
import os
import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus
from bot.services.database import init_db, close_db, get_session
from bot.handlers.channel_events import handle_channel_member_updated, handle_channel_join_request
from aiogram.enums import ChatMemberStatus

from sqlalchemy import delete

class TestChannelAuthorization(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        self.config = get_settings()
        async with get_session() as session:
            await session.execute(
                delete(Subscription).where(Subscription.user_id.in_([888001, 888002, 888003, 888004]))
            )
            await session.execute(
                delete(User).where(User.telegram_id.in_([888001, 888002, 888003, 888004]))
            )
            await session.commit()

    async def asyncTearDown(self):
        await close_db()

    async def test_v3_user_joins_v3_success(self):
        """User assigned to V.3 joins V.3 channel -> Authorized and accepted"""
        uid = 888001
        async with get_session() as session:
            user = User(telegram_id=uid, username="v3_user", full_name="V3 User", assigned_channel="TERTIARY")
            sub = Subscription(user_id=uid, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=10))
            session.add_all([user, sub])
            await session.commit()

        event = MagicMock()
        event.chat.id = self.config.TERTIARY_CHANNEL_ID
        event.chat.type = "channel"
        event.old_chat_member.status = ChatMemberStatus.LEFT
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        event.new_chat_member.user = MagicMock(id=uid, username="v3_user", full_name="V3 User", is_bot=False)
        event.invite_link = None

        bot = AsyncMock()
        mock_chat = MagicMock()
        mock_chat.title = "BareLive V.3"
        bot.get_chat.return_value = mock_chat
        bot.revoke_chat_invite_link = AsyncMock()
        bot.send_message = AsyncMock()
        bot.ban_chat_member = AsyncMock()
        bot.unban_chat_member = AsyncMock()

        await handle_channel_member_updated(event, bot)

        # Must NOT ban/soft-kick
        bot.ban_chat_member.assert_not_called()
        # Must send welcome DM
        welcome_call = bot.send_message.call_args_list[0]
        self.assertEqual(welcome_call.kwargs["chat_id"], uid)
        self.assertIn("ยินดีต้อนรับเข้าสู่ BareLive V.3", welcome_call.kwargs["text"])

        # DB must stay TERTIARY
        async with get_session() as session:
            db_u = await session.get(User, uid)
            self.assertEqual(db_u.assigned_channel, "TERTIARY")

    async def test_v3_user_clicks_old_v2_link_gets_softkicked(self):
        """User assigned to V.3 tries to enter V.2 channel -> Soft-kicked from V.2 immediately"""
        uid = 888002
        async with get_session() as session:
            user = User(telegram_id=uid, username="v3_user2", full_name="V3 User 2", assigned_channel="TERTIARY")
            sub = Subscription(user_id=uid, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=10))
            session.add_all([user, sub])
            await session.commit()

        event = MagicMock()
        event.chat.id = self.config.SECONDARY_CHANNEL_ID
        event.chat.type = "channel"
        event.old_chat_member.status = ChatMemberStatus.LEFT
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        event.new_chat_member.user = MagicMock(id=uid, username="v3_user2", full_name="V3 User 2", is_bot=False)
        event.invite_link = MagicMock(invite_link="https://t.me/+old_v2_link", is_primary=False)

        bot = AsyncMock()
        bot.revoke_chat_invite_link = AsyncMock()
        bot.send_message = AsyncMock()
        bot.ban_chat_member = AsyncMock()
        bot.unban_chat_member = AsyncMock()

        await handle_channel_member_updated(event, bot)

        # 1. Must revoke invite link
        bot.revoke_chat_invite_link.assert_called_once_with(
            chat_id=self.config.SECONDARY_CHANNEL_ID,
            invite_link="https://t.me/+old_v2_link",
        )

        # 2. Must soft-kick from V.2
        bot.ban_chat_member.assert_called_once_with(
            chat_id=self.config.SECONDARY_CHANNEL_ID,
            user_id=uid,
            revoke_messages=False,
        )
        bot.unban_chat_member.assert_called_once_with(
            chat_id=self.config.SECONDARY_CHANNEL_ID,
            user_id=uid,
            only_if_banned=True,
        )

        # 3. Must send wrong-channel warning DM to user
        user_dm_call = bot.send_message.call_args_list[0]
        self.assertEqual(user_dm_call.kwargs["chat_id"], uid)
        self.assertIn("ไม่สามารถเข้าร่วม BareLive V.2 ได้", user_dm_call.kwargs["text"])
        self.assertIn("BareLive V.3", user_dm_call.kwargs["text"])

        # 4. Must send security alert to admin group
        admin_alert_call = bot.send_message.call_args_list[1]
        self.assertEqual(admin_alert_call.kwargs["chat_id"], self.config.ADMIN_GROUP_ID)
        self.assertIn("ตรวจพบสมาชิกเข้าผิดห้อง Channel", admin_alert_call.kwargs["text"])
        self.assertIn("BareLive V.2", admin_alert_call.kwargs["text"])
        self.assertIn("BareLive V.3", admin_alert_call.kwargs["text"])

        # 5. DB must REMAIN TERTIARY (not mutated to SECONDARY!)
        async with get_session() as session:
            db_u = await session.get(User, uid)
            self.assertEqual(db_u.assigned_channel, "TERTIARY")

    async def test_v2_user_clicks_old_v3_link_gets_softkicked(self):
        """User assigned to V.2 tries to enter V.3 channel -> Soft-kicked from V.3 immediately"""
        uid = 888003
        async with get_session() as session:
            user = User(telegram_id=uid, username="v2_user3", full_name="V2 User 3", assigned_channel="SECONDARY", is_moved_to_secondary=True)
            sub = Subscription(user_id=uid, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=5))
            session.add_all([user, sub])
            await session.commit()

        event = MagicMock()
        event.chat.id = self.config.TERTIARY_CHANNEL_ID
        event.chat.type = "channel"
        event.old_chat_member.status = ChatMemberStatus.LEFT
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        event.new_chat_member.user = MagicMock(id=uid, username="v2_user3", full_name="V2 User 3", is_bot=False)
        event.invite_link = None

        bot = AsyncMock()
        bot.send_message = AsyncMock()
        bot.ban_chat_member = AsyncMock()
        bot.unban_chat_member = AsyncMock()

        await handle_channel_member_updated(event, bot)

        # Must soft-kick from V.3
        bot.ban_chat_member.assert_called_once_with(
            chat_id=self.config.TERTIARY_CHANNEL_ID,
            user_id=uid,
            revoke_messages=False,
        )
        bot.unban_chat_member.assert_called_once_with(
            chat_id=self.config.TERTIARY_CHANNEL_ID,
            user_id=uid,
            only_if_banned=True,
        )

        # DB must REMAIN SECONDARY
        async with get_session() as session:
            db_u = await session.get(User, uid)
            self.assertEqual(db_u.assigned_channel, "SECONDARY")

    async def test_join_request_to_wrong_channel_is_declined(self):
        """Join request to wrong channel is declined with notification"""
        uid = 888004
        async with get_session() as session:
            user = User(telegram_id=uid, username="v2_user4", full_name="V2 User 4", assigned_channel="SECONDARY", is_moved_to_secondary=True)
            sub = Subscription(user_id=uid, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=5))
            session.add_all([user, sub])
            await session.commit()

        event = MagicMock()
        event.chat.id = self.config.TERTIARY_CHANNEL_ID
        event.from_user = MagicMock(id=uid, username="v2_user4", full_name="V2 User 4", is_bot=False)

        bot = AsyncMock()
        bot.decline_chat_join_request = AsyncMock()
        bot.approve_chat_join_request = AsyncMock()
        bot.send_message = AsyncMock()

        await handle_channel_join_request(event, bot)

        bot.decline_chat_join_request.assert_called_once_with(chat_id=self.config.TERTIARY_CHANNEL_ID, user_id=uid)
        bot.approve_chat_join_request.assert_not_called()
        bot.send_message.assert_called_once()
        self.assertIn("ไม่สามารถเข้าร่วม BareLive V.3 ได้", bot.send_message.call_args.kwargs["text"])
        self.assertIn("BareLive V.2", bot.send_message.call_args.kwargs["text"])

    async def test_invite_button_is_updated_to_joined(self):
        """When user with last_invite_msg_id joins channel, message reply markup is edited to joined status"""
        uid = 888001
        invite_msg_id = 998877
        async with get_session() as session:
            user = User(
                telegram_id=uid,
                username="v3_user",
                full_name="V3 User",
                assigned_channel="TERTIARY",
                last_invite_msg_id=invite_msg_id,
            )
            sub = Subscription(user_id=uid, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=10))
            session.add_all([user, sub])
            await session.commit()

        event = MagicMock()
        event.chat.id = self.config.TERTIARY_CHANNEL_ID
        event.chat.type = "channel"
        event.old_chat_member.status = ChatMemberStatus.LEFT
        event.new_chat_member.status = ChatMemberStatus.MEMBER
        event.new_chat_member.user = MagicMock(id=uid, username="v3_user", full_name="V3 User", is_bot=False)
        event.invite_link = None

        bot = AsyncMock()
        mock_chat = MagicMock()
        mock_chat.title = "BareLive V.3"
        bot.get_chat.return_value = mock_chat
        bot.edit_message_reply_markup = AsyncMock()
        bot.send_message = AsyncMock()

        await handle_channel_member_updated(event, bot)

        # Verify edit_message_reply_markup was called to change button
        bot.edit_message_reply_markup.assert_called_once()
        call_kwargs = bot.edit_message_reply_markup.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], uid)
        self.assertEqual(call_kwargs["message_id"], invite_msg_id)
        keyboard_buttons = [btn.text for row in call_kwargs["reply_markup"].inline_keyboard for btn in row]
        self.assertIn("✅ เข้าร่วมห้องเรียบร้อยแล้ว", keyboard_buttons)

        # Verify last_invite_msg_id was cleared in DB
        async with get_session() as session:
            db_u = await session.get(User, uid)
            self.assertIsNone(db_u.last_invite_msg_id)


if __name__ == "__main__":
    unittest.main()
