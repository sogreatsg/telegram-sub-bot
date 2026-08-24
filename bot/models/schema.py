import enum
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    BigInteger,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Returns current UTC datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """SQLAlchemy Declarative Base class."""
    pass


class PlanType(str, enum.Enum):
    """Subscription plan types."""
    TRIAL_15M = "TRIAL_15M"
    VIP_12H = "VIP_12H"
    VIP_1D = "VIP_1D"  # Backward compatibility alias
    VIP_3D = "VIP_3D"
    VIP_10D = "VIP_10D"
    VIP_30D = "VIP_30D"
    REFERRAL_VIP = "REFERRAL_VIP"
    MONTHLY_30D = "MONTHLY_30D"  # Legacy support
    PROMOTION = "PROMOTION"


PLAN_DETAILS = {
    PlanType.VIP_12H.value: {
        "name": "VIP 12 ชั่วโมง",
        "badge": "⚡ VIP 12 ชั่วโมง",
        "price": 100,
        "days": 0,
        "hours": 12,
        "minutes": 720,
        "qr_filename": "qr_100.png",
    },
    PlanType.VIP_1D.value: {
        "name": "VIP 12 ชั่วโมง",
        "badge": "⚡ VIP 12 ชั่วโมง",
        "price": 100,
        "days": 0,
        "hours": 12,
        "minutes": 720,
        "qr_filename": "qr_100.png",
    },
    PlanType.VIP_3D.value: {
        "name": "VIP 3 วัน",
        "badge": "🥉 VIP 3 วัน",
        "price": 300,
        "days": 3,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "qr_300.png",
    },
    PlanType.VIP_10D.value: {
        "name": "VIP 10 วัน",
        "badge": "🥈 VIP 10 วัน",
        "price": 500,
        "days": 10,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "qr_500.png",
    },
    PlanType.VIP_30D.value: {
        "name": "VIP 30 วัน",
        "badge": "🥇 VIP 30 วัน",
        "price": 1000,
        "days": 30,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "qr_1000.png",
    },
    PlanType.REFERRAL_VIP.value: {
        "name": "VIP โบนัสชวนเพื่อน",
        "badge": "🎁 VIP ชวนเพื่อน",
        "price": 0,
        "days": 1,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "",
    },
    PlanType.MONTHLY_30D.value: {
        "name": "VIP 30 วัน",
        "badge": "🥇 VIP 30 วัน",
        "price": 1000,
        "days": 30,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "qr_payment.png",
    },
    PlanType.PROMOTION.value: {
        "name": "โปรโมชั่นพิเศษ",
        "badge": "🔥 โปรโมชั่นพิเศษ",
        "price": 0,
        "days": 0,
        "hours": 0,
        "minutes": 0,
        "qr_filename": "",
    },
}

def format_plan_duration(plan_info: dict) -> str:
    """แปลงระยะเวลาของแพ็กเกจเป็นข้อความภาษาไทย เช่น '12 ชั่วโมง', '3 วัน'"""
    if plan_info.get("days", 0) > 0:
        return f"{plan_info['days']} วัน"
    if plan_info.get("hours", 0) > 0:
        return f"{plan_info['hours']} ชั่วโมง"
    if plan_info.get("minutes", 0) > 0:
        return f"{plan_info['minutes']} นาที"
    return f"{plan_info.get('days', 0)} วัน"

def get_dynamic_plan_info(plan_key: str) -> dict:
    plan_info = PLAN_DETAILS.get(plan_key, PLAN_DETAILS.get(PlanType.VIP_30D.value, {})).copy()
    if plan_key == PlanType.PROMOTION.value:
        try:
            from bot.services.promotion import get_promotion_settings
            settings = get_promotion_settings()
            plan_info["price"] = int(settings.get("price", 0))
            plan_info["days"] = int(settings.get("days", 0))
            plan_info["qr_filename"] = settings.get("qr_filename", "")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error loading dynamic plan info: {e}")
    return plan_info


class SubStatus(str, enum.Enum):
    """Subscription statuses."""
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    KICKED = "KICKED"
    KICK_FAILED = "KICK_FAILED"


class SlipStatus(str, enum.Enum):
    """Payment slip statuses."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class GrantType(str, enum.Enum):
    """หมวดหมู่ของการเติมวัน (ใช้ tag ตอนเติมวันโดยตรง ไม่ต้อง parse string ย้อนหลัง)"""
    TRIAL = "TRIAL"
    PURCHASE = "PURCHASE"
    PROMOTION = "PROMOTION"
    REFERRAL_BONUS = "REFERRAL_BONUS"
    ADMIN_GRANT = "ADMIN_GRANT"


class User(Base):
    """Telegram User model."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    referred_by_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"), nullable=True
    )
    referral_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referral_bonus_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referral_rewarded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    assigned_channel: Mapped[str] = mapped_column(String(32), default="PRIMARY", nullable=False)
    is_moved_to_secondary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    # Relationships
    subscription: Mapped[Optional["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    grants: Mapped[List["SubscriptionGrant"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="desc(SubscriptionGrant.id)"
    )
    payment_slips: Mapped[List["PaymentSlip"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="desc(PaymentSlip.id)"
    )
    chat_messages: Mapped[List["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="desc(ChatMessage.id)"
    )

    def __repr__(self) -> str:
        return f"<User(telegram_id={self.telegram_id}, username={self.username}, trial_used={self.trial_used})>"


class Subscription(Base):
    """
    สถานะสมาชิกปัจจุบันของผู้ใช้ (1 แถวต่อ 1 user เท่านั้น — ไม่มีประวัติหลายแถวอีกต่อไป)

    ไม่มีแนวคิด "แพ็กเกจ" ผูกกับแถวนี้อีกต่อไป — expires_at คือยอดวันคงเหลือสะสมล้วนๆ
    (ระบบ "เติมวัน": ทุกการให้สิทธิ์คือ +N วัน เข้า expires_at โดยตรง)
    ประวัติการเติมแต่ละครั้งเก็บแยกไว้ที่ SubscriptionGrant (ledger) แทน
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=SubStatus.PENDING.value, nullable=False, index=True
    )
    joined_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    is_trial_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_label: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    # โควต้าที่เติมไว้แล้วแต่ยังไม่เริ่มนับ (รอผู้ใช้กดเข้า Channel ครั้งแรก/ครั้งถัดไป)
    pending_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_has_value: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pending_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    warned_1d: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    warned_1h: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    stale_alerted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="subscription")

    __table_args__ = (
        Index("ix_subscriptions_status_expires", "status", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription(user_id={self.user_id}, "
            f"status={self.status}, expires_at={self.expires_at}, pending_days={self.pending_days})>"
        )


class SubscriptionGrant(Base):
    """
    Ledger แบบ append-only บันทึกการ 'เติมวัน' ทุกครั้ง (ไม่เคยแก้ไข/ลบ) ใช้สำหรับ audit/ประวัติย้อนหลัง
    แยกออกจาก Subscription (สถานะปัจจุบัน) โดยเจตนา เพื่อไม่ให้ต้อง parse string ย้อนหลังอีก
    """

    __tablename__ = "subscription_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    grant_type: Mapped[str] = mapped_column(String(32), default=GrantType.PURCHASE.value, nullable=False)
    referred_friend_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    has_value: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="grants")

    def __repr__(self) -> str:
        return (
            f"<SubscriptionGrant(user_id={self.user_id}, days={self.days}, "
            f"minutes={self.minutes}, grant_type={self.grant_type})>"
        )


class PaymentSlip(Base):
    """Payment Slip submission model (Supports PromptPay slips & TrueMoney Angpao links)."""

    __tablename__ = "payment_slips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[str] = mapped_column(String(500), nullable=False)
    plan_type: Mapped[str] = mapped_column(
        String(32), default=PlanType.VIP_30D.value, nullable=True
    )
    payment_method: Mapped[str] = mapped_column(
        String(32), default="PROMPTPAY", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=SlipStatus.PENDING.value, nullable=False, index=True
    )
    admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="payment_slips")

    def __repr__(self) -> str:
        return (
            f"<PaymentSlip(id={self.id}, user_id={self.user_id}, "
            f"plan_type={self.plan_type}, payment_method={self.payment_method}, status={self.status}, admin_id={self.admin_id})>"
        )


class ChatMessage(Base):
    """บันทึกประวัติข้อความสนทนาระหว่างผู้ใช้ บอท และแอดมิน"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_role: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # 'USER', 'BOT', 'ADMIN'
    message_text: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="chat_messages")

    def __repr__(self) -> str:
        return (
            f"<ChatMessage(id={self.id}, user_id={self.user_id}, "
            f"sender_role={self.sender_role})>"
        )
