#!/usr/bin/env python3
"""
Automated Safe SQLite Database Backup Script.
Uses SQLite Online Backup API (Safe to run while bot is actively reading/writing in WAL mode).
"""

import os
import sys
import sqlite3
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bot.db"
BACKUP_DIR = BASE_DIR / "backups"
MAX_BACKUPS_TO_KEEP = 14  # Keep last 14 backup snapshots
BANGKOK_TZ = timezone(timedelta(hours=7))


def backup_database() -> Path:
    """Create a consistent online snapshot of the database."""
    if not DB_PATH.exists():
        print(f"[ERROR] Database file not found at: {DB_PATH}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(BANGKOK_TZ).strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"bot_backup_{timestamp}.db"

    print(f"[INFO] Starting SQLite online backup from {DB_PATH} to {backup_file}...")

    # Connect to source and destination databases
    src_conn = sqlite3.connect(DB_PATH)
    dst_conn = sqlite3.connect(backup_file)

    with dst_conn:
        # SQLite Online Backup API ensures consistent lock-free snapshot even during WAL writes
        src_conn.backup(dst_conn, pages=100, sleep=0.01)

    dst_conn.close()
    src_conn.close()

    print(f"[SUCCESS] Backup successfully created: {backup_file} ({backup_file.stat().st_size} bytes)")

    # Clean up old backups exceeding MAX_BACKUPS_TO_KEEP
    cleanup_old_backups()
    return backup_file


def cleanup_old_backups() -> None:
    """Remove older backups beyond retention limit."""
    all_backups = sorted(
        BACKUP_DIR.glob("bot_backup_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if len(all_backups) > MAX_BACKUPS_TO_KEEP:
        for old_backup in all_backups[MAX_BACKUPS_TO_KEEP:]:
            try:
                old_backup.unlink()
                print(f"[CLEANUP] Deleted old backup: {old_backup.name}")
            except Exception as e:
                print(f"[WARN] Failed to delete {old_backup.name}: {e}")


if __name__ == "__main__":
    backup_database()
