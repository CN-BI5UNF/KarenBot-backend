#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$TARGET"
sed "s|@PROJECT_ROOT@|$ROOT|g" "$ROOT/packaging/karenbot-gui.desktop.in" \
  > "$TARGET/karenbot-gui.desktop"
echo "已安装桌面入口：$TARGET/karenbot-gui.desktop"
echo "启动命令：$ROOT/run-gui.sh"
