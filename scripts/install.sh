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

restart_daemon_if_installed() {
  if [ "${AUTO_AI_CR_RESTART_DAEMON:-1}" = "0" ]; then
    return
  fi
  local os
  os="$(uname -s)"
  case "$os" in
    Darwin)
      local plist="$HOME/Library/LaunchAgents/com.auto-ai-cr.daemon.plist"
      if [ -f "$plist" ] && command -v launchctl >/dev/null 2>&1; then
        echo "Restarting auto-ai-cr daemon"
        launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
        launchctl kickstart -k "gui/$(id -u)/com.auto-ai-cr.daemon" >/dev/null 2>&1 || true
      fi
      ;;
    Linux)
      local service="$HOME/.config/systemd/user/com.auto-ai-cr.daemon.service"
      if [ -f "$service" ] && command -v systemctl >/dev/null 2>&1; then
        echo "Restarting auto-ai-cr daemon"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user restart com.auto-ai-cr.daemon.service >/dev/null 2>&1 || true
      fi
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
target="$BIN_DIR/$binary_name"
target_tmp="$(mktemp "$BIN_DIR/.auto-ai-cr.XXXXXX")"
cp "$INSTALL_DIR/$binary_name" "$target_tmp"
chmod +x "$target_tmp" 2>/dev/null || true
mv -f "$target_tmp" "$target"
chmod +x "$target" 2>/dev/null || true
"$target" --version >/dev/null

echo "auto-ai-cr installed: $target"
restart_daemon_if_installed

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
