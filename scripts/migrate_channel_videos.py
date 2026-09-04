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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KNOWN_CHANNELS = {
    "v1": -1003758847086,
    "v2": -1003940881279,
    "v3": -1003900142712,
}


def resolve_channel_id(val: str | int, config_settings) -> int:
    s = str(val).strip().lower()
    if s == "v1":
        return config_settings.CHANNEL_ID or KNOWN_CHANNELS["v1"]
    if s == "v2":
        return config_settings.SECONDARY_CHANNEL_ID or KNOWN_CHANNELS["v2"]
    if s == "v3":
        return config_settings.TERTIARY_CHANNEL_ID or KNOWN_CHANNELS["v3"]
    try:
        return int(val)
    except ValueError:
        return config_settings.CHANNEL_ID or KNOWN_CHANNELS["v1"]


def get_progress_file_path(source_channel: int, target_channel: int, custom_path: str = None) -> str:
    if custom_path:
        return os.path.abspath(custom_path)
    
    src_label = "v1" if source_channel == KNOWN_CHANNELS["v1"] else ("v2" if source_channel == KNOWN_CHANNELS["v2"] else str(source_channel).replace("-", ""))
    dst_label = "v3" if target_channel == KNOWN_CHANNELS["v3"] else ("v2" if target_channel == KNOWN_CHANNELS["v2"] else str(target_channel).replace("-", ""))
    
    if src_label == "v1" and dst_label == "v2" and os.path.exists(os.path.join(BASE_DIR, "migration_progress.json")):
        return os.path.join(BASE_DIR, "migration_progress.json")
    
    return os.path.join(BASE_DIR, f"migration_progress_{src_label}_to_{dst_label}.json")


def load_progress(progress_file: str) -> dict:
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_id": 0, "copied_ids": [], "skipped_ids": [], "total_copied": 0}


def save_progress(progress_file: str, data: dict):
    with open(progress_file, "w", encoding="utf-8") as f:
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
    custom_progress_file: str = None,
):
    bot = Bot(token=bot_token)
    progress_file = get_progress_file_path(source_channel, target_channel, custom_progress_file)
    progress = load_progress(progress_file)
    last_id = progress.get("last_processed_id", 0)

    try:
        me = await bot.get_me()
        logger.info(f"Connected to Telegram as @{me.username} (ID: {me.id})")

        # ตรวจสอบสิทธิ์ใน Source & Target
        source_chat = await bot.get_chat(chat_id=source_channel)
        target_chat = await bot.get_chat(chat_id=target_channel)
        logger.info(f"Source Channel: '{source_chat.title}' ({source_channel})")
        logger.info(f"Target Channel: '{target_chat.title}' ({target_channel})")
        logger.info(f"Progress File : {progress_file}")

        if end_id is None:
            max_id = await get_max_message_id(bot, source_channel)
        else:
            max_id = end_id

        actual_start = max(start_id, last_id + 1)
        total_range = max(0, max_id - actual_start + 1)
        logger.info(f"Scanning Message IDs from {actual_start} to {max_id} (Total: {total_range} IDs)...")
        if dry_run:
            logger.info("⚡ MODE: DRY-RUN (จำลองการตรวจสอบ ไม่มีการส่งข้อมูลจริงเข้าห้องใหม่)")
        else:
            logger.info(f"🚀 MODE: {'FORWARD' if forward_mode else 'COPY (Clean Post without Forward tag)'}")

        if total_range <= 0:
            logger.info("✅ All messages up to max ID have already been processed!")
            return

        copied_count = 0
        skipped_count = 0
        not_found_count = 0

        for msg_id in range(actual_start, max_id + 1):
            if dry_run:
                copied_count += 1
                logger.info(f"[{msg_id}/{max_id}] 🔍 [DRY-RUN] Found candidate Message ID {msg_id}")
                await asyncio.sleep(0.01)
                continue

            success = False
            retry_count = 0

            while retry_count < 3 and not success:
                try:
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

                    success = True
                    progress["last_processed_id"] = msg_id
                    save_progress(progress_file, progress)
                    await asyncio.sleep(delay)

                except TelegramBadRequest as e:
                    err = e.message.lower()
                    if any(kw in err for kw in ["not found", "message to copy not found", "message to forward not found", "empty message"]):
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
                    save_progress(progress_file, progress)

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
        logger.info(f"  • Total Copied All Time: {progress.get('total_copied', copied_count)}")
        logger.info("=" * 60)

    finally:
        await bot.session.close()


def main():
    config = get_settings()
    parser = argparse.ArgumentParser(description="Telegram Video & Message Migrator (Cloud-to-Cloud)")
    parser.add_argument("--source", type=str, default="v1", help="Source Channel ('v1', 'v2', 'v3' or channel ID)")
    parser.add_argument("--target", type=str, default="v3", help="Target Channel ('v1', 'v2', 'v3' or channel ID)")
    parser.add_argument("--token", type=str, default=None, help="Telegram Bot Token (default from .env)")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run scan without sending to target")
    parser.add_argument("--forward", action="store_true", help="Use forward instead of clean copy")
    parser.add_argument("--start-id", type=int, default=1, help="Start Message ID")
    parser.add_argument("--end-id", type=int, default=None, help="End Message ID")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay in seconds between copies (default: 0.4s)")
    parser.add_argument("--progress-file", type=str, default=None, help="Custom progress file path")
    parser.add_argument("--reset-progress", action="store_true", help="Reset progress file and start from start-id")

    args = parser.parse_args()

    token = args.token or config.BOT_TOKEN

    source_cid = resolve_channel_id(args.source, config)
    target_cid = resolve_channel_id(args.target, config)

    prog_file = get_progress_file_path(source_cid, target_cid, args.progress_file)

    if args.reset_progress and os.path.exists(prog_file):
        os.remove(prog_file)
        logger.info(f"Reset progress file successfully: {prog_file}")

    asyncio.run(migrate_videos(
        source_channel=source_cid,
        target_channel=target_cid,
        bot_token=token,
        dry_run=args.dry_run,
        start_id=args.start_id,
        end_id=args.end_id,
        delay=args.delay,
        forward_mode=args.forward,
        custom_progress_file=prog_file,
    ))


if __name__ == "__main__":
    main()
