#!/usr/bin/env sh
set -eu

PLIST_NAME="com.claude.usage.daemon.plist"
SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SRC_FILE="$SRC_DIR/$PLIST_NAME"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/$PLIST_NAME"

if ! command -v launchctl >/dev/null 2>&1; then
  echo "[ERROR] launchctl not found."
  exit 1
fi

if ! command -v cu >/dev/null 2>&1; then
  echo "[ERROR] cu command not found. Install first: pip install ."
  exit 1
fi

mkdir -p "$TARGET_DIR"
cp "$SRC_FILE" "$TARGET_FILE"

launchctl unload "$TARGET_FILE" >/dev/null 2>&1 || true
launchctl load "$TARGET_FILE"

echo "[OK] Installed and loaded $PLIST_NAME"
echo "Check status:"
echo "  launchctl list | rg claude.usage.daemon"
