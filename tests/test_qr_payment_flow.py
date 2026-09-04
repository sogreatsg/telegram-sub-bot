import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.config import get_settings
from bot.models.schema import PlanType, get_dynamic_plan_info
from bot.handlers.payment import handle_payment_method_promptpay
from bot.handlers.admin import handle_admin_swipe_reply, handle_admin_reply_media_command

class TestQRPaymentFlow(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_qr_payment_flow(self):
        config = get_settings()
        
        # 1. Mock CallbackQuery from user requesting QR payment
        callback = MagicMock()
        callback.data = "payment:method:promptpay:VIP_30D"
        callback.from_user = MagicMock()
        callback.from_user.id = 999888777
        callback.from_user.username = "test_user"
        callback.from_user.full_name = "Test User"
        callback.from_user.first_name = "Test"
        
        callback.message = MagicMock()
        callback.message.photo = None
        callback.message.document = None
        callback.message.edit_text = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        
        state = MagicMock()
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()
        
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        
        # Mock user as V2 member
        mock_user = MagicMock()
        mock_user.telegram_id = 999888777
        mock_user.is_moved_to_secondary = True
        mock_user.is_moved_to_tertiary = False
        mock_user.assigned_channel = "SECONDARY"
        
        with patch("bot.handlers.payment.get_session") as mock_session_ctx, \
             patch("bot.handlers.payment.get_or_create_user", return_value=(mock_user, False)), \
             patch("bot.handlers.payment.is_user_v2_member", return_value=True), \
             patch("bot.handlers.payment.is_promptpay_active", return_value=True), \
             patch("bot.handlers.payment.log_chat_message", new_callable=AsyncMock):
            
            mock_session = AsyncMock()
            mock_session_ctx.return_value.__aenter__.return_value = mock_session
            
            await handle_payment_method_promptpay(callback, state, bot)
            
            # Check user received the waiting message
            self.assertTrue(callback.message.edit_text.called or callback.message.answer.called)
            called_args = callback.message.edit_text.call_args or callback.message.answer.call_args
            user_text = called_args[1].get("text", "") or (called_args[0][0] if called_args[0] else "")
            self.assertIn("กรุณารอระบบสร้าง QR Code", user_text)
            self.assertIn("1,000 บาท", user_text)
            
            # Check Admin Group received the alert with user info and mention
            self.assertTrue(bot.send_message.called)
            admin_msg_args = bot.send_message.call_args
            admin_text = admin_msg_args[1].get("text", "")
            self.assertIn("มีคำขอสร้าง QR Code", admin_text)
            self.assertIn("<b>User ID:</b> <code>999888777</code>", admin_text)
            self.assertIn(config.ADMIN_MENTION, admin_text)
            self.assertIn("1,000 บาท", admin_text)

    async def test_admin_swipe_reply_photo(self):
        config = get_settings()
        bot = MagicMock()
        bot_user = MagicMock()
        bot_user.id = 12345
        bot.get_me = AsyncMock(return_value=bot_user)
        bot.send_photo = AsyncMock()
        
        # Message replying to the bot's alert message
        message = MagicMock()
        message.chat.id = config.ADMIN_GROUP_ID
        message.text = None
        
        photo_item = MagicMock()
        photo_item.file_id = "photo_file_123"
        message.photo = [photo_item]
        message.document = None
        message.caption = "สแกน QR Code นี้ได้เลยครับ"
        message.reply = AsyncMock()
        
        message.reply_to_message = MagicMock()
        message.reply_to_message.from_user.id = 12345 # from bot
        message.reply_to_message.text = "📲 มีคำขอสร้าง QR Code\n🔢 User ID: <code>999888777</code>"
        message.reply_to_message.caption = None
        
        mock_user = MagicMock()
        mock_user.telegram_id = 999888777
        mock_user.is_blocked = False
        
        with patch("bot.handlers.admin.get_session") as mock_session_ctx, \
             patch("bot.handlers.admin.log_chat_message", new_callable=AsyncMock):
            mock_session = AsyncMock()
            mock_session.get = AsyncMock(return_value=mock_user)
            mock_session_ctx.return_value.__aenter__.return_value = mock_session
            
            await handle_admin_swipe_reply(message, bot)
            
            # Verify bot sent photo to target user
            bot.send_photo.assert_called_once()
            call_kwargs = bot.send_photo.call_args[1]
            self.assertEqual(call_kwargs["chat_id"], 999888777)
            self.assertEqual(call_kwargs["photo"], "photo_file_123")
            self.assertEqual(call_kwargs["caption"], "สแกน QR Code นี้ได้เลยครับ")
            
            # Verify confirmation reply in admin group
            message.reply.assert_called_once()
            self.assertIn("ส่งรูปภาพ QR Code ไปยังผู้ใช้", message.reply.call_args[0][0])

    async def test_admin_reply_media_command(self):
        config = get_settings()
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        
        message = MagicMock()
        message.chat.id = config.ADMIN_GROUP_ID
        photo_item = MagicMock()
        photo_item.file_id = "qr_pic_999"
        message.photo = [photo_item]
        message.document = None
        message.caption = "/reply 999888777 สแกน QR สำหรับแพ็กเกจ 30 วัน"
        message.reply = AsyncMock()
        
        mock_user = MagicMock()
        mock_user.telegram_id = 999888777
        mock_user.full_name = "Test User"
        mock_user.is_blocked = False
        
        with patch("bot.handlers.admin.get_session") as mock_session_ctx, \
             patch("bot.handlers.admin.log_chat_message", new_callable=AsyncMock):
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_user)))
            mock_session_ctx.return_value.__aenter__.return_value = mock_session
            
            await handle_admin_reply_media_command(message, bot)
            
            bot.send_photo.assert_called_once()
            call_kwargs = bot.send_photo.call_args[1]
            self.assertEqual(call_kwargs["chat_id"], 999888777)
            self.assertEqual(call_kwargs["photo"], "qr_pic_999")
            self.assertEqual(call_kwargs["caption"], "สแกน QR สำหรับแพ็กเกจ 30 วัน")
            
            message.reply.assert_called_once()
            self.assertIn("สำเร็จ", message.reply.call_args[0][0])

if __name__ == "__main__":
    unittest.main()
