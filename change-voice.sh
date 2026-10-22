#!/usr/bin/env bash
# Change the Piper voice and restart only the bot (about ten seconds).
# Usage:  ./change-voice.sh en_GB-cori-medium
# With no arguments it lists the downloaded voices (see download-models.sh).
# It edits PIPER_VOICE in the .env of this folder and recreates the bot container.
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -eq 0 ]]; then
    echo "Current voice: $(grep -E '^PIPER_VOICE=' .env | cut -d= -f2)"
    echo "Available:"; ls models/piper/*.onnx | xargs -n1 basename | sed 's/\.onnx$//; s/^/  /'
    exit 0
fi
voice="$1"
[[ -s "models/piper/$voice.onnx" ]] || { echo "voice not found: models/piper/$voice.onnx"; exit 1; }
# -i.bak works on both macOS (BSD) and GNU sed; the backup holds the passwords, so it is removed
if grep -q '^PIPER_VOICE=' .env; then
    sed -i.bak "s/^PIPER_VOICE=.*/PIPER_VOICE=$voice/" .env && rm -f .env.bak
else
    echo "PIPER_VOICE=$voice" >> .env
fi
docker compose up -d bot >/dev/null 2>&1
echo "voice set: $voice (the high voices take longer to synthesize)"
