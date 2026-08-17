import asyncio
import sys
import os

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.handlers.payment import extract_truemoney_url, get_payment_method_keyboard, get_payment_cancel_keyboard, get_admin_slip_keyboard
from bot.models.schema import PLAN_DETAILS, PlanType, PaymentSlip, SlipStatus, get_dynamic_plan_info
from bot.services.database import get_session, init_db, get_or_create_user

async def run_tests():
    print("1. Testing TrueMoney URL Extraction...")
    urls = [
        ("https://gift.truemoney.com/campaign/?v=0123456789abcdef", "https://gift.truemoney.com/campaign/?v=0123456789abcdef"),
        ("https://gift.truemoney.com/campaign/?v=01a00ed5059e79b66f980b60404587dcb99", "https://gift.truemoney.com/campaign/?v=01a00ed5059e79b66f980b60404587dcb99"),
        ("gift.truemoney.com/campaign/?v=0123456789abcdef", "https://gift.truemoney.com/campaign/?v=0123456789abcdef"),
        ("ส่งซองครับ https://gift.truemoney.com/campaign/?v=test12345 ขอบคุณครับ", "https://gift.truemoney.com/campaign/?v=test12345"),
        ("https://tmn.app.link/abcdef123", "https://tmn.app.link/abcdef123"),
        ("ข้อความทั่วไปไม่มีลิงก์", None),
        ("https://google.com/test", None),
    ]

    for inp, expected in urls:
        res = extract_truemoney_url(inp)
        print(f"  Input: {inp!r} -> Extracted: {res!r}")
        assert res == expected, f"Failed for {inp}: expected {expected}, got {res}"
    print("  -> URL extraction tests PASSED!")

    print("\n2. Testing Payment Keyboards...")
    for plan in [PlanType.VIP_3D.value, PlanType.VIP_10D.value, PlanType.VIP_30D.value, PlanType.PROMOTION.value]:
        kb = get_payment_method_keyboard(plan)
        assert len(kb.inline_keyboard) == 3, f"Payment method keyboard should have 3 rows for {plan}"
        assert f"payment:method:promptpay:{plan}" in kb.inline_keyboard[0][0].callback_data
        assert f"payment:method:truemoney:{plan}" in kb.inline_keyboard[1][0].callback_data
        assert kb.inline_keyboard[2][0].callback_data == "menu:main"

        cancel_kb = get_payment_cancel_keyboard(plan)
        assert len(cancel_kb.inline_keyboard) == 2, f"Cancel keyboard should have 2 rows for {plan}"
        assert cancel_kb.inline_keyboard[0][0].callback_data == f"menu:subscribe:{plan}"
        assert cancel_kb.inline_keyboard[1][0].callback_data == "payment:cancel"

    admin_kb = get_admin_slip_keyboard(123, 456789)
    assert len(admin_kb.inline_keyboard) == 2
    assert admin_kb.inline_keyboard[0][0].callback_data == "admin:approve:123"
    assert admin_kb.inline_keyboard[0][1].callback_data == "admin:reject:123"
    print("  -> Keyboard tests PASSED!")

    print("\n3. Testing Database Initialization & Schema...")
    await init_db()
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=999999999,
            username="testuser",
            full_name="Test User",
        )
        assert user.telegram_id == 999999999

        # Create PromptPay slip
        slip_pp = PaymentSlip(
            user_id=user.telegram_id,
            file_id="photo_file_id_12345",
            plan_type=PlanType.VIP_30D.value,
            payment_method="PROMPTPAY",
            status=SlipStatus.PENDING.value,
        )
        session.add(slip_pp)
        await session.flush()
        assert slip_pp.id is not None
        assert slip_pp.payment_method == "PROMPTPAY"

        # Create TrueMoney Angpao slip
        slip_tm = PaymentSlip(
            user_id=user.telegram_id,
            file_id="https://gift.truemoney.com/campaign/?v=testvoucher123",
            plan_type=PlanType.VIP_3D.value,
            payment_method="TRUEMONEY_ANGPAO",
            status=SlipStatus.PENDING.value,
        )
        session.add(slip_tm)
        await session.flush()
        assert slip_tm.id is not None
        assert slip_tm.payment_method == "TRUEMONEY_ANGPAO"
        assert slip_tm.file_id == "https://gift.truemoney.com/campaign/?v=testvoucher123"

        # Cleanup test records
        await session.delete(slip_pp)
        await session.delete(slip_tm)
        await session.delete(user)
    print("  -> Database & Schema tests PASSED!")

    print("\n🎉 ALL TESTS COMPLETED SUCCESSFULLY WITH 0 ERRORS!")

if __name__ == "__main__":
    asyncio.run(run_tests())
