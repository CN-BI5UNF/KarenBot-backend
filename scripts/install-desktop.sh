#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
TARGET="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
mkdir -p "$TARGET" "$ICON_DIR"
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/packaging/karenbot-gui.desktop.in" \
  > "$TARGET/karenbot-gui.desktop"
cp "$ROOT/packaging/karenbot.svg" "$ICON_DIR/karenbot.svg"
echo "已安装桌面入口：$TARGET/karenbot-gui.desktop"
echo "已安装图标：$ICON_DIR/karenbot.svg"
echo "启动命令：$ROOT/run-gui.sh"
