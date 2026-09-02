import sys
import os
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, ChatMessage
from bot.services.database import init_db, close_db, get_session, get_or_create_user
from bot.services.chat_scanner import scan_and_sync_user_messages
from bot.handlers.admin import build_chat_history_view


async def test_chat_scanner_and_pagination():
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

    # Suppose user has message IDs 1 to 40
    TOTAL_MESSAGES = 40
    base_time = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)

    def mock_forward(chat_id, from_chat_id, message_id, disable_notification=True):
        if 1 <= message_id <= TOTAL_MESSAGES:
            fwd = MagicMock()
            fwd.message_id = message_id + 1000
            fwd.content_type = "text"
            fwd.text = f"Stealth message #{message_id}"
            fwd.caption = None
            fwd.from_user = MagicMock(id=test_uid)
            fwd.forward_from = None
            msg_time = base_time + timedelta(minutes=message_id)
            fwd.date = msg_time
            fwd.forward_date = msg_time
            return fwd
        else:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=MagicMock(), message="message to forward not found")

    mock_bot.forward_message = AsyncMock(side_effect=mock_forward)

    # Run scanner to sync 40 messages
    synced_count = await scan_and_sync_user_messages(bot=mock_bot, user_id=test_uid, max_scan=50)
    print(f"Scanner synced {synced_count} messages.")
    assert synced_count == 40, "Scanner should have synced all 40 messages into DB"
    assert not mock_bot.send_message.called, "bot.send_message must NEVER be called!"

    # Test Pagination (PAGE_SIZE is 15 -> 40 messages = 3 pages)
    # Page None -> default to latest page (Page 3)
    text_latest, markup_latest = await build_chat_history_view(user_id=test_uid, page=None)
    assert "หน้า 3/3" in text_latest
    assert "Stealth message #40" in text_latest
    assert markup_latest is not None
    print("Page 3 (Latest page with message #40) verified.")

    # Page 1 -> earliest messages
    text_p1, markup_p1 = await build_chat_history_view(user_id=test_uid, page=1)
    assert "หน้า 1/3" in text_p1
    assert "Stealth message #1" in text_p1
    print("Page 1 (Earliest page with message #1) verified.")

    # Page 2 -> middle messages
    text_p2, markup_p2 = await build_chat_history_view(user_id=test_uid, page=2)
    assert "หน้า 2/3" in text_p2
    assert "Stealth message #16" in text_p2
    print("Page 2 (Middle page with message #16) verified.")

    await close_db()
    print("All chat scanner & pagination tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_chat_scanner_and_pagination())
