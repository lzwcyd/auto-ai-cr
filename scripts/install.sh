#!/usr/bin/env bash
set -euo pipefail

REPO="${AUTO_AI_CR_REPO:-lzwcyd/auto-ai-cr}"
VERSION="${AUTO_AI_CR_VERSION:-latest}"
INSTALL_DIR="${AUTO_AI_CR_INSTALL_DIR:-$HOME/.auto-ai-cr/app}"
BIN_DIR="${AUTO_AI_CR_BIN_DIR:-$HOME/.local/bin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "auto-ai-cr: python3 is required" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("auto-ai-cr: Python 3.10+ is required")
PY

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if [ -n "${AUTO_AI_CR_ARCHIVE_URL:-}" ]; then
  archive_url="$AUTO_AI_CR_ARCHIVE_URL"
elif [ "$VERSION" = "latest" ]; then
  archive_url="https://github.com/$REPO/releases/latest/download/auto-ai-cr.tar.gz"
else
  archive_url="https://github.com/$REPO/releases/download/$VERSION/auto-ai-cr.tar.gz"
fi

echo "Downloading auto-ai-cr from $archive_url"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$archive_url" -o "$tmp_dir/auto-ai-cr.tar.gz"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$tmp_dir/auto-ai-cr.tar.gz" "$archive_url"
else
  echo "auto-ai-cr: curl or wget is required" >&2
  exit 1
fi

rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
tar -xzf "$tmp_dir/auto-ai-cr.tar.gz" -C "$INSTALL_DIR" --strip-components=1

cat > "$BIN_DIR/auto-ai-cr" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$INSTALL_DIR/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec "${PYTHON_BIN}" -m auto_ai_cr.cli "\$@"
EOF
chmod +x "$BIN_DIR/auto-ai-cr"

echo "auto-ai-cr installed to $INSTALL_DIR"
echo "Executable: $BIN_DIR/auto-ai-cr"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "Add this to your shell profile if auto-ai-cr is not found:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
echo "Start the UI:"
echo "  auto-ai-cr ui --open"
