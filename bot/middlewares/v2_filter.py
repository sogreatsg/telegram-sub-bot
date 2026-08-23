import logging
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery

from bot.config import get_settings
from bot.models.schema import User
from bot.services.database import get_session
from bot.services.channel_service import is_user_v2_member

logger = logging.getLogger(__name__)
config = get_settings()


class V2MemberOnlyCallbackMiddleware(BaseMiddleware):
    """
    Middleware สำหรับกรอง Callback Query ทั้งหมดจากฝั่ง User:
    - สมาชิกห้อง V.2 และ Admin -> ใช้งานปุ่มและเมนูได้ตามปกติ
    - สมาชิกห้อง V.1 และผู้ใช้สมัครใหม่ -> ระงับการทำงาน 100%, ปิดการตอบกลับ และลบแผงปุ่มเก่าทิ้งทันที
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        # ถ้ากดใน Admin Group ให้ผ่านได้เสมอ
        chat = data.get("event_chat")
        if chat and chat.id == config.ADMIN_GROUP_ID:
            return await handler(event, data)

        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # ตรวจสอบสถานะสมาชิกในฐานข้อมูล
        async with get_session() as session:
            db_user = await session.get(User, user.id)
            if is_user_v2_member(db_user):
                return await handler(event, data)

        # หากไม่ใช่สมาชิก V.2 (อยู่ V.1 หรือสมัครใหม่)
        logger.info(f"[V2_FILTER] Blocked callback '{event.data}' from non-V2 user {user.id}")

        # 1. Answer callback silently เพื่อปิด loading spinner ใน Telegram
        try:
            await event.answer()
        except Exception:
            pass

        # 2. ลบ Inline Keyboard ของข้อความเดิมทิ้ง เพื่อไม่ให้กดเล่นได้อีก
        if event.message:
            try:
                await event.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        # 3. ไม่ส่งต่อไปยัง handler ใดๆ ทั้งสิ้น
        return
