import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select, delete
from bot.models.schema import User, Subscription, SubscriptionGrant, PaymentSlip, SlipStatus, SubStatus, PlanType, GrantType
from bot.services.database import get_session
from bot.services.spending import (
    get_plan_price,
    get_plan_display_name,
    get_user_spending_summary,
    build_top_spenders_view,
)
from bot.handlers.admin import (
    get_admin_menu_text_and_kb,
    handle_admin_top_spenders_command,
    handle_admin_top_spenders_page_callback,
    handle_admin_menu_top_spenders_callback,
)


class TestTopSpenders(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # สร้าง mock users และ payment slips ใน db เพื่อทดสอบ
        self.user_a_id = 99911101
        self.user_b_id = 99911102
        self.user_c_id = 99911103

        async with get_session() as session:
            # ลบข้อมูลทดสอบเดิมหากมีค้าง
            for uid in [self.user_a_id, self.user_b_id, self.user_c_id]:
                await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == uid))
                await session.execute(delete(SubscriptionGrant).where(SubscriptionGrant.user_id == uid))
                await session.execute(delete(Subscription).where(Subscription.user_id == uid))
                await session.execute(delete(User).where(User.telegram_id == uid))

            # สร้าง User A (Spender #1: ซื้อ VIP 30 วัน = 1000 บาท และ VIP 10 วัน = 500 บาท -> รวม 1500 บาท)
            user_a = User(telegram_id=self.user_a_id, full_name="User A Top", username="user_a")
            sub_a = Subscription(user_id=self.user_a_id, status=SubStatus.ACTIVE.value, expires_at=datetime.now(timezone.utc) + timedelta(days=40))
            session.add_all([user_a, sub_a])

            slip_a1 = PaymentSlip(user_id=self.user_a_id, file_id="slip_a1", plan_type=PlanType.VIP_30D.value, status=SlipStatus.APPROVED.value)
            slip_a2 = PaymentSlip(user_id=self.user_a_id, file_id="slip_a2", plan_type=PlanType.VIP_10D.value, status=SlipStatus.APPROVED.value)
            session.add_all([slip_a1, slip_a2])

            # สร้าง User B (Spender #2: ซื้อ VIP 3 วัน = 300 บาท)
            user_b = User(telegram_id=self.user_b_id, full_name="User B Mid", username="user_b")
            sub_b = Subscription(user_id=self.user_b_id, status=SubStatus.PENDING.value, pending_days=3)
            session.add_all([user_b, sub_b])

            slip_b1 = PaymentSlip(user_id=self.user_b_id, file_id="slip_b1", plan_type=PlanType.VIP_3D.value, status=SlipStatus.APPROVED.value)
            # เพิ่ม slip pending ที่ยังไม่อนุมัติ (ต้องไม่ถูกนับยอด)
            slip_b2 = PaymentSlip(user_id=self.user_b_id, file_id="slip_b2", plan_type=PlanType.VIP_30D.value, status=SlipStatus.PENDING.value)
            session.add_all([slip_b1, slip_b2])

            # สร้าง User C (ไม่ได้ซื้อเลย = 0 บาท)
            user_c = User(telegram_id=self.user_c_id, full_name="User C Free", username="user_c", trial_used=True)
            session.add(user_c)

            await session.commit()

    async def asyncTearDown(self):
        async with get_session() as session:
            for uid in [self.user_a_id, self.user_b_id, self.user_c_id]:
                await session.execute(delete(PaymentSlip).where(PaymentSlip.user_id == uid))
                await session.execute(delete(SubscriptionGrant).where(SubscriptionGrant.user_id == uid))
                await session.execute(delete(Subscription).where(Subscription.user_id == uid))
                await session.execute(delete(User).where(User.telegram_id == uid))
            await session.commit()

    def test_plan_prices(self):
        """1. ทดสอบการคำนวณราคาของแต่ละแพ็กเกจ"""
        self.assertEqual(get_plan_price("VIP_12H"), 100)
        self.assertEqual(get_plan_price("VIP_1D"), 100)
        self.assertEqual(get_plan_price("VIP_3D"), 300)
        self.assertEqual(get_plan_price("VIP_10D"), 500)
        self.assertEqual(get_plan_price("VIP_30D"), 1000)
        self.assertEqual(get_plan_price("MONTHLY_30D"), 1000)
        self.assertEqual(get_plan_price("REFERRAL_VIP"), 0)
        self.assertEqual(get_plan_price(None), 0)

    async def test_user_spending_summary(self):
        """2. ทดสอบการดึงสรุปยอดและอันดับรายบุคคล"""
        async with get_session() as session:
            summary_a = await get_user_spending_summary(self.user_a_id, session)
            self.assertEqual(summary_a["total_spent"], 1500)
            self.assertEqual(summary_a["approved_count"], 2)
            self.assertEqual(summary_a["rank"], 1)

            summary_b = await get_user_spending_summary(self.user_b_id, session)
            self.assertEqual(summary_b["total_spent"], 300)
            self.assertEqual(summary_b["approved_count"], 1)
            self.assertEqual(summary_b["rank"], 2)

            summary_c = await get_user_spending_summary(self.user_c_id, session)
            self.assertEqual(summary_c["total_spent"], 0)
            self.assertEqual(summary_c["approved_count"], 0)
            self.assertIsNone(summary_c["rank"])

    async def test_top_spenders_view(self):
        """3. ทดสอบการสร้าง Leaderboard View และ UI Text/Keyboard"""
        text, kb = await build_top_spenders_view(period="all", page=1, page_size=10)
        self.assertIn("อันดับยอดชำระเงินสูงสุด (Top Spenders Leaderboard)", text)
        self.assertIn("User A Top", text)
        self.assertIn("1,500 บาท", text)
        self.assertIn("User B Mid", text)
        self.assertIn("300 บาท", text)
        self.assertNotIn("User C Free", text)  # User C ไม่มียอดซื้อ ต้องไม่ติดบอร์ด

        # ตรวจสอบว่ามีปุ่มตัวกรองและปุ่มดูผู้ใช้
        button_callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn(f"admin:view_user:{self.user_a_id}", button_callbacks)
        self.assertIn("admin:top_spenders_page:month:1", button_callbacks)
        self.assertIn("admin:top_spenders_page:year:1", button_callbacks)

    def test_admin_menu_has_top_spender(self):
        """4. ทดสอบว่าหน้าเมนู Admin มีคำสั่งและปุ่ม Top Spender"""
        menu_text, kb = get_admin_menu_text_and_kb()
        self.assertIn("/top_spender", menu_text)
        button_callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("admin_menu:top_spenders", button_callbacks)

    async def test_handle_admin_top_spenders_command(self):
        """5. ทดสอบ Command /top_spender"""
        from bot.config import get_settings
        config = get_settings()

        msg = MagicMock()
        msg.chat.id = config.ADMIN_GROUP_ID
        msg.text = "/top_spender"
        msg.answer = AsyncMock()

        await handle_admin_top_spenders_command(msg)
        msg.answer.assert_called_once()
        args, kwargs = msg.answer.call_args
        self.assertIn("Top Spenders Leaderboard", kwargs.get("text", ""))

    async def test_handle_admin_top_spenders_callbacks(self):
        """6. ทดสอบ Callback การเปลี่ยนหน้าและเมนู Top Spender"""
        from bot.config import get_settings
        config = get_settings()

        # Test page callback
        cb = MagicMock()
        cb.message.chat.id = config.ADMIN_GROUP_ID
        cb.data = "admin:top_spenders_page:all:1"
        cb.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()

        await handle_admin_top_spenders_page_callback(cb)
        cb.message.edit_text.assert_called_once()

        # Test menu callback
        cb_menu = MagicMock()
        cb_menu.message.chat.id = config.ADMIN_GROUP_ID
        cb_menu.data = "admin_menu:top_spenders"
        cb_menu.answer = AsyncMock()
        cb_menu.message.answer = AsyncMock()

        await handle_admin_menu_top_spenders_callback(cb_menu)
        cb_menu.message.answer.assert_called_once()


if __name__ == "__main__":
    asyncio.run(unittest.main())
