#!/usr/bin/env bash
# Builds and signs Roger PTT without Gradle or Android Studio: aapt2 + javac + d8 + apksigner.
#   ./build.sh            -> build/roger-ptt.apk
#   ./build.sh install    -> also "adb install -r"
# Needs: JDK 17+, Android SDK with platforms;android-35 and build-tools;35.0.0 (see install-sdk.sh).
set -euo pipefail
cd "$(dirname "$0")"
SDK="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
# Homebrew's JDK (keg-only): javac, keytool, and the JVM for d8 and apksigner
if [[ -z "${JAVA_HOME:-}" && -d /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home ]]; then
    export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
fi
[[ -n "${JAVA_HOME:-}" ]] && export PATH="$JAVA_HOME/bin:$PATH"
command -v javac >/dev/null || { echo "no JDK found: brew install openjdk@21" >&2; exit 1; }
BT="$SDK/build-tools/35.0.0"
PLATFORM="$SDK/platforms/android-35/android.jar"
[[ -f "$PLATFORM" ]] || { echo "missing $PLATFORM: ./install-sdk.sh" >&2; exit 1; }
rm -rf build && mkdir -p build/res build/gen build/classes
"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/base.apk -I "$PLATFORM" --manifest AndroidManifest.xml --java build/gen build/res.zip
javac --release 17 -cp "$PLATFORM" -d build/classes $(find src build/gen -name '*.java')
"$BT/d8" --release --lib "$PLATFORM" --min-api 26 --output build $(find build/classes -name '*.class')
cp build/base.apk build/unaligned.apk
(cd build && zip -q unaligned.apk classes.dex)
"$BT/zipalign" -f -p 4 build/unaligned.apk build/aligned.apk
# Local signing key, created once with a random password: keystore/ stays out of the repo
mkdir -p keystore
if [[ ! -f keystore/roger.jks ]]; then
    (umask 077; openssl rand -hex 16 > keystore/password)
    keytool -genkeypair -v -keystore keystore/roger.jks -alias roger \
        -keyalg RSA -keysize 2048 -validity 10000 -storepass "$(cat keystore/password)" -keypass "$(cat keystore/password)" \
        -dname "CN=Roger PTT" >/dev/null 2>&1
fi
ks_pass=$(cat keystore/password)
"$BT/apksigner" sign --ks keystore/roger.jks --ks-pass "pass:$ks_pass" --key-pass "pass:$ks_pass" \
    --out build/roger-ptt.apk build/aligned.apk
echo "ok: build/roger-ptt.apk"
if [[ "${1:-}" == "install" ]]; then
    # Main profile only: without --user 0 it also ends up in Samsung's Dual Messenger and Secure
    # Folder (a second icon with the blue dot)
    adb install -r --user 0 build/roger-ptt.apk
fi
