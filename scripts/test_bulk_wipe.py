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

async def test_bulk_wipe(target_user_id: int):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    me = await bot.get_me()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🤖 Bot: @{me.username} (ID: {me.id})", flush=True)
    print(f"👤 Target User ID: {target_user_id}", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    # 1. Probe max_id
    probe = await bot.send_message(chat_id=target_user_id, text="⚡ ทดสอบระบบ Bulk Delete (API 7.0+)")
    max_id = probe.message_id
    print(f"📊 Message ID สูงสุดล่าสุด: {max_id:,}", flush=True)
    try:
        await bot.delete_message(chat_id=target_user_id, message_id=max_id)
    except Exception:
        pass

    # 2. Build chunks of 100 IDs
    total_ids = max_id - 1
    chunk_size = 100
    chunks = []
    for start in range(total_ids, 0, -chunk_size):
        end = max(0, start - chunk_size)
        chunks.append(list(range(start, end, -1)))

    print(f"🚀 เริ่มการล้างแชทแบบ Bulk Delete (กวาดชุดละ {chunk_size} IDs)...", flush=True)
    print(f"📦 จำนวน Chunks ทั้งหมด: {len(chunks):,} Chunks ({total_ids:,} IDs)", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    start_time = time.time()
    queue = asyncio.Queue()
    for c in chunks:
        queue.put_nowait(c)

    scanned_chunks = 0
    scanned_ids = 0
    success_chunks = 0
    error_chunks = 0
    lock = asyncio.Lock()
    pause_until = 0.0

    async def worker(w_id: int):
        nonlocal scanned_chunks, scanned_ids, success_chunks, error_chunks, pause_until
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
                    success_chunks += 1
                await asyncio.sleep(0.03)
            except TelegramBadRequest as e:
                # If bulk fails for any reason, try individual delete in chunk
                async with lock:
                    error_chunks += 1
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id=target_user_id, message_id=mid)
                    except Exception:
                        pass
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    print(f"⚠️ [Rate Limit] รอ {e.retry_after}s ที่ Chunk {chunk[0]}..{chunk[-1]}", flush=True)
                queue.put_nowait(chunk)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                async with lock:
                    error_chunks += 1

            async with lock:
                scanned_chunks += 1
                scanned_ids += len(chunk)
                if scanned_chunks % 20 == 0 or scanned_chunks == len(chunks):
                    pct = (scanned_chunks / len(chunks)) * 100
                    elapsed = time.time() - start_time
                    speed_ids = scanned_ids / max(0.1, elapsed)
                    print(f"⏳ [{pct:5.1f}%] สแกนแล้ว {scanned_ids:,}/{total_ids:,} IDs (Chunk {scanned_chunks}/{len(chunks)}) | ความเร็ว: {speed_ids:,.1f} IDs/s", flush=True)

            queue.task_done()
            await asyncio.sleep(0.03)

    # 4 workers with bulk delete (100 IDs each)
    workers = [asyncio.create_task(worker(i)) for i in range(4)]
    await asyncio.gather(*workers)

    elapsed_total = time.time() - start_time
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🎉 สแกนและกวาดล้างครบ 100% เรียบร้อยแล้ว!", flush=True)
    print(f"⏱️ ใช้เวลาทั้งหมด: {elapsed_total:.2f} วินาที (สปีดเฉลี่ย: {total_ids/max(0.1, elapsed_total):,.1f} IDs/s)", flush=True)
    print(f"📦 Bulk Chunks สำเร็จ: {success_chunks}/{len(chunks)} Chunks", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    await bot.session.close()

if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 8869252777
    asyncio.run(test_bulk_wipe(uid))
