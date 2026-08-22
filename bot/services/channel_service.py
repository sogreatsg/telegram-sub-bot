import logging
from typing import List, Optional, Tuple, Dict, Any
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.config import get_settings
from bot.models.schema import User

logger = logging.getLogger(__name__)
config = get_settings()


def _normalize_chat_id(chat_id: int | str) -> str:
    """แปลง chat_id เป็น string ที่ตัด prefix -100 หรือ - ออก เพื่อให้เปรียบเทียบข้ามรูปแบบได้เสมอ"""
    return str(chat_id).replace("-100", "").replace("-", "").strip()


def is_target_channel(chat_id: int) -> bool:
    """ตรวจสอบว่าเป็น Channel VIP เป้าหมายหรือไม่ (รองรับทั้ง Primary Channel และ Secondary Channel)"""
    norm_chat = _normalize_chat_id(chat_id)
    if norm_chat == _normalize_chat_id(config.CHANNEL_ID):
        return True
    if config.SECONDARY_CHANNEL_ID and norm_chat == _normalize_chat_id(config.SECONDARY_CHANNEL_ID):
        return True
    return False


def is_secondary_channel(chat_id: int) -> bool:
    """ตรวจสอบว่าเป็น Channel ใหม่ (Secondary / Target Channel) หรือไม่"""
    if not config.SECONDARY_CHANNEL_ID:
        return False
    return _normalize_chat_id(chat_id) == _normalize_chat_id(config.SECONDARY_CHANNEL_ID)


def get_all_target_channel_ids() -> List[int]:
    """คืนค่ารายการ Channel ID ทั้งหมดที่บอทดูแล (Primary Channel + Secondary Channel ถ้ามีการตั้งค่า)"""
    channels = [config.CHANNEL_ID]
    if config.SECONDARY_CHANNEL_ID and config.SECONDARY_CHANNEL_ID != config.CHANNEL_ID:
        channels.append(config.SECONDARY_CHANNEL_ID)
    return channels


def get_user_target_channel_id(user: Optional[User]) -> int:
    """
    คืนค่า Channel ID เป้าหมายของผู้ใช้คนนี้:
    - หากผู้ใช้เคยถูกย้ายไปยังห้องใหม่ (is_moved_to_secondary=True หรือ assigned_channel='SECONDARY')
      และมีการตั้งค่า SECONDARY_CHANNEL_ID ไว้ -> คืนค่า SECONDARY_CHANNEL_ID
    - มิฉะนั้น คืนค่า Primary CHANNEL_ID ตามปกติ
    """
    if user and (getattr(user, "is_moved_to_secondary", False) or getattr(user, "assigned_channel", "PRIMARY") == "SECONDARY"):
        if config.SECONDARY_CHANNEL_ID:
            return config.SECONDARY_CHANNEL_ID
    return config.CHANNEL_ID


def get_channel_label(chat_id: int) -> str:
    """คืนค่าป้ายชื่อ Channel สำหรับแสดงผลใน Log และข้อความแจ้งเตือน"""
    if is_secondary_channel(chat_id):
        return "Channel ใหม่ (Target Channel)"
    return "Channel VIP (เดิม)"


async def kick_user_from_channel(bot: Bot, chat_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """
    ดำเนินการ Soft-kick ผู้ใช้ 1 คนออกจาก Channel ที่ระบุ (แบน และ ปลดแบนทันที)
    คืนค่า (success, error_reason)
    """
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=False)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        logger.info(f"Soft-kicked User {user_id} from Channel {chat_id}")
        return True, None
    except TelegramBadRequest as e:
        err_msg = e.message.lower()
        if any(kw in err_msg for kw in ["not found", "not a member", "not in the chat", "user_not_participant"]):
            # ผู้ใช้ไม่ได้อยู่ใน Channel นั้นอยู่แล้ว ถือว่า kick สำเร็จ
            return True, None
        return False, f"TelegramBadRequest: {e.message}"
    except TelegramForbiddenError as e:
        return False, f"TelegramForbiddenError (บอทขาดสิทธิ์ Ban Users): {e.message}"
    except TelegramRetryAfter as e:
        return False, f"RateLimit ({e.retry_after}s)"
    except Exception as e:
        return False, f"Error: {e}"


async def kick_user_from_all_target_channels(bot: Bot, user_id: int) -> Dict[str, Any]:
    """
    ดำเนินการ Soft-kick ผู้ใช้ 1 คนออกจากทุก Target Channel (ทั้งกลุ่มเดิมและกลุ่มใหม่)
    คืนค่าสรุปผลการเตะ
    """
    results = {
        "kicked_channels": [],
        "failed_channels": [],
        "errors": {},
    }
    for cid in get_all_target_channel_ids():
        success, err = await kick_user_from_channel(bot, cid, user_id)
        if success:
            results["kicked_channels"].append(cid)
        else:
            results["failed_channels"].append(cid)
            results["errors"][cid] = err
    return results


async def unban_user_in_channel(bot: Bot, chat_id: int, user_id: int) -> None:
    """ปลดแบนผู้ใช้ใน Channel ที่ระบุ (ป้องกัน blacklist ค้าง)"""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception as e:
        logger.debug(f"Could not unban User {user_id} in Channel {chat_id}: {e}")


async def unban_user_in_all_target_channels(bot: Bot, user_id: int) -> None:
    """ปลดแบนผู้ใช้ในทุก Target Channel ที่บอทดูแล"""
    for cid in get_all_target_channel_ids():
        await unban_user_in_channel(bot, cid, user_id)


async def check_user_in_channel(bot: Bot, chat_id: int, user_id: int) -> Tuple[bool, Optional[str], Optional[Any]]:
    """
    ตรวจสอบสถานะของผู้ใช้ใน Channel ที่ระบุ
    คืนค่า (is_member, status_string, tg_user_obj)
    """
    try:
        cm = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        is_member = cm.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        )
        return is_member, cm.status, getattr(cm, "user", None)
    except Exception:
        return False, None, None


async def check_user_in_target_channels(bot: Bot, user_id: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    ตรวจสอบว่าผู้ใช้อยู่ใน Channel เป้าหมายใดบ้าง
    คืนค่า (is_in_any, found_channel_id, status)
    """
    for cid in get_all_target_channel_ids():
        is_mem, status, _ = await check_user_in_channel(bot, cid, user_id)
        if is_mem:
            return True, cid, status
    return False, None, None
