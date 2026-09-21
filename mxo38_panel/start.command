#!/bin/bash
# 双击启动 MXO38 远程面板（macOS）。关闭这个终端窗口即停止服务。
cd "$(dirname "$0")" || exit 1
if ! command -v node >/dev/null 2>&1; then
  export PATH="/opt/homebrew/bin:$PATH"
fi
if lsof -nP -iTCP:${PANEL_PORT:-8038} -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 ${PANEL_PORT:-8038} 已有面板在运行，直接打开浏览器。"
  open "http://127.0.0.1:${PANEL_PORT:-8038}/"
  exit 0
fi
exec node server.mjs "$@"
