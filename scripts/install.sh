#!/usr/bin/env bash
set -euo pipefail

REPO="${AUTO_AI_CR_REPO:-lzwcyd/auto-ai-cr}"
VERSION="${AUTO_AI_CR_VERSION:-latest}"
INSTALL_DIR="${AUTO_AI_CR_INSTALL_DIR:-$HOME/.auto-ai-cr/bin}"
BIN_DIR="${AUTO_AI_CR_BIN_DIR:-$HOME/.local/bin}"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "auto-ai-cr: $1 is required" >&2
    exit 1
  fi
}

download() {
  local url="$1"
  local output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl --retry 5 --retry-delay 2 --retry-connrefused -fsSL "$url" -o "$output"
  elif command -v wget >/dev/null 2>&1; then
    wget --tries=6 --waitretry=2 --retry-connrefused -qO "$output" "$url"
  else
    echo "auto-ai-cr: curl or wget is required" >&2
    exit 1
  fi
}

detect_asset() {
  local os arch platform
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Darwin)
      case "$arch" in
        arm64|aarch64) platform="macos-arm64" ;;
        x86_64|amd64) platform="macos-x64" ;;
        *) echo "auto-ai-cr: unsupported macOS architecture: $arch" >&2; exit 1 ;;
      esac
      echo "auto-ai-cr-$platform.tar.gz"
      ;;
    Linux)
      case "$arch" in
        x86_64|amd64) platform="linux-x64" ;;
        *) echo "auto-ai-cr: unsupported Linux architecture: $arch" >&2; exit 1 ;;
      esac
      echo "auto-ai-cr-$platform.tar.gz"
      ;;
    MINGW*|MSYS*|CYGWIN*)
      case "$arch" in
        x86_64|amd64) platform="windows-x64" ;;
        *) echo "auto-ai-cr: unsupported Windows architecture: $arch" >&2; exit 1 ;;
      esac
      echo "auto-ai-cr-$platform.zip"
      ;;
    *)
      echo "auto-ai-cr: unsupported OS: $os" >&2
      exit 1
      ;;
  esac
}

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

asset="${AUTO_AI_CR_ASSET:-$(detect_asset)}"
if [ -n "${AUTO_AI_CR_ARCHIVE_URL:-}" ]; then
  archive_url="$AUTO_AI_CR_ARCHIVE_URL"
elif [ "$VERSION" = "latest" ]; then
  archive_url="https://github.com/$REPO/releases/latest/download/$asset"
else
  archive_url="https://github.com/$REPO/releases/download/$VERSION/$asset"
fi

echo "Downloading auto-ai-cr $VERSION ($asset)"
download "$archive_url" "$tmp_dir/$asset"

rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

case "$asset" in
  *.tar.gz)
    need tar
    tar -xzf "$tmp_dir/$asset" -C "$INSTALL_DIR"
    binary_name="auto-ai-cr"
    ;;
  *.zip)
    need unzip
    unzip -q "$tmp_dir/$asset" -d "$INSTALL_DIR"
    binary_name="auto-ai-cr.exe"
    ;;
  *)
    echo "auto-ai-cr: unsupported archive type: $asset" >&2
    exit 1
    ;;
esac

chmod +x "$INSTALL_DIR/$binary_name" 2>/dev/null || true
cp "$INSTALL_DIR/$binary_name" "$BIN_DIR/$binary_name"
chmod +x "$BIN_DIR/$binary_name" 2>/dev/null || true

echo "auto-ai-cr installed: $BIN_DIR/$binary_name"

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
if [ "$binary_name" = "auto-ai-cr.exe" ]; then
  echo "  auto-ai-cr.exe ui --open"
else
  echo "  auto-ai-cr ui --open"
fi
