import logging
import html
from datetime import datetime, timezone
from aiogram import Router, Bot, F
from aiogram.types import ChatMemberUpdated, ChatJoinRequest, Message
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus
from bot.services.database import get_session, get_or_create_user
from bot.services.referral import award_referral_bonus
from bot.services.subscription import activate_pending_subscription
from bot.services.channel_service import is_target_channel, is_secondary_channel, get_channel_label, fetch_channel_title

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="channel_events")

import asyncio
from collections import defaultdict
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime, format_remaining_time, ensure_utc

# Dictionary สำหรับ Lock ป้องกัน concurrency (Event เบิ้ลจาก Telegram)
user_locks = defaultdict(asyncio.Lock)

@router.chat_member()
async def handle_channel_member_updated(event: ChatMemberUpdated, bot: Bot):
    """
    ตรวจจับเหตุการณ์ ChatMemberUpdated เมื่อผู้ใช้กดเข้าร่วม Channel
    - ใช้ asyncio.Lock เพื่อป้องกัน Event ส่งเบิ้ลจาก Telegram
    """
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    user = event.new_chat_member.user

    logger.info(
        f"[ChatMemberUpdated] chat_id={event.chat.id}, user_id={user.id} ({user.full_name}), "
        f"old_status={old_status}, new_status={new_status}, via_invite={bool(event.invite_link)}"
    )

    if not is_target_channel(event.chat.id):
        return

    if user.is_bot:
        return

    is_joined = (
        old_status != new_status
        and new_status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        )
    )

    if not is_joined:
        return

    user_id = user.id
    async with user_locks[user_id]:
        await _process_joined_member(event, bot, user, new_status)


@router.chat_join_request()
async def handle_channel_join_request(event: ChatJoinRequest, bot: Bot):
    """จัดการเมื่อผู้ใช้กดขอเข้าร่วม Channel (Join Request)"""
    if not is_target_channel(event.chat.id):
        return

    user = event.from_user
    user_id = user.id
    logger.info(f"[ChatJoinRequest] chat_id={event.chat.id}, user_id={user_id} ({user.full_name})")

    async with get_session() as session:
        sub = await session.get(Subscription, user_id)
        has_claim = sub is not None and sub.status in (SubStatus.PENDING.value, SubStatus.ACTIVE.value)

    if has_claim:
        try:
            await bot.approve_chat_join_request(chat_id=event.chat.id, user_id=user_id)
            logger.info(f"Approved ChatJoinRequest for User {user_id} (status={sub.status})")
            async with user_locks[user_id]:
                await _process_joined_member(event, bot, user, ChatMemberStatus.MEMBER)
        except Exception as e:
            logger.error(f"Failed to approve ChatJoinRequest for User {user_id}: {e}")
    else:
        try:
            await bot.decline_chat_join_request(chat_id=event.chat.id, user_id=user_id)
            logger.warning(f"Declined unauthorized ChatJoinRequest for User {user_id}")
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text="⚠️ <b>ไม่สามารถเข้าร่วม Channel VIP ได้</b>\n\nเนื่องจากคุณยังไม่มีแพ็กเกจสมาชิกที่เปิดใช้งาน กรุณาพิมพ์ /start เพื่อกดทดลองใช้ฟรี หรือสมัครสมาชิก VIP ครับ",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to decline ChatJoinRequest for User {user_id}: {e}")


async def _process_joined_member(event, bot: Bot, user, new_status):
    user_id = user.id
    now = datetime.now(timezone.utc)

    # ทำลายลิงก์เชิญ (Revoke) ทันทีที่ถูกใช้งาน (ป้องกันการนำกลับมาใช้ซ้ำ 100%)
    inv_link = getattr(event, "invite_link", None)
    if inv_link and not getattr(inv_link, "is_primary", False):
        try:
            await bot.revoke_chat_invite_link(
                chat_id=event.chat.id, 
                invite_link=inv_link.invite_link
            )
            logger.info(f"Revoked invite link {inv_link.invite_link} after use by {user_id}")
        except Exception as e:
            logger.warning(f"Could not revoke invite link: {e}")

    sub_to_activate = None
    plan_title = "สมาชิก VIP"
    duration_str = "30 วัน"
    is_stack_extension = False
    new_expires_at = None
    referred_by_to_award = None
    friend_user_snapshot = None

    async with get_session() as session:
        # 1. ตรวจสอบข้อมูล User ในฐานข้อมูล และอัปเดตชื่อล่าสุดจาก Telegram ทันที
        user_stmt = select(User).where(User.telegram_id == user_id)
        user_obj = (await session.execute(user_stmt)).scalar_one_or_none()
        if user_obj:
            if user.full_name and user_obj.full_name != user.full_name:
                user_obj.full_name = user.full_name
            if user.username and user_obj.username != user.username:
                user_obj.username = user.username
            session.add(user_obj)
        else:
            user_obj, _ = await get_or_create_user(
                session=session,
                telegram_id=user_id,
                username=user.username,
                full_name=user.full_name or f"User {user_id}",
            )

        sub = await session.get(Subscription, user_id)
        trial_already_used_before = bool(user_obj.trial_used) if user_obj else True

        # หากผู้ใช้เข้า Channel ใหม่ ให้บันทึกสถานะผู้ใช้ว่าถูกย้ายเข้าห้องใหม่เรียบร้อย
        is_sec = is_secondary_channel(event.chat.id)
        if is_sec and user_obj:
            user_obj.is_moved_to_secondary = True
            user_obj.assigned_channel = "SECONDARY"
            session.add(user_obj)

        if sub and sub.status == SubStatus.PENDING.value and ((sub.pending_days or 0) > 0 or (sub.pending_minutes or 0) > 0):
            # === มีโควต้า pending รอกดเข้าห้อง -> เปิดใช้งานทันที ===
            grant = await activate_pending_subscription(session, user_id=user_id)
            if grant:
                await session.flush()
                sub_to_activate = grant.subscription
                new_expires_at = grant.new_expires_at
                is_stack_extension = grant.is_stack_extension
                plan_title = sub_to_activate.source_label or "สมาชิก VIP"
                if grant.granted_days > 0:
                    duration_str = f"{grant.granted_days} วัน"
                elif grant.granted_minutes >= 60 and grant.granted_minutes % 60 == 0:
                    duration_str = f"{grant.granted_minutes // 60} ชั่วโมง"
                else:
                    duration_str = f"{grant.granted_minutes} นาที"

                if user_obj and user_obj.referred_by_id and not getattr(user_obj, "referral_rewarded", False):
                    referred_by_to_award = user_obj.referred_by_id
                    friend_user_snapshot = user_obj

                logger.info(f"Activated pending subscription for user {user_id} ({plan_title}) in {get_channel_label(event.chat.id)}, expires_at={new_expires_at}")

        elif sub and sub.expires_at and ensure_utc(sub.expires_at) > now:
            # === กรณีไม่มี PENDING แต่มีวันคงเหลืออยู่แล้ว (สมาชิกเดิมหลุดแล้วเข้าใหม่ หรือย้ายเข้าห้องใหม่) ===
            sub.status = SubStatus.ACTIVE.value
            session.add(sub)
            sub_to_activate = sub
            new_expires_at = sub.expires_at
            plan_title = sub.source_label or "สมาชิก VIP"
            duration_str = f"เหลือ {format_remaining_time(sub.expires_at)}"
            logger.info(f"Existing active subscription detected for user {user_id} ({plan_title}) in {get_channel_label(event.chat.id)}")

    # มอบรางวัล Referral Bonus ให้ผู้แนะนำ (ถ้ามี)
    if referred_by_to_award and friend_user_snapshot:
        try:
            await award_referral_bonus(bot=bot, referrer_id=referred_by_to_award, friend_user=friend_user_snapshot)
        except Exception as e:
            logger.error(f"Failed to award referral bonus: {e}", exc_info=True)

    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    start_time_thai = format_thai_datetime(now)
    expires_at_thai = format_thai_datetime(new_expires_at) if new_expires_at else "ไม่ระบุ"
    try:
        channel_label = await fetch_channel_title(bot, event.chat.id)
    except Exception:
        channel_label = get_channel_label(event.chat.id)
    is_sec_join = is_secondary_channel(event.chat.id)

    if sub_to_activate:
        # 1. ส่งข้อความต้อนรับเข้า DM ของผู้ใช้
        try:
            welcome_dm = (
                f"🎉 <b>ยินดีต้อนรับเข้าสู่ {channel_label}!</b>\n\n"
                f"แพ็กเกจ <b>{plan_title}</b> ของคุณเปิดใช้งานเรียบร้อยแล้ว 🚀\n\n"
                f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
                f"⏰ <b>เวลาเริ่มต้น:</b> <code>{start_time_thai} น.</code>\n"
                f"📅 <b>หมดอายุวันที่:</b> <code>{expires_at_thai} น.</code>\n\n"
                f"ขอให้เพลิดเพลินกับเนื้อหาพิเศษของเราครับ!"
            )
            await bot.send_message(
                chat_id=user_id,
                text=welcome_dm,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not send welcome DM to User {user_id}: {e}")

        # 2. ส่งข้อความแจ้งเตือนเข้ากลุ่ม Admin
        if is_sec_join:
            header_title = "🌟 <b>[Target Channel] มีสมาชิกกดเข้าร่วม Channel ใหม่แล้ว!</b>"
            if is_stack_extension:
                header_title = "🌟 <b>[Target Channel] มีสมาชิกกดเข้าร่วม Channel ใหม่ พร้อมต่อเวลาสะสม!</b>"
        else:
            header_title = "🚪 <b>มีสมาชิกกดเข้าร่วม Channel แล้ว!</b>"
            if is_stack_extension:
                header_title = "🚪 <b>มีสมาชิกกดเข้าร่วม Channel พร้อมต่อเวลาสะสม!</b>"

        admin_log_msg = (
            f"{header_title}\n\n"
            f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📢 <b>Channel:</b> <b>{channel_label}</b> (<code>{event.chat.id}</code>)\n"
            f"📦 <b>แผนที่ใช้งาน:</b> <b>{plan_title}</b>\n"
            f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
            f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
            f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>บันทึกประวัติการเข้าใช้งานลงฐานข้อมูลเรียบร้อย</i>"
        )

        try:
            await bot.send_message(
                chat_id=config.ADMIN_GROUP_ID,
                text=admin_log_msg,
                parse_mode="HTML",
            )
            logger.info(f"Sent channel join audit log to Admin Group for User {user_id} in {channel_label}")
        except Exception as e:
            logger.warning(f"Could not send join log to Admin Group: {e}")

    else:
        # === กรณีเป็น Admin/Creator ของ Channel แต่ไม่มี Subscription -> ข้ามการเตะ ===
        if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            logger.info(f"User {user_id} ({user.full_name}) is Administrator/Creator in Channel with no active sub. Skipping soft-kick.")
            return

        # === กรณีไม่มี Subscription ที่ถูกต้อง -> Soft-kick ===
        logger.warning(f"Unauthorized join detected: User {user_id} ({user.full_name}) has no valid subscription in {channel_label}.")
        kicked = False
        try:
            await bot.ban_chat_member(chat_id=event.chat.id, user_id=user_id, revoke_messages=False)
            await bot.unban_chat_member(chat_id=event.chat.id, user_id=user_id, only_if_banned=True)
            kicked = True
            logger.info(f"Successfully soft-kicked unauthorized User ID={user_id} from {channel_label}.")
        except Exception as e:
            logger.error(f"Failed to soft-kick unauthorized User ID={user_id} from {channel_label}: {e}")

        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"⚠️ <b>ไม่สามารถเข้าร่วม {channel_label} ได้</b>\n\nเนื่องจากคุณยังไม่มีแพ็กเกจสมาชิกที่เปิดใช้งาน กรุณาพิมพ์ /start เพื่อกดทดลองใช้ฟรี หรือสมัครสมาชิก VIP ครับ",
                parse_mode="HTML",
            )
        except Exception:
            pass

        action_status = f"เตะออกจาก {channel_label} ทันทีเรียบร้อย ❌" if kicked else "⚠️ บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์ Ban Users ของบอท)"
        admin_alert = (
            f"🚨 <b>[Security Alert] ตรวจพบผู้ใช้เข้า {channel_label} โดยไม่ผ่านระบบ!</b>\n\n"
            f"👤 <b>ผู้ใช้:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📢 <b>Channel:</b> {channel_label} (<code>{event.chat.id}</code>)\n"
            f"⚡ <b>การดำเนินการ:</b> {action_status}\n\n"
            "ℹ️ <i>ระบบป้องกันไม่ให้ผู้ใช้แอบแฝงหรือค้างในห้อง VIP โดยไม่มีแพ็กเกจ</i>"
        )
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_alert, parse_mode="HTML")
        except Exception:
            pass


@router.message(F.left_chat_member)
async def handle_left_chat_member_service_message(message: Message):
    """ลบข้อความระบบของ Telegram อัตโนมัติเมื่อมีคนออกจากกลุ่มหรือถูกบอทเตะ (Clean Service Message: User left/removed)"""
    if message.chat.type in ("group", "supergroup") and message.chat.id != config.ADMIN_GROUP_ID:
        try:
            await message.delete()
            logger.info(f"Deleted left_chat_member service message in group {message.chat.id}")
        except Exception:
            pass


@router.message(F.new_chat_members)
async def handle_new_chat_members_service_message(message: Message):
    """ลบข้อความระบบของ Telegram อัตโนมัติเมื่อมีคนเข้าร่วมกลุ่ม (Clean Service Message: User joined)"""
    if message.chat.type in ("group", "supergroup") and message.chat.id != config.ADMIN_GROUP_ID:
        try:
            await message.delete()
            logger.info(f"Deleted new_chat_members service message in group {message.chat.id}")
        except Exception:
            pass

