#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${WMIBENCH_INSTALL_DIR:-$HOME/.local/share/WMIBench-app}"
BIN_DIR="${WMIBENCH_BIN_DIR:-$HOME/.local/bin}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

rm -f "$BIN_DIR/wmibench"
rm -f "$APPLICATIONS_DIR/wmibench.desktop"
rm -rf "$INSTALL_DIR"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

echo "WMIBench uninstalled. Saved user data is retained."

