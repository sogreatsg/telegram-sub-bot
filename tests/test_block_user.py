import sys
import os
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import CallbackQuery, Message

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus
from bot.services.database import init_db, get_session, get_or_create_user
from bot.middlewares.v2_filter import (
    V2MemberOnlyCallbackMiddleware,
    V2MemberOnlyMessageMiddleware,
)
from bot.handlers.admin import (
    build_blocked_users_list_view,
    build_admin_user_action_keyboard,
    build_users_list_view,
)


async def test_block_and_unblock_flow():
    await init_db()
    config = get_settings()
    cb_mw = V2MemberOnlyCallbackMiddleware()
    msg_mw = V2MemberOnlyMessageMiddleware()

    test_uid = 888777666
    test_username = "test_block_target"
    test_fullname = "Test Block Target User"

    # 1. Setup User in DB (as a V.2 member so they would normally be allowed)
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=test_uid,
            username=test_username,
            full_name=test_fullname,
        )
        user.is_moved_to_secondary = True
        user.assigned_channel = "SECONDARY"
        user.is_blocked = False
        user.blocked_at = None
        user.blocked_reason = None
        session.add(user)

    handler = AsyncMock()

    # 2. Verify V.2 user is allowed when NOT blocked
    msg_event = MagicMock(spec=Message, text="Hello bot", caption=None)
    data_user = {
        "event_from_user": MagicMock(id=test_uid, username=test_username),
        "event_chat": MagicMock(id=test_uid, type="private"),
    }
    await msg_mw(handler, msg_event, data_user)
    assert handler.called, "Unblocked V2 user should be allowed to send messages"

    handler.reset_mock()
    cb_event = MagicMock(spec=CallbackQuery, data="menu:main", message=MagicMock())
    await cb_mw(handler, cb_event, data_user)
    assert handler.called, "Unblocked V2 user should be allowed to send callbacks"

    # 3. Block the user
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        u = await session.get(User, test_uid)
        u.is_blocked = True
        u.blocked_at = now
        u.blocked_reason = "Test block spam"
        session.add(u)

    # 4. Verify Blocked User's Message is SILENTLY DROPPED (handler NOT called)
    handler.reset_mock()
    await msg_mw(handler, msg_event, data_user)
    assert not handler.called, "Blocked user messages must be silently dropped (no handler execution)"

    # 5. Verify Blocked User's Callback is SILENTLY ANSWERED and DROPPED
    handler.reset_mock()
    cb_event.answer = AsyncMock()
    await cb_mw(handler, cb_event, data_user)
    assert not handler.called, "Blocked user callbacks must be silently dropped"
    assert cb_event.answer.called, "Callback must be answered to clear spinner"

    # 6. Verify User Action Keyboard reflects Blocked state
    async with get_session() as session:
        u = await session.get(User, test_uid)
        kb = build_admin_user_action_keyboard(u, test_uid)
        flat_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("ปลดบล็อก" in t for t in flat_texts), "Keyboard should have unblock button for blocked user"
        assert not any("🚫 บล็อกผู้ใช้" in t for t in flat_texts), "Keyboard should NOT have block button when already blocked"

    # 7. Verify Blocked Users List View
    text, markup = await build_blocked_users_list_view(page=1)
    assert "รายงานรายชื่อผู้ใช้ที่ถูกบล็อก" in text
    assert str(test_uid) in text
    assert "Test block spam" in text
    assert markup is not None

    # 8. Unblock the user
    async with get_session() as session:
        u = await session.get(User, test_uid)
        u.is_blocked = False
        u.blocked_at = None
        u.blocked_reason = None
        session.add(u)

    # 9. Verify User Action Keyboard reflects Unblocked state
    async with get_session() as session:
        u = await session.get(User, test_uid)
        kb_unblocked = build_admin_user_action_keyboard(u, test_uid)
        flat_texts_unblocked = [btn.text for row in kb_unblocked.inline_keyboard for btn in row]
        assert any("🚫 บล็อกผู้ใช้" in t for t in flat_texts_unblocked), "Keyboard should have block button for unblocked user"

    # 10. Verify Unblocked User can interact again
    handler.reset_mock()
    await msg_mw(handler, msg_event, data_user)
    assert handler.called, "Unblocked user should be able to send messages again"

    handler.reset_mock()
    await cb_mw(handler, cb_event, data_user)
    assert handler.called, "Unblocked user should be able to send callbacks again"

    print("All Block & Unblock User tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(test_block_and_unblock_flow())
