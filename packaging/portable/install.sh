#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${WMIBENCH_INSTALL_DIR:-$HOME/.local/share/WMIBench}"
BIN_DIR="${WMIBENCH_BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$INSTALL_DIR/plugins"

for item in WMIBench.exe wmibench wmibench.sha256 wmibench.portable README.md run.sh install.sh run.ps1 run.cmd; do
    if [[ -e "$SOURCE/$item" ]]; then
        cp -R "$SOURCE/$item" "$INSTALL_DIR/$item"
    fi
done

if [[ -d "$SOURCE/plugins" ]]; then
    cp -R "$SOURCE/plugins/." "$INSTALL_DIR/plugins/"
fi

chmod +x "$INSTALL_DIR/wmibench" "$INSTALL_DIR/run.sh" 2>/dev/null || true

# Use an absolute target here. A symlink to run.sh makes run.sh resolve its
# root to BIN_DIR and execute the symlink again.
if [[ -L "$BIN_DIR/wmibench" ]]; then
    rm "$BIN_DIR/wmibench"
fi
cat > "$BIN_DIR/wmibench" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export WMIBENCH_DATA_DIR="$INSTALL_DIR"
exec "$INSTALL_DIR/wmibench" "\$@"
EOF
chmod +x "$BIN_DIR/wmibench"

echo "WMIBench installed to $INSTALL_DIR"
echo "Run: $BIN_DIR/wmibench"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "Add $BIN_DIR to PATH if needed: export PATH=\"$BIN_DIR:\$PATH\""
fi
