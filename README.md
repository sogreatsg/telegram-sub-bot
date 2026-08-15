# 🤖 Telegram Channel Membership Management Bot

Production-ready Telegram Membership Bot built with **Python 3.11+**, **aiogram 3.x**, **SQLAlchemy 2.0 (Async)**, **SQLite (aiosqlite with WAL mode)**, **APScheduler**, and **Docker Compose** optimized for deployment on Google Compute Engine (GCE).

---

## 📋 Features

1. **⏱️ 15-Minute Free Trial (One-Time per User):**
   - User requests trial via interactive inline menu.
   - Bot issues a private 1-use invite link (`member_limit=1`).
   - The 15-minute countdown starts the moment the user actually joins the channel (`ChatMemberUpdated`).
   - Once expired, the bot soft-kicks (ban + immediate unban) the user and notifies them in DM.
   - Strictly enforced one-time usage per Telegram user ID.

2. **💳 30-Day VIP Paid Subscription:**
   - User initiates subscription and views payment details (PromptPay / Bank Transfer).
   - User submits a photo or document of their payment transfer slip via FSM.
   - Bot immediately forwards the slip to the **Admin Group** with `[✅ Approve (30 Days)]` and `[❌ Reject]` inline buttons.
   - Upon admin approval, the bot automatically generates a 1-use invite link and sends it to the user's DM.
   - 30-day countdown begins when the user enters the channel.

3. **⚙️ Automated Background Expiry Worker:**
   - APScheduler runs asynchronously every 60 seconds.
   - Detects all expired subscriptions (`expires_at <= UTC now`).
   - Executes a soft-kick via Telegram Bot API (`ban_chat_member` + `unban_chat_member`).
   - Updates database state to `KICKED` / `EXPIRED`.
   - Sends a friendly renewal DM to the user.

4. **⚡ High-Performance Async SQLite with WAL Mode & Auto-Backup:**
   - Async session factory using `aiosqlite` and SQLAlchemy 2.0.
   - Configured with `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;` for high concurrent read/write throughput and zero database lockups.
   - Built-in Online Backup system (`scripts/backup_db.py`) capturing snapshots with zero downtime.

---

## 🗂️ Project Directory Layout

```
telegram-sub-bot/
├── bot/
│   ├── __init__.py
│   ├── config.py              # Pydantic Settings loading from .env
│   ├── models/
│   │   ├── __init__.py
│   │   └── schema.py          # SQLAlchemy models (User, Subscription, PaymentSlip)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── database.py        # Async session factory, init_db(), SQLite WAL mode
│   │   └── scheduler.py       # APScheduler job checking & kicking expired members
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── user_menu.py       # /start handler, check trial status, generate trial link
│   │   ├── payment.py         # FSM for payment slip submission, notify Admin group
│   │   ├── admin.py           # CallbackQuery handlers for [Approve] and [Reject]
│   │   └── channel_events.py  # ChatMemberUpdated handler to capture user join events
│   └── main.py                # Bot runner, scheduler attachment, polling startup
├── data/                      # Persistent SQLite DB folder (mounted in Docker)
│   └── bot.db                 # SQLite database file
├── backups/                   # Automated daily backup snapshots (.db)
├── scripts/
│   ├── backup_db.py           # Safe SQLite Online Backup API script
│   └── backup_gcs.sh          # Shell script for Google Cloud Storage sync
├── .env.example               # Environment variables template
├── .env                       # Active environment configuration
├── Dockerfile                 # Multi-stage lightweight Python 3.11-slim image
├── docker-compose.yml         # Container management & volume mappings
├── requirements.txt           # Python dependencies
├── deploy.sh                  # One-click deployment script with auto-cron backup
└── README.md                  # Complete documentation & disaster recovery guide
```

---

## 🛠️ Prerequisites & Telegram Bot Setup

### 1. Create Bot via @BotFather
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the prompts to name your bot and choose a username.
3. Save the **Bot Token** (e.g., `8044549718:AAEHL1Xi2JZ_...`).

### 2. Configure Channel Permissions
1. Create or open your **Private Telegram Channel** (`BareLive`).
2. Go to **Channel Settings** > **Administrators** > **Add Administrator**.
3. Add your bot and ensure the following permissions are **Enabled**:
   - ✅ **Add Members / Invite Users via Links** (Required to create 1-use invite links)
   - ✅ **Ban Users** (Required for soft-kicking expired members)
4. Retrieve the **Channel ID** (e.g. `-1003758847086`).

### 3. Configure Admin Group
1. Create a Telegram Group for admins/staff (`BareLiveAdmin`).
2. Add your bot to the group as an Administrator.
3. Retrieve the **Admin Group ID** (e.g. `-5596543919`).

---

## 🚀 Deployment on Google Compute Engine (GCE)

### Step 1: Create a VM Instance on GCP
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) > **Compute Engine** > **VM instances**.
2. Click **Create Instance**:
   - **Name**: `telegram-sub-bot`
   - **Region**: Choose a region close to your target audience (e.g. `asia-southeast1` หรือ `us-central1`).
   - **Machine Configuration**: `E2` series, machine type `e2-micro` (2 vCPU, 1 GB RAM) or `e2-small`.
   - **Boot disk**: `Ubuntu 24.04 LTS` or `Ubuntu 22.04 LTS`, 10–20 GB Standard Persistent Disk.
   - **Firewall**: Default settings are sufficient. *(No inbound ports needed since the bot uses outbound polling).*
3. Click **Create**.

### Step 2: Connect via SSH & Deploy
1. In the GCP Console, click **SSH** next to your VM instance.
2. Clone or copy your project files into the VM:
   ```bash
   git clone <your-repository-url> telegram-sub-bot
   cd telegram-sub-bot
   ```
3. Run the automated deployment script:
   ```bash
   chmod +x deploy.sh
   ./deploy.sh
   ```
4. Verify or edit your `.env` configuration:
   ```dotenv
   BOT_TOKEN=8044549718:AAEHL1Xi2JZ_2H_f6rpUZRj2Bi_WJYbK_Ok
   CHANNEL_ID=-1003758847086
   ADMIN_GROUP_ID=-5596543919
   DATABASE_URL=sqlite+aiosqlite:///data/bot.db
   PROMPT_PAYMENT_INFO="🏦 ข้อมูลการชำระเงิน VIP:\n• ธนาคาร: กสิกรไทย\n• เลขบัญชี: 081-234-5678\n• ชื่อบัญชี: VIP Support\n• ยอดโอน: 500 บาท / 30 วัน"
   LOG_LEVEL=INFO
   ```
5. Deploy and start the bot:
   ```bash
   ./deploy.sh
   ```

---

## 🛡️ Database Protection, Persistence & Disaster Recovery Plan

### 1. Data Persistence (ทำไมข้อมูลไม่หายเวลา Deploy ใหม่)
- `docker-compose.yml` ผูกไดเรกทอรี `./data` บนเครื่องแม่ (Host) เข้ากับ `/app/data` ภายใน Container
- เมื่อมีการอัปเดตโค้ดหรือรัน `docker compose up -d --build` ระบบจะทำการสร้าง Container ใหม่ **แต่ข้อมูล `data/bot.db` จะไม่ถูกลบและยังคงอยู่บน Host เหมือนเดิม 100%**

### 2. ย้ายข้อมูลจากเครื่อง Local ไปยัง Server (GCE Migration)
เพื่อนำข้อมูลสมาชิกล่าสุดที่รันบน Local ขึ้นไปยัง Server:
```bash
# บนเครื่อง Local คัดลอก data/bot.db ไปยัง VM:
gcloud compute scp data/bot.db telegram-sub-bot:/home/$USER/telegram-sub-bot/data/bot.db --zone=<your-zone>
```

### 3. ระบบ Auto-Backup อัตโนมัติ (Automated Daily Backups)
- สคริปต์ [`scripts/backup_db.py`](file:///C:/Users/sogreatsg/telegram-sub-bot/scripts/backup_db.py) ใช้ **SQLite Online Backup API** สามารถสำรองข้อมูลแบบ Snapshot ขณะบอทกำลังทำงานได้โดยไม่ต้องหยุดการทำงาน (Zero Downtime)
- ไฟล์จะถูกจัดเก็บไว้ในโฟลเดอร์ `./backups/` และลบไฟล์เก่าที่เกิน 14 วันทิ้งให้อัตโนมัติ
- `deploy.sh` จะตั้งเวลา **Cron Job** ให้รันอัตโนมัติทุกวันเวลา 03:00 น. ให้โดยอัตโนมัติ

**คำสั่งรัน Backup ด้วยตนเองเมื่อต้องการ:**
```bash
python3 scripts/backup_db.py
```

### 4. สำรองข้อมูลขึ้น Cloud (Google Cloud Storage - GCS)
เพื่อป้องกันกรณีเครื่อง VM เสียหาย:
1. สร้าง Bucket บน Google Cloud Storage:
   ```bash
   gcloud storage buckets create gs://my-bot-backups-bucket --location=asia-southeast1
   ```
2. แก้ไขไฟล์ `scripts/backup_gcs.sh` ใส่ชื่อ Bucket:
   ```bash
   export GCS_BUCKET="gs://my-bot-backups-bucket"
   ```
3. รันสคริปต์ซิงค์ข้อมูล:
   ```bash
   ./scripts/backup_gcs.sh
   ```

### 5. การอัปเกรดเป็น Cloud Database (PostgreSQL) ในอนาคต
เนื่องจากระบบถูกพัฒนาด้วย **SQLAlchemy 2.0 Async**:
- หากระบบมีผู้ใช้จำนวนมาก และต้องการเชื่อมต่อหลาย Server พร้อมกัน
- สามารถเปลี่ยนไปใช้ **PostgreSQL / Google Cloud SQL / Supabase / Neon** ได้ทันที เพียงเปลี่ยนค่าใน `.env`:
  ```dotenv
  DATABASE_URL=postgresql+asyncpg://user:password@cloud-sql-ip:5432/bot_db
  ```
  โดยไม่ต้องแก้ไขซอร์สโค้ดของบอท

---

## 📊 Management & Useful Commands

| งานที่ต้องการทำ | คำสั่ง |
|---|---|
| **ดู Live Logs แบบ Real-time** | `docker compose logs -f` |
| **ตรวจสอบสถานะ Container** | `docker compose ps` |
| **รีสตาร์ทบอท** | `docker compose restart` |
| **หยุดการทำงานของบอท** | `docker compose down` |
| **อัปเดตโค้ดและ Rebuild** | `docker compose up -d --build` |
| **สั่ง Backup ฐานข้อมูลทันที** | `python3 scripts/backup_db.py` |
| **ดูรายการ Backup ทั้งหมด** | `ls -lh backups/` |

---

## ❓ Troubleshooting & FAQs

#### 1. ทำไมบอทไม่ตรวจจับเมื่อมีคนเข้า Channel?
- ตรวจสอบว่าบอทเป็น **Administrator** ใน Channel VIP จริง
- Event `ChatMemberUpdated` ต้องการให้เปิด `chat_member` ใน `allowed_updates` ซึ่งตั้งค่าไว้เรียบร้อยแล้วใน [`bot/main.py`](file:///C:/Users/sogreatsg/telegram-sub-bot/bot/main.py)

#### 2. บอทไม่สามารถเตะสมาชิกที่หมดเวลาได้
- ตรวจสอบสิทธิ์ของบอทใน Channel ว่าเปิดสิทธิ์ **"Ban Users"** แล้วหรือไม่
- บอทไม่สามารถเตะ Creator หรือ Admin ของ Channel ได้ (จะทำงานกับสมาชิกทั่วไปเท่านั้น)
