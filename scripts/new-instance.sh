#!/usr/bin/env bash
# Create an env file for a second, fully separate bot instance.
#
# Each instance has its own Slack app, tokens, database, sandboxes, logs and
# systemd service, so the person running it owns everything and can never see
# the first instance's mail, calendar, github, notes or DMs.
#
# Usage: ./scripts/new-instance.sh <name> [owner-user-id]
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:?usage: ./scripts/new-instance.sh <name> [owner-user-id]}"
OWNER="${2:-}"
if ! printf '%s' "$NAME" | grep -qE '^[a-z0-9-]+$'; then
  echo "name must be lowercase letters, digits and dashes" >&2
  exit 1
fi

OUT="instances/${NAME}.env"
if [[ -e "$OUT" ]]; then
  echo "$OUT already exists, refusing to overwrite" >&2
  exit 1
fi

mkdir -p instances
umask 077
cp .env.example "$OUT"
sed -i.bak -e "s|^BOT_NAME=.*|BOT_NAME=\"Chicen Nugget Bot (${NAME})\"|" "$OUT"
if [[ -n "$OWNER" ]]; then
  sed -i.bak -e "s|^OWNER_USER_ID=.*|OWNER_USER_ID=${OWNER}|" "$OUT"
fi
rm -f "${OUT}.bak"
chmod 600 "$OUT"

cat <<TXT
created ${OUT} (mode 600)

next:
 1. create a second slack app for ${NAME} (socket mode on, bot scopes as in README)
 2. fill SLACK_BOT_TOKEN, SLACK_APP_TOKEN, OWNER_USER_ID, AI_API_KEY
    and any personal integrations (gmail/github/mac) in ${OUT}
 3. deploy it as its own service and unix user:

      SERVICE_NAME=nugget-${NAME} SERVICE_USER=${NAME} \\
        ENV_FILE=instances/${NAME}.env ./deploy-remote.sh root@host ${NAME}-bot

the person is the owner of *their* instance. do not add them to ADMIN_USER_IDS
on yours; that would share your tools and credentials with them.
TXT
