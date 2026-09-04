import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus
from bot.services.channel_service import (
    is_target_channel,
    is_secondary_channel,
    is_tertiary_channel,
    get_all_target_channel_ids,
    get_user_target_channel_id,
    is_user_v2_member,
    get_channel_label,
    format_user_channel_presence,
    kick_user_from_all_target_channels,
)
from bot.services.scheduler import build_active_members_report, sync_pending_members
from bot.handlers.admin import (
    build_admin_user_action_keyboard,
    handle_admin_move_user_v3_command,
    handle_admin_quick_move_v3_callback,
    handle_admin_unmove_user_command,
)
from bot.services.database import init_db, close_db, get_session

async def test_v3_features():
    await init_db()
    config = get_settings()

    print("--- 1. Testing Config & Channel IDs ---")
    assert config.TERTIARY_CHANNEL_ID == -1003900142712, f"TERTIARY_CHANNEL_ID should be -1003900142712, got {config.TERTIARY_CHANNEL_ID}"
    assert is_target_channel(config.CHANNEL_ID) is True
    assert is_target_channel(config.SECONDARY_CHANNEL_ID) is True
    assert is_target_channel(config.TERTIARY_CHANNEL_ID) is True
    assert is_target_channel(-999999999) is False

    assert is_tertiary_channel(config.TERTIARY_CHANNEL_ID) is True
    assert is_tertiary_channel(config.SECONDARY_CHANNEL_ID) is False
    assert is_tertiary_channel(config.CHANNEL_ID) is False

    all_cids = get_all_target_channel_ids()
    assert config.CHANNEL_ID in all_cids
    assert config.SECONDARY_CHANNEL_ID in all_cids
    assert config.TERTIARY_CHANNEL_ID in all_cids
    assert len(all_cids) == 3

    print("--- 2. Testing Channel Labels & Presence Formatting ---")
    assert get_channel_label(config.TERTIARY_CHANNEL_ID) == "BareLive V.3"
    assert get_channel_label(config.SECONDARY_CHANNEL_ID) == "BareLive V.2"
    assert get_channel_label(config.CHANNEL_ID) == "BareLive"

    presence_single = format_user_channel_presence([config.TERTIARY_CHANNEL_ID])
    assert "BareLive V.3" in presence_single, f"Expected BareLive V.3 in presence, got {presence_single}"

    presence_all = format_user_channel_presence([config.CHANNEL_ID, config.SECONDARY_CHANNEL_ID, config.TERTIARY_CHANNEL_ID])
    assert "อยู่ในทั้ง 3 ห้อง" in presence_all

    print("--- 3. Testing User Routing & Permissions ---")
    u_v1 = User(telegram_id=2001, username="user_v1", assigned_channel="PRIMARY")
    u_v2 = User(telegram_id=2002, username="user_v2", assigned_channel="SECONDARY")
    u_v3 = User(telegram_id=2003, username="user_v3", assigned_channel="TERTIARY")

    assert get_user_target_channel_id(u_v1) == config.CHANNEL_ID
    assert get_user_target_channel_id(u_v2) == config.SECONDARY_CHANNEL_ID
    assert get_user_target_channel_id(u_v3) == config.TERTIARY_CHANNEL_ID
    assert get_user_target_channel_id(None) == config.CHANNEL_ID

    assert is_user_v2_member(u_v1) is False
    assert is_user_v2_member(u_v2) is True
    assert is_user_v2_member(u_v3) is True

    print("--- 4. Testing Admin User Keyboard ---")
    kb_v1 = build_admin_user_action_keyboard(u_v1, 2001)
    kb_v1_callbacks = [btn.callback_data for row in kb_v1.inline_keyboard for btn in row]
    assert "admin:quick_move_v3:2001" in kb_v1_callbacks
    assert "admin:quick_move:2001" in kb_v1_callbacks

    kb_v3 = build_admin_user_action_keyboard(u_v3, 2003)
    kb_v3_callbacks = [btn.callback_data for row in kb_v3.inline_keyboard for btn in row]
    assert "admin:quick_move_v3:2003" in kb_v3_callbacks
    assert "admin:quick_unmove:2003" in kb_v3_callbacks

    print("--- 5. Testing Soft-Kick across all 3 Channels ---")
    mock_bot = AsyncMock()
    mock_bot.ban_chat_member.return_value = True
    mock_bot.unban_chat_member.return_value = True

    kick_res = await kick_user_from_all_target_channels(mock_bot, 2003)
    assert len(kick_res["kicked_channels"]) == 3
    assert len(kick_res["failed_channels"]) == 0
    assert mock_bot.ban_chat_member.call_count == 3
    assert mock_bot.unban_chat_member.call_count == 3

    print("--- 6. Testing Scheduler Active Members Report ---")
    mock_bot.get_chat_member_count.return_value = 100
    mock_bot.get_chat_administrators.return_value = [MagicMock()]
    mock_tg_u = MagicMock()
    mock_tg_u.username = "test_user"
    mock_tg_u.full_name = "Test User"
    mock_bot.get_chat_member.return_value = MagicMock(status="member", user=mock_tg_u)

    report = await build_active_members_report(bot=mock_bot)
    assert "รายงานสรุปสถานะสมาชิก" in report
    assert str(config.TERTIARY_CHANNEL_ID) in report
    assert "BareLive V.3" in report

    print("--- 7. Testing /move_user_v3 Command ---")
    mock_message = AsyncMock()
    mock_message.chat.id = config.ADMIN_GROUP_ID
    mock_message.text = "/move_user_v3 2003"
    mock_message.answer = AsyncMock()

    mock_bot.create_chat_invite_link.return_value = MagicMock(invite_link="https://t.me/+mock_invite_v3")
    mock_bot.send_message.return_value = True

    # Seed user 2003 into database
    async with get_session() as session:
        from bot.services.database import get_or_create_user
        db_u, _ = await get_or_create_user(session, telegram_id=2003, username="user_v3", full_name="User V3 Test")
        db_u.assigned_channel = "PRIMARY"
        session.add(db_u)
        await session.commit()

    await handle_admin_move_user_v3_command(mock_message, mock_bot)
    assert mock_message.answer.called
    admin_answer_text = mock_message.answer.call_args[0][0]
    assert "ย้ายสมาชิกไปยัง" in admin_answer_text
    assert "BareLive V.3" in admin_answer_text

    async with get_session() as session:
        u_check = await session.get(User, 2003)
        assert u_check.assigned_channel == "TERTIARY"

    print("--- 8. Testing Quick Move V3 Callback ---")
    mock_callback = AsyncMock()
    mock_callback.message.chat.id = config.ADMIN_GROUP_ID
    mock_callback.from_user.id = 9999
    mock_callback.data = "admin:quick_move_v3:2003"
    mock_callback.answer = AsyncMock()
    mock_callback.message.answer = AsyncMock()

    await handle_admin_quick_move_v3_callback(mock_callback, mock_bot)
    assert mock_callback.message.answer.called
    assert mock_callback.answer.called

    print("--- 9. Testing Move Menu Callback ---")
    from bot.handlers.admin import handle_admin_move_menu_callback
    mock_menu_cb = AsyncMock()
    mock_menu_cb.message.chat.id = config.ADMIN_GROUP_ID
    mock_menu_cb.from_user.id = 9999
    mock_menu_cb.data = "admin:move_menu:2003"
    mock_menu_cb.answer = AsyncMock()
    mock_menu_cb.message.edit_text = AsyncMock()

    await handle_admin_move_menu_callback(mock_menu_cb, mock_bot)
    assert mock_menu_cb.message.edit_text.called
    menu_text = mock_menu_cb.message.edit_text.call_args[1]["text"]
    assert "เมนูจัดการย้าย Channel สำหรับสมาชิก" in menu_text

    await close_db()
    print("\n=========================================")
    print(" ALL V.3 TESTS PASSED SUCCESSFULLY! (100%)")
    print("=========================================\n")

if __name__ == "__main__":
    asyncio.run(test_v3_features())
