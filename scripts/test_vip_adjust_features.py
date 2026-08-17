import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.models.schema import Base, User, Subscription, SubscriptionGrant, SubStatus, GrantType
from bot.utils.time_utils import ensure_utc

async def test_vip_adjustments():
    test_db = BASE_DIR / "data" / "test_vip_adjust.db"
    if test_db.exists():
        test_db.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(timezone.utc)

    print("\n--- [TEST 1] Setup Test User with 30 days active sub ---")
    async with async_session() as session:
        user = User(
            telegram_id=9001,
            username="vip_user",
            full_name="VIP Test User",
            created_at=now,
        )
        sub = Subscription(
            user_id=9001,
            status=SubStatus.ACTIVE.value,
            joined_at=now,
            expires_at=now + timedelta(days=30),
            created_at=now,
        )
        session.add(user)
        session.add(sub)
        await session.commit()

    print("--- [TEST 2] Test Deduct VIP Days (-10 days) ---")
    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        old_exp = sub.expires_at
        sub.expires_at = sub.expires_at - timedelta(days=10)
        session.add(SubscriptionGrant(
            user_id=9001,
            days=-10,
            minutes=0,
            source_label="Admin ลดวัน (-10 วัน)",
            grant_type=GrantType.ADMIN_GRANT.value,
            has_value=False,
        ))
        session.add(sub)
        await session.commit()

    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        expected_exp = now + timedelta(days=20)
        print(f"Old Expiry: {old_exp}, New Expiry: {sub.expires_at}")
        assert abs((ensure_utc(sub.expires_at) - expected_exp).total_seconds()) < 5
        print("Deduct VIP: PASS ✅")

    print("--- [TEST 3] Test Set VIP Days (Exact 45 days) ---")
    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        sub.expires_at = now + timedelta(days=45)
        sub.status = SubStatus.ACTIVE.value
        session.add(SubscriptionGrant(
            user_id=9001,
            days=45,
            minutes=0,
            source_label="Admin กำหนดวันตรง (45 วัน)",
            grant_type=GrantType.ADMIN_GRANT.value,
            has_value=True,
        ))
        session.add(sub)
        await session.commit()

    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        expected_exp = now + timedelta(days=45)
        print(f"Set Expiry: {sub.expires_at}")
        assert abs((ensure_utc(sub.expires_at) - expected_exp).total_seconds()) < 5
        assert sub.status == SubStatus.ACTIVE.value
        print("Set VIP: PASS ✅")

    print("--- [TEST 4] Test Set VIP Days to 0 (Immediate Expire) ---")
    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        sub.expires_at = now
        sub.status = SubStatus.EXPIRED.value
        session.add(sub)
        await session.commit()

    async with async_session() as session:
        sub = await session.get(Subscription, 9001)
        assert sub.status == SubStatus.EXPIRED.value
        print("Expire VIP: PASS ✅")

    await engine.dispose()
    if test_db.exists():
        test_db.unlink()

    print("\n==========================================")
    print("  ALL VIP ADJUSTMENT TESTS PASSED 100%!   ")
    print("==========================================\n")

if __name__ == "__main__":
    asyncio.run(test_vip_adjustments())
