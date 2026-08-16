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
    MONTHLY_30D = "MONTHLY_30D"


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


class User(Base):
    """Telegram User model."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationships
    subscriptions: Mapped[List["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="desc(Subscription.id)"
    )
    payment_slips: Mapped[List["PaymentSlip"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="desc(PaymentSlip.id)"
    )

    def __repr__(self) -> str:
        return f"<User(telegram_id={self.telegram_id}, username={self.username}, trial_used={self.trial_used})>"


class Subscription(Base):
    """Channel Subscription model."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_type: Mapped[str] = mapped_column(
        String(32), default=PlanType.TRIAL_15M.value, nullable=False
    )
    joined_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=SubStatus.PENDING.value, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="subscriptions")

    __table_args__ = (
        Index("ix_subscriptions_status_expires", "status", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription(id={self.id}, user_id={self.user_id}, "
            f"plan_type={self.plan_type}, status={self.status}, expires_at={self.expires_at})>"
        )


class PaymentSlip(Base):
    """Payment Slip submission model."""

    __tablename__ = "payment_slips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=SlipStatus.PENDING.value, nullable=False, index=True
    )
    admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="payment_slips")

    def __repr__(self) -> str:
        return (
            f"<PaymentSlip(id={self.id}, user_id={self.user_id}, "
            f"status={self.status}, admin_id={self.admin_id})>"
        )
