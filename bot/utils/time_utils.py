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


def format_user_title(full_name: Optional[str], username: Optional[str], user_id: int, include_id: bool = True) -> str:
    """
    จัดรูปแบบชื่อผู้ใช้ให้อ่านง่าย สม่ำเสมอ และปลอดภัย:
    - ถ้ามี username: '<b>ชื่อ</b> (@username) | ID: <code>123456789</code>'
    - ถ้าไม่มี username: '<b>ชื่อ</b> (ไม่มี Username) | ID: <code>123456789</code>'
    """
    import html as html_lib

    name = (full_name or "").strip()
    if not name or name == f"User {user_id}":
        name_display = f"User {user_id}"
    else:
        name_display = html_lib.escape(name)

    if username and username.strip():
        u_clean = username.strip().lstrip("@")
        handle_display = f"(@{html_lib.escape(u_clean)})"
    else:
        handle_display = "(ไม่มี Username)"

    if include_id:
        return f"<b>{name_display}</b> {handle_display} | ID: <code>{user_id}</code>"
    return f"<b>{name_display}</b> {handle_display}"


def format_remaining_time(expires_at: Optional[datetime]) -> str:
    """แปลงเวลาคงเหลือให้อ่านง่ายเป็นภาษาไทย (ตรงกับใน /summary ทุกประการ)"""
    if expires_at is None:
        return "ไม่มีกำหนด / ยังไม่เริ่มนับ"
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    diff = expires_at - now
    if diff.total_seconds() <= 0:
        return "หมดอายุแล้ว (0 วัน)"
    
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} วัน")
    if hours > 0:
        parts.append(f"{hours} ชม.")
    if minutes > 0 or (days == 0 and hours == 0):
        parts.append(f"{minutes} นาที")
    if days == 0 and hours == 0 and minutes < 5:
        parts.append(f"{seconds} วินาที")
    
    return " ".join(parts)


def parse_duration_input(text: str, allow_zero: bool = False) -> tuple[int, int, str]:
    """
    แปลงข้อความระยะเวลาที่แอดมินระบุ รองรับทั้งวัน, ชั่วโมง และ นาที เช่น:
    - '30' หรือ '30d' หรือ '30D' หรือ '30วัน' -> (30, 0, '30 วัน')
    - '12h' หรือ '12H' หรือ '12ชม' หรือ '12ชั่วโมง' -> (0, 720, '12 ชั่วโมง')
    - '1d 12h' หรือ '1วัน 12ชั่วโมง' หรือ '1d12h' -> (1, 720, '1 วัน 12 ชั่วโมง')
    - '45m' หรือ '45นาที' -> (0, 45, '45 นาที')
    - '0' (เมื่อ allow_zero=True) -> (0, 0, '0 วัน')

    คืนค่า (days, total_minutes, formatted_label)
    """
    import re
    clean = text.strip().lower()
    if not clean:
        raise ValueError("ไม่ได้ระบุระยะเวลา")

    if clean in ("0", "0d", "0h", "0m", "0วัน", "0ชั่วโมง", "0ชม", "0นาที"):
        if allow_zero:
            return 0, 0, "0 วัน"
        raise ValueError("ระยะเวลาต้องมากกว่า 0")

    # กรณีเป็นตัวเลขจำนวนเต็มล้วน เช่น '30' หรือ '7' -> ถือว่าเป็นจำนวนวัน
    if clean.isdigit():
        d = int(clean)
        if d <= 0 and not allow_zero:
            raise ValueError("จำนวนวันต้องมากกว่า 0")
        return d, 0, f"{d} วัน"

    # ใช้ Regex ตรวจหา patterns วัน, ชั่วโมง, นาที
    pattern = re.compile(
        r'(?:(?P<days>\d+)\s*(?:d|day|days|วัน))|'
        r'(?:(?P<hours>\d+)\s*(?:h|hr|hrs|hour|hours|ชม|ชั่วโมง))|'
        r'(?:(?P<minutes>\d+)\s*(?:m|min|mins|minute|minutes|นาที))',
        re.IGNORECASE
    )

    days = 0
    total_minutes = 0
    matches = list(pattern.finditer(clean))

    if not matches:
        raise ValueError(
            f"รูปแบบระยะเวลาไม่ถูกต้อง: '{text}'\n"
            "ตัวอย่างที่ถูกต้อง: <code>30</code>, <code>30d</code>, <code>12h</code>, <code>1d 12h</code>, <code>30วัน</code>"
        )

    for match in matches:
        if match.group('days'):
            days += int(match.group('days'))
        if match.group('hours'):
            total_minutes += int(match.group('hours')) * 60
        if match.group('minutes'):
            total_minutes += int(match.group('minutes'))

    if days == 0 and total_minutes == 0 and not allow_zero:
        raise ValueError("ระยะเวลาต้องมากกว่า 0")

    parts = []
    if days > 0:
        parts.append(f"{days} วัน")
    if total_minutes > 0:
        hrs = total_minutes // 60
        mins = total_minutes % 60
        if hrs > 0:
            parts.append(f"{hrs} ชั่วโมง")
        if mins > 0:
            parts.append(f"{mins} นาที")

    label = " ".join(parts) if parts else ("0 วัน" if allow_zero else "0 นาที")
    return days, total_minutes, label
