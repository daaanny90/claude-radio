#!/usr/bin/env bash
# Download the models into ./models (only once).
# Whisper: large-v3-turbo (used on a Mac with Metal), small, base and tiny quantized, plus the Silero model for VAD.
# Piper: Italian voices (paola medium official, serena high community CC-BY-4.0) and several English voices.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p models/whisper models/piper

download() {  # download <url> <destination>: skip if the file is already there
    local url="$1" dest="$2"
    if [[ -s "$dest" ]]; then echo "ok, already present: $dest"; return; fi
    echo "downloading $dest"
    curl -fL --progress-bar -o "$dest.part" "$url" && mv "$dest.part" "$dest"
}

W=https://huggingface.co/ggerganov/whisper.cpp/resolve/main
# q8_0 and not q5_1: on CPUs without AVX2 q8_0 is about 25% faster
download "$W/ggml-small-q8_0.bin" models/whisper/ggml-small-q8_0.bin
download "$W/ggml-base-q8_0.bin"  models/whisper/ggml-base-q8_0.bin
download "$W/ggml-tiny-q8_0.bin"  models/whisper/ggml-tiny-q8_0.bin
# English-only models: at the same size they understand better, if you speak English
download "$W/ggml-small.en-q8_0.bin" models/whisper/ggml-small.en-q8_0.bin
download "$W/ggml-base.en-q8_0.bin"  models/whisper/ggml-base.en-q8_0.bin
download "$W/ggml-tiny.en-q8_0.bin"  models/whisper/ggml-tiny.en-q8_0.bin
# For the Mac (Metal): large-v3-turbo, 874 MB, as fast as small.en and much more accurate
download "$W/ggml-large-v3-turbo-q8_0.bin" models/whisper/ggml-large-v3-turbo-q8_0.bin
download https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin models/whisper/ggml-silero-v5.1.2.bin

P=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/it/it_IT/paola/medium
download "$P/it_IT-paola-medium.onnx"      models/piper/it_IT-paola-medium.onnx
download "$P/it_IT-paola-medium.onnx.json" models/piper/it_IT-paola-medium.onnx.json
S=https://huggingface.co/committa/it_IT-serena-high/resolve/main
download "$S/it_IT-serena-high.onnx"      models/piper/it_IT-serena-high.onnx
download "$S/it_IT-serena-high.onnx.json" models/piper/it_IT-serena-high.onnx.json
# English voices: the medium ones are fast, the two high ones take longer to synthesize (about 5 s more per reply on a slow CPU)
E=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en
for v in en_US/lessac/medium/en_US-lessac-medium en_US/hfc_female/medium/en_US-hfc_female-medium en_GB/alan/medium/en_GB-alan-medium \
         en_GB/cori/medium/en_GB-cori-medium en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium \
         en_GB/northern_english_male/medium/en_GB-northern_english_male-medium en_GB/aru/medium/en_GB-aru-medium en_US/ryan/high/en_US-ryan-high en_GB/cori/high/en_GB-cori-high; do
    download "$E/$v.onnx"      "models/piper/$(basename $v).onnx"
    download "$E/$v.onnx.json" "models/piper/$(basename $v).onnx.json"
done

echo; du -sh models/*
