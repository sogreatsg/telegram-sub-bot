import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import get_settings
from bot.services.database import init_db, close_db
from bot.services.scheduler import setup_scheduler
from bot.handlers import (
    user_menu_router,
    payment_router,
    admin_router,
    channel_events_router,
    promotion_admin_router,
    promotion_user_router,
)
from bot.utils.time_utils import BANGKOK_TZ


class ThaiTimeFormatter(logging.Formatter):
    """Custom logging formatter แสดงผลเวลาใน Log เป็นเวลาไทย (UTC+7) เสมอ"""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=BANGKOK_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(log_level: str = "INFO") -> None:
    """กำหนดรูปแบบ Logging ภาษาไทยและเวลาไทย (UTC+7)"""
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    handler = logging.StreamHandler(sys.stdout)
    formatter = ThaiTimeFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.handlers = [handler]

    # Suppress overly chatty library loggers
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


async def send_startup_notification(bot: Bot, admin_group_id: int) -> None:
    """ส่งข้อความแจ้งเตือนเมื่อระบบ Deploy / Service Start เสร็จสมบูรณ์ไปยังกลุ่มแอดมิน"""
    from bot.handlers.admin import get_bot_version_info
    logger = logging.getLogger("bot.main")
    try:
        version_text = get_bot_version_info()
        startup_msg = (
            "🚀 <b>[Deploy Completed] บอทเริ่มการทำงานใหม่เรียบร้อยแล้ว</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 <b>Service Status:</b> Online & พร้อมให้บริการ\n\n"
            f"{version_text}"
        )
        await bot.send_message(
            chat_id=admin_group_id,
            text=startup_msg,
            parse_mode="HTML",
        )
        logger.info(f"Sent startup deployment notification to Admin Group {admin_group_id}")
    except Exception as e:
        logger.warning(f"Could not send startup deployment notification to Admin Group: {e}")


async def main() -> None:
    """Application entry point."""
    config = get_settings()
    setup_logging(config.LOG_LEVEL)
    logger = logging.getLogger("bot.main")

    logger.info("Starting Telegram Membership Bot (Thai Timezone UTC+7)...")

    # 1. Initialize Database & WAL mode
    await init_db()

    # 2. Initialize Bot and Dispatcher with HTML parse mode
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # 3. Register Handler Routers
    dp.include_router(payment_router)
    dp.include_router(admin_router)
    dp.include_router(channel_events_router)
    dp.include_router(promotion_admin_router)
    dp.include_router(promotion_user_router)
    dp.include_router(user_menu_router)

    # 4. Initialize & Start APScheduler
    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info("Background Expiry Scheduler started (Thai Timezone UTC+7).")

    # 5. Fetch bot metadata & verify permissions
    try:
        bot_user = await bot.get_me()
        logger.info(f"Connected to Telegram Bot API as @{bot_user.username} (ID: {bot_user.id})")
        logger.info(f"Target Channel ID: {config.CHANNEL_ID}")
        logger.info(f"Admin Group ID: {config.ADMIN_GROUP_ID}")

        # ตรวจสอบสิทธิ์ของบอทใน Target Channel
        try:
            bot_chat_member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=bot_user.id)
            logger.info(f"Bot status in Channel {config.CHANNEL_ID}: {bot_chat_member.status}")
            if bot_chat_member.status not in ("administrator", "creator"):
                logger.warning(
                    f"⚠️ [WARNING] Bot @{bot_user.username} is NOT an Administrator in Channel {config.CHANNEL_ID}! "
                    f"Telegram requires the bot to be an Admin in the Channel to receive member join events!"
                )
        except Exception as e:
            logger.warning(f"Could not verify Bot status in Channel {config.CHANNEL_ID}: {e}")

        # Delete any pending webhook if previously configured
        await bot.delete_webhook(drop_pending_updates=False)

        # 6. Send startup deployment notification to Admin Group
        if config.ADMIN_GROUP_ID:
            await send_startup_notification(bot=bot, admin_group_id=config.ADMIN_GROUP_ID)

        # 7. Start Polling with required allowed_updates
        allowed_updates = [
            "message",
            "callback_query",
            "chat_member",
            "my_chat_member",
            "chat_join_request",
        ]
        logger.info(f"Starting long polling with allowed_updates: {allowed_updates}")

        await dp.start_polling(
            bot,
            allowed_updates=allowed_updates,
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot execution interrupted by user or system signal.")
    except Exception as e:
        logger.critical(f"Fatal exception in main loop: {e}", exc_info=True)
    finally:
        logger.info("Commencing graceful shutdown...")
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown.")
        await bot.session.close()
        logger.info("Bot HTTP session closed.")
        await close_db()
        logger.info("Database pool closed. Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
