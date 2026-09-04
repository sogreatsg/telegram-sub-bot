import logging
import html
from datetime import datetime, timezone
from aiogram import Router, Bot, F
from aiogram.types import ChatMemberUpdated, ChatJoinRequest, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from bot.config import get_settings
from bot.models.schema import User, Subscription, SubStatus
from bot.services.database import get_session, get_or_create_user
from bot.services.referral import award_referral_bonus
from bot.services.subscription import activate_pending_subscription
from bot.services.channel_service import (
    is_target_channel,
    is_secondary_channel,
    is_tertiary_channel,
    get_channel_label,
    fetch_channel_title,
    get_user_target_channel_id,
    normalize_chat_id,
)

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

    # ถ้าเป็นห้องแชทกลุ่มทั่วไป (ที่ไม่ใช่กลุ่ม Admin) แล้วผู้ใช้เป็นคนที่ถูกบล็อก -> แบนออกจากกลุ่มทันที
    if event.chat.type in ("group", "supergroup") and event.chat.id != config.ADMIN_GROUP_ID:
        async with get_session() as session:
            db_user = await session.get(User, user.id)
            if db_user and getattr(db_user, "is_blocked", False):
                try:
                    await bot.ban_chat_member(chat_id=event.chat.id, user_id=user.id, revoke_messages=True)
                    logger.info(f"[BLOCKED_USER_IN_GROUP] Banned blocked user {user.id} attempting to stay/join group {event.chat.id}")
                except Exception:
                    pass
                return

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
        db_user = await session.get(User, user_id)
        if db_user and getattr(db_user, "is_blocked", False):
            try:
                await bot.decline_chat_join_request(chat_id=event.chat.id, user_id=user_id)
                logger.info(f"Silently declined ChatJoinRequest for blocked User {user_id}")
            except Exception:
                pass
            return

        sub = await session.get(Subscription, user_id)
        has_claim = sub is not None and (
            (sub.status == SubStatus.PENDING.value and ((sub.pending_days or 0) > 0 or (sub.pending_minutes or 0) > 0))
            or (sub.expires_at and ensure_utc(sub.expires_at) > datetime.now(timezone.utc))
        )
        target_channel_id = get_user_target_channel_id(db_user)

    if not has_claim:
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
        return

    # ถ้ามีสิทธิ์สมาชิก แต่ขอกดเข้าห้องที่ไม่ตรงกับห้องประจำตัวของผู้ใช้
    if normalize_chat_id(event.chat.id) != normalize_chat_id(target_channel_id):
        try:
            await bot.decline_chat_join_request(chat_id=event.chat.id, user_id=user_id)
            expected_label = get_channel_label(target_channel_id)
            wrong_label = get_channel_label(event.chat.id)
            logger.warning(f"Declined wrong-channel ChatJoinRequest for User {user_id} in {wrong_label} (assigned: {expected_label})")
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⚠️ <b>ไม่สามารถเข้าร่วม {wrong_label} ได้</b>\n\n"
                        f"สิทธิ์สมาชิกของคุณถูกกำหนดให้อยู่ในห้อง <b>{expected_label}</b> เท่านั้น\n"
                        f"กรุณาใช้ลิงก์เชิญสำหรับห้อง <b>{expected_label}</b> ครับ"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to decline wrong-channel ChatJoinRequest for User {user_id}: {e}")
        return

    try:
        await bot.approve_chat_join_request(chat_id=event.chat.id, user_id=user_id)
        logger.info(f"Approved ChatJoinRequest for User {user_id} (status={sub.status})")
        async with user_locks[user_id]:
            await _process_joined_member(event, bot, user, ChatMemberStatus.MEMBER)
    except Exception as e:
        logger.error(f"Failed to approve ChatJoinRequest for User {user_id}: {e}")


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

    # === กรณีเป็น Admin/Creator ของ Channel แต่ไม่มี Subscription -> ข้ามการเตะ ===
    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        logger.info(f"User {user_id} ({user.full_name}) is Administrator/Creator in Channel with no active sub. Skipping soft-kick.")
        return

    sub_to_activate = None
    plan_title = "สมาชิก VIP"
    duration_str = "30 วัน"
    is_stack_extension = False
    new_expires_at = None
    referred_by_to_award = None
    friend_user_snapshot = None
    is_wrong_channel = False
    expected_channel_id = None

    user_handle = f"@{user.username}" if user.username else "ไม่มี Username"
    full_name_safe = html.escape(user.full_name or "")
    start_time_thai = format_thai_datetime(now)

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
        has_pending = bool(sub and sub.status == SubStatus.PENDING.value and ((sub.pending_days or 0) > 0 or (sub.pending_minutes or 0) > 0))
        has_active = bool(sub and sub.expires_at and ensure_utc(sub.expires_at) > now)
        has_valid_sub = has_pending or has_active

        if not has_valid_sub:
            # ไม่มี Subscription ที่ถูกต้อง -> จะถูก soft-kick ในขั้นตอนถัดไป
            pass
        else:
            # ตรวจสอบว่าผู้ใช้กำลังเข้า Channel ที่ได้รับอนุญาต (ตรงกับ assigned_channel) หรือไม่
            expected_channel_id = get_user_target_channel_id(user_obj)
            if normalize_chat_id(event.chat.id) != normalize_chat_id(expected_channel_id):
                is_wrong_channel = True
            else:
                if has_pending:
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

                elif has_active:
                    # === สมาชิกเดิมหลุดแล้วเข้าใหม่ หรือย้ายเข้าห้องใหม่ตรงตามสิทธิ์ ===
                    sub.status = SubStatus.ACTIVE.value
                    session.add(sub)
                    sub_to_activate = sub
                    new_expires_at = sub.expires_at
                    plan_title = sub.source_label or "สมาชิก VIP"
                    duration_str = f"เหลือ {format_remaining_time(sub.expires_at)}"
                    logger.info(f"Existing active subscription detected for user {user_id} ({plan_title}) in {get_channel_label(event.chat.id)}")

        await session.commit()

    # === จัดการกรณีเข้าผิดห้อง Channel (สิทธิ์ไม่ตรงกับห้องที่กดเข้า) ===
    if is_wrong_channel:
        expected_channel_label = get_channel_label(expected_channel_id)
        wrong_channel_label = get_channel_label(event.chat.id)
        logger.warning(
            f"Unauthorized wrong-channel join: User {user_id} ({user.full_name}) entered {wrong_channel_label} ({event.chat.id}), "
            f"but is assigned to {expected_channel_label} ({expected_channel_id}). Soft-kicking immediately..."
        )
        kicked = False
        try:
            await bot.ban_chat_member(chat_id=event.chat.id, user_id=user_id, revoke_messages=False)
            await bot.unban_chat_member(chat_id=event.chat.id, user_id=user_id, only_if_banned=True)
            kicked = True
            logger.info(f"Successfully soft-kicked User ID={user_id} from wrong channel {wrong_channel_label}.")
        except Exception as e:
            logger.error(f"Failed to soft-kick User ID={user_id} from wrong channel {wrong_channel_label}: {e}")

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>ไม่สามารถเข้าร่วม {wrong_channel_label} ได้</b>\n\n"
                    f"สิทธิ์สมาชิกของคุณถูกกำหนดให้อยู่ในห้อง <b>{expected_channel_label}</b> เท่านั้น\n"
                    f"กรุณาใช้ลิงก์เชิญสำหรับห้อง <b>{expected_channel_label}</b> ที่ได้รับล่าสุดจากทางแอดมินหรือบอทครับ"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        action_status = f"เตะออกจาก {wrong_channel_label} ทันทีเรียบร้อย ❌" if kicked else "⚠️ บอทเตะไม่สำเร็จ (กรุณาตรวจสิทธิ์ Ban Users ของบอท)"
        admin_alert = (
            f"🚨 <b>[Security Alert] ตรวจพบสมาชิกเข้าผิดห้อง Channel!</b>\n\n"
            f"👤 <b>ผู้ใช้:</b> {full_name_safe} ({user_handle})\n"
            f"🔢 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📢 <b>ห้องที่พยายามเข้า:</b> {wrong_channel_label} (<code>{event.chat.id}</code>)\n"
            f"📌 <b>ห้องประจำตัวที่ถูกต้อง:</b> <b>{expected_channel_label}</b> (<code>{expected_channel_id}</code>)\n"
            f"⚡ <b>การดำเนินการ:</b> {action_status}\n\n"
            f"ℹ️ <i>ระบบป้องกันไม่ให้สมาชิกใช้ลิงก์ห้องเก่าเข้าห้องอื่นที่ไม่ได้รับอนุญาต</i>"
        )
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_alert, parse_mode="HTML")
        except Exception:
            pass
        return

    # === จัดการกรณีไม่มี Subscription เลย ===
    if not sub_to_activate:
        channel_label = get_channel_label(event.chat.id)
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
            f"ℹ️ <i>ระบบป้องกันไม่ให้ผู้ใช้แอบแฝงหรือค้างในห้อง VIP โดยไม่มีแพ็กเกจ</i>"
        )
        try:
            await bot.send_message(chat_id=config.ADMIN_GROUP_ID, text=admin_alert, parse_mode="HTML")
        except Exception:
            pass
        return

    # === กรณีเข้าห้องถูกต้องและมี Subscription -> ส่งข้อความต้อนรับและแจ้งแอดมินตามปกติ ===
    # มอบรางวัล Referral Bonus ให้ผู้แนะนำ (ถ้ามี)
    if referred_by_to_award and friend_user_snapshot:
        try:
            await award_referral_bonus(bot=bot, referrer_id=referred_by_to_award, friend_user=friend_user_snapshot)
        except Exception as e:
            logger.error(f"Failed to award referral bonus: {e}", exc_info=True)

    expires_at_thai = format_thai_datetime(new_expires_at) if new_expires_at else "ไม่ระบุ"
    try:
        channel_label = await fetch_channel_title(bot, event.chat.id)
    except Exception:
        channel_label = get_channel_label(event.chat.id)
    is_ter_join = is_tertiary_channel(event.chat.id)
    is_sec_join = is_secondary_channel(event.chat.id)

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
    if is_ter_join:
        header_title = "🌟 <b>[Target Channel V.3] มีสมาชิกกดเข้าร่วม Channel V.3 แล้ว!</b>"
        if is_stack_extension:
            header_title = "🌟 <b>[Target Channel V.3] มีสมาชิกกดเข้าร่วม Channel V.3 พร้อมต่อเวลาสะสม!</b>"
    elif is_sec_join:
        header_title = "🌟 <b>[Target Channel V.2] มีสมาชิกกดเข้าร่วม Channel ใหม่แล้ว!</b>"
        if is_stack_extension:
            header_title = "🌟 <b>[Target Channel V.2] มีสมาชิกกดเข้าร่วม Channel ใหม่ พร้อมต่อเวลาสะสม!</b>"
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

    # 3. แก้ไขปุ่มในข้อความเชิญเดิมของผู้ใช้ให้กลายเป็น "✅ เข้าร่วมห้องเรียบร้อยแล้ว" (ป้องกันการกดซ้ำ)
    if user_obj and getattr(user_obj, "last_invite_msg_id", None):
        invite_mid = user_obj.last_invite_msg_id
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=invite_mid,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ เข้าร่วมห้องเรียบร้อยแล้ว", callback_data="none_joined")]
                    ]
                )
            )
            async with get_session() as session:
                u_clear = await session.get(User, user_id)
                if u_clear:
                    u_clear.last_invite_msg_id = None
                    session.add(u_clear)
                    await session.commit()
            logger.info(f"Successfully updated previous invite message {invite_mid} to joined status for user {user_id}")
        except Exception as e:
            logger.debug(f"Could not edit previous invite button for user {user_id}: {e}")


@router.callback_query(F.data == "none_joined")
async def handle_none_joined_callback(callback: CallbackQuery):
    """เมื่อผู้ใช้กดปุ่มสถานะว่าเข้าร่วมห้องเรียบร้อยแล้ว"""
    await callback.answer("✅ คุณได้เข้าร่วม Channel เรียบร้อยแล้วครับ", show_alert=True)


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


@router.message(F.chat.type.in_({"group", "supergroup"}), F.chat.id != config.ADMIN_GROUP_ID)
async def handle_group_chat_blocked_user_guard(message: Message, bot: Bot):
    """
    ดักจับข้อความในห้องแชทกลุ่มชุมชนทั้งหมด (ยกเว้นกลุ่ม Admin):
    - หากผู้ส่งเป็นผู้ใช้ที่ถูกบล็อก (is_blocked = True)
    - ลบข้อความที่พิมพ์ออกทันที (Silent Delete)
    - แบนผู้ใช้ออกจากห้องแชททันที (Ban from Chat Group)
    """
    if not message.from_user or message.from_user.is_bot:
        return

    user_id = message.from_user.id
    async with get_session() as session:
        db_user = await session.get(User, user_id)
        if not db_user or not getattr(db_user, "is_blocked", False):
            return

    # 1. ลบข้อความที่ผู้ใช้พิมพ์ในกลุ่มทันที
    try:
        await message.delete()
        logger.info(f"[BLOCKED_USER_IN_GROUP] Deleted message from blocked user {user_id} in group {message.chat.id}")
    except Exception as e:
        logger.debug(f"Failed to delete message from blocked user {user_id}: {e}")

    # 2. แบนผู้ใช้ออกจากกลุ่มทันที
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=user_id, revoke_messages=True)
        logger.info(f"[BLOCKED_USER_IN_GROUP] Banned blocked user {user_id} from group {message.chat.id}")
    except Exception as e:
        logger.debug(f"Failed to ban blocked user {user_id} in group {message.chat.id}: {e}")


