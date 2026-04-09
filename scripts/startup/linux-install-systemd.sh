#!/usr/bin/env sh
set -eu

SERVICE_NAME="claude-usage-daemon.service"
SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SRC_FILE="$SRC_DIR/$SERVICE_NAME"
TARGET_DIR="$HOME/.config/systemd/user"
TARGET_FILE="$TARGET_DIR/$SERVICE_NAME"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[ERROR] systemctl not found."
  exit 1
fi

if ! command -v cu >/dev/null 2>&1; then
  echo "[ERROR] cu command not found. Install first: pip install ."
  exit 1
fi

mkdir -p "$TARGET_DIR"
cp "$SRC_FILE" "$TARGET_FILE"

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo "[OK] Installed and started $SERVICE_NAME"
echo "Check status:"
echo "  systemctl --user status $SERVICE_NAME"
