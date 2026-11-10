#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
    DIR="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
    SOURCE="$(readlink -- "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "Run ./install.sh first" >&2
    exit 1
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export WMIBENCH_DATA_DIR="$ROOT"
exec "$PYTHON" -m wmibench "$@"
