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


def split_text_chunks(text: str, max_chunk_size: int = 3800) -> list[str]:
    """ตัดข้อความยาวออกเป็น chunks เพื่อส่งใน Telegram (ไม่ให้เกิน limit 4096 ตัวอักษร)"""
    if not text:
        return []
    if len(text) <= max_chunk_size:
        return [text]

    chunks = []
    current_chunk = []
    current_length = 0

    lines = text.split("\n\n")
    for block in lines:
        block_len = len(block) + 2
        if current_length + block_len > max_chunk_size:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0

            if len(block) > max_chunk_size:
                sub_lines = block.split("\n")
                sub_chunk = []
                sub_len = 0
                for s_line in sub_lines:
                    s_len = len(s_line) + 1
                    if sub_len + s_len > max_chunk_size:
                        if sub_chunk:
                            chunks.append("\n".join(sub_chunk))
                            sub_chunk = []
                            sub_len = 0
                    sub_chunk.append(s_line)
                    sub_len += s_len
                if sub_chunk:
                    chunks.append("\n".join(sub_chunk))
            else:
                current_chunk.append(block)
                current_length += block_len
        else:
            current_chunk.append(block)
            current_length += block_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks
