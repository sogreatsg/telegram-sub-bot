import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.models.schema import User
from bot.middlewares.v2_filter import V2MemberOnlyCallbackMiddleware

async def test_middleware():
    mw = V2MemberOnlyCallbackMiddleware()

    # Mock handler
    handler = AsyncMock()

    # 1. Test Admin Group callback
    event_admin_group = MagicMock()
    event_admin_group.__class__.__name__ = "CallbackQuery"
    data_admin = {
        "event_from_user": MagicMock(id=999),
        "event_chat": MagicMock(id=-1003893668383),  # Admin group
    }
    await mw(handler, event_admin_group, data_admin)
    assert handler.called, "Admin group callbacks should be passed to handler"

    print("Middleware test passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_middleware())
