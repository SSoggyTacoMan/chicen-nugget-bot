#!/usr/bin/env bash
# Deploy Chicen Nugget Bot to a remote host and (re)start it as a systemd service.
# Usage: ./deploy-remote.sh [user@host] [remote-dir]
set -euo pipefail

REMOTE="${1:-maaren@hackclub.app}"
REMOTE_DIR="${2:-slackbot}"
SERVICE="chicen-nugget-bot"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

echo "==> syncing to ${REMOTE}:${REMOTE_DIR}"
rsync -az \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude 'data/' \
  --exclude 'logs/' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  --exclude '.git/' \
  -e "ssh ${SSH_OPTS[*]}" \
  "${LOCAL_DIR}/" "${REMOTE}:${REMOTE_DIR}/"

echo "==> installing dependencies"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "cd ${REMOTE_DIR} && \
  { command -v python3.13 >/dev/null && PY=python3.13 || PY=python3; } && \
  \$PY -m venv .venv && \
  .venv/bin/pip install -q --upgrade pip && \
  .venv/bin/pip install -q -r requirements.txt && \
  echo DEPS_OK && \$PY --version"

echo "==> configuring systemd service"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "cd ${REMOTE_DIR} && mkdir -p logs data && \
  if [ -f /etc/systemd/system/mochi.service ]; then \
    systemctl stop mochi 2>/dev/null || true; \
    systemctl disable mochi 2>/dev/null || true; \
    rm -f /etc/systemd/system/mochi.service; \
    echo REMOVED_OLD_SERVICE; \
  fi && \
  cat > /etc/systemd/system/${SERVICE}.service <<'UNIT'
[Unit]
Description=Chicen Nugget Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/slackbot
ExecStart=/root/slackbot/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload && \
  systemctl enable ${SERVICE} >/dev/null 2>&1 && \
  systemctl restart ${SERVICE} && \
  echo SERVICE_STARTED"

echo "==> service status"
ssh "${SSH_OPTS[@]}" "${REMOTE}" "sleep 3; systemctl --no-pager --full status ${SERVICE} | head -15; echo '--- log ---'; tail -20 ${REMOTE_DIR}/logs/bot.log 2>/dev/null || true"

echo "==> done"
