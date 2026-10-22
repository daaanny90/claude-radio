#!/usr/bin/env bash
# Open a Claude Code session on the radio:
#   ./session.sh <name> <folder>      e.g. ./session.sh alpha ~/code/my-app
# Creates a tmux session with that name, starts claude with the radio channel and gets past
# the initial dialogs by itself (folder trust, development channels warning, promotions).
# Names: NATO alphabet (alpha, bravo, charlie...). To watch it: tmux attach -t <name>.
# The session state ends up in state/<name>.json (Claude Code hooks, see state-hook.sh).
set -euo pipefail
name="${1:?session name (alpha, bravo, charlie...)}"
dir="${2:?project folder}"
dir="$(cd "$dir" && pwd)"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/state"
if tmux has-session -t "$name" 2>/dev/null; then echo "session $name is already open"; exit 1; fi
# The user's interactive shell (zsh on the Mac) loads PATH, nvm and the rest; the command arrives
# via send-keys. An ANTHROPIC_API_KEY exported from the shell profile would override the Claude
# subscription login: unset it.
# The radio channel comes from channel/mcp.json for this session only: registered at user scope
# it would attach every Claude Code session on the Mac to the radio.
# Model of the radio sessions (RADIO_MODEL in .env, e.g. opus): empty = Claude Code's default
model=$(sed -n 's/^RADIO_MODEL=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
# Effort of the radio sessions (RADIO_EFFORT: low, medium, high, xhigh, max): empty = the one in the settings
effort=$(sed -n 's/^RADIO_EFFORT=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
# Project folders (PROJECT_ROOTS): the lobby and the operator look for projects there
roots=$(sed -n 's/^PROJECT_ROOTS=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
tmux new-session -d -s "$name" -x 160 -y 45 -c "$dir"
tmux send-keys -t "$name" " unset ANTHROPIC_API_KEY; RADIO_SESSION=$name RADIO_STATE_DIR=$here/state RADIO_DIR=$here RADIO_PROJECT_ROOTS=$roots claude ${model:+--model $model} ${effort:+--effort $effort} --mcp-config $here/channel/mcp.json --dangerously-load-development-channels server:radio" Enter
pane() { tmux capture-pane -p -t "$name" | sed '/^\s*$/d'; }
for _ in $(seq 1 30); do
    sleep 2
    p=$(pane)
    if echo "$p" | grep -q "I am using this for local development"; then tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -q "Yes, I trust this folder"; then tmux send-keys -t "$name" Down; sleep 0.5; tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -q "Not now"; then tmux send-keys -t "$name" Down; sleep 0.5; tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -qE "mode on|\? for shortcuts|for shortcuts"; then
        sleep 6   # the bot must notice any stale adapter with the same name
        echo "session $name ready in $dir"; exit 0
    fi
done
echo "session $name started but I do not see the prompt: look with tmux attach -t $name"
exit 1
