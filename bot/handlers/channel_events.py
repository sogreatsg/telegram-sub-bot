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


async def _process_joined_member(event: ChatMemberUpdated, bot: Bot, user, new_status):
    user_id = user.id
    now = datetime.now(timezone.utc)

    # ทำลายลิงก์เชิญ (Revoke) ทันทีที่ถูกใช้งาน (ป้องกันการนำกลับมาใช้ซ้ำ 100%)
    if event.invite_link and not event.invite_link.is_primary:
        try:
            await bot.revoke_chat_invite_link(
                chat_id=event.chat.id, 
                invite_link=event.invite_link.invite_link
            )
            logger.info(f"Revoked invite link {event.invite_link.invite_link} after use by {user_id}")
        except Exception as e:
            logger.warning(f"Could not revoke invite link: {e}")

    # ตรวจสอบว่าเป็น Administrator หรือ Owner หรือไม่ (ถ้าใช่ ให้ข้าม ไม่ต้องเตะ)
    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        logger.info(f"User {user_id} joined/promoted as Administrator/Creator. Skipping subscription check.")
        return

    async with get_session() as session:
        # 1. ตรวจสอบข้อมูล User ในฐานข้อมูล
        user_stmt = select(User).where(User.telegram_id == user_id)
        user_obj = (await session.execute(user_stmt)).scalar_one_or_none()

        # 2. ตรวจสอบว่าผู้ใช้มี ACTIVE subscription ที่ยังไม่หมดอายุอยู่แล้วหรือไม่ (เช่น หลุดแล้วเข้าใหม่)
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

        if existing_active:
            # ตรวจสอบว่ามีรายการ PENDING แพ็กเกจที่รอต่อเวลาอยู่ด้วยหรือไม่
            pending_stmt = (
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.status == SubStatus.PENDING.value,
                )
                .order_by(Subscription.id.desc())
            )
            pending_to_stack = (await session.execute(pending_stmt)).scalars().first()
            user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
            full_name_safe = html.escape(user.full_name or "")

            if pending_to_stack:
                add_days = 0
                if pending_to_stack.plan_type in PLAN_DETAILS:
                    add_days = get_dynamic_plan_info(pending_to_stack.plan_type)["days"]
                elif pending_to_stack.plan_type.startswith("PROMOTION_"):
                    try:
                        add_days = int(pending_to_stack.plan_type.replace("PROMOTION_", "").replace("D", ""))
                    except Exception:
                        add_days = 30
                elif pending_to_stack.plan_type.startswith("MANUAL_VIP_"):
                    try:
                        add_days = int(pending_to_stack.plan_type.replace("MANUAL_VIP_", "").replace("D", ""))
                    except Exception:
                        add_days = 30
                        
                if add_days > 0:
                    existing_active.expires_at = max(ensure_utc(existing_active.expires_at), now) + timedelta(days=add_days)
                    existing_active.plan_type = pending_to_stack.plan_type
                    pending_to_stack.status = SubStatus.ACTIVE.value
                    session.add(existing_active)
                    session.add(pending_to_stack)
                    logger.info(f"User {user_id} re-joined with pending sub #{pending_to_stack.id}. Stacked +{add_days} days to existing active sub #{existing_active.id}")

                    # แจ้งเตือนเข้า Admin Group สำหรับการต่อเวลาสะสม
                    new_exp_thai = format_thai_datetime(existing_active.expires_at)
                    admin_rejoin_msg = (
                        "🚪 <b>สมาชิกเข้าสู่ Channel พร้อมต่อเวลาสะสม!</b>\n\n"
                        f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
                        f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                        f"📦 <b>แพ็กเกจที่เพิ่ม:</b> +{add_days} วัน\n"
                        f"📅 <b>วันหมดอายุใหม่:</b> <code>{new_exp_thai} น.</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "ℹ️ <i>ระบบได้ต่อเวลาสะสมและบันทึกประวัติเรียบร้อย</i>"
                    )
                    try:
                        await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_rejoin_msg, parse_mode="HTML")
                    except Exception:
                        pass
                else:
                    logger.info(f"User {user_id} re-joined channel with already ACTIVE subscription ID={existing_active.id}, no valid pending days to stack")
            else:
                logger.info(f"User {user_id} re-joined channel with already ACTIVE subscription ID={existing_active.id}")
                # แจ้งเตือนเข้า Admin Group สำหรับการ Re-join ที่มี VIP อยู่แล้ว
                exp_thai = format_thai_datetime(existing_active.expires_at)
                admin_rejoin_msg = (
                    "🚪 <b>สมาชิกเข้าสู่ Channel (มีสถานะ VIP ใช้งานอยู่แล้ว)</b>\n\n"
                    f"👤 <b>ผู้ใช้งาน:</b> {full_name_safe} ({user_handle})\n"
                    f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"📦 <b>แพ็กเกจ:</b> <b>{existing_active.plan_type}</b>\n"
                    f"📅 <b>หมดอายุวันที่:</b> <code>{exp_thai} น.</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "ℹ️ <i>ผู้ใช้มีสถานะสมาชิกที่ยังไม่หมดอายุในระบบ</i>"
                )
                try:
                    await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_rejoin_msg, parse_mode="HTML")
                except Exception:
                    pass
            return

        # 3. ค้นหา Subscription ล่าสุดที่มีสถานะ PENDING ของผู้ใช้คนนี้
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubStatus.PENDING.value,
            )
            .order_by(Subscription.id.desc())
        )
        result = await session.execute(stmt)
        sub = result.scalars().first()

        # ตรวจสอบความถูกต้องของ PENDING Subscription (ป้องกันการใช้สิทธิ์ซ้ำ หรือลิงก์เก่าค้าง)
        is_sub_valid = False
        if sub:
            sub_created = sub.created_at if sub.created_at else now
            if sub_created.tzinfo is None:
                sub_created = sub_created.replace(tzinfo=timezone.utc)
            is_stale = (now - sub_created) > timedelta(hours=48)
            is_trial_abuse = (sub.plan_type == PlanType.TRIAL_15M.value and user_obj and user_obj.trial_used)

            if is_stale or is_trial_abuse:
                logger.warning(
                    f"Rejecting invalid PENDING sub #{sub.id} for User {user_id}: "
                    f"is_stale={is_stale}, is_trial_abuse={is_trial_abuse}"
                )
                sub.status = SubStatus.EXPIRED.value
                session.add(sub)
                sub = None  # สั่งให้เข้าเงื่อนไข unauthorized เพื่อ Soft-kick ทันที!
            else:
                is_sub_valid = True

        # กรณีมี PENDING Subscription ที่ถูกต้อง -> เปิดใช้งานตามปกติ
        if sub and is_sub_valid:
            sub.joined_at = now
            sub.status = SubStatus.ACTIVE.value

            referred_by_to_award = None
            friend_user_snapshot = None

            if sub.plan_type == PlanType.TRIAL_15M.value:
                trial_minutes = config.TRIAL_DURATION_MINUTES
                sub.expires_at = now + timedelta(minutes=trial_minutes)
                plan_title = f"ทดลองใช้งานฟรี {trial_minutes} นาที"
                duration_str = f"{trial_minutes} นาที"

                # บันทึกว่าผู้ใช้ได้ใช้สิทธิ์ trial แล้ว
                if user_obj:
                    # ถ้ายังไม่เคยใช้ trial และมีผู้แนะนำ -> เตรียมมอบรางวัลให้ผู้แนะนำ
                    if not user_obj.trial_used and user_obj.referred_by_id:
                        referred_by_to_award = user_obj.referred_by_id
                        friend_user_snapshot = user_obj

                    user_obj.trial_used = True
                    session.add(user_obj)

            elif sub.plan_type == PlanType.REFERRAL_VIP.value:
                bonus_days = user_obj.referral_bonus_days if (user_obj and user_obj.referral_bonus_days > 0) else 1
                sub.expires_at = now + timedelta(days=bonus_days)
                plan_title = f"สมาชิก 🎁 VIP โบนัสชวนเพื่อน ({bonus_days} วัน)"
                duration_str = f"{bonus_days} วัน"

            elif sub.plan_type in PLAN_DETAILS:
                p_info = get_dynamic_plan_info(sub.plan_type)
                sub.expires_at = now + timedelta(days=p_info["days"])
                plan_title = f"สมาชิก {p_info['badge']}"
                duration_str = f"{p_info['days']} วัน"

            elif sub.plan_type.startswith("PROMOTION_"):
                try:
                    days = int(sub.plan_type.replace("PROMOTION_", "").replace("D", ""))
                except Exception:
                    days = 30
                sub.expires_at = now + timedelta(days=days)
                plan_title = f"สมาชิก 🔥 โปรโมชั่นพิเศษ {days} วัน"
                duration_str = f"{days} วัน"

            elif sub.plan_type.startswith("MANUAL_VIP_"):
                try:
                    days = int(sub.plan_type.replace("MANUAL_VIP_", "").replace("D", ""))
                except Exception:
                    days = 30
                sub.expires_at = now + timedelta(days=days)
                plan_title = f"สมาชิก VIP {days} วัน"
                duration_str = f"{days} วัน"

            else:
                sub.expires_at = now + timedelta(days=30)
                plan_title = sub.plan_type
                duration_str = "30 วัน"

            session.add(sub)
            sub_id = sub.id
            start_time_thai = format_thai_datetime(now)
            expires_at_thai = format_thai_datetime(sub.expires_at)
            logger.info(
                f"Activated Subscription ID={sub_id} for User ID={user_id}. "
                f"Plan={sub.plan_type}, Expires={expires_at_thai}"
            )

    # มอบรางวัล Referral Bonus ให้ผู้แนะนำ (ถ้ามี)
    if referred_by_to_award and friend_user_snapshot:
        try:
            await award_referral_bonus(bot=bot, referrer_id=referred_by_to_award, friend_user=friend_user_snapshot)
        except Exception as e:
            logger.error(f"Failed to award referral bonus: {e}", exc_info=True)

    # ดำเนินการต่อหลังจบ Transaction DB
    if sub and is_sub_valid:
        # 1. ส่งข้อความต้อนรับและแจ้งเวลาหมดอายุให้ผู้ใช้ทาง DM
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
            logger.warning(f"Could not send channel join welcome DM to User ID={user_id}: {e}")

        # 2. ส่งข้อความแจ้งเตือนเข้ากลุ่ม Admin
        user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
        full_name_safe = html.escape(user.full_name or "")
        admin_log_msg = (
            "🚪 <b>มีสมาชิกกดเข้าร่วม Channel แล้ว!</b>\n\n"
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
            logger.info(f"Sent channel join audit log to Admin Group for User {user_id}")
        except Exception as e:
            logger.warning(f"Could not send join log to Admin Group: {e}")

    else:
        # กรณีไม่มี PENDING และไม่มี ACTIVE subscription -> ผู้ใช้แอบเข้า / ใช้ลิงก์เก่า / แชร์ลิงก์
        # ทำการ Soft-kick ออกจาก Channel ทันทีเพื่อความปลอดภัย
        logger.warning(f"Unauthorized join detected: User {user_id} ({user.full_name}) has no active/pending subscription.")
        kicked = False
        try:
            await bot.ban_chat_member(
                chat_id=config.CHANNEL_ID,
                user_id=user_id,
                revoke_messages=False,
            )
            await bot.unban_chat_member(
                chat_id=config.CHANNEL_ID,
                user_id=user_id,
                only_if_banned=True,
            )
            kicked = True
            logger.info(f"Successfully soft-kicked unauthorized User ID={user_id} from Channel.")
        except Exception as e:
            logger.error(f"Failed to soft-kick unauthorized User ID={user_id}: {e}")
            try:
                alert_perm = (
                    f"⚠️ <b>[เตือนภัยสิทธิ์แอดมิน] บอทไม่สามารถเตะ User ID <code>{user_id}</code> ได้!</b>\n\n"
                    f"สาเหตุ: <code>{html.escape(str(e))}</code>\n"
                    "👉 <b>กรุณาตรวจสอบว่าบอทมีสิทธิ์ 'Ban Users / แบนผู้ใช้' ใน Channel VIP</b>"
                )
                await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=alert_perm, parse_mode="HTML")
            except Exception:
                pass

        # แจ้งเตือนผู้ใช้ทาง DM
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ <b>ไม่สามารถเข้าร่วม Channel VIP ได้</b>\n\n"
                    "เนื่องจากคุณยังไม่มีแพ็กเกจสมาชิกที่เปิดใช้งาน หรือลิงก์เชิญนี้หมดอายุแล้วครับ\n\n"
                    "👉 <b>กรุณาพิมพ์ /start</b> ในแชทนี้เพื่อกดทดลองใช้ฟรี หรือสมัครสมาชิก VIP 30 วันครับ"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        # แจ้งเตือนแอดมินในกลุ่ม Admin Group
        user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
        full_name_safe = html.escape(user.full_name or "")
        try:
            action_status = "เตะออกจาก Channel ทันทีเรียบร้อย ❌" if kicked else "⚠️ บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์ Ban Users ของบอท)"
            admin_alert = (
                "🚨 <b>[Security Alert] ตรวจพบผู้ใช้เข้า Channel โดยไม่ผ่านระบบ!</b>\n\n"
                f"👤 <b>ผู้ใช้:</b> {full_name_safe} ({user_handle})\n"
                f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
                f"⚡ <b>การดำเนินการ:</b> {action_status}\n\n"
                "ℹ️ <i>ระบบป้องกันไม่ให้ผู้ใช้แอบแฝงหรือค้างในห้อง VIP โดยไม่มีแพ็กเกจ</i>"
            )
            await bot.send_message(
                chat_id=config.ADMIN_GROUP_ID,
                text=admin_alert,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Could not send unauthorized join alert to Admin Group: {e}")
