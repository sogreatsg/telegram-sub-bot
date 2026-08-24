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

async def analyze_skipped_messages(target_user_id: int):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    me = await bot.get_me()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🤖 กำลังวิเคราะห์ข้อความสำหรับ User: {target_user_id}", flush=True)
    print(f"🤖 บอท: @{me.username} (ID: {me.id})", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    probe = await bot.send_message(chat_id=target_user_id, text=".")
    max_id = probe.message_id
    try:
        await bot.delete_message(chat_id=target_user_id, message_id=max_id)
    except Exception:
        pass

    user_messages = []       # message can't be deleted
    not_found_messages = []  # message to delete not found
    other_errors = {}
    deleted_now = []

    # ตรวจสอบทีละ Chunk หรือทีละ ID
    chunk_size = 100
    chunks = []
    for s in range(max_id - 1, 0, -chunk_size):
        e = max(0, s - chunk_size)
        chunks.append(list(range(s, e, -1)))

    print(f"🔍 เริ่มการวิเคราะห์ Message IDs ทั้งหมด 1 ถึง {max_id - 1:,}...", flush=True)

    # Let's inspect each ID that gave an error
    for chunk in chunks:
        try:
            await bot.delete_messages(chat_id=target_user_id, message_ids=chunk)
        except TelegramBadRequest as e:
            # When a bulk chunk fails, inspect each ID in this chunk
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id=target_user_id, message_id=mid)
                    deleted_now.append(mid)
                except TelegramBadRequest as sub_e:
                    msg = sub_e.message.lower()
                    if "can't be deleted" in msg or "cannot be deleted" in msg:
                        user_messages.append(mid)
                    elif "message to delete not found" in msg:
                        not_found_messages.append(mid)
                    else:
                        other_errors[mid] = sub_e.message
                except Exception as ex:
                    other_errors[mid] = str(ex)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1.0)
            try:
                await bot.delete_messages(chat_id=target_user_id, message_ids=chunk)
            except Exception:
                pass
        except Exception as e:
            pass

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("📊 [ผลสรุปคำตอบจาก Telegram API ทั้งหมด]:", flush=True)
    print(f"1. 👤 ข้อความฝั่งผู้ใช้ (User Messages - Telegram ตอบ 'message can't be deleted'):")
    print(f"   • จำนวน: {len(user_messages)} ข้อความ", flush=True)
    if user_messages:
        print(f"   • ตัวอย่าง Message IDs: {user_messages[:15]}", flush=True)

    print(f"\n2. 💨 ข้อความที่ไม่มีบน Server แล้ว (Telegram ตอบ 'message to delete not found'):")
    print(f"   • จำนวน: {len(not_found_messages)} ข้อความ", flush=True)
    if not_found_messages:
        print(f"   • ตัวอย่าง Message IDs: {not_found_messages[:15]}", flush=True)

    if other_errors:
        print(f"\n3. ❓ ข้อผิดพลาดอื่นๆ: {len(other_errors)} ข้อความ", flush=True)
        for mid, err in list(other_errors.items())[:5]:
            print(f"   • ID {mid}: {err}", flush=True)
    else:
        print(f"\n3. ❓ ข้อผิดพลาดอื่นๆ: ไม่มี (0 ข้อความ)", flush=True)

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(analyze_skipped_messages(8869252777))
