import asyncio
import logging
from typing import Optional, Callable, Dict, Any, List
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from bot.models.schema import User
from bot.services.database import get_session
from bot.services.channel_service import is_user_v2_member

logger = logging.getLogger(__name__)


async def clean_user_chat_messages(bot: Bot, user_id: int, max_scan_range: int = 500) -> tuple[int, bool, str]:
    """
    ลบข้อความทั้งหมดที่บอทเคยส่งหา User ในแชทส่วนตัว (Private DM):
    - ส่ง Probe ข้อความสั้นๆ เพื่อหา message_id สูงสุดล่าสุด
    - ลบ Probe ทันที
    - ลบข้อความย้อนหลังของบอททั้งหมดตั้งแต่ ID 1 ถึง max_id
    
    คืนค่า: (จำนวนข้อความที่ลบสำเร็จ, สถานะสำเร็จหรือไม่, รายละเอียดเพิ่มเติม)
    """
    try:
        probe = await bot.send_message(chat_id=user_id, text=".")
        max_id = probe.message_id
        try:
            await bot.delete_message(chat_id=user_id, message_id=max_id)
        except Exception:
            pass
    except TelegramForbiddenError:
        return 0, False, "BLOCKED_BOT"
    except TelegramBadRequest as e:
        logger.debug(f"Cannot send probe to user {user_id}: {e}")
        return 0, False, f"BAD_REQUEST: {e}"
    except Exception as e:
        logger.debug(f"Error sending probe to user {user_id}: {e}")
        return 0, False, str(e)

    start_id = max(1, max_id - max_scan_range)
    all_ids = list(range(start_id, max_id))
    if not all_ids:
        return 0, True, "NO_MESSAGES"

    deleted_count = 0
    # แบ่งเป็น Chunk ละ 50 ข้อความ
    chunk_size = 50
    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i + chunk_size]
        try:
            # ลองใช้ delete_messages แบบชุดก่อน
            await bot.delete_messages(chat_id=user_id, message_ids=chunk)
            deleted_count += len(chunk)
        except (TelegramBadRequest, Exception):
            # หากล้มเหลว (เช่น มีข้อความของ user ปนอยู่) ให้วนลบทีละ ID
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=mid)
                    deleted_count += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 0.5)
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=mid)
                        deleted_count += 1
                    except Exception:
                        pass
                except Exception:
                    pass

        await asyncio.sleep(0.04)

    return deleted_count, True, "SUCCESS"
