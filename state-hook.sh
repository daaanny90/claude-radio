#!/usr/bin/env bash
# Claude Code hook (configured in ~/.claude/settings.json by install-mac.sh): writes the radio
# session state to state/<name>.json. The bot reads it for "status", for the ticks while Claude
# works, for the progress phrases, and to read the last terminal text if Claude finishes
# without using reply.
#
# Argument: working | idle | waiting. The hook JSON arrives on stdin
# (hook_event_name, tool_name, tool_input, last_assistant_message...).
# Fields written: state, ts, event, tool, target, steps (tools used since the last phrase),
# tool_ts (last tool other than reply: tells whether Claude worked after the last answer), last_text,
# notice (the notification text, when the session is waiting for something on screen).
# Without jq only state and ts are saved, as before.
[[ -n "${RADIO_SESSION:-}" && -n "${RADIO_STATE_DIR:-}" ]] || exit 0
mkdir -p "$RADIO_STATE_DIR"
file="$RADIO_STATE_DIR/$RADIO_SESSION.json"
state=$1
input=$(cat 2>/dev/null)
[[ "$input" == \{* ]] || input='{}'
old=$(cat "$file" 2>/dev/null)
jq_bin=$(command -v jq || true)
[[ -z "$jq_bin" && -x "$HOME/.local/bin/jq" ]] && jq_bin="$HOME/.local/bin/jq"

if [[ -n "$jq_bin" ]] && printf '%s' "$input" | "$jq_bin" -c \
    --arg state "$state" --argjson ts "$(date +%s)" --arg old "$old" '
    ($old | try fromjson catch {}) as $old
    | (.tool_input // {}) as $i
    | ($i.description // $i.file_path // $i.notebook_path // $i.pattern // $i.url
       // $i.query // $i.command // $i.skill // "" | tostring | .[0:160]) as $target
    | {
        state: $state,
        ts: $ts,
        event: (.hook_event_name // ""),
        tool: (if $state == "working" then (.tool_name // "") else "" end),
        target: (if $state == "working" then $target else "" end),
        steps: (if .tool_name then (if $old.state == "working" then (($old.steps // 0) + 1) else 1 end)
                elif $state == "working" then 0
                else ($old.steps // 0) end),
        tool_ts: (if .tool_name and .tool_name != "mcp__radio__reply" then $ts else ($old.tool_ts // 0) end),
        notice: (if $state == "waiting" then ((.message // "") | tostring | .[0:200]) else "" end),
        last_text: (if $state == "idle" then ((.last_assistant_message // "") | tostring | .[0:1500])
                    else ($old.last_text // "") end)
      }' > "$file.tmp" 2>/dev/null; then
    mv -f "$file.tmp" "$file"
    exit 0
fi
rm -f "$file.tmp"
printf '{"state":"%s","ts":%s}\n' "$state" "$(date +%s)" > "$file"
exit 0
