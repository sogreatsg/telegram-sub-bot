import logging
import html
import json
import os
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from aiogram import Bot
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import GrantType, User
from bot.services.database import get_session
from bot.services.chat_logger import log_chat_message
from bot.services.subscription import grant_subscription

from bot.services.channel_service import get_user_target_channel_id, get_channel_label, unban_user_in_channel

logger = logging.getLogger(__name__)
config = get_settings()
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

# จัดเก็บการตั้งค่า Referral ใน data/referral_settings.json
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REFERRAL_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "referral_settings.json")


def get_referral_settings() -> Dict[str, Any]:
    """ดึงการตั้งค่าระบบแนะนำเพื่อนจากไฟล์ JSON"""
    if not os.path.exists(REFERRAL_SETTINGS_FILE):
        return {"is_active": False}
    try:
        with open(REFERRAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"is_active": False}


def save_referral_settings(settings: Dict[str, Any]) -> None:
    """บันทึกการตั้งค่าระบบแนะนำเพื่อนลงไฟล์ JSON"""
    os.makedirs(os.path.dirname(REFERRAL_SETTINGS_FILE), exist_ok=True)
    with open(REFERRAL_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


def update_referral_settings(is_active: bool = None) -> None:
    """อัปเดตสถานะเปิด/ปิด ระบบแนะนำเพื่อน"""
    settings = get_referral_settings()
    if is_active is not None:
        settings["is_active"] = is_active
    save_referral_settings(settings)


def is_referral_active() -> bool:
    """ตรวจสอบว่าระบบแนะนำเพื่อนเปิดใช้งานอยู่หรือไม่"""
    return bool(get_referral_settings().get("is_active", False))


def get_referral_link(bot_username: str, user_id: int) -> str:
    """สร้าง Deep link แนะนำเพื่อน: https://t.me/<bot_username>?start=ref_<user_id>"""
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def get_share_url(bot_username: str, user_id: int) -> str:
    """สร้าง URL สำหรับแชร์ต่อให้เพื่อนใน Telegram"""
    ref_link = get_referral_link(bot_username, user_id)
    share_text = (
        "กลุ่มใหม่ ดูห้องล้อคแบไลฟ์ ไม่ต้องเสียคอยเยอะ ดูห้องล้อคจุกๆๆ เข้าเลย กดลิ้งนี้\n"
        f"{ref_link}"
    )
    encoded_text = urllib.parse.quote(share_text)
    return f"https://t.me/share/url?url={urllib.parse.quote(ref_link)}&text={encoded_text}"


async def award_referral_bonus(bot: Bot, referrer_id: int, friend_user: User) -> bool:
    """
    มอบรางวัล VIP ฟรี 1 วัน (+24 ชม.) ให้แก่ผู้แนะนำเมื่อเพื่อนเข้า Channel สำเร็จครั้งแรก
    รองรับการสะสมวัน (Day Stacking) ต่อเนื่องตามจำนวนเพื่อนที่ชวนได้
    ป้องกันการให้ซ้ำซ้อน (Idempotent): เพื่อน 1 คนให้โบนัสผู้แนะนำได้เพียง 1 ครั้งเท่านั้น
    หากระบบ Referral ปิดอยู่ (is_referral_active() is False) จะไม่แจกโบนัส และตัดสิทธิ์เพื่อนคนนี้ไม่ให้ได้โบนัสย้อนหลัง
    """
    if not referrer_id or referrer_id == friend_user.telegram_id:
        return False

    friend_id = friend_user.telegram_id
    now = datetime.now(timezone.utc)
    friend_name = html.escape(friend_user.full_name or f"User {friend_id}")
    friend_handle = f"@{friend_user.username}" if friend_user.username else f"ID: {friend_id}"

    # ตรวจสอบสถานะเปิด/ปิด ระบบ Referral (ถ้าปิดอยู่ จะไม่แจกวันโบนัส และตัดสิทธิ์เพื่อนคนนี้ทันที)
    if not is_referral_active():
        logger.info(
            f"Referral system is currently disabled. Skipping reward for friend User {friend_id} -> Referrer {referrer_id}. "
            "Invalidating referral attribution."
        )
        async with get_session() as session:
            friend_db = await session.get(User, friend_id)
            if friend_db:
                friend_db.referred_by_id = None
                friend_db.referral_rewarded = True
                session.add(friend_db)
        return False

    invite_url = None

    async with get_session() as session:
        # 1. ตรวจสอบว่าเพื่อนคนนี้เคยให้รางวัลไปแล้วหรือไม่
        friend_db = await session.get(User, friend_id)
        if friend_db and getattr(friend_db, "referral_rewarded", False):
            logger.warning(f"Referral reward already awarded for friend User {friend_id} -> Referrer {referrer_id}. Skipping.")
            return False

        # 2. ค้นหาผู้แนะนำ
        referrer = (await session.execute(
            select(User).where(User.telegram_id == referrer_id)
        )).scalar_one_or_none()

        if not referrer:
            logger.warning(f"Referrer ID {referrer_id} not found in database for friend {friend_id}")
            return False

        # 3. มาร์กว่าเพื่อนคนนี้ให้รางวัลเรียบร้อยแล้ว
        if friend_db:
            friend_db.referral_rewarded = True
            session.add(friend_db)

        # 4. เพิ่มสถิติ Referral ให้ผู้แนะนำ
        referrer.referral_count = (referrer.referral_count or 0) + 1
        referrer.referral_bonus_days = (referrer.referral_bonus_days or 0) + 1
        session.add(referrer)

        # 5. เติมวัน +1 วัน (24 ชม.) ให้ผู้แนะนำ -- บวกทันทีถ้ามี ACTIVE อยู่แล้ว มิเช่นนั้นสะสมเข้า pending
        grant = await grant_subscription(
            session,
            user_id=referrer_id,
            days=1,
            source_label=f"🎁 VIP โบนัสชวนเพื่อน (จาก User {friend_id})",
            grant_type=GrantType.REFERRAL_BONUS.value,
            referred_friend_id=friend_id,
            has_value=False,
            is_in_channel=False,
        )

    sub_extended = grant.is_stack_extension
    new_expires_at = grant.new_expires_at
    if sub_extended:
        logger.info(f"Referral bonus: Extended active sub for User {referrer_id} by +1 day. New expires_at: {new_expires_at}")

    target_channel_id = get_user_target_channel_id(referrer)
    target_channel_label = get_channel_label(target_channel_id)

    # 4. หากไม่มี Active Sub -> สร้าง Invite Link ส่งให้ผู้แนะนำ
    if not sub_extended:
        try:
            await unban_user_in_channel(bot, target_channel_id, referrer_id)
            invite_obj = await bot.create_chat_invite_link(
                chat_id=target_channel_id,
                member_limit=1,
                expire_date=now + timedelta(days=7),
                name=f"RefBonus-{referrer_id}",
            )
            invite_url = invite_obj.invite_link
        except Exception as e:
            logger.error(f"Failed to generate referral bonus invite link for User {referrer_id} in {target_channel_id}: {e}")

    # 5. ส่งข้อความแจ้งเตือนทาง DM ให้ผู้แนะนำ
    try:
        if sub_extended and new_expires_at:
            exp_thai = format_thai_datetime(new_expires_at)
            dm_text = (
                "🎉 <b>ยินดีด้วย! เพื่อนที่คุณแนะนำได้เข้าร่วมทดลองใช้งานแล้ว</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>เพื่อน:</b> {friend_name} ({friend_handle})\n"
                "🎁 คุณได้รับโบนัส <b>+1 วัน VIP (24 ชม.)</b> เพิ่มทันที!\n"
                f"⏳ <b>วันหมดอายุใหม่ของคุณ:</b> <code>{exp_thai} น.</code>\n\n"
                f"👥 <b>ชวนเพื่อนสำเร็จสะสม:</b> <b>{referrer.referral_count} คน</b>\n"
                f"🏆 <b>โบนัสสะสมทั้งหมด:</b> <b>{referrer.referral_bonus_days} วัน</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>ระบบสะสมและขยายเวลาให้โดยอัตโนมัติใน {target_channel_label} ชวนเพื่อนเพิ่มเพื่อรับวันใช้งานฟรีต่อเนื่องได้เลยครับ!</i>"
            )
        else:
            bonus_total_days = grant.subscription.pending_days or referrer.referral_bonus_days or 1
            link_info = f"\n🔗 <b>ลิงก์เข้า {target_channel_label} ของคุณ:</b>\n{invite_url}\n\n⏱️ <i>เวลานับถอยหลัง {bonus_total_days} วัน จะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel</i>" if invite_url else ""
            dm_text = (
                "🎉 <b>ยินดีด้วย! เพื่อนที่คุณแนะนำได้เข้าร่วมทดลองใช้งานแล้ว</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>เพื่อน:</b> {friend_name} ({friend_handle})\n"
                f"🎁 คุณได้รับสิทธิ์ <b>VIP โบนัสสะสมรวม {bonus_total_days} วัน</b> เรียบร้อยแล้ว!\n"
                f"{link_info}\n"
                f"👥 <b>ชวนเพื่อนสำเร็จสะสม:</b> <b>{referrer.referral_count} คน</b>\n"
                f"🏆 <b>โบนัสสะสมทั้งหมด:</b> <b>{bonus_total_days} วัน</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>ชวนเพื่อนเพิ่มเพื่อสะสมวันใช้งานฟรีได้เรื่อยๆ ครับ!</i>"
            )

        await bot.send_message(chat_id=referrer_id, text=dm_text, parse_mode="HTML")
        await log_chat_message(user_id=referrer_id, sender_role="BOT", message_text=f"[ระบบมอบโบนัสชวนเพื่อน +1 วัน จาก {friend_name}]")
    except Exception as e:
        logger.warning(f"Could not send referral bonus DM to referrer {referrer_id}: {e}")

    # 6. แจ้งเตือนเข้า Admin Group
    try:
        ref_user_name = html.escape(referrer.full_name or f"User {referrer_id}")
        ref_user_handle = f"@{referrer.username}" if referrer.username else f"ID: {referrer_id}"
        admin_alert = (
            "🎁 <b>มีผู้ใช้ได้รับโบนัสแนะนำเพื่อน (Referral Reward)!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>ผู้แนะนำ:</b> {ref_user_name} ({ref_user_handle})\n"
            f"🔢 <b>Referrer ID:</b> <code>{referrer_id}</code>\n"
            f"👤 <b>เพื่อนใหม่ที่เข้า:</b> {friend_name} ({friend_handle})\n"
            f"🔢 <b>Friend ID:</b> <code>{friend_user.telegram_id}</code>\n"
            "➕ <b>โบนัสที่ได้รับ:</b> +1 วัน VIP (24 ชม.)\n"
            f"📊 <b>สถิติผู้แนะนำ:</b> ชวนสำเร็จสะสม {referrer.referral_count} คน (โบนัสรวม {referrer.referral_bonus_days} วัน)\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_alert, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send referral alert to Admin Group: {e}")

    return True
