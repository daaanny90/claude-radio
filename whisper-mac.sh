#!/usr/bin/env bash
# Native whisper-server on the Mac (Metal), same HTTP API as the container: launchd keeps it up
# (mac/radio.whisper.plist). It reads model, language and threads from .env, like docker-compose.
# Port 8781: 8080 on the Mac is often taken by something else (and whisper-server dies silently).
set -euo pipefail
cd "$(dirname "$0")"
val() { grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2-; }
model="models/whisper/$( v=$(val WHISPER_MODEL); echo "${v:-ggml-small.en-q8_0.bin}" )"
[[ -f "$model" ]] || { echo "model $model is missing (run download-models.sh)" >&2; exit 1; }
server="${WHISPER_SERVER:-$(command -v whisper-server || echo /opt/homebrew/bin/whisper-server)}"
threads=$(val WHISPER_THREADS); lang=$(val WHISPER_LANGUAGE); beam=$(val WHISPER_BEAM_SIZE); actx=$(val WHISPER_AUDIO_CTX); extra=$(val WHISPER_EXTRA)
# shellcheck disable=SC2086
exec "$server" --host 127.0.0.1 --port "${WHISPER_PORT:-8781}" -m "$model" -l "${lang:-en}" \
    -t "${threads:-6}" -bs "${beam:-1}" -ac "${actx:-0}" -nf --vad -vm models/whisper/ggml-silero-v5.1.2.bin ${extra:-}
