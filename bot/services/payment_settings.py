import json
import os
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAYMENT_SETTINGS_FILE = os.path.join(BASE_DIR, "data", "payment_settings.json")


def get_payment_settings() -> Dict[str, Any]:
    """ดึงการตั้งค่าช่องทางชำระเงินจากไฟล์ JSON"""
    if not os.path.exists(PAYMENT_SETTINGS_FILE):
        return {
            "promptpay_active": True,
            "truemoney_active": True,
            "auto_approve_truemoney": False,
        }
    try:
        with open(PAYMENT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "promptpay_active" not in data:
                data["promptpay_active"] = True
            if "truemoney_active" not in data:
                data["truemoney_active"] = True
            if "auto_approve_truemoney" not in data:
                data["auto_approve_truemoney"] = False
            return data
    except Exception:
        return {
            "promptpay_active": True,
            "truemoney_active": True,
            "auto_approve_truemoney": False,
        }


def save_payment_settings(settings: Dict[str, Any]) -> None:
    """บันทึกการตั้งค่าช่องทางชำระเงินลงไฟล์ JSON"""
    os.makedirs(os.path.dirname(PAYMENT_SETTINGS_FILE), exist_ok=True)
    with open(PAYMENT_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)


def is_promptpay_active() -> bool:
    """ตรวจสอบว่าการชำระเงินผ่าน QR Code (PromptPay) เปิดใช้งานอยู่หรือไม่"""
    return bool(get_payment_settings().get("promptpay_active", True))


def update_promptpay_setting(is_active: bool) -> None:
    """เปิด/ปิด การชำระเงินผ่าน QR Code (PromptPay)"""
    settings = get_payment_settings()
    settings["promptpay_active"] = is_active
    save_payment_settings(settings)


def is_truemoney_active() -> bool:
    """ตรวจสอบว่าการชำระเงินผ่านซองของขวัญ TrueMoney เปิดใช้งานอยู่หรือไม่"""
    return bool(get_payment_settings().get("truemoney_active", True))


def update_truemoney_setting(is_active: bool) -> None:
    """เปิด/ปิด การชำระเงินผ่านซองของขวัญ TrueMoney"""
    settings = get_payment_settings()
    settings["truemoney_active"] = is_active
    save_payment_settings(settings)


def is_auto_approve_active() -> bool:
    """ตรวจสอบว่าระบบอนุมัติอัตโนมัติเมื่อผู้ใช้ส่งซอง TrueMoney เปิดใช้งานอยู่หรือไม่"""
    return bool(get_payment_settings().get("auto_approve_truemoney", False))


def update_auto_approve_setting(is_active: bool) -> None:
    """เปิด/ปิด ระบบอนุมัติอัตโนมัติเมื่อผู้ใช้ส่งซอง TrueMoney"""
    settings = get_payment_settings()
    settings["auto_approve_truemoney"] = is_active
    save_payment_settings(settings)


def is_auto_approve_truemoney_active() -> bool:
    """ตรวจสอบว่าระบบอนุมัติอัตโนมัติสำหรับซอง TrueMoney เปิดใช้งานอยู่หรือไม่ (Alias)"""
    return is_auto_approve_active()


def update_auto_approve_truemoney_setting(is_active: bool) -> None:
    """เปิด/ปิด ระบบอนุมัติอัตโนมัติสำหรับซอง TrueMoney (Alias)"""
    update_auto_approve_setting(is_active)

