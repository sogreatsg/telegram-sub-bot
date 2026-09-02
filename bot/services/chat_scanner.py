import asyncio
import logging
import html
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import select

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from bot.config import get_settings
from bot.models.schema import User, ChatMessage
from bot.services.database import get_session
from bot.services.chat_logger import log_chat_message

logger = logging.getLogger(__name__)
config = get_settings()


async def scan_and_sync_user_messages(bot: Bot, user_id: int, max_scan: int = 30) -> int:
    """
    สแกนดึงข้อความสดจาก Telegram Cloud ย้อนหลังแบบ Stealth 100%
    (ไม่มีการส่งข้อความหา User ใดๆ ทั้งสิ้น User จะไม่รู้ตัว ไม่มีการแจ้งเตือน)
    และซิงค์ข้อความใหม่ที่ยังไม่มีใน Database ลงตาราง chat_messages อัตโนมัติ
    คืนค่า: จำนวนข้อความใหม่ที่ค้นพบและบันทึกเพิ่ม
    """
    if not user_id or not config.ADMIN_GROUP_ID:
        return 0

    try:
        bot_info = await bot.get_me()
        bot_id = bot_info.id
    except Exception:
        bot_id = bot.id

    # 1. ค้นหา Message ID ล่าสุดแบบ Stealth (ใช้ forward_message + Binary Search ไม่ส่งข้อความหา User)
    max_id = await _find_max_message_id_stealth(bot=bot, user_id=user_id, bot_id=bot_id)
    if not max_id or max_id < 1:
        return 0

    # 2. ดึงข้อความเดิมที่มีใน Database เพื่อใช้ตรวจสอบความซ้ำซ้อน
    existing_texts = set()
    async with get_session() as session:
        stmt = select(ChatMessage.message_text).where(ChatMessage.user_id == user_id).order_by(ChatMessage.id.desc()).limit(50)
        existing_texts = set((await session.execute(stmt)).scalars().all())

    # 3. กำหนดช่วง Message ID ที่จะสแกนย้อนหลัง
    start_id = max_id
    end_id = max(1, max_id - max_scan + 1)
    ids_to_scan = list(range(start_id, end_id - 1, -1))

    discovered_messages: List[Dict[str, Any]] = []

    # 4. สแกนทีละ Batch (Batch ละ 5 ข้อความ เพื่อความเร็วและป้องกัน Rate Limit)
    batch_size = 5
    for i in range(0, len(ids_to_scan), batch_size):
        chunk = ids_to_scan[i : i + batch_size]
        tasks = [_probe_single_message(bot, user_id, mid, bot_id) for mid in chunk]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, dict) and res.get("message_text"):
                discovered_messages.append(res)

        await asyncio.sleep(0.05)

    # 5. นำข้อความที่สแกนเจอและยังไม่มีใน DB บันทึกลงตาราง chat_messages
    new_count = 0
    if discovered_messages:
        # เรียงตาม Message ID จากเก่าไปใหม่
        discovered_messages.sort(key=lambda x: x["message_id"])
        async with get_session() as session:
            for item in discovered_messages:
                txt = item["message_text"]
                if txt not in existing_texts:
                    msg = ChatMessage(
                        user_id=user_id,
                        sender_role=item["sender_role"],
                        message_text=txt[:4000],
                        created_at=item.get("created_at") or datetime.now(timezone.utc),
                    )
                    session.add(msg)
                    existing_texts.add(txt)
                    new_count += 1
            if new_count > 0:
                await session.flush()
                logger.info(f"[CHAT_SCAN_STEALTH] Discovered and synced {new_count} new messages for user {user_id} from Telegram Cloud")

    return new_count


async def _find_max_message_id_stealth(bot: Bot, user_id: int, bot_id: int) -> Optional[int]:
    """
    ค้นหา Message ID ล่าสุดในห้องแชทของ User แบบ Stealth 100%
    โดยใช้ Exponential Probe + Binary Search ผ่าน forward_message
    (ไม่มีการส่งข้อความหา User ใดๆ ทั้งสิ้น)
    """
    # ตรวจสอบจุดเริ่มต้น
    low = 1
    low_res = await _probe_single_message(bot, user_id, low, bot_id)
    if not low_res:
        # หากข้อความที่ 1 ถูกลบ ให้ลองสุ่มช่วงเริ่มต้น
        found_any = False
        for test_id in [2, 3, 5, 10, 20, 50, 100, 200]:
            if await _probe_single_message(bot, user_id, test_id, bot_id):
                low = test_id
                found_any = True
                break
        if not found_any:
            return None

    # Step 1: Exponential Search หาขอบเขตบน (Upper Bound)
    step = 10
    high = low + step
    while True:
        res = await _probe_single_message(bot, user_id, high, bot_id)
        if res:
            low = high
            high = high + step
            step = min(step * 2, 200)
            if high > 10000:
                break
        else:
            break

    # Step 2: Binary Search หา exact max_id
    best_id = low
    l, r = low, high
    while l <= r:
        mid = (l + r) // 2
        res = await _probe_single_message(bot, user_id, mid, bot_id)
        if res:
            best_id = mid
            l = mid + 1
        else:
            r = mid - 1

    return best_id


async def _probe_single_message(bot: Bot, user_id: int, message_id: int, bot_id: int) -> Optional[Dict[str, Any]]:
    """
    ลองดึงข้อความ 1 ข้อความจากแชทผ่านการ Forward ไปยังกลุ่ม Admin แล้วลบออกทันที
    *ฝั่ง User จะไม่มีข้อความใหม่ ไม่มีการแจ้งเตือน (Silent Read บน Telegram Cloud)*
    """
    try:
        fwd = await bot.forward_message(
            chat_id=config.ADMIN_GROUP_ID,
            from_chat_id=user_id,
            message_id=message_id,
            disable_notification=True,
        )
        # ลบข้อความที่ Forward ออกจาก Admin Group ทันทีเพื่อไม่ให้รก
        try:
            await bot.delete_message(chat_id=config.ADMIN_GROUP_ID, message_id=fwd.message_id)
        except Exception:
            pass

        # ตรวจสอบว่าใครเป็นคนส่ง (บอท หรือ ผู้ใช้)
        is_bot = False
        if fwd.from_user and fwd.from_user.id == bot_id:
            is_bot = True
        elif fwd.forward_from and fwd.forward_from.id == bot_id:
            is_bot = True

        sender_role = "BOT" if is_bot else "USER"
        msg_text = fwd.text or fwd.caption or (f"[{fwd.content_type}]" if fwd.content_type else "[ข้อความ/ไฟล์]")
        msg_date = fwd.forward_date or fwd.date or datetime.now(timezone.utc)

        return {
            "message_id": message_id,
            "sender_role": sender_role,
            "message_text": msg_text,
            "created_at": msg_date,
        }
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return None
    except Exception:
        return None
