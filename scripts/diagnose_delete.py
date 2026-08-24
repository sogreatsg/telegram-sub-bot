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
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from bot.config import get_settings

async def diagnose():
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)
    user_id = 8869252777

    me = await bot.get_me()
    print(f"Bot Username: @{me.username}")
    print(f"Target User ID: {user_id}")

    probe = await bot.send_message(chat_id=user_id, text=".")
    max_id = probe.message_id
    try:
        await bot.delete_message(chat_id=user_id, message_id=max_id)
    except Exception:
        pass

    print(f"Server max_id: {max_id}")
    
    deleted = []
    not_found = 0
    cant_be_deleted = 0
    other_errors = 0
    lock = asyncio.Lock()
    pause_until = 0.0

    queue = asyncio.Queue()
    for mid in range(max_id, 0, -1):
        queue.put_nowait(mid)

    async def worker():
        nonlocal not_found, cant_be_deleted, other_errors, pause_until
        while not queue.empty():
            import time
            now = time.time()
            if now < pause_until:
                await asyncio.sleep(pause_until - now + 0.1)
            try:
                mid = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
                async with lock:
                    deleted.append(mid)
                    print(f"🎯 [FOUND & DELETED BOT MSG] ID: {mid}")
                await asyncio.sleep(0.015)
            except TelegramBadRequest as e:
                async with lock:
                    msg = e.message.lower()
                    if "message to delete not found" in msg:
                        not_found += 1
                    elif "can't be deleted" in msg or "cannot be deleted" in msg:
                        cant_be_deleted += 1
                        print(f"⚠️ [USER MESSAGE DETECTED] ID {mid}: {e.message}")
                    else:
                        other_errors += 1
                        print(f"❓ ID {mid}: {e.message}")
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                queue.put_nowait(mid)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                async with lock:
                    other_errors += 1
            queue.task_done()
            await asyncio.sleep(0.015)

    workers = [asyncio.create_task(worker()) for _ in range(6)]
    await asyncio.gather(*workers)

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"Scan Completed: 1 to {max_id}")
    print(f"Total Bot Messages Deleted: {len(deleted)}")
    print(f"Messages Not Found on Server (Already deleted in Telegram Cloud): {not_found}")
    print(f"User Messages (Can't be deleted by bot): {cant_be_deleted}")
    print(f"Other: {other_errors}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(diagnose())
