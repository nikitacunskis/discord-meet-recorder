#!/bin/bash
# Registers the native messaging host for Chrome (macOS / Linux).
# Usage: ./install.sh <extension-id from chrome://extensions>
set -euo pipefail

ID="${1:?Pass the extension ID (chrome://extensions, Developer mode, ID field)}"
DIR="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
  Darwin)
    HOST_DIRS=("$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts")
    ;;
  Linux)
    # Chrome and Chromium keep separate host registries — register in the ones that exist
    HOST_DIRS=()
    for d in "$HOME/.config/google-chrome" "$HOME/.config/chromium"; do
      [ -d "$d" ] && HOST_DIRS+=("$d/NativeMessagingHosts")
    done
    if [ ${#HOST_DIRS[@]} -eq 0 ]; then
      HOST_DIRS=("$HOME/.config/google-chrome/NativeMessagingHosts")
    fi
    ;;
  *)
    echo "Windows: run native/install.ps1 in PowerShell instead." >&2
    exit 1
    ;;
esac

chmod +x "$DIR/dvt_host.py"
for HOST_DIR in "${HOST_DIRS[@]}"; do
  mkdir -p "$HOST_DIR"
  cat > "$HOST_DIR/com.dvt.recorder.json" <<EOF
{
  "name": "com.dvt.recorder",
  "description": "Discord Voice Transcriber native host",
  "path": "$DIR/dvt_host.py",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$ID/"]
}
EOF
  echo "Registered: $HOST_DIR/com.dvt.recorder.json -> $DIR/dvt_host.py"
done
echo "Fully restart Chrome (quit, not just close the window) so it picks up the host."
