#!/usr/bin/env bash
# Storybook of a folder, reachable from the tailnet, phone included:
#   tools/storybook.sh [start] [folder]   starts it if not already running and prints the address
#   tools/storybook.sh url [folder]       prints the address if it is running (exit 1 if not)
#   tools/storybook.sh stop [folder]      stops the one started from here
# The folder is the one with Storybook's package.json (usually the worktree; default: the current one).
#
# If a Storybook for that folder is already running and reachable on the Tailscale IP (for example
# one started by hand, listening on all interfaces) that one is used. Otherwise one is started:
# - listening only on the Mac's Tailscale IP (TAILSCALE_IP from the radio's .env): not on Wi-Fi
# - fixed port per radio session: alpha 6101, bravo 6102, ...; other folders 6130-6189
# - in its own tmux session (storybook-<name>), so it outlives the Claude session that asked for it
# - log in the radio's logs/, never in the folder: it is the project's repository
set -euo pipefail
radio="$(cd "$(dirname "$0")/.." && pwd)"
action=start
case "${1:-}" in start|url|stop) action=$1; shift ;; esac
dir="$(cd "${1:-.}" && pwd -P)"

has_storybook() { [[ -f "$1/package.json" ]] && jq -e '((.devDependencies // {}) + (.dependencies // {})) | has("storybook")' "$1/package.json" >/dev/null 2>&1; }
# If the folder has no Storybook, walk up to the repository root
probe="$dir"
until has_storybook "$probe"; do
    [[ "$probe" == "/" || -d "$probe/.git" || -f "$probe/.git" ]] && { echo "no Storybook in $dir"; exit 2; }
    probe="$(dirname "$probe")"
done
dir="$probe"

ip=$(sed -n 's/^TAILSCALE_IP=//p' "$radio/.env" 2>/dev/null | tail -n 1 | tr -d '"')
[[ -n "$ip" ]] || { echo "TAILSCALE_IP missing from the radio's .env ($radio/.env)"; exit 1; }

# Name and port: from the radio session (RADIO_SESSION) or from the worktree suffix (shop-ui--bravo)
nato=(alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu)
name="${RADIO_SESSION:-}"
[[ -z "$name" && "$(basename "$dir")" == *--* ]] && name="${dir##*--}"
port=""
for i in "${!nato[@]}"; do [[ "${nato[$i]}" == "$name" ]] && port=$((6101 + i)); done
if [[ -z "$port" ]]; then
    name="$(basename "$dir")"
    port=$((6130 + $(printf '%s' "$dir" | cksum | cut -d' ' -f1) % 60))
fi
tmux_session="storybook-${name//[.:]/-}"
log="$radio/logs/storybook-${name//[.:\/]/-}.log"

# A Storybook already listening for this folder and reachable from the tailnet: prints its port
already_running() {
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 && $1 == "node" { print $2, $9 }' | sort -u |
    while read -r pid address; do
        [[ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" == "$dir" ]] || continue
        ps -o command= -p "$pid" | grep -q storybook || continue
        host="${address%:*}"
        [[ "$host" == "*" || "$host" == "$ip" ]] || continue
        echo "${address##*:}"
        return
    done
}

print_info() {
    echo "Storybook for $dir"
    echo "http://$ip:$1"
    echo "One story: http://$ip:$1/?path=/story/<id>   full screen: http://$ip:$1/iframe.html?id=<id>&viewMode=story"
    echo "List of stories (id, title, file): http://$ip:$1/index.json"
}

case "$action" in
url)
    p="$(already_running)"
    [[ -n "$p" ]] || { echo "no reachable Storybook for $dir"; exit 1; }
    print_info "$p"
    ;;
stop)
    if tmux has-session -t "$tmux_session" 2>/dev/null; then
        tmux kill-session -t "$tmux_session"
        echo "stopped $tmux_session"
    else
        echo "no Storybook started from here for $dir (ones launched by hand are left alone)"
    fi
    ;;
start)
    p="$(already_running)"
    if [[ -n "$p" ]]; then print_info "$p"; exit 0; fi
    [[ -d "$dir/node_modules" ]] || { echo "dependencies not installed in $dir: run pnpm install first"; exit 1; }
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "port $port is taken by another process: check with lsof -nP -iTCP:$port"; exit 1
    fi
    tmux kill-session -t "$tmux_session" 2>/dev/null || true
    : > "$log"
    # --ci and --exact-port: no interactive prompts (in the background it would hang waiting for an answer)
    tmux new-session -d -s "$tmux_session" -c "$dir" \
        "export PATH='$PATH'; exec pnpm exec storybook dev -p $port --host $ip --no-open --ci --exact-port --disable-telemetry >>'$log' 2>&1"
    # The first time Vite prepares the dependencies: it can take more than a minute
    for _ in $(seq 1 90); do
        if curl -s -o /dev/null -m 2 "http://$ip:$port/index.json"; then print_info "$port"; exit 0; fi
        if ! tmux has-session -t "$tmux_session" 2>/dev/null; then
            echo "Storybook stopped. Last lines of $log:"; tail -n 15 "$log"; exit 1
        fi
        sleep 2
    done
    echo "Storybook is not responding after three minutes: check $log"; exit 1
    ;;
esac
