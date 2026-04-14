#!/usr/bin/env bash
set -euo pipefail

deps=(pdftoppm tesseract gs gunicorn python3)
missing=0

for dep in "${deps[@]}"; do
  if command -v "$dep" >/dev/null 2>&1; then
    echo "[OK] $dep"
  else
    echo "[MISSING] $dep"
    missing=1
  fi
done

if [[ "$missing" -eq 1 ]]; then
  echo "Some dependencies are missing."
  exit 1
fi

echo "All required dependencies are available."
