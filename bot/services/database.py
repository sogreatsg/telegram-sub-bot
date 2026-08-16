import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Tuple
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import get_settings
from bot.models.schema import Base, User

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


async def init_db() -> None:
    """Initialize database tables and verify SQLite WAL mode."""
    logger.info("Initializing database...")
    async with engine.begin() as conn:
        if "sqlite" in config.DATABASE_URL:
            # Explicitly run WAL pragma during init
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in config.DATABASE_URL:
            try:
                await conn.execute(text("ALTER TABLE payment_slips ADD COLUMN plan_type VARCHAR(32) DEFAULT 'VIP_30D';"))
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
        if user.username != username:
            user.username = username
            updated = True
        if user.full_name != full_name:
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
