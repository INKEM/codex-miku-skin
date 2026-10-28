#!/usr/bin/env bash
set -euo pipefail

SKIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${MIKU_STATE_DIR:-$SKIN_DIR/.state}"
CONFIG_PATH="${MIKU_CONFIG:-$HOME/.codex/config.toml}"
PYTHON_BIN="${MIKU_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for CANDIDATE in "$(command -v python3 || true)" "$HOME/miniconda3/bin/python3"; do
    if [[ -x "$CANDIDATE" ]] && "$CANDIDATE" -c 'import sys, tomlkit; assert sys.version_info >= (3, 11)' 2>/dev/null; then
      PYTHON_BIN="$CANDIDATE"
      break
    fi
  done
fi
ACTION="${1:-install}"
if [[ $# -gt 0 ]]; then shift; fi

if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys, tomlkit; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  echo '需要带有 tomlkit 的 Python。可通过 MIKU_PYTHON=/完整路径/python3 指定。' >&2
  exit 1
fi

case "$ACTION" in
  check|prepare|prepare-update|status)
    exec "$PYTHON_BIN" "$SKIN_DIR/install.py" "$ACTION" --state-dir "$STATE_DIR" --config "$CONFIG_PATH" "$@"
    ;;
  install)
    "$PYTHON_BIN" "$SKIN_DIR/install.py" prepare --state-dir "$STATE_DIR" --config "$CONFIG_PATH" "$@"
    ;;
  update)
    "$PYTHON_BIN" "$SKIN_DIR/install.py" prepare-update --state-dir "$STATE_DIR" --config "$CONFIG_PATH" "$@"
    ;;
  uninstall) ;;
  *) echo '用法：bash install.sh [install|update|uninstall|check|prepare|prepare-update|status]' >&2; exit 2 ;;
esac

if [[ -w /usr/lib/chatgpt/resources ]]; then
  exec "$PYTHON_BIN" "$SKIN_DIR/install.py" "$ACTION" --state-dir "$STATE_DIR" --config "$CONFIG_PATH" "$@"
fi

echo '下一步只会替换已校验的皮肤资源；首次安装还会设置三个外观配置项。'
echo '请在本机终端的 sudo 提示中输入系统密码；密码无需发送给任何人。'
exec sudo "$PYTHON_BIN" "$SKIN_DIR/install.py" "$ACTION" --state-dir "$STATE_DIR" --config "$CONFIG_PATH" "$@"
