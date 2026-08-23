import json
import os
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOTIFICATION_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "notification_settings.json")


def get_notification_settings() -> Dict[str, Any]:
    """ดึงการตั้งค่าระบบแจ้งเตือนแอดมินจากไฟล์ JSON"""
    if not os.path.exists(NOTIFICATION_SETTINGS_FILE):
        return {
            "unanswered_dm_reminder_active": True,
        }
    try:
        with open(NOTIFICATION_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "unanswered_dm_reminder_active" not in data:
                data["unanswered_dm_reminder_active"] = True
            return data
    except Exception:
        return {
            "unanswered_dm_reminder_active": True,
        }


def save_notification_settings(settings: Dict[str, Any]) -> None:
    """บันทึกการตั้งค่าระบบแจ้งเตือนแอดมินลงไฟล์ JSON"""
    os.makedirs(os.path.dirname(NOTIFICATION_SETTINGS_FILE), exist_ok=True)
    with open(NOTIFICATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


def update_unanswered_dm_reminder_setting(is_active: bool) -> None:
    """อัปเดตสถานะเปิด/ปิด การแจ้งเตือนข้อความค้างตอบ (Unanswered DM Reminder)"""
    settings = get_notification_settings()
    settings["unanswered_dm_reminder_active"] = is_active
    save_notification_settings(settings)


def is_unanswered_dm_reminder_active() -> bool:
    """ตรวจสอบว่าระบบแจ้งเตือนข้อความค้างตอบ (Unanswered DM Reminder) เปิดใช้งานอยู่หรือไม่"""
    return bool(get_notification_settings().get("unanswered_dm_reminder_active", True))
