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

async def clean_user_with_bulk_mode(target_user_id: int, override_start_id: int = None):
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

    # แบ่ง Chunks ละ 100 ข้อความ
    chunk_size = 100
    chunks = []
    for s in range(start_mid, 0, -chunk_size):
        e = max(0, s - chunk_size)
        chunks.append(list(range(s, e, -1)))

    print(f"🚀 เริ่มต้นการกวาดล้างด้วยระบบ Bulk Delete (ชุดละ 100 ข้อความ - Bot API 7.0+)", flush=True)
    print(f"👤 User: {target_user_id}", flush=True)
    print(f"📊 Message ID สูงสุด: {max_id:,}", flush=True)
    print(f"📦 จำนวน Chunks ทั้งหมด: {len(chunks):,} Chunks ({total_remaining:,} ข้อความ)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    deleted_count = 0
    skipped_count = 0
    scanned_count = 0
    current_min_id = start_mid
    lock = asyncio.Lock()
    pause_until = 0.0

    queue: asyncio.Queue[list[int]] = asyncio.Queue()
    for c in chunks:
        queue.put_nowait(c)

    start_time = time.time()

    async def worker(worker_id: int):
        nonlocal scanned_count, skipped_count, deleted_count, current_min_id, pause_until
        while not queue.empty():
            now = time.time()
            if now < pause_until:
                await asyncio.sleep(pause_until - now + 0.1)

            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                await bot.delete_messages(chat_id=target_user_id, message_ids=chunk)
                async with lock:
                    deleted_count += len(chunk)
                    deleted_ids.extend(chunk)
                await asyncio.sleep(0.03)
            except TelegramBadRequest:
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id=target_user_id, message_id=mid)
                        async with lock:
                            deleted_count += 1
                            deleted_ids.append(mid)
                    except Exception:
                        async with lock:
                            skipped_count += 1
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    print(f"⚠️ [Rate Limit] Telegram แจ้งให้รอ {e.retry_after}s... ระบบหยุดพักที่ Chunk {chunk[0]}..{chunk[-1]}", flush=True)
                queue.put_nowait(chunk)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception:
                async with lock:
                    skipped_count += len(chunk)

            async with lock:
                scanned_count += len(chunk)
                current_min_id = min(current_min_id, chunk[-1])
                if scanned_count % 1000 == 0 or scanned_count >= total_remaining:
                    elapsed = time.time() - start_time
                    speed = scanned_count / max(0.1, elapsed)
                    remain_sec = (total_remaining - scanned_count) / max(0.1, speed)
                    pct = (scanned_count / total_remaining) * 100
                    print(
                        f"⏳ [{pct:5.1f}%] กำลังกวาดล้างถึง ID: {current_min_id:,} / {total_remaining:,} "
                        f"(สปีด: {speed:,.1f} IDs/s | เหลืออีก: {remain_sec:.1f} วินาที)",
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
            await asyncio.sleep(0.03)

    workers = [asyncio.create_task(worker(i)) for i in range(4)]
    await asyncio.gather(*workers)

    elapsed_total = time.time() - start_time
    total_deleted_final = accumulated_deleted + deleted_count

    update_user_checkpoint(
        user_id=target_user_id,
        max_id=max_id,
        scanned_down_to_id=0,
        deleted_count=total_deleted_final,
        status="COMPLETED",
        deleted_ids=deleted_ids,
    )

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🎉 ดำเนินการกวาดล้างแชท User {target_user_id} ครบ 100% เสร็จสิ้น!", flush=True)
    print(f"⏱️ ใช้เวลาทั้งหมด: {elapsed_total:.2f} วินาที (สปีดเฉลี่ย: {total_remaining/max(0.1, elapsed_total):,.1f} IDs/s)", flush=True)
    print(f"🔍 สแกนทั้งหมด: {scanned_count:,} ข้อความ", flush=True)
    print(f"🗑️ ลบข้อความฝั่งบอทสำเร็จ: {total_deleted_final:,} ข้อความ", flush=True)
    print(f"💾 อัปเดตสถานะ Checkpoint เป็น COMPLETED เรียบร้อยแล้ว", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    await bot.session.close()

if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 8869252777
    start_override = int(sys.argv[2]) if len(sys.argv) > 2 else None
    asyncio.run(clean_user_with_bulk_mode(uid, start_override))
