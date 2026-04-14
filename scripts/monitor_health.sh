#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://127.0.0.1:5050/health}"

if curl -fsS "$URL" >/dev/null; then
  echo "[OK] health endpoint reachable: $URL"
else
  echo "[ALERT] health endpoint unreachable: $URL"
  exit 1
fi
