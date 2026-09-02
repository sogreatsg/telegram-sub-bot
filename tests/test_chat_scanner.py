import sys
import os
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, ChatMessage
from bot.services.database import init_db, close_db, get_session, get_or_create_user
from bot.services.chat_scanner import scan_and_sync_user_messages


async def test_chat_scanner_stealth():
    await init_db()
    config = get_settings()

    test_uid = int(time.time() * 1000) % 1000000000
    async with get_session() as session:
        await get_or_create_user(
            session=session,
            telegram_id=test_uid,
            username="scanner_test_user",
            full_name="Scanner Test User",
        )

    # Setup mock bot
    mock_bot = MagicMock()
    mock_bot.id = 123456789
    mock_bot.get_me = AsyncMock(return_value=MagicMock(id=123456789))
    mock_bot.send_message = AsyncMock()  # Must NOT be called
    mock_bot.delete_message = AsyncMock()

    # Suppose user has message IDs 1 to 25
    TOTAL_MESSAGES = 25

    def mock_forward(chat_id, from_chat_id, message_id, disable_notification=True):
        if 1 <= message_id <= TOTAL_MESSAGES:
            fwd = MagicMock()
            fwd.message_id = message_id + 1000
            fwd.content_type = "text"
            fwd.text = f"Stealth message #{message_id}"
            fwd.caption = None
            fwd.from_user = MagicMock(id=test_uid)
            fwd.forward_from = None
            fwd.date = datetime.now(timezone.utc)
            fwd.forward_date = datetime.now(timezone.utc)
            return fwd
        else:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=MagicMock(), message="message to forward not found")

    mock_bot.forward_message = AsyncMock(side_effect=mock_forward)

    # Run scanner
    synced_count = await scan_and_sync_user_messages(bot=mock_bot, user_id=test_uid, max_scan=10)
    print(f"Scanner synced {synced_count} messages.")
    assert synced_count == 10, "Scanner should have synced 10 messages into DB"

    # CRITICAL CHECK: bot.send_message must NEVER be called (0 notifications to user!)
    assert not mock_bot.send_message.called, "bot.send_message must NEVER be called! (Stealth guaranteed)"

    # Verify messages in DB
    async with get_session() as session:
        from sqlalchemy import select
        msgs = (await session.execute(
            select(ChatMessage).where(ChatMessage.user_id == test_uid).order_by(ChatMessage.id.asc())
        )).scalars().all()
        assert len(msgs) == synced_count
        print(f"Verified {len(msgs)} messages in ChatMessage table.")

    await close_db()
    print("Stealth chat scanner tests passed successfully (User was 100% untouched)!")


if __name__ == "__main__":
    asyncio.run(test_chat_scanner_stealth())
