import asyncio
import os
import sys

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from bot.config import get_settings
from bot.services.chat_cleaner_state import get_user_checkpoint, get_clean_status_summary

async def check_local(target_user_id: int):
    config = get_settings()
    bot = Bot(token=config.BOT_TOKEN)

    me = await bot.get_me()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"🤖 บอท: @{me.username} (ID: {me.id}, ชื่อ: {me.first_name})", flush=True)
    print(f"👤 ตรวจสอบผู้ใช้: {target_user_id}", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    # 1. ข้อมูล Checkpoint ในระบบ Local
    cp = get_user_checkpoint(target_user_id)
    if cp:
        print("💾 [ข้อมูล Checkpoint ใน Local Storage (clean_chat_state.json)]:", flush=True)
        print(f"  • สถานะ: {cp.get('status')}", flush=True)
        print(f"  • Message ID สูงสุดที่บันทึก: {cp.get('max_id', 0):,}", flush=True)
        print(f"  • Message ID สูงสุดที่เคยสแกนเสร็จ: {cp.get('last_scanned_max_id', 0):,}", flush=True)
        print(f"  • สแกนลงมาถึง ID: {cp.get('scanned_down_to_id', 0):,}", flush=True)
        print(f"  • จำนวนข้อความบอทที่ลบสำเร็จ: {cp.get('deleted_count', 0):,} ข้อความ", flush=True)
        print(f"  • วันเวลาที่สแกนครบ: {cp.get('completed_at')}", flush=True)
    else:
        print("💾 [ข้อมูล Checkpoint]: ยังไม่มีประวัติ Checkpoint ของผู้ใช้นี้", flush=True)

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)

    # 2. ตรวจสอบสดกับเซิร์ฟเวอร์ Telegram Cloud
    print("📡 [ผลการตรวจสอบสดกับ Telegram Cloud Server]:", flush=True)
    try:
        probe = await bot.send_message(chat_id=target_user_id, text=".")
        max_id = probe.message_id
        try:
            await bot.delete_message(chat_id=target_user_id, message_id=max_id)
        except Exception:
            pass
        print(f"  • Message ID ล่าสุดบน Cloud Server: ID {max_id:,}", flush=True)
        print("  • การเชื่อมต่อกับห้องแชท: ✅ เชื่อมต่อได้ปกติ (ไม่ได้บล็อกบอท)", flush=True)
    except TelegramForbiddenError:
        print("  • การเชื่อมต่อ: ⚠️ ผู้ใช้บล็อกบอท (Blocked)", flush=True)
        max_id = 0
    except Exception as e:
        print(f"  • การเชื่อมต่อ: ❌ เกิดข้อผิดพลาด -> {e}", flush=True)
        max_id = 0

    # 3. สุ่มตรวจ 100 ข้อความล่าสุดว่ามีข้อความบอทตกค้างหรือไม่
    if max_id > 1:
        print(f"🔍 ทดสอบเจาะจงลบย้อนหลัง 100 Message IDs (ตั้งแต่ ID {max_id - 1:,} ลงไป)...", flush=True)
        deleted_count = 0
        cant_delete_count = 0
        not_found_count = 0
        lock = asyncio.Lock()
        
        queue = asyncio.Queue()
        for mid in range(max_id - 1, max(0, max_id - 101), -1):
            queue.put_nowait(mid)
            
        async def worker():
            nonlocal deleted_count, cant_delete_count, not_found_count
            while not queue.empty():
                try:
                    mid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await bot.delete_message(chat_id=target_user_id, message_id=mid)
                    async with lock:
                        deleted_count += 1
                except TelegramBadRequest as e:
                    msg = e.message.lower()
                    async with lock:
                        if "message to delete not found" in msg:
                            not_found_count += 1
                        elif "can't be deleted" in msg or "cannot be deleted" in msg:
                            cant_delete_count += 1
                except Exception:
                    pass
                queue.task_done()
                await asyncio.sleep(0.015)
                
        workers = [asyncio.create_task(worker()) for _ in range(6)]
        await asyncio.gather(*workers)
                
        print(f"  • ข้อความของบอทที่พบและลบได้: {deleted_count} ข้อความ", flush=True)
        print(f"  • ข้อความที่ไม่มีในระบบแล้ว (ถูกลบเกลี้ยงแล้ว): {not_found_count} ข้อความ", flush=True)
        print(f"  • ข้อความฝั่งผู้ใช้ (บอทไม่มีสิทธิ์ลบ): {cant_delete_count} ข้อความ", flush=True)
        
        if deleted_count == 0:
            print("  ✨ สรุปผลความสะอาด: ห้องแชทสะอาด 100% (ไม่มีข้อความของบอทหลงเหลืออยู่บนเซิร์ฟเวอร์)", flush=True)
        else:
            print(f"  🧹 สรุปผลความสะอาด: พบข้อความตกค้างและลบออกเพิ่มอีก {deleted_count} ข้อความ", flush=True)

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    await bot.session.close()

if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 8869252777
    asyncio.run(check_local(uid))
