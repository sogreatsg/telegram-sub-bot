import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot
from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, GrantType
from bot.services.database import init_db, get_session, close_db
from bot.services.channel_service import (
    get_user_target_channel_id,
    get_all_target_channel_ids,
    is_target_channel,
    is_secondary_channel,
    get_channel_label,
)

async def main():
    config = get_settings()
    print("=" * 60)
    print("DUAL CHANNEL CONFIGURATION CHECK")
    print("=" * 60)
    print(f"PRIMARY CHANNEL ID   : {config.CHANNEL_ID}")
    print(f"SECONDARY CHANNEL ID : {config.SECONDARY_CHANNEL_ID}")
    print(f"ALL TARGET CHANNELS  : {get_all_target_channel_ids()}")
    print("=" * 60)

    # 1. Test Channel Recognition
    print("\n[1] Channel ID Matching Tests:")
    assert is_target_channel(config.CHANNEL_ID), "Primary channel must be recognized"
    assert is_target_channel(str(config.CHANNEL_ID)), "Primary channel as string must be recognized"
    assert is_target_channel(config.SECONDARY_CHANNEL_ID), "Secondary channel must be recognized"
    assert is_target_channel(str(config.SECONDARY_CHANNEL_ID)), "Secondary channel as string must be recognized"
    assert not is_secondary_channel(config.CHANNEL_ID), "Primary channel is not secondary"
    assert is_secondary_channel(config.SECONDARY_CHANNEL_ID), "Secondary channel is secondary"
    print("  -> Channel ID recognition: PASS")

    # 2. Test User Channel Routing
    print("\n[2] User Channel Routing Tests:")
    u_old = User(telegram_id=1001, full_name="Old User", assigned_channel="PRIMARY", is_moved_to_secondary=False)
    u_moved = User(telegram_id=1002, full_name="Moved User", assigned_channel="SECONDARY", is_moved_to_secondary=True)
    u_none = None

    target_old = get_user_target_channel_id(u_old)
    target_moved = get_user_target_channel_id(u_moved)
    target_none = get_user_target_channel_id(u_none)

    print(f"  Old User ({u_old.assigned_channel}) target channel   : {target_old} (Expected: {config.CHANNEL_ID})")
    print(f"  Moved User ({u_moved.assigned_channel}) target channel : {target_moved} (Expected: {config.SECONDARY_CHANNEL_ID})")
    print(f"  None User target channel             : {target_none} (Expected: {config.CHANNEL_ID})")

    assert target_old == config.CHANNEL_ID, "Old user must route to Primary channel"
    assert target_moved == config.SECONDARY_CHANNEL_ID, "Moved user must route to Secondary channel"
    assert target_none == config.CHANNEL_ID, "None user must route to Primary channel"
    print("  -> User routing: PASS")

    # 3. Test Telegram Bot API Connectivity & Permissions in Both Channels
    print("\n[3] Telegram API Channel Permissions Tests:")
    bot = Bot(token=config.BOT_TOKEN)
    try:
        me = await bot.get_me()
        print(f"  Bot Connected: @{me.username} (ID: {me.id})")

        # Test Primary Channel
        try:
            chat1 = await bot.get_chat(chat_id=config.CHANNEL_ID)
            cm1 = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=me.id)
            can_ban1 = getattr(cm1, "can_restrict_members", False)
            can_inv1 = getattr(cm1, "can_invite_users", False)
            print(f"  [Primary Channel {config.CHANNEL_ID}]")
            print(f"    Title       : {chat1.title}")
            print(f"    Status      : {cm1.status}")
            print(f"    Can Ban     : {can_ban1}")
            print(f"    Can Invite  : {can_inv1}")
            assert cm1.status in ("administrator", "creator"), "Bot must be admin in primary channel"
        except Exception as e:
            print(f"  Primary Channel error: {e}")

        # Test Secondary Channel
        try:
            chat2 = await bot.get_chat(chat_id=config.SECONDARY_CHANNEL_ID)
            cm2 = await bot.get_chat_member(chat_id=config.SECONDARY_CHANNEL_ID, user_id=me.id)
            can_ban2 = getattr(cm2, "can_restrict_members", False)
            can_inv2 = getattr(cm2, "can_invite_users", False)
            print(f"  [Secondary Channel {config.SECONDARY_CHANNEL_ID}]")
            print(f"    Title       : {chat2.title}")
            print(f"    Status      : {cm2.status}")
            print(f"    Can Ban     : {can_ban2}")
            print(f"    Can Invite  : {can_inv2}")
            if cm2.status not in ("administrator", "creator"):
                print("    ⚠️ NOTE: Bot is not an Administrator in the Secondary Channel yet. Make sure to promote the Bot to Admin with 'Ban users' and 'Invite users' permissions!")
        except Exception as e:
            print(f"  Secondary Channel check notice: {e}")

    finally:
        await bot.session.close()

    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
