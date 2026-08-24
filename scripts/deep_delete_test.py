import asyncio
import os
import sys
import time

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

async def deep_clean():
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)
    user_id = 8869252777

    me = await bot.get_me()
    print(f"🤖 Bot Username: @{me.username} (ID: {me.id}, Name: {me.first_name})", flush=True)
    print(f"👤 Target User ID: {user_id}", flush=True)

    probe = await bot.send_message(chat_id=user_id, text=".")
    max_id = probe.message_id
    print(f"📊 Message ID สูงสุดล่าสุด: {max_id}", flush=True)
    try:
        await bot.delete_message(chat_id=user_id, message_id=max_id)
    except Exception:
        pass

    total_to_scan = max_id - 1
    print(f"🚀 เริ่มต้นการล้างแชทใหม่ทั้งหมด 100% ตั้งแต่ ID {total_to_scan:,} ลงไปจนถึง ID 1...", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    deleted_ids = []
    cant_delete_ids = [] # User messages (Telegram returns can't be deleted)
    not_found_count = 0
    other_errors = 0
    scanned_count = 0
    current_min_id = total_to_scan
    lock = asyncio.Lock()
    pause_until = 0.0

    queue: asyncio.Queue[int] = asyncio.Queue()
    for mid in range(total_to_scan, 0, -1):
        queue.put_nowait(mid)

    start_time = time.time()

    async def worker(w_id: int):
        nonlocal scanned_count, not_found_count, other_errors, current_min_id, pause_until
        while not queue.empty():
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
                    deleted_ids.append(mid)
                    print(f"  🗑️ [ลบสำเร็จจริง] Message ID: {mid} | ยอดรวม: {len(deleted_ids)}", flush=True)
                await asyncio.sleep(0.015)
            except TelegramBadRequest as e:
                msg = e.message.lower()
                async with lock:
                    if "message to delete not found" in msg:
                        not_found_count += 1
                    elif "can't be deleted" in msg or "cannot be deleted" in msg:
                        cant_delete_ids.append(mid)
                        print(f"  👤 [ข้อความฝั่งผู้ใช้ - บอทลบไม่ได้ตามกฎ Telegram] ID {mid}", flush=True)
                    else:
                        other_errors += 1
                        print(f"  ❓ [Error อื่นๆ] ID {mid}: {e.message}", flush=True)
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    print(f"⚠️ [Rate Limit] Telegram แจ้งให้รอ {e.retry_after}s ที่ ID {mid}", flush=True)
                queue.put_nowait(mid)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                async with lock:
                    other_errors += 1

            async with lock:
                scanned_count += 1
                current_min_id = min(current_min_id, mid)
                if scanned_count % 500 == 0 or scanned_count == total_to_scan:
                    pct = (scanned_count / total_to_scan) * 100
                    elapsed = time.time() - start_time
                    speed = scanned_count / max(1.0, elapsed)
                    remain_sec = (total_to_scan - scanned_count) / max(1.0, speed)
                    print(
                        f"⏳ [ความคืบหน้า {pct:.1f}%] กำลังตรวจลบอยู่ที่ ID: {current_min_id:,} / {total_to_scan:,} "
                        f"(สปีด: {speed:.1f} IDs/s | เหลืออีก: {remain_sec/60:.1f} นาที) "
                        f"| ลบข้อความบอทแล้ว: {len(deleted_ids)} ข้อความ",
                        flush=True
                    )

            queue.task_done()
            await asyncio.sleep(0.015)

    workers = [asyncio.create_task(worker(i)) for i in range(6)]
    await asyncio.gather(*workers)

    elapsed_total = time.time() - start_time
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🎉 ตรวจสอบและลบครบ 100% ทุก Message ID สำหรับ User: {user_id}", flush=True)
    print(f"⏱️ ใช้เวลาทั้งหมด: {elapsed_total/60:.2f} นาที", flush=True)
    print(f"🔍 สแกนทั้งหมด: {scanned_count:,} IDs", flush=True)
    print(f"🗑️ ลบข้อความของบอทสำเร็จจริง: {len(deleted_ids)} ข้อความ (IDs: {deleted_ids})", flush=True)
    print(f"👤 ข้อความฝั่งผู้ใช้ (Telegram ไม่อนุญาตให้บอทลบ): {len(cant_delete_ids)} ข้อความ (IDs: {cant_delete_ids[:20]})", flush=True)
    print(f"⏭️ ข้อความที่ไม่อยู่ในระบบ (ถูกลบไปแล้ว): {not_found_count:,} ข้อความ", flush=True)
    print(f"⚠️ ข้อผิดพลาดอื่นๆ: {other_errors}", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(deep_clean())
