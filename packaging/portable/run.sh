#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export WMIBENCH_DATA_DIR="$ROOT"
exec "$ROOT/wmibench" "$@"
