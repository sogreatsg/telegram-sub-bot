import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User
from bot.services.database import init_db, close_db, get_session
from bot.handlers.user_menu import handle_start

from sqlalchemy import delete

class TestStartNotification(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        self.config = get_settings()
        async with get_session() as session:
            await session.execute(
                delete(User).where(User.telegram_id.in_([99911101, 99911102, 99911103]))
            )
            await session.commit()

    async def asyncTearDown(self):
        await close_db()

    async def test_v1_user_start(self):
        user_id = 99911101
        async with get_session() as session:
            # Create V1 user (not moved to v2/v3)
            user = User(
                telegram_id=user_id,
                username="v1_starter",
                full_name="V1 Starter",
                assigned_channel="PRIMARY",
                is_moved_to_secondary=False,
            )
            session.add(user)
            await session.commit()

        message = MagicMock()
        message.from_user = MagicMock(id=user_id, username="v1_starter", full_name="V1 Starter", first_name="V1")
        message.text = "/start"
        message.answer = AsyncMock()

        state = MagicMock()
        state.clear = AsyncMock()

        bot = MagicMock()
        bot.send_message = AsyncMock()

        await handle_start(message=message, state=state, bot=bot)

        # 1. Verify admin alert was sent
        bot.send_message.assert_called_once()
        admin_call_kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(admin_call_kwargs["chat_id"], self.config.ADMIN_GROUP_ID)
        self.assertIn("BareLive V.1", admin_call_kwargs["text"])
        self.assertIn("บอทไม่ตอบกลับใน DM", admin_call_kwargs["text"])

        # 2. Verify bot did NOT reply to the V1 user's DM
        message.answer.assert_not_called()

    async def test_v2_user_start(self):
        user_id = 99911102
        async with get_session() as session:
            # Create V2 user
            user = User(
                telegram_id=user_id,
                username="v2_starter",
                full_name="V2 Starter",
                assigned_channel="SECONDARY",
                is_moved_to_secondary=False,
            )
            session.add(user)
            await session.commit()

        message = MagicMock()
        message.from_user = MagicMock(id=user_id, username="v2_starter", full_name="V2 Starter", first_name="V2")
        message.text = "/start"
        message.answer = AsyncMock()

        state = MagicMock()
        state.clear = AsyncMock()

        bot = MagicMock()
        bot.send_message = AsyncMock()

        await handle_start(message=message, state=state, bot=bot)

        # 1. Verify admin alert was sent
        bot.send_message.assert_called_once()
        admin_call_kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(admin_call_kwargs["chat_id"], self.config.ADMIN_GROUP_ID)
        self.assertIn("BareLive V.2", admin_call_kwargs["text"])
        self.assertIn("ตอบกลับเมนูหลักใน DM", admin_call_kwargs["text"])

        # 2. Verify bot DID reply to V2 user's DM
        message.answer.assert_called_once()

    async def test_v3_user_start(self):
        user_id = 99911103
        async with get_session() as session:
            # Create V3 user
            user = User(
                telegram_id=user_id,
                username="v3_starter",
                full_name="V3 Starter",
                assigned_channel="TERTIARY",
                is_moved_to_secondary=False,
            )
            session.add(user)
            await session.commit()

        message = MagicMock()
        message.from_user = MagicMock(id=user_id, username="v3_starter", full_name="V3 Starter", first_name="V3")
        message.text = "/start"
        message.answer = AsyncMock()

        state = MagicMock()
        state.clear = AsyncMock()

        bot = MagicMock()
        bot.send_message = AsyncMock()

        await handle_start(message=message, state=state, bot=bot)

        # 1. Verify admin alert was sent
        bot.send_message.assert_called_once()
        admin_call_kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(admin_call_kwargs["chat_id"], self.config.ADMIN_GROUP_ID)
        self.assertIn("BareLive V.3", admin_call_kwargs["text"])
        self.assertIn("ตอบกลับเมนูหลักใน DM", admin_call_kwargs["text"])

        # 2. Verify bot DID reply to V3 user's DM
        message.answer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
