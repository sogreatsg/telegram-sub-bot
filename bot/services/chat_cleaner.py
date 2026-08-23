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


async def clean_user_chat_messages(bot: Bot, user_id: int) -> tuple[int, bool, str]:
    """
    ลบข้อความทั้งหมดที่บอทเคยส่งหา User ในแชทส่วนตัว (Private DM) ตั้งแต่ข้อความแรกสุด (ID 1) ถึงข้อความล่าสุด:
    - ส่ง Probe ข้อความสั้นๆ เพื่อหา message_id สูงสุดล่าสุด
    - ลบ Probe ทันที
    - กวาดล้างข้อความย้อนหลังของบอททั้งหมดตั้งแต่ ID 1 ถึง max_id แบบ 100% Full History
    
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

    # กวาดล้างตั้งแต่ข้อความที่ 1 จนถึงข้อความล่าสุดทั้งหมด
    all_ids = list(range(1, max_id))
    if not all_ids:
        return 0, True, "NO_MESSAGES"

    deleted_count = 0
    # Telegram Bot API deleteMessages รองรับได้สูงสุดครั้งละ 100 IDs
    chunk_size = 100
    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i + chunk_size]
        try:
            # ลองใช้ delete_messages ลบพร้อมกัน 100 ข้อความใน 1 API call
            await bot.delete_messages(chat_id=user_id, message_ids=chunk)
            deleted_count += len(chunk)
        except (TelegramBadRequest, Exception):
            # หากติดเงื่อนไข (เช่น มีข้อความของ User ปนอยู่) ให้ลบแยกแบบ Concurrent
            async def _safe_delete(mid: int) -> int:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=mid)
                    return 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 0.5)
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=mid)
                        return 1
                    except Exception:
                        return 0
                except Exception:
                    return 0

            results = await asyncio.gather(*[_safe_delete(mid) for mid in chunk])
            deleted_count += sum(results)

        await asyncio.sleep(0.03)

    return deleted_count, True, "SUCCESS"
