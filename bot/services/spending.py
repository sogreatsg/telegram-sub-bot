import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, Any, List

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.models.schema import (
    User,
    Subscription,
    SubscriptionGrant,
    PaymentSlip,
    SlipStatus,
    SubStatus,
    PlanType,
    GrantType,
    PLAN_DETAILS,
    get_dynamic_plan_info,
)
from bot.services.database import get_session
from bot.utils.time_utils import (
    BANGKOK_TZ,
    format_thai_datetime,
    format_user_title,
    format_remaining_time,
    ensure_utc,
)

logger = logging.getLogger(__name__)


def get_plan_price(plan_type: Optional[str]) -> int:
    """คืนราคาเงินบาท (THB) ของแพ็กเกจหรือสลิปการชำระเงิน"""
    if not plan_type:
        return 0
    if plan_type in PLAN_DETAILS:
        return int(get_dynamic_plan_info(plan_type).get("price", 0))
    if plan_type == PlanType.PROMOTION.value or str(plan_type).startswith("PROMOTION"):
        promo = get_dynamic_plan_info(PlanType.PROMOTION.value)
        return int(promo.get("price", 0))
    info = get_dynamic_plan_info(plan_type)
    return int(info.get("price", 0))


def get_plan_display_name(plan_type: Optional[str]) -> str:
    """คืนชื่อแสดงผลของแพ็กเกจ เช่น VIP 30 วัน, VIP 12 ชั่วโมง"""
    if not plan_type:
        return "-"
    if plan_type in PLAN_DETAILS:
        return PLAN_DETAILS[plan_type].get("name", plan_type)
    if plan_type == PlanType.PROMOTION.value or str(plan_type).startswith("PROMOTION"):
        return "โปรโมชั่นพิเศษ"
    return plan_type


async def get_user_spending_summary(user_id: int, session: AsyncSession) -> Dict[str, Any]:
    """
    คำนวณยอดชำระเงินสะสม จำนวนครั้งที่อนุมัติ วันที่ชำระล่าสุด
    และอันดับ Top Spender ของผู้ใช้รายคน
    """
    # 1. ดึงสลิปที่อนุมัติทั้งหมดของผู้ใช้นี้
    user_slips_stmt = (
        select(PaymentSlip)
        .where(
            PaymentSlip.user_id == user_id,
            PaymentSlip.status == SlipStatus.APPROVED.value,
        )
        .order_by(PaymentSlip.created_at.desc())
    )
    user_slips = (await session.execute(user_slips_stmt)).scalars().all()

    total_spent = sum(get_plan_price(s.plan_type) for s in user_slips)
    approved_count = len(user_slips)
    last_paid_at = user_slips[0].created_at if user_slips else None

    # 2. คำนวณอันดับ Ranking ทั่วระบบ (All-Time)
    rank = None
    total_spenders = 0
    if approved_count > 0:
        all_approved_stmt = (
            select(PaymentSlip)
            .where(PaymentSlip.status == SlipStatus.APPROVED.value)
        )
        all_approved = (await session.execute(all_approved_stmt)).scalars().all()
        
        spending_map: Dict[int, Dict[str, Any]] = {}
        for s in all_approved:
            uid = s.user_id
            if uid not in spending_map:
                spending_map[uid] = {"total_spent": 0, "count": 0, "last_paid": s.created_at}
            spending_map[uid]["total_spent"] += get_plan_price(s.plan_type)
            spending_map[uid]["count"] += 1
            if s.created_at and (not spending_map[uid]["last_paid"] or s.created_at > spending_map[uid]["last_paid"]):
                spending_map[uid]["last_paid"] = s.created_at

        sorted_spenders = sorted(
            spending_map.items(),
            key=lambda x: (x[1]["total_spent"], x[1]["count"], x[1]["last_paid"] or datetime.min),
            reverse=True,
        )
        total_spenders = len(sorted_spenders)
        for idx, (uid, _) in enumerate(sorted_spenders, start=1):
            if uid == user_id:
                rank = idx
                break

    return {
        "total_spent": total_spent,
        "approved_count": approved_count,
        "last_paid_at": last_paid_at,
        "rank": rank,
        "total_spenders": total_spenders,
    }


async def build_top_spenders_view(
    period: str = "all",
    page: int = 1,
    page_size: int = 10,
) -> Tuple[str, InlineKeyboardMarkup]:
    """
    สร้างข้อความสรุปและคีย์บอร์ดสำหรับรายงานอันดับ Top Spenders Leaderboard
    รองรับการกรองช่วงเวลา: 'all' (ตลอดกาล), 'month' (เดือนนี้), 'year' (ปีนี้)
    """
    now_utc = datetime.now(timezone.utc)
    now_bkk = datetime.now(BANGKOK_TZ)

    # กำหนดช่วงเวลาสำหรับฟิลเตอร์
    start_dt_utc: Optional[datetime] = None
    period_title = "🌟 ตลอดกาล (All-Time)"

    if period == "month":
        start_bkk = now_bkk.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_dt_utc = start_bkk.astimezone(timezone.utc)
        month_thai_names = [
            "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
            "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
        ]
        period_title = f"📅 ประจำเดือน {month_thai_names[now_bkk.month]} {now_bkk.year + 543}"
    elif period == "year":
        start_bkk = now_bkk.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        start_dt_utc = start_bkk.astimezone(timezone.utc)
        period_title = f"📆 ประจำปี {now_bkk.year + 543} ({now_bkk.year})"
    else:
        period = "all"
        period_title = "🌟 ตลอดกาล (All-Time)"

    async with get_session() as session:
        # 1. ดึงสลิปที่อนุมัติทั้งหมด (ตามเงื่อนไขเวลา)
        stmt = (
            select(PaymentSlip)
            .where(PaymentSlip.status == SlipStatus.APPROVED.value)
            .order_by(PaymentSlip.created_at.desc())
        )
        all_slips = (await session.execute(stmt)).scalars().all()

        # กรองตามช่วงเวลาใน Python เพื่อความชัวร์เรื่อง timezone / naive datetime
        filtered_slips: List[PaymentSlip] = []
        for s in all_slips:
            if start_dt_utc:
                slip_dt = ensure_utc(s.created_at)
                if slip_dt and slip_dt >= start_dt_utc:
                    filtered_slips.append(s)
            else:
                filtered_slips.append(s)

        # 2. รวมยอดตาม User ID
        spending_map: Dict[int, Dict[str, Any]] = {}
        total_revenue = 0
        total_transactions = len(filtered_slips)

        for s in filtered_slips:
            price = get_plan_price(s.plan_type)
            total_revenue += price
            uid = s.user_id
            if uid not in spending_map:
                spending_map[uid] = {
                    "user_id": uid,
                    "total_spent": 0,
                    "count": 0,
                    "last_paid": s.created_at,
                    "last_plan": s.plan_type,
                    "plans": [],
                }
            spending_map[uid]["total_spent"] += price
            spending_map[uid]["count"] += 1
            spending_map[uid]["plans"].append(s.plan_type)
            if s.created_at and (not spending_map[uid]["last_paid"] or s.created_at > spending_map[uid]["last_paid"]):
                spending_map[uid]["last_paid"] = s.created_at
                spending_map[uid]["last_plan"] = s.plan_type

        # 3. จัดอันดับยอดเงินมากไปน้อย
        ranked_spenders = sorted(
            spending_map.values(),
            key=lambda x: (x["total_spent"], x["count"], x["last_paid"] or datetime.min),
            reverse=True,
        )

        total_spenders = len(ranked_spenders)
        total_pages = max(1, (total_spenders + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size
        page_items = ranked_spenders[offset:offset + page_size]

        # 4. ดึงข้อมูล User & Subscription สำหรับหน้านี้
        user_rows = []
        for idx, item in enumerate(page_items, start=offset + 1):
            uid = item["user_id"]
            user = await session.get(User, uid, options=[selectinload(User.subscription)])
            full_name = html.escape(user.full_name) if user and user.full_name else f"User {uid}"
            username = user.username if user else None
            
            # สถานะ VIP ปัจจุบัน
            u_sub = user.subscription if user else None
            is_active = bool(u_sub and u_sub.status == SubStatus.ACTIVE.value and u_sub.expires_at and ensure_utc(u_sub.expires_at) > now_utc)
            is_pending = bool(u_sub and u_sub.status == SubStatus.PENDING.value and ((u_sub.pending_days or 0) > 0 or (u_sub.pending_minutes or 0) > 0))

            if is_active:
                rem_str = format_remaining_time(u_sub.expires_at)
                vip_status_str = f"🟢 ACTIVE (เหลือ {rem_str})"
            elif is_pending:
                vip_status_str = f"🟡 PENDING (โควต้าสะสม <b>{u_sub.pending_days} วัน</b>)"
            else:
                vip_status_str = "⚪ EXPIRED (ไม่อยู่ในห้อง)"

            user_rows.append({
                "rank": idx,
                "user_id": uid,
                "full_name": full_name,
                "username": username,
                "total_spent": item["total_spent"],
                "count": item["count"],
                "last_paid": item["last_paid"],
                "last_plan": item["last_plan"],
                "vip_status_str": vip_status_str,
            })

    # --- สร้างเนื้อหาข้อความ HTML ---
    now_thai = format_thai_datetime(now_utc)
    lines = [
        "💎 <b>อันดับยอดชำระเงินสูงสุด (Top Spenders Leaderboard)</b>",
        f"📅 <b>ช่วงเวลา:</b> <b>{period_title}</b>",
        f"🕒 <b>ข้อมูล ณ วันที่:</b> <code>{now_thai} น.</code>",
        f"📄 <b>หน้าที่:</b> <b>{page}/{total_pages}</b> (ทั้งหมด {total_spenders} คน)",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>สถิติภาพรวมรายได้ (Revenue Overview):</b>",
        f"• 👥 ผู้ใช้งานที่มียอดชำระ: <b>{total_spenders} คน</b>",
        f"• 💳 รายการชำระเงินสำเร็จ: <b>{total_transactions} รายการ</b>",
        f"• 💰 ยอดเงินรวมทั้งหมด: <b>{total_revenue:,.2f} บาท</b>",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    if total_spenders == 0:
        lines.append("ℹ️ <i>ไม่พบข้อมูลการชำระเงินในช่วงเวลานี้</i>")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for row in user_rows:
            r = row["rank"]
            medal = medals[r - 1] if r <= 3 else f"<b>#{r}</b>"
            u_header = format_user_title(row["full_name"], row["username"], row["user_id"])
            last_paid_thai = format_thai_datetime(row["last_paid"]) if row["last_paid"] else "-"
            plan_name = get_plan_display_name(row["last_plan"])

            user_block = [
                f"{medal} <b>อันดับ {r}.</b> {u_header}",
                f"   • 💰 <b>ยอดชำระสะสม:</b> <b>{row['total_spent']:,} บาท</b> (ชำระสำเร็จ <b>{row['count']} ครั้ง</b>)",
                f"   • 📦 <b>สถานะ VIP:</b> {row['vip_status_str']}",
                f"   • 📅 <b>ชำระล่าสุด:</b> <code>{last_paid_thai} น.</code> (แพ็กเกจ: <i>{plan_name}</i>)",
                "",
            ]
            lines.extend(user_block)

    # --- สร้างปุ่มคีย์บอร์ด Inline Keyboard ---
    buttons: List[List[InlineKeyboardButton]] = []

    # 1. ปุ่มสำหรับกดเข้าไปดูข้อมูลสมาชิกแต่ละคนในหน้านี้
    for row in user_rows:
        btn_text = f"👤 {row['rank']}. จัดการ {row['full_name']} ({row['total_spent']:,}฿)"
        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"admin:view_user:{row['user_id']}",
            )
        ])

    # 2. แถบเลือกช่วงเวลา (Period Filter Tabs)
    all_tag = "⭐ ตลอดกาล (เลือกอยู่)" if period == "all" else "⭐ ตลอดกาล"
    month_tag = "📅 เดือนนี้ (เลือกอยู่)" if period == "month" else "📅 เดือนนี้"
    year_tag = "📆 ปีนี้ (เลือกอยู่)" if period == "year" else "📆 ปีนี้"

    buttons.append([
        InlineKeyboardButton(text=all_tag, callback_data="admin:top_spenders_page:all:1"),
        InlineKeyboardButton(text=month_tag, callback_data="admin:top_spenders_page:month:1"),
        InlineKeyboardButton(text=year_tag, callback_data="admin:top_spenders_page:year:1"),
    ])

    # 3. ปุ่มเปลี่ยนหน้า (Pagination)
    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ ก่อนหน้า",
                callback_data=f"admin:top_spenders_page:{period}:{page - 1}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"📄 {page}/{total_pages}",
            callback_data="admin:noop",
        )
    )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="ถัดไป ➡️",
                callback_data=f"admin:top_spenders_page:{period}:{page + 1}",
            )
        )

    if total_pages > 1:
        buttons.append(nav_row)

    # 4. ปุ่ม Refresh และปุ่มกลับเมนูหลัก
    buttons.append([
        InlineKeyboardButton(
            text="🔄 รีเฟรช",
            callback_data=f"admin:top_spenders_page:{period}:{page}",
        ),
        InlineKeyboardButton(
            text="🔙 กลับเมนูแอดมิน",
            callback_data="admin_menu:main",
        ),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
