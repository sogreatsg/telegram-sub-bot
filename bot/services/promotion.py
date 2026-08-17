import json
import os
from typing import Dict, Any

PROMOTION_FILE = "data/promotion.json"

def get_promotion_settings() -> Dict[str, Any]:
    """Load promotion settings from file."""
    if not os.path.exists(PROMOTION_FILE):
        return {
            "is_active": False,
            "days": 0,
            "price": 0,
            "qr_filename": ""
        }
    try:
        with open(PROMOTION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "is_active": False,
            "days": 0,
            "price": 0,
            "qr_filename": ""
        }

def save_promotion_settings(settings: Dict[str, Any]) -> None:
    """Save promotion settings to file."""
    os.makedirs(os.path.dirname(PROMOTION_FILE), exist_ok=True)
    with open(PROMOTION_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

def update_promotion(is_active: bool = None, days: int = None, price: int = None, qr_filename: str = None):
    settings = get_promotion_settings()
    if is_active is not None:
        settings["is_active"] = is_active
    if days is not None:
        settings["days"] = days
    if price is not None:
        settings["price"] = price
    if qr_filename is not None:
        settings["qr_filename"] = qr_filename
    save_promotion_settings(settings)
