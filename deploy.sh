#!/usr/bin/env bash
# ==============================================================================
# Telegram Membership Bot - Automated GCE / Ubuntu Deployment Script
# Supports: Ubuntu 22.04 LTS / 24.04 LTS on Google Compute Engine (GCE)
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

echo -e "${BLUE}====================================================${NC}"
echo -e "${BLUE}   Telegram Membership Bot - GCE Deployment Setup   ${NC}"
echo -e "${BLUE}====================================================${NC}"

# 1. Verify OS environment
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: $NAME ($VERSION)"
else
    log_warn "Could not detect /etc/os-release. Proceeding with standard Debian/Ubuntu assumptions."
fi

# 2. Install Docker and Docker Compose plugin if missing
if ! command -v docker &> /dev/null; then
    log_info "Docker is not installed. Installing official Docker packages..."
    sudo apt-get update -y
    sudo apt-get install -y ca-certificates curl gnupg lsb-release python3

    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update -y
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Enable and start docker service
    sudo systemctl enable docker
    sudo systemctl start docker

    # Add current user to docker group if not root
    if [ "$USER" != "root" ]; then
        sudo usermod -aG docker "$USER"
        log_warn "Added '$USER' to docker group. You may need to log out and log back in for group changes to take effect without sudo."
    fi
    log_success "Docker & Docker Compose installed successfully."
else
    log_info "Docker is already installed ($(docker --version))."
fi

# 3. Create persistent data and backup directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log_info "Configuring persistent data and backup directories..."
mkdir -p data backups
# Set permissions so non-root container user (UID 1001) can read/write SQLite DB
sudo chmod -R 777 data backups
chmod +x scripts/*.py scripts/*.sh 2>/dev/null || true
log_success "Directories './data' and './backups' are ready."

# 4. Configure Automated Daily Backup Cron Job
CRON_CMD="0 3 * * * python3 $SCRIPT_DIR/scripts/backup_db.py >> $SCRIPT_DIR/backups/backup.log 2>&1"
if command -v crontab &> /dev/null; then
    CURRENT_CRON=$(crontab -l 2>/dev/null || true)
    if [[ "$CURRENT_CRON" != *"$SCRIPT_DIR/scripts/backup_db.py"* ]]; then
        (echo "$CURRENT_CRON"; echo "$CRON_CMD") | crontab -
        log_success "Scheduled automated daily SQLite backup at 03:00 UTC."
    else
        log_info "Automated backup cron job already registered."
    fi
fi

# 5. Check for .env file
if [ ! -f .env ]; then
    log_warn ".env configuration file not found!"
    if [ -f .env.example ]; then
        cp .env.example .env
        log_warn "Created .env from .env.example."
        log_error "Please edit .env with your BOT_TOKEN, CHANNEL_ID, and ADMIN_GROUP_ID before launching!"
        echo -e "Run: ${YELLOW}nano .env${NC} and then re-run ${YELLOW}./deploy.sh${NC}"
        exit 1
    else
        log_error ".env.example file is missing. Please create .env manually."
        exit 1
    fi
fi

# 6. Build and start containers with Docker Compose
log_info "Building and launching Telegram Membership Bot via Docker Compose..."
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    log_error "Neither 'docker compose' nor 'docker-compose' found."
    exit 1
fi

sudo $COMPOSE_CMD down --remove-orphans || true
sudo $COMPOSE_CMD up -d --build

log_success "Telegram Membership Bot container is running!"
echo ""
echo -e "${GREEN}====================================================${NC}"
echo -e "  Deployment Complete! Useful Commands:"
echo -e "  • View live logs:      ${YELLOW}docker compose logs -f${NC}"
echo -e "  • Restart bot:         ${YELLOW}docker compose restart${NC}"
echo -e "  • Stop bot:            ${YELLOW}docker compose down${NC}"
echo -e "  • Database location:   ${YELLOW}./data/bot.db${NC} (Persistent)"
echo -e "  • Run manual backup:   ${YELLOW}python3 scripts/backup_db.py${NC}"
echo -e "  • Backup directory:    ${YELLOW}./backups/${NC} (Auto-cleans older than 14 days)"
echo -e "${GREEN}====================================================${NC}"
