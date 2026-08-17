import logging
import html
from datetime import datetime, timedelta, timezone
from aiogram import Router, Bot
from aiogram.types import ChatMemberUpdated, ChatJoinRequest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus, PlanType, PLAN_DETAILS
from bot.services.database import get_session
from bot.services.referral import award_referral_bonus

logger = logging.getLogger(__name__)
config = get_settings()
router = Router(name="channel_events")

import asyncio
from collections import defaultdict
from bot.utils.time_utils import BANGKOK_TZ, format_thai_datetime

# Dictionary สำหรับ Lock ป้องกัน concurrency (Event เบิ้ลจาก Telegram)
user_locks = defaultdict(asyncio.Lock)

def ensure_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def is_target_channel(chat_id: int) -> bool:
    """ตรวจสอบว่าเป็น Channel VIP เป้าหมายหรือไม่ (รองรับทั้งรูปแบบมีและไม่มี -100)"""
    target = config.CHANNEL_ID
    if chat_id == target:
        return True
    str_chat = str(chat_id).replace("-100", "").replace("-", "")
    str_target = str(target).replace("-100", "").replace("-", "")
    return str_chat == str_target

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
    now = datetime.now(timezone.utc)
    logger.info(f"[ChatJoinRequest] chat_id={event.chat.id}, user_id={user_id} ({user.full_name})")

    async with get_session() as session:
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_([SubStatus.PENDING.value, SubStatus.ACTIVE.value]),
            )
            .order_by(Subscription.id.desc())
        )
        sub = (await session.execute(stmt)).scalars().first()

    if sub:
        try:
            await bot.approve_chat_join_request(chat_id=event.chat.id, user_id=user_id)
            logger.info(f"Approved ChatJoinRequest for User {user_id} (Sub #{sub.id})")
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
    sub_id = None

    async with get_session() as session:
        # 1. ตรวจสอบข้อมูล User ในฐานข้อมูล
        user_stmt = select(User).where(User.telegram_id == user_id)
        user_obj = (await session.execute(user_stmt)).scalar_one_or_none()

        # 2. ค้นหา Subscription ที่มีสถานะ PENDING ล่าสุด (ถ้ามี)
        pending_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubStatus.PENDING.value,
            )
            .order_by(Subscription.id.desc())
        )
        pending_sub = (await session.execute(pending_stmt)).scalars().first()

        # 3. ตรวจสอบ ACTIVE subscription ที่ยังไม่หมดอายุ (ถ้ามี)
        active_stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.id.desc())
        )
        existing_active = (await session.execute(active_stmt)).scalar_one_or_none()

        if pending_sub:
            # === กรณีมี PENDING subscription (เช่น เพิ่งกด Trial หรือ แอดมินเพิ่งอนุมัติแพ็กเกจ) ===
            sub_created = pending_sub.created_at if pending_sub.created_at else now
            if sub_created.tzinfo is None:
                sub_created = sub_created.replace(tzinfo=timezone.utc)
            is_stale = (now - sub_created) > timedelta(hours=48)
            is_trial_abuse = (pending_sub.plan_type == PlanType.TRIAL_15M.value and user_obj and user_obj.trial_used)

            if is_stale or is_trial_abuse:
                logger.warning(f"Invalid pending sub #{pending_sub.id} for User {user_id}: is_stale={is_stale}, is_trial_abuse={is_trial_abuse}")
                pending_sub.status = SubStatus.EXPIRED.value
                session.add(pending_sub)
                pending_sub = None

        if pending_sub:
            # คำนวณระยะเวลาของแพ็กเกจ
            p_type = pending_sub.plan_type
            if p_type == PlanType.TRIAL_15M.value:
                trial_minutes = config.TRIAL_DURATION_MINUTES
                pending_sub.expires_at = now + timedelta(minutes=trial_minutes)
                plan_title = f"ทดลองใช้งานฟรี {trial_minutes} นาที"
                duration_str = f"{trial_minutes} นาที"
                if user_obj:
                    if not user_obj.trial_used and user_obj.referred_by_id:
                        referred_by_to_award = user_obj.referred_by_id
                        friend_user_snapshot = user_obj
                    user_obj.trial_used = True
                    session.add(user_obj)
            elif p_type == PlanType.REFERRAL_VIP.value:
                bonus_days = user_obj.referral_bonus_days if (user_obj and user_obj.referral_bonus_days > 0) else 1
                pending_sub.expires_at = now + timedelta(days=bonus_days)
                plan_title = f"สมาชิก 🎁 VIP โบนัสชวนเพื่อน ({bonus_days} วัน)"
                duration_str = f"{bonus_days} วัน"
            elif p_type in PLAN_DETAILS:
                p_info = get_dynamic_plan_info(p_type)
                add_days = p_info["days"]
                plan_title = f"สมาชิก {p_info['badge']}"
                duration_str = f"{add_days} วัน"
                if existing_active and existing_active.plan_type != PlanType.TRIAL_15M.value:
                    # ถ้ามี VIP เดิมอยู่แล้วและไม่ใช่ Trial -> ต่อเวลาสะสม
                    base_time = max(ensure_utc(existing_active.expires_at), now)
                    pending_sub.expires_at = base_time + timedelta(days=add_days)
                    existing_active.status = SubStatus.EXPIRED.value
                    session.add(existing_active)
                    is_stack_extension = True
                else:
                    # สมัครใหม่ หรือ Upgrade จาก Trial
                    if existing_active:
                        existing_active.status = SubStatus.EXPIRED.value
                        session.add(existing_active)
                    pending_sub.expires_at = now + timedelta(days=add_days)
            elif p_type.startswith("PROMOTION_"):
                try:
                    add_days = int(p_type.replace("PROMOTION_", "").replace("D", ""))
                except Exception:
                    add_days = 30
                plan_title = f"สมาชิก 🔥 โปรโมชั่นพิเศษ {add_days} วัน"
                duration_str = f"{add_days} วัน"
                if existing_active and existing_active.plan_type != PlanType.TRIAL_15M.value:
                    base_time = max(ensure_utc(existing_active.expires_at), now)
                    pending_sub.expires_at = base_time + timedelta(days=add_days)
                    existing_active.status = SubStatus.EXPIRED.value
                    session.add(existing_active)
                    is_stack_extension = True
                else:
                    if existing_active:
                        existing_active.status = SubStatus.EXPIRED.value
                        session.add(existing_active)
                    pending_sub.expires_at = now + timedelta(days=add_days)
            elif p_type.startswith("MANUAL_VIP_"):
                try:
                    add_days = int(p_type.replace("MANUAL_VIP_", "").replace("D", ""))
                except Exception:
                    add_days = 30
                plan_title = f"สมาชิก VIP {add_days} วัน"
                duration_str = f"{add_days} วัน"
                pending_sub.expires_at = now + timedelta(days=add_days)
            else:
                pending_sub.expires_at = now + timedelta(days=30)
                plan_title = f"สมาชิก {p_type}"
                duration_str = "30 วัน"

            pending_sub.joined_at = now
            pending_sub.status = SubStatus.ACTIVE.value
            session.add(pending_sub)
            await session.flush()
            sub_id = pending_sub.id
            new_expires_at = pending_sub.expires_at
            sub_to_activate = pending_sub
            logger.info(f"Activated subscription #{sub_id} for user {user_id} ({plan_title})")

        elif existing_active:
            # === กรณีไม่มี PENDING แต่มี ACTIVE อยู่แล้ว (สมาชิกเดิมหลุดแล้วเข้าใหม่) ===
            sub_to_activate = existing_active
            sub_id = existing_active.id
            new_expires_at = existing_active.expires_at
            p_type = existing_active.plan_type
            if p_type in PLAN_DETAILS:
                p_info = get_dynamic_plan_info(p_type)
                plan_title = f"สมาชิก {p_info['badge']}"
                duration_str = f"{p_info['days']} วัน"
            elif p_type == PlanType.TRIAL_15M.value:
                plan_title = "ทดลองใช้งานฟรี"
                duration_str = f"{config.TRIAL_DURATION_MINUTES} นาที"
            else:
                plan_title = f"สมาชิก {p_type}"
                duration_str = "30 วัน"
            logger.info(f"Existing active subscription #{sub_id} detected for user {user_id} ({plan_title})")

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

    if sub_to_activate:
        # 1. ส่งข้อความต้อนรับเข้า DM ของผู้ใช้
        try:
            welcome_dm = (
                f"🎉 <b>ยินดีต้อนรับเข้าสู่ VIP Channel!</b>\n\n"
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
        header_title = "🚪 <b>มีสมาชิกกดเข้าร่วม Channel แล้ว!</b>"
        if is_stack_extension:
            header_title = "🚪 <b>มีสมาชิกกดเข้าร่วม Channel พร้อมต่อเวลาสะสม!</b>"

        admin_log_msg = (
            f"{header_title}\n\n"
            f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📦 <b>แผนที่ใช้งาน:</b> <b>{plan_title}</b>\n"
            f"⏳ <b>ระยะเวลา:</b> {duration_str}\n"
            f"🟢 <b>เวลาเริ่มต้น (Start):</b> <code>{start_time_thai} น.</code>\n"
            f"🔴 <b>เวลาหมดอายุ (End):</b> <code>{expires_at_thai} น.</code>\n"
            f"🆔 <b>Subscription ID:</b> <code>#{sub_id}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>บันทึกประวัติการเข้าใช้งานลงฐานข้อมูลเรียบร้อย</i>"
        )

        try:
            await bot.send_message(
                chat_id=config.ADMIN_GROUP_ID,
                text=admin_log_msg,
                parse_mode="HTML",
            )
            logger.info(f"Sent channel join audit log to Admin Group for User {user_id} (Sub #{sub_id})")
        except Exception as e:
            logger.warning(f"Could not send join log to Admin Group: {e}")

    else:
        # === กรณีเป็น Admin/Creator ของ Channel แต่ไม่มี Subscription -> ข้ามการเตะ ===
        if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            logger.info(f"User {user_id} ({user.full_name}) is Administrator/Creator in Channel with no active sub. Skipping soft-kick.")
            return

        # === กรณีไม่มี Subscription ที่ถูกต้อง -> Soft-kick ===
        logger.warning(f"Unauthorized join detected: User {user_id} ({user.full_name}) has no valid subscription.")
        kicked = False
        try:
            await bot.ban_chat_member(chat_id=event.chat.id, user_id=user_id, revoke_messages=False)
            await bot.unban_chat_member(chat_id=event.chat.id, user_id=user_id, only_if_banned=True)
            kicked = True
            logger.info(f"Successfully soft-kicked unauthorized User ID={user_id} from Channel.")
        except Exception as e:
            logger.error(f"Failed to soft-kick unauthorized User ID={user_id}: {e}")

        try:
            await bot.send_message(
                chat_id=user_id,
                text="⚠️ <b>ไม่สามารถเข้าร่วม Channel VIP ได้</b>\n\nเนื่องจากคุณยังไม่มีแพ็กเกจสมาชิกที่เปิดใช้งาน กรุณาพิมพ์ /start เพื่อกดทดลองใช้ฟรี หรือสมัครสมาชิก VIP ครับ",
                parse_mode="HTML",
            )
        except Exception:
            pass

        action_status = "เตะออกจาก Channel ทันทีเรียบร้อย ❌" if kicked else "⚠️ บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์ Ban Users ของบอท)"
        admin_alert = (
            "🚨 <b>[Security Alert] ตรวจพบผู้ใช้เข้า Channel โดยไม่ผ่านระบบ!</b>\n\n"
            f"👤 <b>ผู้ใช้:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
            f"⚡ <b>การดำเนินการ:</b> {action_status}\n\n"
            "ℹ️ <i>ระบบป้องกันไม่ให้ผู้ใช้แอบแฝงหรือค้างในห้อง VIP โดยไม่มีแพ็กเกจ</i>"
        )
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_alert, parse_mode="HTML")
        except Exception:
            pass
