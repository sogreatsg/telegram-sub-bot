import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubscriptionGrant, SubStatus, GrantType, PlanType
from bot.services.database import init_db, get_session, close_db
from bot.services.subscription import grant_subscription, activate_pending_subscription
from bot.services.channel_service import (
    get_user_target_channel_id,
    get_all_target_channel_ids,
    is_target_channel,
    is_secondary_channel,
    get_channel_label,
)
from sqlalchemy import select

async def run_lifecycle_tests():
    config = get_settings()
    await init_db()
    print("=" * 70)
    print("STARTING FULL LIFECYCLE & ISOLATION SIMULATION TESTS")
    print("=" * 70)

    # -------------------------------------------------------------
    # TEST 1: Primary User (ห้องเก่า) Full Flow
    # -------------------------------------------------------------
    print("\n[TEST 1] Primary User (ห้องเก่า - ไม่ถูกย้าย):")
    async with get_session() as session:
        # 1.1 สร้าง User ปกติ
        user1 = User(
            telegram_id=900000001,
            full_name="Primary Normal User",
            username="primary_user",
            assigned_channel="PRIMARY",
            is_moved_to_secondary=False,
        )
        session.add(user1)
        await session.commit()

        # 1.2 ตรวจสอบ Target Channel ของ User ปกติ
        target_cid = get_user_target_channel_id(user1)
        assert target_cid == config.CHANNEL_ID, f"Target must be primary {config.CHANNEL_ID}, got {target_cid}"
        assert get_channel_label(target_cid) == "Channel VIP (เดิม)"
        print(f"  1.1 Target Channel check: {target_cid} ({get_channel_label(target_cid)}) -> PASS")

        # 1.3 ทดสอบการให้สิทธิ์แบบ PENDING (เหมือนกดทดลองฟรี หรือ ซื้อแพ็กเกจ)
        grant = await grant_subscription(
            session=session,
            user_id=user1.telegram_id,
            days=30,
            minutes=0,
            source_label="สมาชิก VIP 30 วัน",
            grant_type=GrantType.PURCHASE.value,
            has_value=True,
            is_in_channel=False,
        )
        await session.commit()

        sub = await session.get(Subscription, user1.telegram_id)
        assert sub.status == SubStatus.PENDING.value
        assert sub.pending_days == 30
        print(f"  1.2 Subscription PENDING status: {sub.status}, pending_days={sub.pending_days} -> PASS")

        # 1.4 จำลองการกดเข้าห้องเดิม (Primary Channel)
        # เหตุการณ์ที่เกิดขึ้นใน channel_events.py:
        assert is_target_channel(config.CHANNEL_ID)
        assert not is_secondary_channel(config.CHANNEL_ID)

        act_grant = await activate_pending_subscription(session, user_id=user1.telegram_id)
        await session.commit()

        sub_active = await session.get(Subscription, user1.telegram_id)
        user_chk = await session.get(User, user1.telegram_id)
        assert sub_active.status == SubStatus.ACTIVE.value
        assert sub_active.expires_at > datetime.now(timezone.utc)
        # สถานะ User ต้องยังคงเป็น PRIMARY ไม่ถูกเปลี่ยนไปเป็น SECONDARY
        assert user_chk.assigned_channel == "PRIMARY"
        assert user_chk.is_moved_to_secondary is False
        print(f"  1.3 Joined Primary Channel: status={sub_active.status}, assigned_channel={user_chk.assigned_channel} -> PASS")

    # -------------------------------------------------------------
    # TEST 2: Move User & Secondary Channel (ห้องใหม่) Full Flow
    # -------------------------------------------------------------
    print("\n[TEST 2] Move User & Secondary Channel (ห้องใหม่):")
    async with get_session() as session:
        # 2.1 สร้าง User คนที่ 2
        user2 = User(
            telegram_id=900000002,
            full_name="User To Be Moved",
            username="moved_user",
            assigned_channel="PRIMARY",
            is_moved_to_secondary=False,
        )
        session.add(user2)
        await session.commit()

        # 2.2 จำลองคำสั่ง /move_user
        user2.assigned_channel = "SECONDARY"
        user2.is_moved_to_secondary = True
        session.add(user2)
        await session.commit()

        # 2.3 ตรวจสอบ Target Channel ของ User ที่ถูกย้าย
        target_cid2 = get_user_target_channel_id(user2)
        assert target_cid2 == config.SECONDARY_CHANNEL_ID, f"Target must be secondary {config.SECONDARY_CHANNEL_ID}, got {target_cid2}"
        assert get_channel_label(target_cid2) == "Channel ใหม่ (Target Channel)"
        print(f"  2.1 Moved user target channel: {target_cid2} ({get_channel_label(target_cid2)}) -> PASS")

        # 2.4 ให้สิทธิ์ PENDING
        grant2 = await grant_subscription(
            session=session,
            user_id=user2.telegram_id,
            days=30,
            minutes=0,
            source_label="สมาชิก VIP 30 วัน",
            grant_type=GrantType.PURCHASE.value,
            has_value=True,
            is_in_channel=False,
        )
        await session.commit()

        # 2.5 จำลองการกดเข้าห้องใหม่ (Secondary Channel)
        assert is_target_channel(config.SECONDARY_CHANNEL_ID)
        assert is_secondary_channel(config.SECONDARY_CHANNEL_ID)

        act_grant2 = await activate_pending_subscription(session, user_id=user2.telegram_id)
        await session.commit()

        sub_active2 = await session.get(Subscription, user2.telegram_id)
        user_chk2 = await session.get(User, user2.telegram_id)
        assert sub_active2.status == SubStatus.ACTIVE.value
        assert user_chk2.assigned_channel == "SECONDARY"
        assert user_chk2.is_moved_to_secondary is True
        print(f"  2.2 Joined Secondary Channel: status={sub_active2.status}, assigned_channel={user_chk2.assigned_channel} -> PASS")

        # 2.6 ต่ออายุ (Renewal) ในอนาคต -> ตรวจสอบว่ายังคงได้ห้องใหม่อัตโนมัติ
        target_future = get_user_target_channel_id(user_chk2)
        assert target_future == config.SECONDARY_CHANNEL_ID
        print(f"  2.3 Future renewal target channel: {target_future} (Secondary) -> PASS")

    # -------------------------------------------------------------
    # TEST 3: Verification of Non-Interference (ห้องเก่าไม่ได้รับผลกระทบ)
    # -------------------------------------------------------------
    print("\n[TEST 3] Verification of Non-Interference (ห้องเก่าไม่ได้รับผลกระทบ):")
    async with get_session() as session:
        u1 = await session.get(User, 900000001)
        u2 = await session.get(User, 900000002)

        assert get_user_target_channel_id(u1) == config.CHANNEL_ID, "User 1 must remain in Primary Channel"
        assert get_user_target_channel_id(u2) == config.SECONDARY_CHANNEL_ID, "User 2 must remain in Secondary Channel"
        print("  3.1 User 1 (Old) and User 2 (New) cleanly segregated -> PASS")

    # -------------------------------------------------------------
    # TEST 4: Cleanup test data
    # -------------------------------------------------------------
    async with get_session() as session:
        for uid in [900000001, 900000002]:
            sub_del = await session.get(Subscription, uid)
            if sub_del:
                await session.delete(sub_del)
            grants_del = (await session.execute(select(SubscriptionGrant).where(SubscriptionGrant.user_id == uid))).scalars().all()
            for g in grants_del:
                await session.delete(g)
            u_del = await session.get(User, uid)
            if u_del:
                await session.delete(u_del)
        await session.commit()
    print("\n[TEST 4] Cleanup test records: PASS")

    await close_db()
    print("\n" + "=" * 70)
    print("ALL LIFECYCLE TESTS PASSED 100% WITH ZERO REGRESSION!")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(run_lifecycle_tests())
