import logging
from bot.models.schema import ChatMessage
from bot.services.database import get_session

logger = logging.getLogger(__name__)


async def log_chat_message(user_id: int, sender_role: str, message_text: str) -> None:
    """
    บันทึกข้อความการสนทนาลงในฐานข้อมูล
    sender_role: 'USER', 'BOT', 'ADMIN'
    """
    if not user_id or not message_text:
        return

    try:
        async with get_session() as session:
            msg = ChatMessage(
                user_id=user_id,
                sender_role=sender_role,
                message_text=message_text[:4000],
            )
            session.add(msg)
            await session.flush()
    except Exception as e:
        logger.error(f"Failed to log chat message for User ID {user_id} ({sender_role}): {e}")
