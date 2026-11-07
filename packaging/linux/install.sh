#!/usr/bin/env bash
set -euo pipefail

APP_NAME="WMIBench"
# Retain the install location and desktop ID for in-place upgrades.
INSTALL_DIR="${WMIBENCH_INSTALL_DIR:-${REMOTEOPS_INSTALL_DIR:-$HOME/.local/share/remoteops}}"
BIN_DIR="${WMIBENCH_BIN_DIR:-${REMOTEOPS_BIN_DIR:-$HOME/.local/bin}}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

need_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_command python3

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$APPLICATIONS_DIR"

echo "Installing $APP_NAME into $INSTALL_DIR"
rsync_available=0
if command -v rsync >/dev/null 2>&1; then
  rsync_available=1
fi

if [[ "$rsync_available" -eq 1 ]]; then
  rsync -a --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude "build" \
    --exclude "dist" \
    "$PACKAGE_ROOT/" "$INSTALL_DIR/"
else
  rm -rf "$INSTALL_DIR/src" "$INSTALL_DIR/packaging" "$INSTALL_DIR/tests"
  cp -R "$PACKAGE_ROOT/src" "$INSTALL_DIR/src"
  cp -R "$PACKAGE_ROOT/packaging" "$INSTALL_DIR/packaging"
  cp -R "$PACKAGE_ROOT/tests" "$INSTALL_DIR/tests"
  cp "$PACKAGE_ROOT/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
  cp "$PACKAGE_ROOT/README.md" "$INSTALL_DIR/README.md"
fi

if [[ -f "$PACKAGE_ROOT/ADDING_WMI_COMMANDS.md" ]]; then
  cp "$PACKAGE_ROOT/ADDING_WMI_COMMANDS.md" "$INSTALL_DIR/ADDING_WMI_COMMANDS.md"
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install "$INSTALL_DIR"

cat > "$BIN_DIR/wmibench" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/python" -m wmibench "\$@"
EOF
chmod +x "$BIN_DIR/wmibench"
cp "$BIN_DIR/wmibench" "$BIN_DIR/remoteops"
chmod +x "$BIN_DIR/remoteops"

cat > "$APPLICATIONS_DIR/remoteops.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WMIBench
Comment=Windows administration over WMI/DCOM and WinRM
Exec=$BIN_DIR/wmibench
Terminal=false
Categories=System;Network;RemoteAccess;
StartupNotify=true
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

echo
echo "$APP_NAME installed."
echo "Run it with: wmibench"
echo
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "Note: $BIN_DIR is not currently on PATH."
  echo "Add this to your shell profile:"
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi
