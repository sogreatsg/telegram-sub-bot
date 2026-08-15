#!/usr/bin/env bash
# ==============================================================================
# Automated SQLite Backup + Upload to Google Cloud Storage (GCS)
# Add to crontab: 0 3 * * * /path/to/telegram-sub-bot/scripts/backup_gcs.sh
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJECT_DIR/backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/bot_backup_${TIMESTAMP}.db"

# Optional: Set your GCS Bucket name here (e.g., gs://my-bot-backups-bucket/)
GCS_BUCKET="${GCS_BUCKET:-}"

mkdir -p "$BACKUP_DIR"

echo "[INFO] Creating SQLite WAL online backup..."
python3 "$SCRIPT_DIR/backup_db.py"

# Upload to Google Cloud Storage if bucket is configured
if [ -n "$GCS_BUCKET" ]; then
    LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/bot_backup_*.db | head -n 1)
    echo "[INFO] Uploading $LATEST_BACKUP to $GCS_BUCKET..."
    gcloud storage cp "$LATEST_BACKUP" "$GCS_BUCKET" || gsutil cp "$LATEST_BACKUP" "$GCS_BUCKET"
    echo "[SUCCESS] Cloud backup uploaded successfully!"
fi
