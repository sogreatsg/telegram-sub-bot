import asyncio
import sys
import sqlite3
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from bot.config import get_settings

async def reset_all():
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    print("[INFO] Connecting to SQLite database to find users to kick...")
    db_path = BASE_DIR / "data" / "bot.db"
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Find all users
    try:
        users = cur.execute("SELECT telegram_id FROM users").fetchall()
        user_ids = [u[0] for u in users]
    except Exception as e:
        print(f"[WARN] Error reading users: {e}")
        user_ids = []

    # Also make sure ID 8869252777 is included in case not in DB
    if 8869252777 not in user_ids:
        user_ids.append(8869252777)

    print(f"[INFO] Found {len(user_ids)} user(s) to kick: {user_ids}")

    # Kick each user from the channel
    for uid in user_ids:
        print(f"[INFO] Soft-kicking User ID={uid} from Channel {config.CHANNEL_ID}...")
        try:
            await bot.ban_chat_member(chat_id=config.CHANNEL_ID, user_id=uid)
            await bot.unban_chat_member(chat_id=config.CHANNEL_ID, user_id=uid, only_if_banned=True)
            print(f"[SUCCESS] User {uid} soft-kicked and unbanned (ready to join anew).")
        except TelegramBadRequest as e:
            print(f"[INFO] User {uid} was not in channel: {e.message}")
        except Exception as e:
            print(f"[WARN] Error kicking user {uid}: {e}")

    # Clear database tables
    print("[INFO] Clearing database tables (payment_slips, subscriptions, users)...")
    cur.execute("DELETE FROM payment_slips")
    cur.execute("DELETE FROM subscriptions")
    cur.execute("DELETE FROM users")
    con.commit()
    con.close()

    await bot.session.close()
    print("[SUCCESS] All test data wiped! Database is completely fresh and ready.")

if __name__ == "__main__":
    asyncio.run(reset_all())
