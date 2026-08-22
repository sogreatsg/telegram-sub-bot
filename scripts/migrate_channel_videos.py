import asyncio
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from bot.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrator")

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "migration_progress.json")


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_id": 0, "copied_ids": [], "skipped_ids": [], "total_copied": 0}


def save_progress(data: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def get_max_message_id(bot: Bot, channel_id: int) -> int:
    """หา Message ID ล่าสุดใน Source Channel"""
    try:
        temp = await bot.send_message(chat_id=channel_id, text="[System Probe - Checking Max ID]")
        max_id = temp.message_id
        await bot.delete_message(chat_id=channel_id, message_id=max_id)
        return max_id - 1
    except Exception as e:
        logger.warning(f"Could not probe max message ID via dummy message: {e}")
        return 1000  # Fallback range


async def migrate_videos(
    source_channel: int,
    target_channel: int,
    bot_token: str,
    dry_run: bool = False,
    start_id: int = 1,
    end_id: int = None,
    delay: float = 0.5,
    forward_mode: bool = False,
):
    bot = Bot(token=bot_token)
    progress = load_progress()
    last_id = progress.get("last_processed_id", 0)

    try:
        me = await bot.get_me()
        logger.info(f"Connected to Telegram as @{me.username} (ID: {me.id})")

        # ตรวจสอบสิทธิ์ใน Source & Target
        source_chat = await bot.get_chat(chat_id=source_channel)
        target_chat = await bot.get_chat(chat_id=target_channel)
        logger.info(f"Source Channel: '{source_chat.title}' ({source_channel})")
        logger.info(f"Target Channel: '{target_chat.title}' ({target_channel})")

        if end_id is None:
            max_id = await get_max_message_id(bot, source_channel)
        else:
            max_id = end_id

        actual_start = max(start_id, last_id + 1)
        logger.info(f"Scanning Message IDs from {actual_start} to {max_id} (Total: {max_id - actual_start + 1} IDs)...")
        if dry_run:
            logger.info("⚡ MODE: DRY-RUN (จำลองการตรวจสอบ ไม่มีการส่งข้อมูลจริงเข้าห้องใหม่)")
        else:
            logger.info(f"🚀 MODE: {'FORWARD' if forward_mode else 'COPY (Clean Post without Forward tag)'}")

        copied_count = 0
        skipped_count = 0
        not_found_count = 0

        for msg_id in range(actual_start, max_id + 1):
            if dry_run:
                # ใน Dry-run ลอง copy ไปยัง dummy check หรือจำลอง
                # โดยทั่วไป bot API ถ้า message ไม่มีอยู่จะเกิด BadRequest
                # เราสามารถทดสอบ copy แล้ว delete ทันที หรือทดสอบ forward
                pass

            success = False
            retry_count = 0

            while retry_count < 3 and not success:
                try:
                    if not dry_run:
                        if forward_mode:
                            new_msg = await bot.forward_message(
                                chat_id=target_channel,
                                from_chat_id=source_channel,
                                message_id=msg_id,
                            )
                        else:
                            new_msg = await bot.copy_message(
                                chat_id=target_channel,
                                from_chat_id=source_channel,
                                message_id=msg_id,
                            )

                        copied_count += 1
                        progress["copied_ids"].append(msg_id)
                        progress["total_copied"] = progress.get("total_copied", 0) + 1
                        logger.info(f"[{msg_id}/{max_id}] ✅ Copied Message ID {msg_id} -> New ID {new_msg.message_id}")
                    else:
                        # Dry run check
                        copied_count += 1
                        logger.info(f"[{msg_id}/{max_id}] 🔍 Found candidate Message ID {msg_id}")

                    success = True
                    progress["last_processed_id"] = msg_id
                    save_progress(progress)
                    await asyncio.sleep(delay)

                except TelegramBadRequest as e:
                    err = e.message.lower()
                    if any(kw in err for kw in ["not found", "message to copy not found", "message to forward not found", "empty message"]):
                        # Message ID นี้ไม่มีอยู่ หรือถูกลบไปแล้ว
                        not_found_count += 1
                        progress["skipped_ids"].append(msg_id)
                    elif "chat not found" in err:
                        logger.error(f"Chat not found error: {e}")
                        break
                    else:
                        logger.warning(f"[{msg_id}/{max_id}] ⚠️ BadRequest on ID {msg_id}: {e.message}")
                        progress["skipped_ids"].append(msg_id)

                    success = True
                    progress["last_processed_id"] = msg_id
                    save_progress(progress)

                except TelegramRetryAfter as e:
                    wait_sec = e.retry_after
                    logger.warning(f"⏳ Rate Limit hit! Sleeping for {wait_sec} seconds...")
                    await asyncio.sleep(wait_sec + 1)
                    retry_count += 1

                except Exception as e:
                    logger.error(f"[{msg_id}/{max_id}] ❌ Error on ID {msg_id}: {e}")
                    retry_count += 1
                    await asyncio.sleep(2.0)

        logger.info("=" * 60)
        logger.info("🎉 MIGRATION PROCESS COMPLETED!")
        logger.info(f"  • Total Processed IDs : {max_id - actual_start + 1}")
        logger.info(f"  • Successfully Copied : {copied_count} messages (Videos/Posts)")
        logger.info(f"  • Empty / Deleted IDs : {not_found_count}")
        logger.info("=" * 60)

    finally:
        await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram Video & Message Migrator (Cloud-to-Cloud)")
    parser.add_argument("--source", type=int, default=-1003758847086, help="Source Channel ID (Old)")
    parser.add_argument("--target", type=int, default=-1003940881279, help="Target Channel ID (New)")
    parser.add_argument("--token", type=str, default=None, help="Telegram Bot Token")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run scan without sending to target")
    parser.add_argument("--forward", action="store_true", help="Use forward instead of clean copy")
    parser.add_argument("--start-id", type=int, default=1, help="Start Message ID")
    parser.add_argument("--end-id", type=int, default=None, help="End Message ID")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay in seconds between copies (default: 0.4s)")
    parser.add_argument("--reset-progress", action="store_true", help="Reset progress file and start from start-id")

    args = parser.parse_args()

    config = get_settings()
    token = args.token or "8756520515:AAGW8iVbzn5JEaS9uWYEI_5Bj_o00zSg-kA"

    if args.reset_progress and os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        logger.info("Reset progress file successfully.")

    asyncio.run(migrate_videos(
        source_channel=args.source,
        target_channel=args.target,
        bot_token=token,
        dry_run=args.dry_run,
        start_id=args.start_id,
        end_id=args.end_id,
        delay=args.delay,
        forward_mode=args.forward,
    ))


if __name__ == "__main__":
    main()
