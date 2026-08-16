from datetime import datetime, timezone, timedelta
from typing import Optional

BANGKOK_TZ = timezone(timedelta(hours=7))

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """แปลงเวลาให้เป็น UTC ที่มี timezone เสมอ (ป้องกัน TypeError ระหว่าง naive และ aware datetimes)"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def format_thai_datetime(dt: Optional[datetime]) -> str:
    """แปลงเวลาเป็นเวลาไทย (UTC+7) รูปแบบ วัน/เดือน/ปี ชั่วโมง:นาที:วินาที"""
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    thai_dt = dt.astimezone(BANGKOK_TZ)
    return thai_dt.strftime("%d/%m/%Y %H:%M:%S")
