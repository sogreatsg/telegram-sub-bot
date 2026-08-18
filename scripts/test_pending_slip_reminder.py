import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from bot.models.schema import Base, User, PaymentSlip, SlipStatus, PlanType
import bot.services.scheduler as sched_module
from bot.services.scheduler import check_pending_slips_reminder

async def test_slip_reminder():
    test_db = BASE_DIR / "data" / "test_slip_reminder.db"
    if test_db.exists():
        test_db.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{test_db}", echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        async with async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Monkeypatch get_session
    sched_module.get_session = mock_get_session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.send_photo = AsyncMock()
    mock_bot.send_document = AsyncMock()

    now = datetime.now(timezone.utc)

    print("\n--- [TEST 1] Setup Test Users and Slips ---")
    async with async_session() as session:
        user = User(
            telegram_id=8888,
            username="slip_tester",
            full_name="Slip Tester",
            created_at=now,
        )
        session.add(user)

        # Slip 1: Recent slip (created 30s ago) -> Should NOT be reminded
        slip_recent = PaymentSlip(
            id=1,
            user_id=8888,
            file_id="https://gift.truemoney.com/campaign/?v=test1",
            plan_type=PlanType.VIP_30D.value,
            payment_method="TRUEMONEY_ANGPAO",
            status=SlipStatus.PENDING.value,
            created_at=now - timedelta(seconds=30),
        )

        # Slip 2: Overdue slip (created 90s ago, never reminded) -> Should BE reminded
        slip_overdue = PaymentSlip(
            id=2,
            user_id=8888,
            file_id="https://gift.truemoney.com/campaign/?v=test2",
            plan_type=PlanType.VIP_30D.value,
            payment_method="TRUEMONEY_ANGPAO",
            status=SlipStatus.PENDING.value,
            created_at=now - timedelta(seconds=90),
        )

        # Slip 3: Overdue photo slip (created 120s ago, never reminded) -> Should BE reminded
        slip_photo = PaymentSlip(
            id=3,
            user_id=8888,
            file_id="photo_file_id_123",
            plan_type=PlanType.VIP_30D.value,
            payment_method="PROMPTPAY",
            status=SlipStatus.PENDING.value,
            created_at=now - timedelta(seconds=120),
        )

        # Slip 4: Approved slip (created 100s ago) -> Should NOT be reminded
        slip_approved = PaymentSlip(
            id=4,
            user_id=8888,
            file_id="photo_file_id_456",
            plan_type=PlanType.VIP_30D.value,
            payment_method="PROMPTPAY",
            status=SlipStatus.APPROVED.value,
            created_at=now - timedelta(seconds=100),
        )

        session.add_all([slip_recent, slip_overdue, slip_photo, slip_approved])
        await session.commit()

    print("--- [TEST 2] Run check_pending_slips_reminder (Round 1) ---")
    await check_pending_slips_reminder(mock_bot)

    # Verify: Slip 2 (message) and Slip 3 (photo) were reminded
    assert mock_bot.send_message.call_count == 1, f"Expected 1 TrueMoney reminder, got {mock_bot.send_message.call_count}"
    assert mock_bot.send_photo.call_count == 1, f"Expected 1 Photo reminder, got {mock_bot.send_photo.call_count}"
    print("Round 1 Reminders Sent: PASS ✅")

    async with async_session() as session:
        s2 = await session.get(PaymentSlip, 2)
        s3 = await session.get(PaymentSlip, 3)
        assert s2.reminder_count == 1
        assert s3.reminder_count == 1
        assert s2.last_reminded_at is not None
        assert s3.last_reminded_at is not None

    print("--- [TEST 3] Run check_pending_slips_reminder immediately (0s later) ---")
    mock_bot.send_message.reset_mock()
    mock_bot.send_photo.reset_mock()
    await check_pending_slips_reminder(mock_bot)

    # Verify: No new reminders because < 60s since last reminder
    assert mock_bot.send_message.call_count == 0
    assert mock_bot.send_photo.call_count == 0
    print("Throttle Verification: PASS ✅")

    print("--- [TEST 4] Fast-forward 65 seconds and test Round 2 Reminder ---")
    async with async_session() as session:
        s2 = await session.get(PaymentSlip, 2)
        s2.last_reminded_at = now - timedelta(seconds=65)
        session.add(s2)
        await session.commit()

    await check_pending_slips_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 1
    print("Round 2 Reminder (Repeat): PASS ✅")

    print("--- [TEST 5] Approve Slip 2 and verify no more reminders ---")
    async with async_session() as session:
        s2 = await session.get(PaymentSlip, 2)
        s2.status = SlipStatus.APPROVED.value
        s2.last_reminded_at = now - timedelta(seconds=120)
        session.add(s2)
        await session.commit()

    mock_bot.send_message.reset_mock()
    await check_pending_slips_reminder(mock_bot)
    assert mock_bot.send_message.call_count == 0
    print("Stop Reminder after Approval: PASS ✅")

    await engine.dispose()
    if test_db.exists():
        test_db.unlink()

    print("\n==========================================")
    print("  ALL SLIP REMINDER TESTS PASSED 100%!    ")
    print("==========================================\n")

if __name__ == "__main__":
    asyncio.run(test_slip_reminder())
