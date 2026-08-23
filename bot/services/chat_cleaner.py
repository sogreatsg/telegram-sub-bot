import asyncio
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
) -> Dict[str, Any]:
    """
    ลบข้อความทั้งหมดที่บอทเคยส่งหา User ในแชทส่วนตัว (Private DM) ตั้งแต่ ID 1 ถึง max_id
    มีระบบบันทึก State / Checkpoint อัตโนมัติ:
    - หาก User คนนี้เคยทำเสร็จสมบูรณ์แล้ว (COMPLETED) -> ข้ามทันที (ไม่ต้องวนใหม่)
    - หากเคยทำค้างไว้ที่ ID ใด -> เริ่มทำต่อจากจุดเดิม (Resume from Checkpoint)
    - บันทึก Checkpoint ทุกๆ 100 ข้อความ และบันทึกสถานะเมื่อเสร็จสิ้น
    """
    result = {
        "deleted_count": 0,
        "max_id": 0,
        "scanned_count": 0,
        "skipped_count": 0,
        "success": False,
        "already_completed": False,
        "resumed_from_id": None,
        "detail": ""
    }

    # 1. ตรวจสอบ Checkpoint เดิม (ถ้าไม่ได้สั่ง force_restart)
    cp = get_user_checkpoint(user_id)
    if not force_restart and is_user_clean_completed(user_id):
        result["success"] = True
        result["already_completed"] = True
        result["deleted_count"] = cp.get("deleted_count", 0) if cp else 0
        result["max_id"] = cp.get("max_id", 0) if cp else 0
        result["detail"] = "ALREADY_COMPLETED"
        logger.info(f"User {user_id} was already cleaned to 100% completion. Skipping.")
        return result

    # 2. Probe หา Message ID สูงสุด
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

    # 3. กำหนดจุดเริ่มต้นในการสแกน (ถ้ามี Checkpoint เดิม ให้ทำต่อจากจุดเดิม)
    start_mid = max_id - 1
    deleted_ids = []
    accumulated_deleted = 0

    if cp and not force_restart:
        prev_scanned_id = cp.get("scanned_down_to_id")
        if prev_scanned_id and 1 < prev_scanned_id < max_id:
            start_mid = prev_scanned_id
            accumulated_deleted = cp.get("deleted_count", 0)
            deleted_ids = list(cp.get("deleted_ids", []))
            result["resumed_from_id"] = start_mid
            logger.info(f"Resuming clean for user {user_id} from Checkpoint ID: {start_mid}")

    deleted_count = 0
    skipped_count = 0
    scanned_count = 0

    # 4. วนลูปไล่ลบทีละ 1 ข้อความลงไปจนถึง ID 1
    for mid in range(start_mid, 0, -1):
        scanned_count += 1
        try:
            await bot.delete_message(chat_id=user_id, message_id=mid)
            deleted_count += 1
            deleted_ids.append(mid)
            await asyncio.sleep(0.02)
        except TelegramBadRequest:
            skipped_count += 1
        except TelegramRetryAfter as e:
            logger.info(f"Rate limited by Telegram for user {user_id}, waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1.0)
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
                deleted_count += 1
                deleted_ids.append(mid)
            except Exception:
                skipped_count += 1
        except Exception as e:
            logger.debug(f"Error deleting message {mid} for user {user_id}: {e}")
            skipped_count += 1

        # บันทึก Checkpoint ทุกๆ 100 ข้อความ
        if scanned_count % 100 == 0:
            update_user_checkpoint(
                user_id=user_id,
                max_id=max_id,
                scanned_down_to_id=mid,
                deleted_count=accumulated_deleted + deleted_count,
                status="IN_PROGRESS",
                username=username,
                full_name=full_name,
                deleted_ids=deleted_ids,
            )

        await asyncio.sleep(0.02)

    # 5. เมื่อสแกนครบจนถึง ID 1 -> บันทึกสถานะ COMPLETED
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
