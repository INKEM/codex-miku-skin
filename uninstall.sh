#!/usr/bin/env bash
set -euo pipefail
SKIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SKIN_DIR/install.sh" uninstall "$@"
