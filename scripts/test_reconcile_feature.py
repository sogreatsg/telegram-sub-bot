import asyncio
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from bot.models.schema import Base, User, Subscription, SubscriptionGrant, SubStatus, GrantType, PlanType
from bot.services.reconciliation import reconcile_user, reconcile_all_users

async def test_reconciliation():
    # Use temporary sqlite in-memory or temp file
    test_db = BASE_DIR / "data" / "test_reconcile.db"
    if test_db.exists():
        test_db.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(timezone.utc)
    base_join_time = now - timedelta(days=10)

    print("\n--- [TEST 1] Setting up Test User A with duplicate referral grants ---")
    async with async_session() as session:
        # Referrer User A (joined 10 days ago, has 30d VIP purchase + 15m trial)
        user_a = User(
            telegram_id=1001,
            username="user_a",
            full_name="User Alpha",
            trial_used=True,
            referral_count=5, # WRONGLY set to 5 because of bug
            referral_bonus_days=5, # WRONGLY set to 5 because of bug
            created_at=base_join_time,
        )
        session.add(user_a)

        # Friend 1: Valid friend who actually joined & used trial
        friend_1 = User(
            telegram_id=2001,
            username="friend_1",
            full_name="Friend One",
            trial_used=True,
            referred_by_id=1001,
            referral_rewarded=False,
            created_at=base_join_time + timedelta(days=1),
        )
        session.add(friend_1)

        # Friend 2: Fake/unjoined friend (never used trial, never joined)
        friend_2 = User(
            telegram_id=2002,
            username="friend_2",
            full_name="Friend Two",
            trial_used=False,
            referred_by_id=1001,
            referral_rewarded=False,
            created_at=base_join_time + timedelta(days=2),
        )
        session.add(friend_2)

        # Subscription for User A: currently has incorrect expires_at because of 5 fake ref days
        sub_a = Subscription(
            user_id=1001,
            status=SubStatus.ACTIVE.value,
            joined_at=base_join_time,
            expires_at=base_join_time + timedelta(days=35, minutes=15), # 30 purchase + 5 fake ref days + 15m trial
            created_at=base_join_time,
        )
        session.add(sub_a)

        # Grants for User A:
        # 1. Purchase VIP 30D
        session.add(SubscriptionGrant(
            user_id=1001,
            days=30,
            minutes=0,
            source_label="สมาชิก VIP 30 วัน",
            grant_type=GrantType.PURCHASE.value,
            created_at=base_join_time,
        ))
        # 2. Duplicate Referral Grants (5 grants awarded instead of 1)
        for i in range(1, 6):
            session.add(SubscriptionGrant(
                user_id=1001,
                days=1,
                minutes=0,
                source_label=f"🎁 VIP โบนัสชวนเพื่อน #{i}",
                grant_type=GrantType.REFERRAL_BONUS.value,
                created_at=base_join_time + timedelta(days=i),
            ))

        await session.commit()

    print("Test data created in DB.")

    print("\n--- [TEST 2] Running reconcile_user on User A ---")
    async with async_session() as session:
        res = await reconcile_user(session, 1001, commit=True)
        print(f"Reconcile Result:")
        print(f"  • User ID: {res.user_id}")
        print(f"  • Ref Count: {res.ref_count_old} -> {res.ref_count_new} (Expected 1)")
        print(f"  • Ref Bonus Days: {res.ref_bonus_days_old} -> {res.ref_bonus_days_new} (Expected 1)")
        print(f"  • Excess Grants Deleted: {res.excess_ref_grants_deleted} (Expected 4)")
        print(f"  • Total Entitled: Purchase={res.purchase_days}d + Admin={res.admin_days}d + Ref={res.referral_days}d + Trial={res.trial_minutes}m = {res.total_days}d {res.total_minutes}m")
        print(f"  • Old Expiry: {res.expires_at_old}")
        print(f"  • New Expiry: {res.expires_at_new}")
        print(f"  • Message: {res.message}")

        # Assertions
        assert res.ref_count_new == 1, f"Expected 1 valid friend, got {res.ref_count_new}"
        assert res.ref_bonus_days_new == 1, f"Expected 1 valid bonus day, got {res.ref_bonus_days_new}"
        assert res.excess_ref_grants_deleted == 4, f"Expected 4 deleted grants, got {res.excess_ref_grants_deleted}"
        assert res.total_days == 31, f"Expected 31 total days (30 purchase + 1 ref), got {res.total_days}"
        assert res.total_minutes == 15, f"Expected 15 total minutes, got {res.total_minutes}"
        
        expected_expiry = base_join_time + timedelta(days=31, minutes=15)
        assert abs((res.expires_at_new - expected_expiry).total_seconds()) < 2, f"Expiry mismatch! Expected {expected_expiry}, got {res.expires_at_new}"

    print("\n--- [TEST 3] Verifying database state after commit ---")
    async with async_session() as session:
        user_after = await session.get(User, 1001)
        sub_after = await session.get(Subscription, 1001)
        grants_after = (await session.execute(
            select(SubscriptionGrant).where(SubscriptionGrant.user_id == 1001)
        )).scalars().all()
        ref_grants_after = [g for g in grants_after if g.grant_type == GrantType.REFERRAL_BONUS.value]

        print(f"DB User: count={user_after.referral_count}, bonus_days={user_after.referral_bonus_days}")
        print(f"DB Sub: status={sub_after.status}, expires_at={sub_after.expires_at}")
        print(f"DB Grants count: {len(grants_after)} (1 purchase + 1 referral)")

        assert user_after.referral_count == 1
        assert user_after.referral_bonus_days == 1
        assert len(ref_grants_after) == 1
        assert len(grants_after) == 2

    print("\n--- [TEST 4] Running reconcile_all_users ---")
    async with async_session() as session:
        all_results = await reconcile_all_users(session, commit=True)
        print(f"All scanned: {len(all_results)} users")
        for r in all_results:
            print(f"User {r.user_id}: {r.message}")

    await engine.dispose()
    if test_db.exists():
        test_db.unlink()

    print("\n==========================================")
    print("  ALL RECONCILIATION TESTS PASSED! 100% ")
    print("==========================================\n")

if __name__ == "__main__":
    asyncio.run(test_reconciliation())
