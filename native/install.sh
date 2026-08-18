#!/bin/bash
# Reģistrē native messaging hostu Chrome pārlūkam.
# Lietošana: ./install.sh <extension-id no chrome://extensions>
set -euo pipefail

ID="${1:?Norādi extension ID (chrome://extensions, Developer mode, lauks ID)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"

chmod +x "$DIR/dvt_host.py"
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
echo "Reģistrēts: $HOST_DIR/com.dvt.recorder.json -> $DIR/dvt_host.py"
echo "Pārstartē Chrome (pilnībā, Cmd+Q), lai hosts būtu redzams."
