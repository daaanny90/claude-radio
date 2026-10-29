#!/usr/bin/env bash
# Runs the bot tests in a throwaway container built from bot/Dockerfile, with the working copy of bot/ mounted,
# then the launcher's on the host.
# Needs Docker, and python3 and git on the host: no .env, models or projects.json (the tests use fakes and tests/fixtures/projects.json).
set -euo pipefail
cd "$(dirname "$0")/.."
image=claude-radio-tests
docker build -q -t "$image" bot >/dev/null
failed=0
docker run --rm -v "$PWD/bot:/app:ro" -v "$PWD/tests:/tests:ro" \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
    -e PROJECTS_FILE=/tests/fixtures/projects.json \
    -e LOG_DIR=/tmp/logs -e STATE_DIR=/tmp/state -e BOARD_DIR=/tmp/board \
    "$image" python /tests/run_all.py || failed=1
# the launcher runs on the host (git, tmux), and so do its tests, on throwaway repositories
PYTHONDONTWRITEBYTECODE=1 python3 tests/run_all.py tests/host || failed=1
exit "$failed"
