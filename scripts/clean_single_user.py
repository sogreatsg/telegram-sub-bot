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

async def clean_user_to_zero(target_user_id: int):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    # 1. Probe max_id
    probe = await bot.send_message(chat_id=target_user_id, text=".")
    max_id = probe.message_id
    try:
        await bot.delete_message(chat_id=target_user_id, message_id=max_id)
    except Exception:
        pass

    total_to_scan = max_id - 1
    print(f"🚀 เริ่มต้นการกวาดล้างข้อความของ User: {target_user_id}", flush=True)
    print(f"📊 Message ID สูงสุดที่ตรวจพบ: ID {max_id}", flush=True)
    print(f"🎯 เป้าหมาย: สแกนและลบข้อความตั้งแต่ ID {total_to_scan:,} ลงไปจนถึง ID 1 (รวม {total_to_scan:,} ข้อความ)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    deleted_ids = []
    skipped_count = 0
    scanned_count = 0
    current_min_id = total_to_scan
    lock = asyncio.Lock()
    pause_until = 0.0

    queue: asyncio.Queue[int] = asyncio.Queue()
    for mid in range(total_to_scan, 0, -1):
        queue.put_nowait(mid)

    start_time = time.time()

    async def worker(worker_id: int):
        nonlocal scanned_count, skipped_count, current_min_id, pause_until
        while not queue.empty():
            now = time.time()
            if now < pause_until:
                await asyncio.sleep(pause_until - now + 0.1)

            try:
                mid = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                await bot.delete_message(chat_id=target_user_id, message_id=mid)
                async with lock:
                    deleted_ids.append(mid)
                    print(f"  🗑️ [ลบสำเร็จ] Message ID: {mid} (ลบของบอทไปแล้วรวม: {len(deleted_ids)} ข้อความ)", flush=True)
                await asyncio.sleep(0.04)
            except TelegramBadRequest:
                async with lock:
                    skipped_count += 1
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    print(f"⚠️ [Rate Limit] Telegram แจ้งให้รอ {e.retry_after}s... ระบบจะหยุดพักที่ ID {mid}", flush=True)
                queue.put_nowait(mid)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                async with lock:
                    skipped_count += 1

            async with lock:
                scanned_count += 1
                current_min_id = min(current_min_id, mid)
                if scanned_count % 100 == 0 or scanned_count == total_to_scan:
                    pct = (scanned_count / total_to_scan) * 100
                    elapsed = time.time() - start_time
                    speed = scanned_count / max(1.0, elapsed)
                    remain_sec = (total_to_scan - scanned_count) / max(1.0, speed)
                    print(
                        f"⏳ [ความคืบหน้า {pct:.1f}%] กำลังตรวจลบอยู่ที่ ID: {current_min_id:,} / {total_to_scan:,} "
                        f"(สแกนแล้ว {scanned_count:,} IDs | ความเร็ว: {speed:.1f} IDs/s | เหลือ: {remain_sec/60:.1f} นาที) "
                        f"| ลบข้อความบอทแล้ว: {len(deleted_ids)} ข้อความ",
                        flush=True
                    )

            queue.task_done()
            await asyncio.sleep(0.03)

    # 4 workers running gently and reliably
    workers = [asyncio.create_task(worker(i)) for i in range(4)]
    await asyncio.gather(*workers)

    elapsed_total = time.time() - start_time
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🎉 กวาดล้างข้อความเสร็จสิ้น 100% สำหรับ User: {target_user_id}", flush=True)
    print(f"⏱️ ใช้เวลาทั้งหมด: {elapsed_total/60:.2f} นาที", flush=True)
    print(f"🔍 สแกนครบทั้งหมด: {scanned_count:,} Message IDs (ตั้งแต่ ID {total_to_scan:,} ลงไปจนถึง ID 1)", flush=True)
    print(f"🗑️ ลบข้อความของบอทสำเร็จจริง: {len(deleted_ids)} ข้อความ", flush=True)
    print(f"📋 รายการ Message ID ที่ลบได้: {deleted_ids}", flush=True)
    print(f"⏭️ ข้ามข้อความฝั่งผู้ใช้ / ที่ไม่มีอยู่: {skipped_count:,} ข้อความ", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    await bot.session.close()

if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 8869252777
    asyncio.run(clean_user_to_zero(uid))
