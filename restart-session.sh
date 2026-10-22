#!/usr/bin/env bash
# Restart Claude Code in a radio session, keeping the conversation:
#   ./restart-session.sh <name> [--force]       e.g. ./restart-session.sh bravo
# Needed after a change to the channel (channel/radio.ts) or to the rules (CLAUDE.md): a session
# only picks them up at startup. It closes claude with /exit and relaunches it in the same folder
# with --continue, which resumes the last conversation of that folder. Worktree, branch and
# uncommitted changes are not touched. It refuses if the session is working (unless --force).
set -euo pipefail
name="${1:?session name (alpha, bravo...)}"
here="$(cd "$(dirname "$0")" && pwd)"
tmux has-session -t "$name" 2>/dev/null || { echo "session $name does not exist"; exit 1; }

state=$(jq -r '.state // ""' "$here/state/$name.json" 2>/dev/null || true)
if [[ "$state" == "working" && "${2:-}" != "--force" ]]; then
    echo "$name is working: wait for it to finish (or add --force)"; exit 1
fi

dir=$(tmux display -p -t "$name" '#{pane_current_path}')
shell_active() { [[ "$(tmux display -p -t "$name" '#{pane_current_command}')" =~ ^-?(zsh|bash|sh|fish)$ ]]; }
pane() { tmux capture-pane -p -t "$name" | sed '/^\s*$/d'; }

if ! shell_active; then
    # Escape closes any dialog open above the prompt, then /exit closes claude
    tmux send-keys -t "$name" Escape
    sleep 0.5
    tmux send-keys -t "$name" "/exit"
    sleep 0.5
    tmux send-keys -t "$name" Enter
    for _ in $(seq 1 20); do
        sleep 1
        shell_active && break
    done
    shell_active || { echo "claude in $name did not close: look with tmux attach -t $name"; exit 1; }
fi

# Model of the radio sessions (RADIO_MODEL in .env, e.g. opus): empty = Claude Code's default
model=$(sed -n 's/^RADIO_MODEL=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
# Effort of the radio sessions (RADIO_EFFORT: low, medium, high, xhigh, max): empty = the one in the settings
effort=$(sed -n 's/^RADIO_EFFORT=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
# Project folders (PROJECT_ROOTS): the lobby and the operator look for projects there
roots=$(sed -n 's/^PROJECT_ROOTS=//p' "$here/.env" 2>/dev/null | tail -n 1 | tr -d '"')
# Same command as session.sh, plus --continue
tmux send-keys -t "$name" " unset ANTHROPIC_API_KEY; RADIO_SESSION=$name RADIO_STATE_DIR=$here/state RADIO_DIR=$here RADIO_PROJECT_ROOTS=$roots claude --continue ${model:+--model $model} ${effort:+--effort $effort} --mcp-config $here/channel/mcp.json --dangerously-load-development-channels server:radio" Enter
for _ in $(seq 1 30); do
    sleep 2
    p=$(pane)
    if echo "$p" | grep -q "I am using this for local development"; then tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -q "Yes, I trust this folder"; then tmux send-keys -t "$name" Down; sleep 0.5; tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -q "Not now"; then tmux send-keys -t "$name" Down; sleep 0.5; tmux send-keys -t "$name" Enter; continue; fi
    if echo "$p" | grep -qE "mode on|\? for shortcuts|for shortcuts"; then
        echo "session $name restarted in $dir with the previous conversation"; exit 0
    fi
done
echo "claude relaunched in $name but I do not see the prompt: look with tmux attach -t $name"
exit 1
