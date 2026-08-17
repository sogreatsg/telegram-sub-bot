#!/usr/bin/env python3
"""
Script to generate bot/version.json with the latest git commit information.
Used automatically by deploy.sh or run manually.
"""
import json
import subprocess
import os
from pathlib import Path


def generate_version():
    root_dir = Path(__file__).parent.parent
    version_file = root_dir / "bot" / "version.json"

    c_hash = "Unknown"
    c_date = "Unknown"
    c_msg = "Unknown"
    recent = []

    try:
        # 1. Commit Hash
        h_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if h_res.returncode == 0 and h_res.stdout.strip():
            c_hash = h_res.stdout.strip()

        # 2. Date
        d_res = subprocess.run(
            ["git", "log", "-1", "--format=%ad", "--date=format:%d/%m/%Y %H:%M"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if d_res.returncode == 0 and d_res.stdout.strip():
            c_date = d_res.stdout.strip()

        # 3. Message
        m_res = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if m_res.returncode == 0 and m_res.stdout.strip():
            c_msg = m_res.stdout.strip()

        # 4. Recent 5 logs
        l_res = subprocess.run(
            ["git", "log", "-n", "5", "--format=%h|%ad|%s", "--date=format:%d/%m/%Y %H:%M"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if l_res.returncode == 0 and l_res.stdout.strip():
            for line in l_res.stdout.strip().split("\n"):
                p = line.split("|", 2)
                if len(p) >= 3:
                    recent.append(f"• <code>{p[0]}</code>: {p[2]} (<i>{p[1]}</i>)")
    except Exception as e:
        print(f"Warning: Git command failed: {e}")

    # If git was not available, try reading existing version.json
    if c_hash == "Unknown" and version_file.exists():
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                old_data = json.load(f)
                c_hash = old_data.get("commit", c_hash)
                c_date = old_data.get("date", c_date)
                c_msg = old_data.get("message", c_msg)
                recent = old_data.get("recent_logs", recent)
        except Exception:
            pass

    data = {
        "commit": c_hash,
        "date": c_date,
        "message": c_msg,
        "recent_logs": recent,
    }

    version_file.parent.mkdir(parents=True, exist_ok=True)
    with open(version_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    try:
        print(f"[VERSION] Generated {version_file} successfully: {c_hash} ({c_date}) - {c_msg}")
    except Exception:
        pass


if __name__ == "__main__":
    generate_version()
