#!/usr/bin/env bash
# Screenshot of a page, usually a full-screen Storybook story (iframe.html?id=...):
#   tools/screenshots.sh <url> <name> [mobile|desktop|both]      (default: both)
# Writes <name>-phone.png (390x844 like a phone, double density) and <name>-desktop.png (1280x800)
# and prints the paths, one per line. The name becomes the caption on the board: keep it readable.
# Folder: RADIO_SCREENSHOTS, or /tmp/radio-screenshots. Never the worktree: it is the project's repository.
# Uses the headless Chromium that Playwright has already downloaded into its cache: nothing to install.
set -euo pipefail
url="${1:?page url}"
name="${2:?file name}"
mode="${3:-both}"
out="${RADIO_SCREENSHOTS:-/tmp/radio-screenshots}"
mkdir -p "$out"

# The newest headless Chromium in Playwright's cache, then Chromium, then Chrome
browser() {
    local f rev found="" best=0
    for f in "$HOME"/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell; do
        [[ -x "$f" ]] || continue
        rev=$(echo "$f" | sed -E 's/.*chromium_headless_shell-([0-9]+).*/\1/')
        if [ "$rev" -gt "$best" ]; then best=$rev; found=$f; fi
    done
    if [[ -z "$found" ]]; then
        for f in "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium \
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
            if [[ -x "$f" ]]; then found=$f; break; fi
        done
    fi
    [[ -n "$found" ]] || { echo "no Chromium found: npx playwright install chromium-headless-shell" >&2; return 1; }
    echo "$found"
}
b=$(browser)
profile=$(mktemp -d)
trap 'rm -rf "$profile"' EXIT

# shoot <file> <width> <height> <density>. --virtual-time-budget waits for the page to finish
# loading (virtual time does not advance while requests are in flight). macOS has no timeout:
# the 60-second cap is enforced by the loop
shoot() {
    rm -f "$1"
    "$b" --headless --hide-scrollbars --no-first-run --no-default-browser-check --disable-gpu \
        --user-data-dir="$profile" --window-size="$2,$3" --force-device-scale-factor="$4" \
        --virtual-time-budget=15000 --screenshot="$1" "$url" >/dev/null 2>&1 &
    local pid=$! t=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        t=$((t + 1))
        if [ "$t" -ge 60 ]; then kill "$pid" 2>/dev/null; echo "timed out: $url" >&2; return 1; fi
    done
    [[ -s "$1" ]] || { echo "screenshot failed: $url" >&2; return 1; }
    echo "$1"
}

if [[ "$mode" == mobile || "$mode" == both ]]; then shoot "$out/$name-phone.png" 390 844 2; fi
if [[ "$mode" == desktop || "$mode" == both ]]; then shoot "$out/$name-desktop.png" 1280 800 1; fi
