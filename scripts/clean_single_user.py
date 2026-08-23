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
from bot.services.chat_cleaner_state import (
    get_user_checkpoint,
    update_user_checkpoint,
)

async def clean_user_with_tuned_resume(target_user_id: int, override_start_id: int = None):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    # 1. Probe max_id
    probe = await bot.send_message(chat_id=target_user_id, text=".")
    max_id = probe.message_id
    try:
        await bot.delete_message(chat_id=target_user_id, message_id=max_id)
    except Exception:
        pass

    # 2. Check Checkpoint
    cp = get_user_checkpoint(target_user_id)
    accumulated_deleted = cp.get("deleted_count", 0) if cp else 0
    deleted_ids = list(cp.get("deleted_ids", [])) if cp else []

    start_mid = override_start_id or (cp.get("scanned_down_to_id") if cp else (max_id - 1))
    if not start_mid or start_mid <= 1:
        start_mid = max_id - 1

    total_remaining = start_mid

    print(f"🚀 เริ่มต้นการกวาดล้างต่อด้วยความเร็ว Tuned Mode (6 Workers @ 18.8 IDs/s)", flush=True)
    print(f"👤 User: {target_user_id}", flush=True)
    print(f"📊 Message ID สูงสุด: {max_id:,}", flush=True)
    print(f"🔄 ทำต่อจาก Checkpoint: Message ID {start_mid:,} ลงไปจนถึง ID 1 (เหลือ {total_remaining:,} ข้อความ)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    deleted_count = 0
    skipped_count = 0
    scanned_count = 0
    current_min_id = start_mid
    lock = asyncio.Lock()
    pause_until = 0.0

    queue: asyncio.Queue[int] = asyncio.Queue()
    for mid in range(start_mid, 0, -1):
        queue.put_nowait(mid)

    start_time = time.time()

    async def worker(worker_id: int):
        nonlocal scanned_count, skipped_count, deleted_count, current_min_id, pause_until
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
                    deleted_count += 1
                    deleted_ids.append(mid)
                    print(f"  🗑️ [ลบสำเร็จ] Message ID: {mid} | ยอดรวมบอทที่ลบได้: {accumulated_deleted + deleted_count} ข้อความ", flush=True)
                await asyncio.sleep(0.015)
            except TelegramBadRequest:
                async with lock:
                    skipped_count += 1
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    print(f"⚠️ [Rate Limit] Telegram แจ้งให้รอ {e.retry_after}s... ระบบหยุดพักที่ ID {mid}", flush=True)
                queue.put_nowait(mid)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception:
                async with lock:
                    skipped_count += 1

            async with lock:
                scanned_count += 1
                current_min_id = min(current_min_id, mid)
                if scanned_count % 200 == 0 or scanned_count == total_remaining:
                    pct = (scanned_count / total_remaining) * 100
                    total_pct = ((max_id - current_min_id) / max_id) * 100
                    elapsed = time.time() - start_time
                    speed = scanned_count / max(1.0, elapsed)
                    remain_sec = (total_remaining - scanned_count) / max(1.0, speed)
                    print(
                        f"⏳ [ภาพรวม {total_pct:.1f}% | ช่วงนี้ {pct:.1f}%] "
                        f"กำลังตรวจลบอยู่ที่ ID: {current_min_id:,} / {max_id:,} "
                        f"(สปีด: {speed:.1f} IDs/s | เหลืออีก: {remain_sec/60:.1f} นาที) "
                        f"| ลบข้อความบอทแล้ว: {accumulated_deleted + deleted_count} ข้อความ",
                        flush=True
                    )
                    update_user_checkpoint(
                        user_id=target_user_id,
                        max_id=max_id,
                        scanned_down_to_id=current_min_id,
                        deleted_count=accumulated_deleted + deleted_count,
                        status="IN_PROGRESS",
                        deleted_ids=deleted_ids,
                    )

            queue.task_done()
            await asyncio.sleep(0.015)

    # 6 Tuned Workers (18.8 IDs/s Sweet Spot)
    workers = [asyncio.create_task(worker(i)) for i in range(6)]
    await asyncio.gather(*workers)

    total_del = accumulated_deleted + deleted_count
    update_user_checkpoint(
        user_id=target_user_id,
        max_id=max_id,
        scanned_down_to_id=0,
        deleted_count=total_del,
        status="COMPLETED",
        deleted_ids=deleted_ids,
    )

    elapsed_total = time.time() - start_time
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🎉 กวาดล้างข้อความเสร็จสิ้น 100% ถึง ID 1 สำหรับ User: {target_user_id}", flush=True)
    print(f"⏱️ ใช้เวลาทั้งหมดช่วงนี้: {elapsed_total/60:.2f} นาที", flush=True)
    print(f"🔍 สแกนครบทั้งหมด: {scanned_count:,} Message IDs (ลงไปจนถึง ID 1)", flush=True)
    print(f"🗑️ ลบข้อความของบอทสำเร็จจริงรวม: {total_del} ข้อความ", flush=True)
    print(f"📋 รายการ Message ID ที่ลบได้: {deleted_ids}", flush=True)
    print(f"⏭️ ข้ามข้อความฝั่งผู้ใช้ / ที่ไม่มีอยู่: {skipped_count:,} ข้อความ", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    await bot.session.close()

if __name__ == "__main__":
    uid = 8869252777
    start_id = 11364  # Resume from 11,364
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        uid = int(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        start_id = int(sys.argv[2])
    asyncio.run(clean_user_with_tuned_resume(uid, override_start_id=start_id))
