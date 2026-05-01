#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/sxl7530-hashs/chatgpt2api.git}"
APP_DIR="${APP_DIR:-/opt/chatgpt2api}"
APP_USER="${APP_USER:-chatgpt2api}"
APP_PORT="${APP_PORT:-3000}"
SWAP_SIZE="${SWAP_SIZE:-2G}"
SERVICE_NAME="${SERVICE_NAME:-chatgpt2api}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root." >&2
    exit 1
  fi
}

install_packages() {
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gcc \
    libpq-dev \
    python3.13 \
    python3.13-venv
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
}

ensure_swap() {
  if swapon --show | grep -q '/swapfile'; then
    return
  fi
  fallocate -l "${SWAP_SIZE}" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl vm.swappiness=20
  grep -q '^vm.swappiness=' /etc/sysctl.conf || echo 'vm.swappiness=20' >> /etc/sysctl.conf
}

prepare_user() {
  if id "${APP_USER}" >/dev/null 2>&1; then
    return
  fi
  useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
}

prepare_app() {
  mkdir -p "${APP_DIR}"
  if [ -d "${APP_DIR}/.git" ]; then
    git -C "${APP_DIR}" fetch origin main
    git -C "${APP_DIR}" reset --hard origin/main
  else
    git clone --depth 1 "${REPO_URL}" "${APP_DIR}"
  fi
  cd "${APP_DIR}"
  mkdir -p data
  if [ ! -f config.json ]; then
    cat > config.json <<EOF
{
  "auth-key": "${CHATGPT2API_AUTH_KEY:-change-me}",
  "image_retention_days": 7,
  "image_cleanup_interval_minutes": 60
}
EOF
  fi
  if [ ! -d web_dist ] || [ ! -f web_dist/index.html ]; then
    echo "web_dist is missing. Build it locally and upload it, or run npm build on a larger machine." >&2
  fi
  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
}

install_python_deps() {
  cd "${APP_DIR}"
  sudo -u "${APP_USER}" env UV_LINK_MODE=copy uv sync --frozen --no-dev --no-install-project
}

write_env() {
  cat > "/etc/${SERVICE_NAME}.env" <<EOF
CHATGPT2API_AUTH_KEY=${CHATGPT2API_AUTH_KEY:-}
CHATGPT2API_BASE_URL=${CHATGPT2API_BASE_URL:-}
CHATGPT2API_OPENAI_API_KEY=${CHATGPT2API_OPENAI_API_KEY:-}
STORAGE_BACKEND=${STORAGE_BACKEND:-json}
MALLOC_ARENA_MAX=2
PYTHONUNBUFFERED=1
EOF
  chmod 600 "/etc/${SERVICE_NAME}.env"
}

write_service() {
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=chatgpt2api
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/${SERVICE_NAME}.env
ExecStart=/usr/local/bin/uv run uvicorn main:app --host 0.0.0.0 --port ${APP_PORT} --workers 1 --no-access-log --timeout-keep-alive 10
Restart=always
RestartSec=5
MemoryMax=768M
TasksMax=256
LimitNOFILE=65535
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

start_service() {
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
}

require_root
install_packages
install_uv
ensure_swap
prepare_user
prepare_app
install_python_deps
write_env
write_service
start_service

echo "chatgpt2api is running on port ${APP_PORT}."
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
