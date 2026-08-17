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
from bot.models.schema import Base, User, Subscription, SubStatus
from bot.services.scheduler import build_active_members_report
import bot.services.scheduler as sched_module

async def test_summary_sorting():
    test_db = BASE_DIR / "data" / "test_summary_sort.db"
    if test_db.exists():
        test_db.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    # Monkeypatch get_session in sched_module
    sched_module.get_session = async_session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(timezone.utc)

    # Create 3 users with different joined_at times:
    # User 1: joined 10 days ago (Oldest)
    # User 2: joined 2 days ago (Middle)
    # User 3: joined 1 hour ago (Newest/Latest)
    async with async_session() as session:
        u1 = User(telegram_id=101, username="oldest_user", full_name="Oldest User", created_at=now - timedelta(days=10))
        s1 = Subscription(user_id=101, status=SubStatus.ACTIVE.value, joined_at=now - timedelta(days=10), expires_at=now + timedelta(days=20), created_at=now - timedelta(days=10))

        u2 = User(telegram_id=102, username="middle_user", full_name="Middle User", created_at=now - timedelta(days=2))
        s2 = Subscription(user_id=102, status=SubStatus.ACTIVE.value, joined_at=now - timedelta(days=2), expires_at=now + timedelta(days=28), created_at=now - timedelta(days=2))

        u3 = User(telegram_id=103, username="latest_user", full_name="Latest User", created_at=now - timedelta(hours=1))
        s3 = Subscription(user_id=103, status=SubStatus.ACTIVE.value, joined_at=now - timedelta(hours=1), expires_at=now + timedelta(days=30), created_at=now - timedelta(hours=1))

        session.add_all([u1, s1, u2, s2, u3, s3])
        await session.commit()

    report = await build_active_members_report(bot=None)
    print("Report Output:\n", report)

    # Verification: Latest User (103) should appear first (1.), Middle User (102) second (2.), Oldest (101) third (3.)
    pos_latest = report.find("Latest User")
    pos_middle = report.find("Middle User")
    pos_oldest = report.find("Oldest User")

    assert pos_latest != -1, "Latest User not found"
    assert pos_middle != -1, "Middle User not found"
    assert pos_oldest != -1, "Oldest User not found"

    print(f"Indices: Latest={pos_latest}, Middle={pos_middle}, Oldest={pos_oldest}")
    assert pos_latest < pos_middle < pos_oldest, "Sorting order is incorrect! Should be Latest < Middle < Oldest"

    print("Summary Sorting: PASS ✅")

    await engine.dispose()
    if test_db.exists():
        test_db.unlink()

if __name__ == "__main__":
    asyncio.run(test_summary_sorting())
