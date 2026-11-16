#!/usr/bin/env bash
# Deploy Chicen Nugget Bot to a remote host and (re)start it as a systemd service.
# Usage: ./deploy-remote.sh user@host [remote-dir]
# Override the unit name with SERVICE_NAME=... and the runtime account with
# SERVICE_USER=... if they differ from the defaults. The service deliberately
# runs as a dedicated non-root user: the bot has shell/browser tools, so root
# would turn a prompt injection into full host compromise.
set -euo pipefail

REMOTE="${1:?usage: ./deploy-remote.sh user@host [remote-dir]}"
REMOTE_DIR="${2:-slackbot}"
SERVICE="${SERVICE_NAME:-chicen-nugget-bot}"
SERVICE_USER="${SERVICE_USER:-chicen}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-${LOCAL_DIR}/.env}"
if [ ! -f "${ENV_FILE}" ]; then
  echo "env file not found: ${ENV_FILE}" >&2
  exit 1
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
SSH=(ssh "${SSH_OPTS[@]}" "${REMOTE}")

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
  --exclude '.env' \
  --exclude 'instances/' \
  -e "ssh ${SSH_OPTS[*]}" \
  "${LOCAL_DIR}/" "${REMOTE}:${REMOTE_DIR}/"

REMOTE_PATH="$("${SSH[@]}" "cd ${REMOTE_DIR} && pwd")"

echo "==> uploading ${ENV_FILE}"
scp -q -o BatchMode=yes -o ConnectTimeout=20 "${ENV_FILE}" "${REMOTE}:${REMOTE_PATH}/.env"

echo "==> preparing service account (${SERVICE_USER})"
"${SSH[@]}" "id -u ${SERVICE_USER} >/dev/null 2>&1 || \
  (useradd --system --create-home --home /home/${SERVICE_USER} --shell /usr/sbin/nologin ${SERVICE_USER} 2>/dev/null || \
   adduser --system --home /home/${SERVICE_USER} --shell /usr/sbin/nologin ${SERVICE_USER})"
SERVICE_HOME="$("${SSH[@]}" "getent passwd ${SERVICE_USER} | cut -d: -f6")"
SERVICE_HOME="${SERVICE_HOME:-/home/${SERVICE_USER}}"

echo "==> installing dependencies"
# PLAYWRIGHT_BROWSERS_PATH=0 keeps the browser inside .venv so the service
# account can read it after the chown below (instead of root's ~/.cache).
"${SSH[@]}" "cd ${REMOTE_DIR} && \
  { command -v python3.13 >/dev/null && PY=python3.13 || PY=python3; } && \
  \$PY -m venv .venv && \
  .venv/bin/pip install -q --upgrade pip && \
  .venv/bin/pip install -q -r requirements.txt && \
  (PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/playwright install chromium >/dev/null 2>&1 || true) && \
  mkdir -p data logs && \
  if [ -f data/bot.db ]; then .venv/bin/python -c 'import sqlite3,time; s=sqlite3.connect(\"data/bot.db\"); d=sqlite3.connect(\"data/bot.db.bak.\"+str(int(time.time()))); s.backup(d); d.close(); s.close()'; ls -1t data/bot.db.bak.* 2>/dev/null | tail -n +4 | xargs -r rm -f; fi && \
  chown -R ${SERVICE_USER}: '${REMOTE_PATH}' && \
  chmod 600 '${REMOTE_PATH}/.env' && \
  echo DEPS_OK && \$PY --version"

echo "==> configuring systemd service"
"${SSH[@]}" "if [ -f /etc/systemd/system/mochi.service ]; then \
    systemctl stop mochi 2>/dev/null || true; \
    systemctl disable mochi 2>/dev/null || true; \
    rm -f /etc/systemd/system/mochi.service; \
    echo REMOVED_OLD_SERVICE; \
  fi"
cat > "/tmp/${SERVICE}.service" <<UNIT
[Unit]
Description=Chicen Nugget Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${REMOTE_PATH}
Environment=HOME=${SERVICE_HOME}
Environment=PLAYWRIGHT_BROWSERS_PATH=0
ExecStart=${REMOTE_PATH}/.venv/bin/python main.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${REMOTE_PATH}

[Install]
WantedBy=multi-user.target
UNIT
scp -q -o BatchMode=yes -o ConnectTimeout=20 "/tmp/${SERVICE}.service" "${REMOTE}:/etc/systemd/system/${SERVICE}.service"
rm -f "/tmp/${SERVICE}.service"
"${SSH[@]}" "systemctl daemon-reload && \
  systemctl enable ${SERVICE} >/dev/null 2>&1 && \
  systemctl restart ${SERVICE} && \
  echo SERVICE_STARTED"

echo "==> service status"
"${SSH[@]}" "sleep 3; systemctl --no-pager --full status ${SERVICE} | head -15; echo '--- log ---'; tail -20 ${REMOTE_PATH}/logs/bot.log 2>/dev/null || true"

echo "==> done"
