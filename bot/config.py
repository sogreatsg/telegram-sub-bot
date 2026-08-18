from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or .env file."""

    BOT_TOKEN: str = Field(..., description="Telegram Bot API Token from @BotFather")
    CHANNEL_ID: int = Field(..., description="Target private Telegram Channel ID (e.g. -1001234567890)")
    ADMIN_GROUP_ID: int = Field(..., description="Admin Telegram Group ID (e.g. -1009876543210)")
    ADMIN_MENTION: str = Field(
        default="@sgrtbl",
        description="Telegram handle or username to tag in admin group alerts",
    )
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///data/bot.db",
        description="Async SQLAlchemy database connection URL",
    )
    PAYMENT_QR_PATH: str = Field(
        default="bot/assets/qr_payment.png",
        description="Local path to PromptPay QR Code image file",
    )
    PROMPT_PAYMENT_INFO: str = Field(
        default=(
            "💳 <b>สมัครสมาชิก VIP 30 วัน (ราคา 300 บาท)</b>\n\n"
            "📲 <b>สแกน QR Code เพื่อชำระเงิน</b>\n"
            "• ยอดชำระ: <b>300 บาท</b>\n"
            "• สแกนจ่ายผ่านแอปธนาคารได้ทันที\n\n"
            "📸 <b>ขั้นตอนถัดไป:</b>\n"
            "เมื่อโอนเงินเรียบร้อยแล้ว กรุณาส่งรูปสลิปเข้ามาในแชทนี้ได้เลยครับ"
        ),
        description="Payment instruction text shown to the user",
    )
    # Production defaults: TRIAL_DURATION_MINUTES=15, PAID_DURATION_MINUTES=43200 (30 days)
    TRIAL_DURATION_MINUTES: int = Field(
        default=15,
        description="Trial duration in minutes (15 minutes for production)",
    )
    PAID_DURATION_MINUTES: int = Field(
        default=43200,
        description="Paid VIP duration in minutes (43200 minutes = 30 days for production)",
    )
    CHECK_INTERVAL_SECONDS: int = Field(
        default=60,
        description="Scheduler interval in seconds to check expired members (60s default)",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL",
    )
    FREE_CHAT_GROUP_URL: str = Field(
        default="https://t.me/barelivechat",
        description="Free community chat group URL",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of Settings."""
    return Settings()
