import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.services.notification_settings import save_chat_group_id, get_saved_chat_group_id
from bot.services.channel_service import get_discussion_chat_id, resolve_chat_group

async def test_chat_group_resolution():
    # 1. Test save & get from JSON
    save_chat_group_id(-100999888777)
    assert get_saved_chat_group_id() == -100999888777

    bot = AsyncMock()
    bot.get_chat.return_value = MagicMock(id=-100999888777, title="Test Chat")

    # 2. Test get_discussion_chat_id
    cid = await get_discussion_chat_id(bot)
    assert cid == -100999888777

    # 3. Test explicit target
    cid2 = await get_discussion_chat_id(bot, explicit_target="-100111222333")
    assert cid2 == -100999888777  # mock returns same id or passed id

    print("Chat group resolution test passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_chat_group_resolution())
