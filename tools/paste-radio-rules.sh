#!/usr/bin/env bash
# Adds or updates the "## Radio" section (text in CLAUDE-radio.md) in the CLAUDE.md files passed.
# Usage: tools/paste-radio-rules.sh <CLAUDE.md>...
# The section runs from the "## Radio" heading to the next second-level heading or the end of the file.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
section="$here/CLAUDE-radio.md"
[[ -f "$section" ]] || { echo "missing $section" >&2; exit 1; }
for file in "$@"; do
    if [[ ! -f "$file" ]]; then
        echo "skipping $file: does not exist" >&2
        continue
    fi
    tmp=$(mktemp)
    if grep -q '^## Radio' "$file"; then
        awk -v sec="$section" '
            /^## Radio/ { while ((getline line < sec) > 0) print line; skip = 1; next }
            skip && /^## / { skip = 0; print ""; }
            !skip { print }
        ' "$file" > "$tmp"
        echo "updated: $file"
    else
        { cat "$file"; echo; cat "$section"; } > "$tmp"
        echo "added: $file"
    fi
    cat "$tmp" > "$file"
    rm -f "$tmp"
done
