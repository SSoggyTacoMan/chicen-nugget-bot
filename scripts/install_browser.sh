#!/usr/bin/env bash
# Install Chromium for the browser tool. Run once on the server after deploying.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/bin/playwright ]; then
  PW=.venv/bin/playwright
else
  PW=playwright
fi

if [ "$(uname -s)" = "Linux" ]; then
  "$PW" install --with-deps chromium
else
  "$PW" install chromium
fi

echo "browser ready"
