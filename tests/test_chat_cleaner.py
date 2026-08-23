import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.services.chat_cleaner import clean_user_chat_messages

async def test_chat_cleaner():
    bot = AsyncMock()
    # Mock probe message
    probe_msg = MagicMock(message_id=25)
    bot.send_message.return_value = probe_msg
    bot.delete_message.return_value = True
    bot.delete_messages.return_value = True

    del_count, success, detail = await clean_user_chat_messages(bot, user_id=123456789)
    assert success is True, "clean_user_chat_messages should succeed"
    assert del_count > 0, "Should report deleted messages"
    assert bot.delete_message.called, "bot.delete_message should be called"

    print("Chat cleaner test passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_chat_cleaner())
