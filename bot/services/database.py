import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional, Tuple
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import get_settings
from bot.models.schema import Base, GrantType, User

logger = logging.getLogger(__name__)
config = get_settings()

# Configure SQLite engine
# For SQLite, check_same_thread must be False
engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {},
)

# Listen to connect event on the underlying sync engine to enforce WAL mode for SQLite
if "sqlite" in config.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

# Session factory
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager providing a transactional database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _parse_legacy_dt(value: Any) -> Optional[datetime]:
    """แปลงค่า datetime ที่อ่านแบบ raw SQL (string) จากตารางเก่าให้เป็น tz-aware datetime"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _infer_legacy_grant_type(plan_type: str) -> str:
    """เดาหมวดหมู่ของแถวเก่า (ใช้ครั้งเดียวตอน migrate เท่านั้น ไม่ใช่ runtime logic)"""
    if plan_type == "TRIAL_15M":
        return GrantType.TRIAL.value
    if plan_type.startswith("REFERRAL_VIP"):
        return GrantType.REFERRAL_BONUS.value
    if plan_type.startswith("MANUAL_VIP_"):
        return GrantType.ADMIN_GRANT.value
    if plan_type.startswith("PROMOTION"):
        return GrantType.PROMOTION.value
    return GrantType.PURCHASE.value


async def _migrate_legacy_subscriptions(conn: AsyncConnection) -> None:
    """
    Migrate ตาราง subscriptions แบบเก่า (หลายแถวต่อ user, เก็บ plan_type เป็น string)
    ไปเป็นโครงสร้างใหม่: subscriptions (1 แถวต่อ user, เก็บแค่ยอดวันคงเหลือ)
    + subscription_grants (ledger ประวัติการเติมวันทุกครั้ง, backfill จากแถวเก่าทั้งหมด)

    รันครั้งเดียวตอนตรวจพบ schema เก่า (ดู init_db) แล้วจะลบตารางเก่าทิ้งเมื่อเสร็จ
    """
    from bot.services.subscription import is_free_grant_plan, parse_plan_days, plan_badge_text

    now = datetime.now(timezone.utc)

    result = await conn.execute(text(
        "SELECT id, user_id, plan_type, joined_at, expires_at, status, warned_1d, stale_alerted, created_at "
        "FROM subscriptions_legacy_v1 ORDER BY user_id, id"
    ))
    rows = result.fetchall()

    by_user: dict = defaultdict(list)
    for row in rows:
        by_user[row.user_id].append(row)

    grant_insert = text(
        "INSERT INTO subscription_grants (user_id, days, minutes, source_label, grant_type, has_value, created_at) "
        "VALUES (:user_id, :days, :minutes, :source_label, :grant_type, :has_value, :created_at)"
    )
    sub_insert = text(
        "INSERT INTO subscriptions (user_id, status, joined_at, expires_at, is_trial_active, source_label, "
        "pending_days, pending_minutes, pending_has_value, pending_since, warned_1d, stale_alerted, created_at) "
        "VALUES (:user_id, :status, :joined_at, :expires_at, :is_trial_active, :source_label, "
        ":pending_days, :pending_minutes, :pending_has_value, :pending_since, :warned_1d, :stale_alerted, :created_at)"
    )

    for user_id, user_rows in by_user.items():
        # 1. Backfill ledger: 1 SubscriptionGrant ต่อแถวเก่า 1 แถว (เก็บประวัติทั้งหมดไว้ ไม่ทิ้ง)
        for r in user_rows:
            plan_type = r.plan_type or "VIP_30D"
            days, minutes = parse_plan_days(plan_type)
            await conn.execute(grant_insert, {
                "user_id": user_id,
                "days": days,
                "minutes": minutes,
                "source_label": plan_badge_text(plan_type),
                "grant_type": _infer_legacy_grant_type(plan_type),
                "has_value": not is_free_grant_plan(plan_type),
                "created_at": r.created_at,
            })

        # 2. เลือกแถวที่จะเป็น "สถานะปัจจุบัน" ของ user นี้ ตามลำดับความสำคัญ
        def _exp(r):
            return _parse_legacy_dt(r.expires_at)

        active_rows = [r for r in user_rows if r.status == "ACTIVE" and _exp(r) and _exp(r) > now]
        kick_failed_rows = [r for r in user_rows if r.status == "KICK_FAILED"]
        pending_rows = [r for r in user_rows if r.status == "PENDING"]

        if active_rows:
            best = max(active_rows, key=_exp)
            plan_type = best.plan_type or "VIP_30D"
            await conn.execute(sub_insert, {
                "user_id": user_id, "status": "ACTIVE",
                "joined_at": best.joined_at, "expires_at": best.expires_at,
                "is_trial_active": plan_type == "TRIAL_15M",
                "source_label": plan_badge_text(plan_type),
                "pending_days": 0, "pending_minutes": 0, "pending_has_value": False, "pending_since": None,
                "warned_1d": bool(best.warned_1d), "stale_alerted": bool(best.stale_alerted),
                "created_at": best.created_at,
            })
        elif kick_failed_rows:
            best = max(kick_failed_rows, key=lambda r: r.id)
            plan_type = best.plan_type or "VIP_30D"
            await conn.execute(sub_insert, {
                "user_id": user_id, "status": "KICK_FAILED",
                "joined_at": best.joined_at, "expires_at": best.expires_at,
                "is_trial_active": False, "source_label": plan_badge_text(plan_type),
                "pending_days": 0, "pending_minutes": 0, "pending_has_value": False, "pending_since": None,
                "warned_1d": bool(best.warned_1d), "stale_alerted": bool(best.stale_alerted),
                "created_at": best.created_at,
            })
        elif pending_rows:
            total_days = 0
            total_minutes = 0
            has_value = False
            earliest_created = None
            for r in pending_rows:
                plan_type = r.plan_type or "VIP_30D"
                d, m = parse_plan_days(plan_type)
                total_days += d
                total_minutes += m
                if not is_free_grant_plan(plan_type):
                    has_value = True
                rc = _parse_legacy_dt(r.created_at)
                if earliest_created is None or (rc and rc < earliest_created):
                    earliest_created = rc
            latest = max(pending_rows, key=lambda r: r.id)
            await conn.execute(sub_insert, {
                "user_id": user_id, "status": "PENDING",
                "joined_at": None, "expires_at": None,
                "is_trial_active": False, "source_label": plan_badge_text(latest.plan_type or "VIP_30D"),
                "pending_days": total_days, "pending_minutes": total_minutes,
                "pending_has_value": has_value, "pending_since": earliest_created or latest.created_at,
                "warned_1d": False, "stale_alerted": False,
                "created_at": latest.created_at,
            })
        else:
            latest = max(user_rows, key=lambda r: r.id)
            plan_type = latest.plan_type or "VIP_30D"
            await conn.execute(sub_insert, {
                "user_id": user_id, "status": latest.status,
                "joined_at": latest.joined_at, "expires_at": latest.expires_at,
                "is_trial_active": False, "source_label": plan_badge_text(plan_type),
                "pending_days": 0, "pending_minutes": 0, "pending_has_value": False, "pending_since": None,
                "warned_1d": bool(latest.warned_1d), "stale_alerted": bool(latest.stale_alerted),
                "created_at": latest.created_at,
            })

    await conn.execute(text("DROP TABLE subscriptions_legacy_v1;"))
    logger.warning(
        f"Legacy subscription migration complete: consolidated {len(rows)} old row(s) across "
        f"{len(by_user)} user(s) into the new 1-row-per-user model (full history backfilled into subscription_grants)."
    )


async def init_db() -> None:
    """Initialize database tables and verify SQLite WAL mode."""
    logger.info("Initializing database...")
    async with engine.begin() as conn:
        if "sqlite" in config.DATABASE_URL:
            # Explicitly run WAL pragma during init
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))

        # ตรวจสอบว่าตาราง subscriptions เป็น schema เก่า (หลายแถวต่อ user, มี plan_type) หรือไม่
        needs_subscription_migration = False
        if "sqlite" in config.DATABASE_URL:
            info_result = await conn.execute(text("PRAGMA table_info(subscriptions);"))
            existing_cols = {row[1] for row in info_result.fetchall()}
            if existing_cols and "pending_days" not in existing_cols:
                needs_subscription_migration = True
                await conn.execute(text("ALTER TABLE subscriptions RENAME TO subscriptions_legacy_v1;"))
                # SQLite ไม่ rename index ตามตารางให้อัตโนมัติ -- ต้องล้าง index ชื่อเดิมก่อน
                # ไม่งั้นจะชนกับ index ชื่อเดียวกันที่ schema ใหม่จะสร้างตอน create_all() ด้านล่าง
                for old_index_name in (
                    "ix_subscriptions_user_id",
                    "ix_subscriptions_status",
                    "ix_subscriptions_expires_at",
                    "ix_subscriptions_status_expires",
                ):
                    await conn.execute(text(f"DROP INDEX IF EXISTS {old_index_name};"))

        await conn.run_sync(Base.metadata.create_all)

        if needs_subscription_migration:
            await _migrate_legacy_subscriptions(conn)

        if "sqlite" in config.DATABASE_URL:
            try:
                await conn.execute(text("ALTER TABLE payment_slips ADD COLUMN plan_type VARCHAR(32) DEFAULT 'VIP_30D';"))
            except Exception:
                pass
            try:
                await conn.execute(text("ALTER TABLE payment_slips ADD COLUMN payment_method VARCHAR(32) DEFAULT 'PROMPTPAY';"))
            except Exception:
                pass
            try:
                await conn.execute(text("ALTER TABLE users ADD COLUMN referred_by_id BIGINT;"))
            except Exception:
                pass
            try:
                await conn.execute(text("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0;"))
            except Exception:
                pass
            try:
                await conn.execute(text("ALTER TABLE users ADD COLUMN referral_bonus_days INTEGER DEFAULT 0;"))
            except Exception:
                pass
            try:
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at DESC);"))
            except Exception:
                pass
    logger.info("Database initialized successfully with WAL mode enabled.")


async def close_db() -> None:
    """Dispose of the database engine connection pool."""
    logger.info("Closing database connection pool...")
    await engine.dispose()
    logger.info("Database connection closed.")


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str],
    full_name: str,
    referred_by_id: Optional[int] = None,
) -> Tuple[User, bool]:
    """
    Fetch an existing user or create a new one. Updates username/full_name if changed.
    Returns (User, is_created).
    """
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    
    if user:
        updated = False
        if username is not None and user.username != username:
            user.username = username
            updated = True
        if full_name and full_name != f"User {telegram_id}" and user.full_name != full_name:
            user.full_name = full_name
            updated = True
        elif not user.full_name and full_name:
            user.full_name = full_name
            updated = True
        if updated:
            session.add(user)
        return user, False

    # ตรวจสอบ referred_by_id ต้องไม่ใช่ตัวเอง
    valid_referrer = referred_by_id if (referred_by_id and referred_by_id != telegram_id) else None

    new_user = User(
        telegram_id=telegram_id,
        username=username,
        full_name=full_name,
        trial_used=False,
        referred_by_id=valid_referrer,
        referral_count=0,
        referral_bonus_days=0,
    )
    session.add(new_user)
    await session.flush()
    return new_user, True
