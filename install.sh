#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "Python 3 is required" >&2
    exit 1
}

"$PYTHON" -c 'import sys; sys.version_info >= (3, 12) or sys.exit("Python 3.12+ is required")'

"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install --editable "$ROOT"
mkdir -p "$ROOT/plugins"
chmod +x "$ROOT/run.sh"

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
ln -sfn "$ROOT/run.sh" "$BIN_DIR/wmibench"

echo "WMIBench installed in $ROOT/.venv"
echo "Run: wmibench or ./run.sh"
echo "Launcher: $BIN_DIR/wmibench"
