#!/usr/bin/env bash
set -euo pipefail
SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
    SCRIPT_PATH="$(readlink -f -- "$SCRIPT_PATH")"
fi
ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
export WMIBENCH_DATA_DIR="$ROOT"
exec "$ROOT/wmibench" "$@"
