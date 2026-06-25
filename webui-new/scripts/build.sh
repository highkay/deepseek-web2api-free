#!/usr/bin/env bash
#
# build_webui.sh — install deps + build the React webui
#
# Usage:  bash scripts/build_webui.sh
# Output: webui-new/dist/  (served by FastAPI on /webui)
#
# Requires: Node.js 18+ and either npm or pnpm.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d node_modules ]; then
  if command -v pnpm >/dev/null 2>&1; then
    echo "==> pnpm install"
    pnpm install --frozen-lockfile=false
  else
    echo "==> npm install"
    npm install --no-audit --no-fund
  fi
fi

echo "==> npm run build"
npm run build

echo
echo "Build complete: webui-new/dist/"
ls -la dist/ | head -10
