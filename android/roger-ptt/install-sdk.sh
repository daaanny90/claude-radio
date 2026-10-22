#!/usr/bin/env bash
# Installs the minimum Android SDK needed by build.sh, in ~/Library/Android/sdk, without Android Studio.
# Prerequisite: brew install --cask android-commandlinetools (sdkmanager) and a JDK 17+.
set -euo pipefail
SDK="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
if [[ -z "${JAVA_HOME:-}" && -d /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ]]; then
    export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
fi
[[ -n "${JAVA_HOME:-}" ]] && export PATH="$JAVA_HOME/bin:$PATH"
mkdir -p "$SDK"
yes | sdkmanager --sdk_root="$SDK" --licenses >/dev/null
sdkmanager --sdk_root="$SDK" "platforms;android-35" "build-tools;35.0.0" "platform-tools"
echo "ok: $SDK"
