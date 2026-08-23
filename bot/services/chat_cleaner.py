import asyncio
import logging
from typing import Dict, Any
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

logger = logging.getLogger(__name__)


async def clean_user_chat_messages(bot: Bot, user_id: int) -> Dict[str, Any]:
    """
    ลบข้อความทั้งหมดที่บอทเคยส่งหา User ในแชทส่วนตัว (Private DM) ตั้งแต่ ID 1 ถึง max_id
    ไล่ลบทีละ 1 ข้อความแบบ Sequential 100% ความแม่นยำสูงสุด ไม่ติด Flood Control
    
    คืนค่า dict:
    - deleted_count: จำนวนข้อความของบอทที่ลบสำเร็จจริง
    - max_id: Message ID สูงสุดที่พบในแชท
    - scanned_count: จำนวน Message ID ที่สแกน
    - skipped_count: จำนวนข้อความที่ข้าม (ของ User หรือที่ไม่มีอยู่)
    - success: สำเร็จหรือไม่
    - detail: รายละเอียดผลลัพธ์
    """
    result = {
        "deleted_count": 0,
        "max_id": 0,
        "scanned_count": 0,
        "skipped_count": 0,
        "success": False,
        "detail": ""
    }

    try:
        probe = await bot.send_message(chat_id=user_id, text=".")
        max_id = probe.message_id
        result["max_id"] = max_id
        try:
            await bot.delete_message(chat_id=user_id, message_id=max_id)
        except Exception:
            pass
    except TelegramForbiddenError:
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
        result["success"] = True
        result["detail"] = "NO_MESSAGES"
        return result

    deleted_count = 0
    skipped_count = 0

    for mid in range(max_id - 1, 0, -1):
        try:
            await bot.delete_message(chat_id=user_id, message_id=mid)
            deleted_count += 1
            await asyncio.sleep(0.015)
        except TelegramBadRequest:
            skipped_count += 1
        except TelegramRetryAfter as e:
            logger.info(f"Rate limited by Telegram for user {user_id}, waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1.0)
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
                deleted_count += 1
            except Exception:
                skipped_count += 1
        except Exception as e:
            logger.debug(f"Error deleting message {mid} for user {user_id}: {e}")
            skipped_count += 1

        await asyncio.sleep(0.015)

    result["deleted_count"] = deleted_count
    result["scanned_count"] = (max_id - 1)
    result["skipped_count"] = skipped_count
    result["success"] = True
    result["detail"] = "SUCCESS"
    return result
