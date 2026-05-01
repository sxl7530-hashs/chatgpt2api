#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/sxl7530-hashs/chatgpt2api.git}"
APP_DIR="${APP_DIR:-/opt/chatgpt2api}"
APP_PORT="${APP_PORT:-3000}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root." >&2
    exit 1
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi
  apt-get update
  apt-get install -y ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc || \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
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
  "image_cleanup_interval_minutes": 60,
  "storage_backend": "json"
}
EOF
  fi
  cat > .env <<EOF
CHATGPT2API_PORT=${APP_PORT}
CHATGPT2API_AUTH_KEY=${CHATGPT2API_AUTH_KEY:-}
CHATGPT2API_BASE_URL=${CHATGPT2API_BASE_URL:-}
CHATGPT2API_OPENAI_API_KEY=${CHATGPT2API_OPENAI_API_KEY:-}
STORAGE_BACKEND=${STORAGE_BACKEND:-json}
EOF
}

start_app() {
  cd "${APP_DIR}"
  docker compose -f deploy/docker-compose.1g.yml --env-file .env up -d --build
  docker compose -f deploy/docker-compose.1g.yml ps
}

require_root
install_docker
ensure_swap
prepare_app
start_app

echo "chatgpt2api is starting on port ${APP_PORT}."
echo "Check logs with: docker logs -f chatgpt2api"
