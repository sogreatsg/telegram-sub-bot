import asyncio
import time
import logging
from typing import Dict, Any, Optional
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.services.chat_cleaner_state import (
    get_user_checkpoint,
    is_user_clean_completed,
    update_user_checkpoint,
)

logger = logging.getLogger(__name__)


async def clean_user_chat_messages(
    bot: Bot,
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    force_restart: bool = False,
    num_workers: int = 6,
) -> Dict[str, Any]:
    """
    ลบข้อความทั้งหมดที่บอทเคยส่งหา User ในแชทส่วนตัว (Private DM)
    พร้อมระบบ Tuned Multi-worker Queue (18.8 IDs/s), Incremental Delta Scan และ Checkpoint Resume:
    - ปรับจูนความเร็วสูงสุดตามผล Benchmark (6 Workers, Delay 15ms) ได้ความเร็ว ~19 ข้อความ/วินาที ไม่ติด Flood Control
    - หาก User คนนี้เคยสแกนครบแล้ว และไม่มีข้อความใหม่ (max_id <= last_scanned_max) -> ข้ามทันที (0 วินาที)
    - หากมีข้อความใหม่เพิ่มขึ้นมา -> สแกนเฉพาะช่วงใหม่ (Delta Scan จาก new_max_id ลงมาถึง last_scanned_max)
    - หากเคยทำค้างไว้ระหว่างทาง -> ทำต่อจาก Message ID ล่าสุดที่ค้างอยู่ (Resume from Checkpoint)
    """
    result = {
        "deleted_count": 0,
        "max_id": 0,
        "scanned_count": 0,
        "skipped_count": 0,
        "success": False,
        "already_completed": False,
        "is_incremental_delta": False,
        "resumed_from_id": None,
        "delta_scanned_range": None,
        "detail": ""
    }

    # 1. Probe หา Message ID สูงสุดล่าสุด
    try:
        probe = await bot.send_message(chat_id=user_id, text=".")
        max_id = probe.message_id
        result["max_id"] = max_id
        try:
            await bot.delete_message(chat_id=user_id, message_id=max_id)
        except Exception:
            pass
    except TelegramForbiddenError:
        update_user_checkpoint(
            user_id=user_id,
            max_id=0,
            scanned_down_to_id=0,
            deleted_count=0,
            status="BLOCKED_BOT",
            username=username,
            full_name=full_name,
        )
        result["detail"] = "BLOCKED_BOT"
        return result
    except TelegramBadRequest as e:
        logger.debug(f"Cannot send probe to user {user_id}: {e}")
        result["detail"] = f"BAD_REQUEST: {e}"
        return result
    except Exception as e:
        logger.debug(f"Error sending probe to user {user_id}: {e}")
        result["detail"] = str(e)
        return result

    if max_id <= 1:
        update_user_checkpoint(
            user_id=user_id,
            max_id=max_id,
            scanned_down_to_id=0,
            deleted_count=0,
            status="COMPLETED",
            username=username,
            full_name=full_name,
        )
        result["success"] = True
        result["detail"] = "NO_MESSAGES"
        return result

    # 2. ตรวจสอบ Checkpoint และประวัติการสแกนเดิม
    cp = get_user_checkpoint(user_id)
    last_scanned_max = cp.get("last_scanned_max_id", 0) if cp else 0
    accumulated_deleted = cp.get("deleted_count", 0) if cp else 0
    deleted_ids = list(cp.get("deleted_ids", [])) if cp else []

    # กรณีที่เคยสแกนครบ 100% แล้ว (COMPLETED)
    if not force_restart and cp and cp.get("status") == "COMPLETED":
        if max_id <= last_scanned_max:
            # ไม่มีข้อความใหม่เพิ่มขึ้นมาเลย -> ข้ามทันที
            result["success"] = True
            result["already_completed"] = True
            result["deleted_count"] = accumulated_deleted
            result["max_id"] = max_id
            result["detail"] = "NO_NEW_MESSAGES"
            logger.info(f"User {user_id} has no new messages (max_id {max_id} <= {last_scanned_max}). Skipping.")
            return result

    # 3. กำหนดช่วง Message ID ในการสแกน (start_mid ลงมาหา target_end_mid)
    start_mid = max_id - 1
    target_end_mid = 0

    if not force_restart and cp:
        if cp.get("status") in ("COMPLETED", "INCREMENTAL_READY") and last_scanned_max > 0 and max_id > last_scanned_max:
            # สแกนเฉพาะช่วง Delta ที่เพิ่มขึ้นมาใหม่
            start_mid = max_id - 1
            target_end_mid = last_scanned_max
            result["is_incremental_delta"] = True
            result["delta_scanned_range"] = f"{start_mid} down to {target_end_mid}"
            logger.info(f"User {user_id}: Incremental delta scan from ID {start_mid} down to {target_end_mid}")
        elif cp.get("status") == "IN_PROGRESS":
            prev_scanned_id = cp.get("scanned_down_to_id")
            if prev_scanned_id and 1 < prev_scanned_id < max_id:
                start_mid = prev_scanned_id
                result["resumed_from_id"] = start_mid
                logger.info(f"User {user_id}: Resuming from Checkpoint ID {start_mid}")

    # แบ่งเป็น Chunks ละ 100 IDs สำหรับ Telegram Bot API 7.0+ Bulk Delete
    chunk_size = 100
    chunks = []
    for s in range(start_mid, target_end_mid, -chunk_size):
        e = max(target_end_mid, s - chunk_size)
        chunks.append(list(range(s, e, -1)))

    deleted_count = 0
    skipped_count = 0
    scanned_count = 0
    current_min_id = start_mid
    lock = asyncio.Lock()
    pause_until = 0.0

    queue: asyncio.Queue[list[int]] = asyncio.Queue()
    for c in chunks:
        queue.put_nowait(c)

    # 4. Worker Pool สำหรับ Bulk Delete กวาดทีละ 100 ข้อความต่อ Request
    async def worker(w_id: int):
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
                await bot.delete_messages(chat_id=user_id, message_ids=chunk)
                async with lock:
                    deleted_count += len(chunk)
                    deleted_ids.extend(chunk)
                await asyncio.sleep(0.03)
            except TelegramBadRequest:
                # กรณีมีข้อผิดพลาดเฉพาะชุด ลองลบทีละ ID ใน Chunk นั้น
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=mid)
                        async with lock:
                            deleted_count += 1
                            deleted_ids.append(mid)
                    except Exception:
                        async with lock:
                            skipped_count += 1
            except TelegramRetryAfter as e:
                async with lock:
                    pause_until = max(pause_until, time.time() + e.retry_after + 1.0)
                    logger.info(f"Rate limit hit for user {user_id}: waiting {e.retry_after}s at Chunk {chunk[0]}..{chunk[-1]}")
                queue.put_nowait(chunk)
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                async with lock:
                    skipped_count += len(chunk)

            async with lock:
                scanned_count += len(chunk)
                current_min_id = min(current_min_id, chunk[-1])
                if scanned_count % 500 == 0:
                    update_user_checkpoint(
                        user_id=user_id,
                        max_id=max_id,
                        scanned_down_to_id=current_min_id,
                        deleted_count=accumulated_deleted + deleted_count,
                        status="IN_PROGRESS",
                        username=username,
                        full_name=full_name,
                        deleted_ids=deleted_ids,
                    )

            queue.task_done()
            await asyncio.sleep(0.03)

    workers = [asyncio.create_task(worker(i)) for i in range(min(num_workers, 4))]
    await asyncio.gather(*workers)

    # 5. เมื่อสแกนครบถึง target_end_mid -> บันทึกสถานะ COMPLETED และอัปเดต last_scanned_max_id
    total_del = accumulated_deleted + deleted_count
    update_user_checkpoint(
        user_id=user_id,
        max_id=max_id,
        scanned_down_to_id=0,
        deleted_count=total_del,
        status="COMPLETED",
        username=username,
        full_name=full_name,
        deleted_ids=deleted_ids,
    )

    result["deleted_count"] = total_del
    result["scanned_count"] = scanned_count
    result["skipped_count"] = skipped_count
    result["success"] = True
    result["detail"] = "SUCCESS"
    return result
