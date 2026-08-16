import logging
import html
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional
from aiogram import Bot
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType
from bot.services.database import get_session
from bot.services.chat_logger import log_chat_message

logger = logging.getLogger(__name__)
config = get_settings()
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime


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
    """
    if not referrer_id or referrer_id == friend_user.telegram_id:
        return False

    now = datetime.now(timezone.utc)
    friend_name = html.escape(friend_user.full_name or f"User {friend_user.telegram_id}")
    friend_handle = f"@{friend_user.username}" if friend_user.username else f"ID: {friend_user.telegram_id}"

    sub_extended = False
    new_sub_created = False
    invite_url = None
    new_expires_at = None

    async with get_session() as session:
        # 1. ค้นหาผู้แนะนำ
        referrer = (await session.execute(
            select(User).where(User.telegram_id == referrer_id)
        )).scalar_one_or_none()

        if not referrer:
            logger.warning(f"Referrer ID {referrer_id} not found in database for friend {friend_user.telegram_id}")
            return False

        # 2. เพิ่มสถิติ Referral
        referrer.referral_count += 1
        referrer.referral_bonus_days += 1
        session.add(referrer)

        # 3. ตรวจสอบ Subscription ปัจจุบันของผู้แนะนำ (เช็ค Active ก่อน)
        active_sub_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == referrer_id,
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.id.desc())
        )
        active_sub = (await session.execute(active_sub_stmt)).scalar_one_or_none()

        if active_sub:
            # ซ้อนทับและขยายเวลาเพิ่มอีก 1 วัน (24 ชั่วโมง) จากเวลาหมดอายุเดิม!
            active_sub.expires_at = active_sub.expires_at + timedelta(days=1)
            session.add(active_sub)
            sub_extended = True
            new_expires_at = active_sub.expires_at
            logger.info(
                f"Referral bonus: Extended active sub #{active_sub.id} for User {referrer_id} by +1 day. "
                f"New expires_at: {new_expires_at}"
            )
        else:
            # เช็คว่ามี Pending Referral Sub หรือไม่
            pending_sub_stmt = (
                select(Subscription)
                .where(
                    Subscription.user_id == referrer_id,
                    Subscription.status == SubStatus.PENDING.value,
                )
                .order_by(Subscription.id.desc())
            )
            pending_sub = (await session.execute(pending_sub_stmt)).scalar_one_or_none()

            if not pending_sub:
                # สร้าง Subscription ใหม่เป็น VIP โบนัสชวนเพื่อน
                new_sub = Subscription(
                    user_id=referrer_id,
                    plan_type=PlanType.REFERRAL_VIP.value,
                    status=SubStatus.PENDING.value,
                )
                session.add(new_sub)
                new_sub_created = True

    # 4. หากไม่มี Active Sub -> สร้าง Invite Link ส่งให้ผู้แนะนำ
    if not sub_extended:
        try:
            invite_obj = await bot.create_chat_invite_link(
                chat_id=config.CHANNEL_ID,
                member_limit=1,
                expire_date=now + timedelta(days=7),
                name=f"RefBonus-{referrer_id}",
            )
            invite_url = invite_obj.invite_link
        except Exception as e:
            logger.error(f"Failed to generate referral bonus invite link for User {referrer_id}: {e}")

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
                "💡 <i>ระบบสะสมและขยายเวลาให้โดยอัตโนมัติ ชวนเพื่อนเพิ่มเพื่อรับวันใช้งานฟรีต่อเนื่องได้เลยครับ!</i>"
            )
        else:
            bonus_total_days = referrer.referral_bonus_days or 1
            link_info = f"\n🔗 <b>ลิงก์เข้า Channel VIP ของคุณ:</b>\n{invite_url}\n\n⏱️ <i>เวลานับถอยหลัง {bonus_total_days} วัน จะเริ่มนับทันทีที่คุณกดเข้าร่วม Channel</i>" if invite_url else ""
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
