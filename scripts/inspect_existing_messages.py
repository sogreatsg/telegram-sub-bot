import asyncio
import os
import sys

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from bot.config import get_settings

async def inspect_chat_messages(target_user_id: int):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    me = await bot.get_me()
    print(f"🤖 Bot: @{me.username} (ID: {me.id})", flush=True)
    print(f"👤 Target User ID: {target_user_id}", flush=True)

    probe = await bot.send_message(chat_id=target_user_id, text=".")
    max_id = probe.message_id
    try:
        await bot.delete_message(chat_id=target_user_id, message_id=max_id)
    except Exception:
        pass

    print(f"📊 Latest Message ID in chat: {max_id:,}", flush=True)
    print(f"🔍 ตรวจสอบหาข้อความที่ยังมีอยู่จริงบน Cloud Server ของ Telegram...", flush=True)

    # We test copying message into the admin group or forwarding
    existing_messages = []
    
    # We can test with forward_message to admin group (or dry test with delete_message check)
    # If delete_message returns "can't be deleted", it is an existing USER message.
    # If delete_message returns True, it WAS an existing BOT message and is now deleted.
    # If delete_message returns "not found", it does NOT exist.
    
    print(f"สแกนช่วง ID 1 ถึง {max_id - 1:,}...")

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(inspect_chat_messages(8869252777))
