#!/usr/bin/env bash
# Status line of the radio sessions (channel/settings.json, passed by session.sh): after each answer Claude Code
# hands it, at no token cost, the session's model, effort, context and cost as JSON. It keeps that JSON in
# state/status/<name>.json for the board, then draws the user's own status line, if there is one.
input=$(cat)
if [[ -n "${RADIO_SESSION:-}" && -n "${RADIO_STATE_DIR:-}" ]]; then
    mkdir -p "$RADIO_STATE_DIR/status"
    file="$RADIO_STATE_DIR/status/$RADIO_SESSION.json"
    printf '%s' "$input" > "$file.tmp" && mv "$file.tmp" "$file"
fi
jq_bin=$(command -v jq || true)
[[ -z "$jq_bin" && -x "$HOME/.local/bin/jq" ]] && jq_bin="$HOME/.local/bin/jq"
own=""
[[ -n "$jq_bin" ]] && own=$("$jq_bin" -r '.statusLine.command // empty' "$HOME/.claude/settings.json" 2>/dev/null)
[[ -n "$own" ]] && printf '%s' "$input" | sh -c "$own"
exit 0
