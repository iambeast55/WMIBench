#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${REMOTEOPS_INSTALL_DIR:-$HOME/.local/share/remoteops}"
BIN_DIR="${REMOTEOPS_BIN_DIR:-$HOME/.local/bin}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

rm -f "$BIN_DIR/remoteops"
rm -f "$APPLICATIONS_DIR/remoteops.desktop"
rm -rf "$INSTALL_DIR"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

echo "RemoteOps uninstalled."

