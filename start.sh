#!/usr/bin/env bash
set -euo pipefail
TIFFANY_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TIFFANY_PYTHON="${TIFFANY_PYTHON:-python3}"
if ! command -v "$TIFFANY_PYTHON" >/dev/null 2>&1; then
    echo "Tiffany requires Python 3.11+ and venv. Set TIFFANY_PYTHON to its executable." >&2
    exit 2
fi
exec "$TIFFANY_PYTHON" -B "$TIFFANY_PROJECT_DIR/launcher.py" "$@"
