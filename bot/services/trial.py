import json
import os
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRIAL_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "trial_settings.json")


def get_trial_settings() -> Dict[str, Any]:
    """ดึงการตั้งค่าระบบทดลองใช้งานฟรีจากไฟล์ JSON"""
    if not os.path.exists(TRIAL_SETTINGS_FILE):
        return {"is_active": False}
    try:
        with open(TRIAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"is_active": False}


def save_trial_settings(settings: Dict[str, Any]) -> None:
    """บันทึกการตั้งค่าระบบทดลองใช้งานฟรีลงไฟล์ JSON"""
    os.makedirs(os.path.dirname(TRIAL_SETTINGS_FILE), exist_ok=True)
    with open(TRIAL_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


def update_trial_settings(is_active: bool = None) -> None:
    """อัปเดตสถานะเปิด/ปิด ระบบทดลองใช้งานฟรี"""
    settings = get_trial_settings()
    if is_active is not None:
        settings["is_active"] = is_active
    save_trial_settings(settings)


def is_trial_active() -> bool:
    """ตรวจสอบว่าระบบทดลองใช้งานฟรีเปิดใช้งานอยู่หรือไม่"""
    return bool(get_trial_settings().get("is_active", False))
