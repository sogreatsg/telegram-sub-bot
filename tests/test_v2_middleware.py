import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import CallbackQuery, Message

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User
from bot.services.database import init_db, close_db
from bot.middlewares.v2_filter import (
    V2MemberOnlyCallbackMiddleware,
    V2MemberOnlyMessageMiddleware,
)

async def test_middleware():
    await init_db()
    config = get_settings()
    cb_mw = V2MemberOnlyCallbackMiddleware()
    msg_mw = V2MemberOnlyMessageMiddleware()

    handler = AsyncMock()

    # 1. Test Admin Group callback
    event_admin_group = MagicMock(spec=CallbackQuery, data="admin:summary")
    data_admin = {
        "event_from_user": MagicMock(id=999),
        "event_chat": MagicMock(id=config.ADMIN_GROUP_ID),
    }
    await cb_mw(handler, event_admin_group, data_admin)
    assert handler.called, "Admin group callbacks should be passed to handler"

    # 2. Test Admin Group message
    handler.reset_mock()
    msg_admin_group = MagicMock(spec=Message, text="/admin")
    await msg_mw(handler, msg_admin_group, data_admin)
    assert handler.called, "Admin group messages should be passed to handler"

    # 3. Test non-V2 private DM message (should be passed to handler for logging and admin alert)
    handler.reset_mock()
    msg_private = MagicMock(spec=Message, text="/start", caption=None)
    data_non_v2 = {
        "event_from_user": MagicMock(id=999999, username="test_non_v2"),
        "event_chat": MagicMock(id=999999, type="private"),
    }
    await msg_mw(handler, msg_private, data_non_v2)
    assert handler.called, "Non-V2 private DM messages should be passed to handler for chat log & admin forwarding"

    # 4. Test non-V2 callback query (should be blocked)
    handler.reset_mock()
    cb_private = MagicMock(spec=CallbackQuery, data="menu:subscribe:VIP_30D", message=MagicMock())
    await cb_mw(handler, cb_private, data_non_v2)
    assert not handler.called, "Non-V2 callback queries should be completely blocked"

    await close_db()
    print("All middleware tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_middleware())
