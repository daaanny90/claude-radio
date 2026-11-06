#!/usr/bin/env bash
# Install (or update) the radio on this Mac or Linux machine. Idempotent: it can be run again.
#   ./install.sh
# It does: .env with fresh passwords (if missing), channel dependencies (bun), channel
# registration for Claude Code, state hooks in ~/.claude/settings.json, the launcher as a service,
# whisper (on the Mac a launchd agent for the native whisper-server with Metal, on Linux two
# containers on the CPU), Mumble and bot containers, Radio section in the projects' CLAUDE.md files.
# Prerequisites: Docker with Compose (OrbStack or Docker Desktop on the Mac), tmux, jq, python3, openssl,
# claude, bun (~/.bun), on the Mac whisper-cpp (brew); Tailscale connected.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
os=$(uname -s)
bun="${BUN:-$HOME/.bun/bin/bun}"; [[ -x "$bun" ]] || bun="$(command -v bun || true)"
for c in docker tmux jq claude python3 openssl; do command -v "$c" >/dev/null || { echo "missing $c" >&2; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo "missing docker compose (the Compose plugin)" >&2; exit 1; }
if [[ "$os" == Darwin ]]; then
    command -v whisper-server >/dev/null || [[ -x /opt/homebrew/bin/whisper-server ]] || { echo "missing whisper-server: brew install whisper-cpp" >&2; exit 1; }
fi
[[ -x "$bun" ]] || { echo "missing bun: curl -fsSL https://bun.sh/install | bash" >&2; exit 1; }
mkdir -p logs state models/whisper models/piper board

# set_env KEY VALUE: the KEY line of .env, added if missing. Through cat and not sed -i, which differs between
# macOS and Linux, and so that .env keeps its permissions
set_env() {
    if grep -q "^$1=" .env; then sed "s|^$1=.*|$1=$2|" .env > .env.tmp; else { cat .env; echo "$1=$2"; } > .env.tmp; fi
    cat .env.tmp > .env && rm -f .env.tmp
}

# 1. .env: from the template, with passwords generated here and never printed
if [[ ! -f .env ]]; then
    sed -e "s|^MUMBLE_SERVER_PASSWORD=.*|MUMBLE_SERVER_PASSWORD=$(openssl rand -hex 12)|" \
        -e "s|^MUMBLE_SUPERUSER_PASSWORD=.*|MUMBLE_SUPERUSER_PASSWORD=$(openssl rand -hex 16)|" \
        -e "s|^TAILSCALE_IP=.*|TAILSCALE_IP=|" .env.example > .env
    echo "created .env with fresh passwords"
    if [[ "$os" != Darwin ]]; then
        # No Metal: whisper.cpp in Docker on the CPU (compose profile whisper-docker, started by every
        # docker compose up through COMPOSE_PROFILES), with the models a CPU keeps up with
        set_env COMPOSE_PROFILES whisper-docker
        set_env WHISPER_URL http://whisper:8080/inference
        set_env WHISPER_FAST_URL http://whisper-fast:8080/inference
        set_env WHISPER_MODEL ggml-small.en-q8_0.bin
        set_env WHISPER_FAST_MODEL ggml-base.en-q8_0.bin
        set_env WHISPER_THREADS 4
        echo "whisper in Docker on the CPU: small.en and base.en (WHISPER_MODEL, WHISPER_FAST_MODEL in .env)"
    fi
fi
ip=$(tailscale ip -4 2>/dev/null | head -n 1 || true)
if [[ -n "$ip" ]]; then
    set_env TAILSCALE_IP "$ip"
    echo "Mumble on the Tailscale IP $ip"
else
    echo "WARNING: Tailscale is not connected (tailscale up): Mumble stays on localhost until you run this script again"
fi

# 2. channel: dependencies, and the file that session.sh passes to claude with --mcp-config.
# Not at user scope: every Claude Code session on the Mac would connect to the radio.
(cd channel && "$bun" install --silent)
printf '{\n  "mcpServers": {\n    "radio": {\n      "command": "%s",\n      "args": ["%s/channel/radio.ts"]\n    }\n  }\n}\n' "$bun" "$here" > channel/mcp.json
claude mcp remove --scope user radio >/dev/null 2>&1 || true
echo "radio channel: $bun $here/channel/radio.ts (channel/mcp.json)"

# 3. state hooks and the permission for the radio tools in ~/.claude/settings.json (backup next to it)
settings="$HOME/.claude/settings.json"
[[ -f "$settings" ]] || echo '{}' > "$settings"
# the backup only the first time: on later runs it would already contain the radio.
# settings.json.prima-della-radio is the name used by earlier versions: if it exists, the backup is already there
[[ -f "$settings.before-radio" || -f "$settings.prima-della-radio" ]] || cp "$settings" "$settings.before-radio"
jq --arg w "$here/state-hook.sh working" --arg i "$here/state-hook.sh idle" --arg n "$here/state-hook.sh waiting" '
    # Drops any earlier radio hook (also under the old name stato-hook.sh or another folder), then adds it here
    def is_radio: (.hooks // []) | any(.[]; (.command // "") | (contains("state-hook.sh") or contains("stato-hook.sh")));
    def add_hook($ev; $cmd):
        .hooks[$ev] = ([(.hooks[$ev] // [])[] | select(is_radio | not)] + [{"hooks": [{"type": "command", "command": $cmd}]}]);
    add_hook("UserPromptSubmit"; $w) | add_hook("PreToolUse"; $w) | add_hook("Stop"; $i) | add_hook("Notification"; $n)
    | .permissions.allow = ((.permissions.allow // []) + ["mcp__radio__reply", "mcp__radio__show", "mcp__radio__move"] | unique)
' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
echo "state hooks in $settings"

# 4. the launcher as a service: on the Mac two launchd agents, native whisper-server and launcher (if the plist does
# not change a restart is enough; after a bootout launchd needs a few moments before accepting the bootstrap,
# error 5, EIO), on Linux a systemd user unit
# The plists in mac/ and the unit in linux/ are templates: __RADIO_DIR__, __HOME__, __PYTHON3__ and __PATH__ are filled in here.
# PATH: the folders of claude, node, bun, tmux and python3 as this shell finds them, plus the system ones
agent_path=$(for c in claude node "$bun" tmux python3 jq; do p=$(command -v "$c" 2>/dev/null) && dirname "$p"; done | awk '!seen[$0]++' | paste -sd: -)
agent_path="${agent_path:+$agent_path:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
python3_bin=$(command -v python3)
if [[ "$os" == Darwin ]]; then
    # Migration: earlier versions named the launcher agent radio.lanciatore (Italian for launcher): remove it
    if launchctl print "gui/$(id -u)/radio.lanciatore" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/radio.lanciatore" 2>/dev/null || true
        echo "agent radio.lanciatore removed (now radio.launcher)"
    fi
    rm -f "$HOME/Library/LaunchAgents/radio.lanciatore.plist"
    for agent in radio.whisper radio.launcher; do
        plist="$HOME/Library/LaunchAgents/$agent.plist"
        sed -e "s|__RADIO_DIR__|$here|g" -e "s|__HOME__|$HOME|g" -e "s|__PYTHON3__|$python3_bin|g" -e "s|__PATH__|$agent_path|g" \
            "mac/$agent.plist" > "$plist.new"
        if launchctl print "gui/$(id -u)/$agent" >/dev/null 2>&1; then
            if cmp -s "$plist.new" "$plist"; then
                rm -f "$plist.new"
                launchctl kickstart -k "gui/$(id -u)/$agent"
                echo "agent $agent restarted"
                continue
            fi
            launchctl bootout "gui/$(id -u)/$agent" 2>/dev/null || true
            for _ in $(seq 1 20); do launchctl print "gui/$(id -u)/$agent" >/dev/null 2>&1 || break; sleep 0.5; done
        fi
        mv -f "$plist.new" "$plist"
        for _ in $(seq 1 10); do launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null && break; sleep 1; done
        launchctl print "gui/$(id -u)/$agent" >/dev/null 2>&1 || { echo "agent $agent does not start: launchctl bootstrap gui/$(id -u) $plist" >&2; exit 1; }
        echo "agent $agent started"
    done
else
    # Linux: the launcher as a systemd user service (whisper runs in Docker). KillMode=process in the unit: the
    # launcher starts the tmux server, and a restart that stopped the whole control group would close every session
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    if systemctl --user show-environment >/dev/null 2>&1; then
        mkdir -p "$unit_dir"
        sed -e "s|__RADIO_DIR__|$here|g" -e "s|__PYTHON3__|$python3_bin|g" -e "s|__PATH__|$agent_path|g" \
            linux/radio-launcher.service > "$unit_dir/radio-launcher.service"
        systemctl --user daemon-reload
        systemctl --user enable --quiet radio-launcher.service
        systemctl --user restart radio-launcher.service
        echo "service radio-launcher started"
        # Without lingering, user services stop at logout and start only at login
        [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" == yes ]] ||
            echo "To keep the launcher running after you log out and start it at boot: loginctl enable-linger $(id -un)"
    else
        echo "WARNING: no systemd user session here (WSL without systemd?): start the launcher yourself and keep it running:"
        echo "    tmux new-session -d -s radio-launcher 'python3 $here/launcher.py >> $here/logs/launcher.log 2>&1'"
    fi
fi

# 5. containers: Mumble and bot. projects.json is mounted as a file: if it is missing, Docker creates a folder
# A failed run of docker compose without the file leaves an empty projects.json/ directory behind
[[ -d projects.json ]] && rmdir projects.json 2>/dev/null || true
[[ -f projects.json ]] || { cp projects.example.json projects.json; echo "created projects.json from the example: put your projects in it"; }
docker compose up -d --build

# 6. radio rules in the CLAUDE.md files of the registered projects
python3 - <<'PY' | xargs -I{} ./tools/paste-radio-rules.sh {}
import glob, json, os
for p in json.load(open("projects.json"))["projects"]:
    main = os.path.expanduser(p["dir"]).rstrip("/")
    # the main checkout and the radio worktrees already open (<dir>--<branch>, before <dir>--<name>)
    for d in [main] + sorted(glob.glob(main + "--*")):
        if os.path.isfile(os.path.join(d, "CLAUDE.md")):
            print(os.path.join(d, "CLAUDE.md"))
PY

echo
echo "Done. In Mumla: address ${ip:-<Tailscale IP of this computer>}, port 64738, password: grep MUMBLE_SERVER_PASSWORD .env"
echo "Board on the phone: http://${ip:-<Tailscale IP of this computer>}:8791 (on first access, say \"board code\" to the radio)"
if [[ "$os" == Darwin ]]; then
    echo "Checks: docker compose ps; tail logs/whisper.log; tail logs/launcher.log; docker compose exec -T bot python smoke_test.py"
else
    echo "Checks: docker compose ps; systemctl --user status radio-launcher; tail logs/launcher.log; docker compose exec -T bot python smoke_test.py"
fi
