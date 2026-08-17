import logging
import html
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType, PLAN_DETAILS, get_dynamic_plan_info
from bot.services.database import get_session, get_or_create_user
from bot.services.chat_logger import log_chat_message
from bot.services.referral import get_referral_link, get_share_url

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="user_menu")

from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

def get_main_menu_keyboard(trial_available: bool = True) -> InlineKeyboardMarkup:
    """สร้างปุ่มเมนูหลักแบบ Interactive Inline Keyboard พร้อม 3 แพ็กเกจ และระบบชวนเพื่อน"""
    trial_button_text = "⏱️ ทดลองใช้ฟรี 15 นาที" if trial_available else "⏱️ ทดลองฟรี (ใช้สิทธิ์แล้ว)"
    
    keyboard = [
        [
            InlineKeyboardButton(
                text=trial_button_text,
                callback_data="menu:trial",
            )
        ],
        [
            InlineKeyboardButton(
                text="🥉 VIP 3 วัน — 300 บาท",
                callback_data="menu:subscribe:VIP_3D",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🥈 VIP 10 วัน — 500 บาท",
                callback_data="menu:subscribe:VIP_10D",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🥇 VIP 30 วัน — 1,000 บาท",
                callback_data="menu:subscribe:VIP_30D",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎁 ชวนเพื่อนรับ VIP ฟรี (+1 วัน/คน)",
                callback_data="menu:referral",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💬 เข้ากลุ่มแชทพูดคุย (ฟรี)",
                url=config.FREE_CHAT_GROUP_URL,
            ),
        ],
        [
            InlineKeyboardButton(
                text="📊 สถานะสมาชิกของฉัน",
                callback_data="menu:my_status",
            ),
            InlineKeyboardButton(
                text="❓ วิธีใช้งาน & ช่วยเหลือ",
                callback_data="menu:help",
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """สร้างปุ่มย้อนกลับไปเมนูหลัก"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 กลับสู่เมนูหลัก", callback_data="menu:main")]
        ]
    )


def format_time_remaining(expires_at: datetime) -> str:
    """แปลงเวลาคงเหลือให้อ่านง่ายเป็นภาษาไทย"""
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    diff = expires_at - now
    if diff.total_seconds() <= 0:
        return "หมดอายุแล้ว"
    
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} วัน")
    if hours > 0:
        parts.append(f"{hours} ชั่วโมง")
    if minutes > 0 or (days == 0 and hours == 0):
        parts.append(f"{minutes} นาที")
    if days == 0 and hours == 0 and minutes < 5:
        parts.append(f"{seconds} วินาที")
    
    return " ".join(parts)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    """จัดการคำสั่ง /start ตรวจสอบผู้ใช้ ล้างสถานะ FSM รองรับ Deep link แนะนำเพื่อน และแสดงเมนูหลัก"""
    try:
        await state.clear()
    except Exception:
        pass

    if not message.from_user:
        return

    # ตรวจสอบ Payload Deep Link เช่น /start ref_123456789
    referrer_id = None
    if message.text:
        parts = message.text.strip().split()
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1].replace("ref_", ""))
            except ValueError:
                referrer_id = None

    telegram_user = message.from_user
    trial_available = True
    try:
        async with get_session() as session:
            user, _ = await get_or_create_user(
                session=session,
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                full_name=telegram_user.full_name or telegram_user.first_name,
                referred_by_id=referrer_id,
            )
            trial_available = not user.trial_used
    except Exception as e:
        logger.error(f"Error checking user in handle_start: {e}", exc_info=True)

    first_name_safe = html.escape(telegram_user.first_name or "")
    welcome_text = (
        f"👋 <b>ยินดีต้อนรับสู่ระบบสมาชิก BareLive, {first_name_safe}!</b>\n\n"
        "เข้าถึงเนื้อหาสุดพิเศษใน Channel VIP ส่วนตัว พร้อมการอัปเดตแบบเรียลไทม์\n\n"
        "🌟 <b>กรุณาเลือกแพ็กเกจที่ต้องการด้านล่าง:</b>\n"
        "• <b>⏱️ ทดลองใช้ฟรี 15 นาที</b>: ทดลองเข้าชม Channel ฟรี 1 ครั้ง\n"
        "• <b>🥉 VIP 3 วัน</b>: ราคา 300 บาท\n"
        "• <b>🥈 VIP 10 วัน</b>: ราคา 500 บาท\n"
        "• <b>🥇 VIP 30 วัน</b>: ราคา 1,000 บาท\n\n"
        "💬 <b>ห้องพูดคุยสาธารณะ (เข้าฟรี):</b>\n"
        "สามารถกดปุ่ม <b>'💬 เข้ากลุ่มแชทพูดคุย (ฟรี)'</b> ด้านล่าง เพื่อเข้าร่วมพูดคุยกับเพื่อนๆ ได้ทันทีครับ!"
    )

    try:
        await message.answer(
            text=welcome_text,
            reply_markup=get_main_menu_keyboard(trial_available=trial_available),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to send start message: {e}", exc_info=True)

    try:
        await log_chat_message(user_id=telegram_user.id, sender_role="USER", message_text="/start")
    except Exception:
        pass


@router.callback_query(F.data == "menu:main")
async def handle_menu_main(callback: CallbackQuery, state: FSMContext):
    """จัดการการกดปุ่มกลับสู่เมนูหลัก"""
    await state.clear()
    if not callback.from_user or not callback.message:
        return

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )
        trial_available = not user.trial_used

    menu_text = (
        f"👋 <b>เมนูหลักระบบสมาชิก BareLive</b>\n\n"
        "กรุณาเลือกรายการที่ต้องการ:"
    )

    try:
        await callback.message.edit_text(
            text=menu_text,
            reply_markup=get_main_menu_keyboard(trial_available=trial_available),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text=menu_text,
            reply_markup=get_main_menu_keyboard(trial_available=trial_available),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "menu:trial")
async def handle_trial_request(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """จัดการคำขอทดลองใช้งานฟรี"""
    await state.clear()
    if not callback.from_user:
        return

    user_id = callback.from_user.id
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=user_id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )

        # ตรวจสอบว่าเคยใช้สิทธิ์ทดลองไปแล้วหรือยัง (เข้าร่วม channel แล้ว)
        if user.trial_used:
            await callback.answer(
                "❌ คุณได้ใช้สิทธิ์ทดลองใช้งานฟรีไปแล้ว!",
                show_alert=True,
            )
            return

        # ตรวจสอบว่ามีแพ็กเกจที่ยังใช้งานอยู่หรือไม่
        active_sub_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubStatus.ACTIVE.value,
            )
            .order_by(Subscription.id.desc())
        )
        active_sub = (await session.execute(active_sub_stmt)).scalar_one_or_none()
        if active_sub:
            await callback.answer(
                "ℹ️ คุณมีแพ็กเกจสมาชิกที่กำลังใช้งานอยู่แล้ว!",
                show_alert=True,
            )
            return

        # ตรวจสอบว่ามีรายการ PENDING trial เดิมอยู่หรือไม่
        pending_sub_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.plan_type == PlanType.TRIAL_15M.value,
                Subscription.status == SubStatus.PENDING.value,
            )
            .order_by(Subscription.id.desc())
        )
        pending_sub = (await session.execute(pending_sub_stmt)).scalar_one_or_none()

        if not pending_sub:
            pending_sub = Subscription(
                user_id=user_id,
                plan_type=PlanType.TRIAL_15M.value,
                status=SubStatus.PENDING.value,
            )
            session.add(pending_sub)
            await session.flush()

    # สร้างลิงก์เชิญแบบใช้งานได้ 1 ครั้งสำหรับ Channel ส่วนตัว (หมดอายุภายใน 48 ชม. หากไม่กดเข้า)
    try:
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=config.CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now(timezone.utc) + timedelta(hours=48),
            name=f"Trial-{user_id}",
        )
        invite_url = invite_link_obj.invite_link
    except Exception as e:
        logger.error(f"Failed to generate trial invite link for user {user_id}: {e}", exc_info=True)
        await callback.answer(
            "⚠️ ไม่สามารถสร้างลิงก์เชิญได้ กรุณาตรวจสอบว่าบอทเป็น Admin ใน Channel",
            show_alert=True,
        )
        return

    trial_min = config.TRIAL_DURATION_MINUTES
    trial_message = (
        f"🎉 <b>ลิงก์ทดลองใช้งานฟรี {trial_min} นาทีของคุณพร้อมแล้ว!</b>\n\n"
        f"🔗 <b>ลิงก์เชิญส่วนตัว (ใช้ได้ครั้งเดียว):</b>\n<code>{invite_url}</code>\n\n"
        "⚠️ <b>ข้อควรทราบสำคัญ:</b>\n"
        "• ลิงก์นี้สามารถใช้งานได้เพียง 1 ครั้งเท่านั้น\n"
        f"• <b>ระบบจะเริ่มนับถอยหลัง {trial_min} นาทีทันทีที่คุณกดเข้าร่วม Channel</b>\n"
        f"• เมื่อครบกำหนด {trial_min} นาที ระบบจะนำคุณออกจาก Channel อัตโนมัติ\n\n"
        "กดปุ่มด้านล่างเพื่อเข้าร่วมได้เลยครับ!"
    )

    join_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 เข้าร่วม Channel ทันที", url=invite_url)],
            [InlineKeyboardButton(text="🔙 กลับสู่เมนูหลัก", callback_data="menu:main")],
        ]
    )

    if callback.message:
        await callback.message.edit_text(
            text=trial_message,
            reply_markup=join_keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "menu:referral")
async def handle_referral_menu(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """แสดงหน้าต่างระบบชวนเพื่อน (Referral System) พร้อมลิงก์เฉพาะตัวและสถิติ"""
    await state.clear()
    if not callback.from_user or not callback.message:
        return

    user_id = callback.from_user.id
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username or "barelive_sub_bot"
    except Exception:
        bot_username = "barelive_sub_bot"

    ref_link = get_referral_link(bot_username, user_id)
    share_url = get_share_url(bot_username, user_id)

    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=user_id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )
        ref_count = user.referral_count or 0
        bonus_days = user.referral_bonus_days or 0

    ref_text = (
        "🎁 <b>ระบบแนะนำเพื่อน — รับสิทธิ์ VIP ฟรี!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "เพียงแชร์ลิงก์ให้เพื่อนเข้ามาทดลองใช้งานฟรีใน Channel "
        "คุณจะได้รับ <b>VIP ฟรีทันที 1 วัน (+24 ชม.) ต่อเพื่อน 1 คน</b> "
        "(สะสมวันได้เรื่อยๆ ไม่จำกัดจำนวนคน!)\n\n"
        "📊 <b>สถิติของคุณในปัจจุบัน:</b>\n"
        f"• 👥 เพื่อนที่ชวนสำเร็จแล้ว: <b>{ref_count} คน</b>\n"
        f"• 🏆 โบนัส VIP ที่ได้รับสะสม: <b>{bonus_days} วัน</b>\n\n"
        "🔗 <b>ลิงก์แนะนำเฉพาะตัวของคุณ:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "💡 <i>แตะที่ข้อความลิงก์ด้านบนเพื่อคัดลอก หรือกดปุ่ม 'แชร์ให้เพื่อน' ด้านล่างได้ทันที</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 กดแชร์ให้เพื่อนใน Telegram",
                    url=share_url,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 กลับสู่เมนูหลัก",
                    callback_data="menu:main",
                ),
            ],
        ]
    )

    try:
        await callback.message.edit_text(
            text=ref_text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        await callback.message.answer(
            text=ref_text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "menu:my_status")
async def handle_my_status(callback: CallbackQuery, state: FSMContext):
    """แสดงสถานะแพ็กเกจสมาชิกของผู้ใช้งาน (เวลาไทย)"""
    await state.clear()
    if not callback.from_user or not callback.message:
        return

    user_id = callback.from_user.id
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=user_id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name or callback.from_user.first_name,
        )

        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id.desc())
        )
        sub = (await session.execute(stmt)).scalars().first()

    status_text = f"📊 <b>สถานะการเป็นสมาชิกของคุณ</b>\n\n"
    status_text += f"👤 <b>Telegram ID:</b> <code>{user_id}</code>\n"
    status_text += f"⏱️ <b>สิทธิ์ทดลองฟรี:</b> {'ใช้สิทธิ์แล้ว' if user.trial_used else 'ยังไม่เคยใช้ (พร้อมใช้งาน)'}\n"
    status_text += f"🎁 <b>โบนัสชวนเพื่อน:</b> ชวนสำเร็จ {user.referral_count or 0} คน (รับโบนัสสะสม {user.referral_bonus_days or 0} วัน)\n\n"

    if not sub:
        status_text += "🔴 <b>สถานะ:</b> ไม่มีแพ็กเกจที่ใช้งานอยู่\n"
        status_text += "คุณสามารถเริ่มทดลองใช้ฟรี หรือสมัครสมาชิกได้จากเมนูด้านล่างครับ"
    elif sub.status == SubStatus.ACTIVE.value:
        status_text += "🟢 <b>สถานะ:</b> กำลังใช้งาน (ACTIVE)\n"
        plan_label = f"ทดลองใช้ฟรี {config.TRIAL_DURATION_MINUTES} นาที" if sub.plan_type == PlanType.TRIAL_15M.value else "สมาชิก VIP"
        status_text += f"📦 <b>แพ็กเกจ:</b> {plan_label}\n"
        if sub.joined_at:
            status_text += f"📅 <b>เริ่มเข้าใช้งาน:</b> <code>{format_thai_datetime(sub.joined_at)} น.</code>\n"
        if sub.expires_at:
            status_text += f"⏳ <b>หมดอายุวันที่:</b> <code>{format_thai_datetime(sub.expires_at)} น.</code>\n"
            status_text += f"⏰ <b>เวลาคงเหลือ:</b> {format_time_remaining(sub.expires_at)}\n"
    elif sub.status == SubStatus.PENDING.value:
        status_text += "🟡 <b>สถานะ:</b> รอกดเข้าร่วม Channel\n"
        plan_label = "ทดลองใช้ฟรี" if sub.plan_type == PlanType.TRIAL_15M.value else "สมาชิก VIP"
        status_text += f"📦 <b>แพ็กเกจ:</b> {plan_label}\n"
        status_text += "ระบบได้สร้างลิงก์เชิญให้คุณแล้ว เวลาจะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel ครับ"
    elif sub.status in (SubStatus.EXPIRED.value, SubStatus.KICKED.value, SubStatus.KICK_FAILED.value):
        status_text += "🔴 <b>สถานะ:</b> หมดอายุแล้ว (EXPIRED)\n"
        plan_label = "ทดลองใช้ฟรี" if sub.plan_type == PlanType.TRIAL_15M.value else "สมาชิก VIP"
        status_text += f"📦 <b>แพ็กเกจล่าสุด:</b> {plan_label}\n"
        if sub.expires_at:
            status_text += f"📅 <b>หมดอายุเมื่อ:</b> <code>{format_thai_datetime(sub.expires_at)} น.</code>\n"
        status_text += "\nต้องการเข้าใช้งานต่อ สามารถพิมพ์ /start และกดสมัครแพ็กเกจ 30 วันได้ทันทีครับ"
    else:
        status_text += f"⚪ <b>สถานะ:</b> {sub.status}\n"

    await callback.message.edit_text(
        text=status_text,
        reply_markup=get_back_to_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def handle_help(callback: CallbackQuery, state: FSMContext):
    """แสดงวิธีใช้งานและคำถามที่พบบ่อย (FAQ)"""
    await state.clear()
    if not callback.message:
        return

    help_text = (
        "❓ <b>วิธีใช้งาน & คำถามที่พบบ่อย (FAQ)</b>\n\n"
        "• <b>การทดลองใช้ฟรีทำงานอย่างไร?</b>\n"
        "กดปุ่ม 'ทดลองใช้ฟรี' บอทจะส่งลิงก์เชิญแบบ 1 ครั้งให้คุณ "
        "โดยเวลานับถอยหลังจะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel\n\n"
        "• <b>ระบบแนะนำเพื่อน (รับ VIP ฟรี 1 วัน):</b>\n"
        "กดปุ่ม '🎁 ชวนเพื่อนรับ VIP ฟรี' คัดลอกลิงก์ส่งให้เพื่อน "
        "เมื่อเพื่อนกดเข้าทดลองใช้งาน Channel คุณจะได้รับ VIP ฟรี +1 วันทันที (สะสมวันได้เรื่อยๆ!)\n\n"
        "• <b>ขั้นตอนการสมัครสมาชิก VIP:</b>\n"
        "1. เลือกแพ็กเกจที่ต้องการ (3 วัน 300฿, 10 วัน 500฿ หรือ 30 วัน 1,000฿)\n"
        "2. เลือกช่องทางชำระเงิน:\n"
        "   • <b>📲 สแกน QR Code:</b> สแกนจ่ายและส่งรูปสลิปเข้ามาในแชท\n"
        "   • <b>🧧 ซองของขวัญ TrueMoney:</b> สร้างซองแดงและส่งลิงก์ซองเข้ามาในแชท\n\n"
        "• <b>จะได้รับลิงก์เข้า Channel เมื่อใด?</b>\n"
        "เมื่อแอดมินตรวจสอบความถูกต้องและกดอนุมัติ "
        "บอทจะส่งลิงก์เชิญส่วนตัวให้คุณในแชทนี้โดยอัตโนมัติทันทีครับ\n\n"
        "• <b>คำสั่งที่มีประโยชน์:</b>\n"
        "/start - เปิดเมนูหลัก\n"
        "/status - ตรวจสอบสถานะและเวลาสมาชิกคงเหลือ\n"
        "/cancel - ยกเลิกการทำรายการหรือการทำงานปัจจุบัน"
    )

    await callback.message.edit_text(
        text=help_text,
        reply_markup=get_back_to_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("status"))
async def handle_status_command(message: Message, state: FSMContext):
    """จัดการคำสั่ง /status โดยตรง (เวลาไทย)"""
    await state.clear()
    if not message.from_user:
        return

    user_id = message.from_user.id
    async with get_session() as session:
        user, _ = await get_or_create_user(
            session=session,
            telegram_id=user_id,
            username=message.from_user.username,
            full_name=message.from_user.full_name or message.from_user.first_name,
        )
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id.desc())
        )
        sub = (await session.execute(stmt)).scalars().first()

    status_text = f"📊 <b>สถานะการเป็นสมาชิกของคุณ</b>\n\n"
    status_text += f"👤 <b>Telegram ID:</b> <code>{user_id}</code>\n"
    status_text += f"⏱️ <b>สิทธิ์ทดลองฟรี:</b> {'ใช้สิทธิ์แล้ว' if user.trial_used else 'ยังไม่เคยใช้ (พร้อมใช้งาน)'}\n"
    status_text += f"🎁 <b>โบนัสชวนเพื่อน:</b> ชวนสำเร็จ {user.referral_count or 0} คน (รับโบนัสสะสม {user.referral_bonus_days or 0} วัน)\n\n"

    if not sub or sub.status in (SubStatus.EXPIRED.value, SubStatus.KICKED.value, SubStatus.KICK_FAILED.value):
        status_text += "🔴 <b>สถานะ:</b> ไม่มีแพ็กเกจที่ใช้งานอยู่\n"
        status_text += "พิมพ์ /start เพื่อทดลองใช้ฟรี หรือสมัครสมาชิก VIP"
    elif sub.status == SubStatus.ACTIVE.value:
        status_text += "🟢 <b>สถานะ:</b> กำลังใช้งาน (ACTIVE)\n"
        if sub.plan_type == PlanType.TRIAL_15M.value:
            plan_label = f"ทดลองใช้ฟรี {config.TRIAL_DURATION_MINUTES} นาที"
        elif sub.plan_type in PLAN_DETAILS:
            plan_label = get_dynamic_plan_info(sub.plan_type)["badge"]
        elif sub.plan_type.startswith("MANUAL_VIP_"):
            plan_label = sub.plan_type.replace("MANUAL_VIP_", "VIP ").replace("D", " วัน")
        else:
            plan_label = sub.plan_type

        status_text += f"📦 <b>แพ็กเกจ:</b> {plan_label}\n"
        if sub.expires_at:
            status_text += f"⏳ <b>หมดอายุวันที่:</b> <code>{format_thai_datetime(sub.expires_at)} น.</code>\n"
            status_text += f"⏰ <b>เวลาคงเหลือ:</b> {format_time_remaining(sub.expires_at)}\n"
    elif sub.status == SubStatus.PENDING.value:
        status_text += "🟡 <b>สถานะ:</b> รอกดเข้าร่วม Channel (รอคุณใช้ลิงก์เชิญ)\n"

    await message.answer(
        text=status_text,
        reply_markup=get_main_menu_keyboard(trial_available=not user.trial_used),
        parse_mode="HTML",
    )
    await log_chat_message(user_id=user_id, sender_role="USER", message_text="/status")


@router.message(F.chat.type == "private", StateFilter(default_state), F.text, ~F.text.startswith("/"))
async def handle_user_dm_message(message: Message, state: FSMContext, bot: Bot):
    """บันทึกข้อความที่ผู้ใช้พิมพ์คุยกับบอทในแชทส่วนตัว (DM) และส่งต่อเข้า Admin Group แบบ Real-time"""
    if not message.from_user:
        return

    # 0. ตรวจสอบว่าผู้ใช้ส่งลิงก์ซองของขวัญ TrueMoney เข้ามาหรือไม่
    from bot.handlers.payment import extract_truemoney_url, process_truemoney_submission
    angpao_url = extract_truemoney_url(message.text or "")
    if angpao_url:
        await process_truemoney_submission(message=message, state=state, bot=bot, angpao_url=angpao_url)
        return

    telegram_user = message.from_user
    user_id = telegram_user.id
    msg_text = message.text or (f"[{message.content_type}]" if message.content_type else "[ข้อความ/ไฟล์]")
    
    # 1. บันทึกข้อความของผู้ใช้ลงในฐานข้อมูล
    await log_chat_message(user_id=user_id, sender_role="USER", message_text=msg_text)

    # 2. ส่งต่อข้อความไปยังกลุ่ม Admin Group แบบ Real-time
    user_name = html.escape(telegram_user.full_name or telegram_user.first_name)
    user_handle = f"@{telegram_user.username}" if telegram_user.username else "ไม่มี Username"
    time_now = format_thai_datetime(datetime.now(timezone.utc))

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 ดูประวัติการคุย", callback_data=f"admin:view_chat:{user_id}"),
                InlineKeyboardButton(text="👤 ดูข้อมูลสมาชิก", callback_data=f"admin:view_user:{user_id}"),
            ],
        ]
    )

    admin_alert = (
        "💬 <b>มีข้อความใหม่จากผู้ใช้ (Direct Message)!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ผู้ใช้:</b> {user_name} ({user_handle})\n"
        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📝 <b>ข้อความ:</b>\n<i>{html.escape(msg_text)}</i>\n"
        f"📅 <b>เวลา:</b> <code>{time_now} น.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>แตะข้อความด้านล่างเพื่อคัดลอกคำสั่งตอบกลับ:</b>\n"
        f"<code>/reply {user_id} </code>"
    )

    try:
        await bot.send_message(
            chat_id=config.ADMIN_GROUP_ID,
            text=admin_alert,
            reply_markup=admin_keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to forward user DM to Admin Group {config.ADMIN_GROUP_ID}: {e}")


@router.message(F.chat.type == "private", F.text.startswith("/"))
async def handle_unknown_command(message: Message):
    """Fallback สำหรับคำสั่งที่ไม่มีในระบบ (เพื่อไม่ให้บอทเมินเงียบๆ)"""
    await message.answer(
        "❌ <b>ไม่รู้จักคำสั่งนี้ครับ</b>\n"
        "กรุณาตรวจสอบความถูกต้อง หรือพิมพ์ /start เพื่อกลับสู่เมนูหลักครับ",
        parse_mode="HTML"
    )
