import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

STATE_FILE_PATH = os.path.join("data", "clean_chat_state.json")


def _get_default_state() -> Dict[str, Any]:
    return {
        "session": {
            "is_running": False,
            "started_at": None,
            "total_users": 0,
            "current_user_index": 0,
            "current_user_id": None,
            "current_user_name": None,
            "total_msgs_deleted": 0,
            "updated_at": None,
        },
        "users": {},  # str(user_id) -> user checkpoint dict
    }


def get_clean_state() -> Dict[str, Any]:
    """โหลด State การลบข้อความจากไฟล์ JSON"""
    if not os.path.exists(STATE_FILE_PATH):
        return _get_default_state()
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "session" not in data:
                data["session"] = _get_default_state()["session"]
            if "users" not in data:
                data["users"] = {}
            return data
    except Exception as e:
        logger.error(f"Error reading clean_chat_state.json: {e}")
        return _get_default_state()


def save_clean_state(state: Dict[str, Any]) -> None:
    """บันทึก State การลบข้อความลงไฟล์ JSON แบบ Atomic Safe"""
    try:
        os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
        tmp_path = f"{STATE_FILE_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        if os.path.exists(STATE_FILE_PATH):
            os.replace(tmp_path, STATE_FILE_PATH)
        else:
            os.rename(tmp_path, STATE_FILE_PATH)
    except Exception as e:
        logger.error(f"Error saving clean_chat_state.json: {e}")


def get_user_checkpoint(user_id: int) -> Optional[Dict[str, Any]]:
    """ดึงข้อมูล Checkpoint การลบข้อความของ User รายคน"""
    state = get_clean_state()
    return state.get("users", {}).get(str(user_id))


def is_user_clean_completed(user_id: int, current_max_id: Optional[int] = None) -> bool:
    """
    ตรวจสอบว่า User คนนี้สแกนและลบข้อความเสร็จสิ้นครบถ้วนแล้วหรือไม่
    หากมีการระบุ current_max_id: ถ้า current_max_id <= last_scanned_max_id แปลว่าไม่มีข้อความใหม่เพิ่มขึ้น -> เสร็จสมบูรณ์แล้ว
    """
    cp = get_user_checkpoint(user_id)
    if not cp:
        return False
    if cp.get("status") != "COMPLETED":
        return False
    if current_max_id is not None:
        last_max = cp.get("last_scanned_max_id", 0) or cp.get("max_id", 0)
        return current_max_id <= last_max
    return True


def update_user_checkpoint(
    user_id: int,
    max_id: int,
    scanned_down_to_id: int,
    deleted_count: int,
    status: str = "IN_PROGRESS",
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    deleted_ids: Optional[list] = None,
) -> None:
    """อัปเดต Checkpoint ตำแหน่ง Message ID ที่สแกนถึงสำหรับ User รายคน"""
    state = get_clean_state()
    now_str = datetime.now(timezone.utc).isoformat()
    uid_str = str(user_id)

    if uid_str not in state["users"]:
        state["users"][uid_str] = {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "max_id": max_id,
            "last_scanned_max_id": max_id if status == "COMPLETED" else 0,
            "scanned_down_to_id": scanned_down_to_id,
            "deleted_count": deleted_count,
            "deleted_ids": deleted_ids or [],
            "status": status,
            "started_at": now_str,
            "updated_at": now_str,
            "completed_at": now_str if status == "COMPLETED" else None,
        }
    else:
        u_data = state["users"][uid_str]
        u_data["max_id"] = max(u_data.get("max_id", 0), max_id)
        u_data["scanned_down_to_id"] = scanned_down_to_id
        u_data["deleted_count"] = deleted_count
        u_data["status"] = status
        u_data["updated_at"] = now_str
        if username:
            u_data["username"] = username
        if full_name:
            u_data["full_name"] = full_name
        if deleted_ids is not None:
            u_data["deleted_ids"] = deleted_ids
        if status == "COMPLETED":
            u_data["last_scanned_max_id"] = max(u_data.get("last_scanned_max_id", 0), max_id)
            u_data["completed_at"] = now_str

    save_clean_state(state)


def update_session_state(
    is_running: bool,
    total_users: int = 0,
    current_user_index: int = 0,
    current_user_id: Optional[int] = None,
    current_user_name: Optional[str] = None,
    total_msgs_deleted: int = 0,
) -> None:
    """อัปเดตสถานะ Session การกวาดล้างภาพรวม"""
    state = get_clean_state()
    now_str = datetime.now(timezone.utc).isoformat()
    sess = state["session"]

    sess["is_running"] = is_running
    sess["updated_at"] = now_str
    if total_users > 0:
        sess["total_users"] = total_users
    if current_user_index > 0:
        sess["current_user_index"] = current_user_index
    if current_user_id is not None:
        sess["current_user_id"] = current_user_id
    if current_user_name is not None:
        sess["current_user_name"] = current_user_name
    if total_msgs_deleted > 0:
        sess["total_msgs_deleted"] = total_msgs_deleted
    if is_running and not sess.get("started_at"):
        sess["started_at"] = now_str
    elif not is_running:
        sess["started_at"] = None

    save_clean_state(state)


def reset_for_incremental_rescan() -> None:
    """
    รีเซ็ตสถานะเพื่อให้รันสแกนรอบใหม่ โดยยังคงจำ last_scanned_max_id เดิมไว้
    ทำให้รอบใหม่จะสแกนเฉพาะข้อความช่วงที่เพิ่มขึ้นมาใหม่ (Delta Scan) เท่านั้น
    """
    state = get_clean_state()
    state["session"] = _get_default_state()["session"]
    for u in state.get("users", {}).values():
        if u.get("status") == "COMPLETED":
            u["status"] = "INCREMENTAL_READY"
            u["scanned_down_to_id"] = u.get("last_scanned_max_id", 0)
        else:
            u["status"] = "IN_PROGRESS"
    save_clean_state(state)


def reset_all_clean_checkpoints() -> None:
    """รีเซ็ตประวัติ Checkpoint ทั้งหมดแบบสมบูรณ์ 100% (ล้างเพื่อเริ่มจาก 0 ใหม่ทั้งหมด)"""
    state = _get_default_state()
    save_clean_state(state)


def get_clean_status_summary() -> Dict[str, Any]:
    """ดึงข้อมูลสรุปสถานะการล้างแชททั้งหมด"""
    state = get_clean_state()
    users = state.get("users", {})
    session = state.get("session", {})

    completed_users = [u for u in users.values() if u.get("status") == "COMPLETED"]
    in_progress_users = [u for u in users.values() if u.get("status") in ("IN_PROGRESS", "INCREMENTAL_READY")]
    blocked_users = [u for u in users.values() if u.get("status") == "BLOCKED_BOT"]
    total_deleted_msgs = sum(u.get("deleted_count", 0) for u in users.values())

    return {
        "session": session,
        "total_tracked_users": len(users),
        "completed_count": len(completed_users),
        "in_progress_count": len(in_progress_users),
        "blocked_count": len(blocked_users),
        "total_deleted_msgs": total_deleted_msgs,
        "completed_users": completed_users,
        "in_progress_users": in_progress_users,
    }
