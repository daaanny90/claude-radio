#!/usr/bin/env bash
# Runs the bot tests in a throwaway container built from bot/Dockerfile, with the working copy of bot/ mounted.
# Needs only Docker: no .env, models or projects.json (the tests use fakes and tests/fixtures/projects.json).
set -euo pipefail
cd "$(dirname "$0")/.."
image=claude-radio-tests
docker build -q -t "$image" bot >/dev/null
exec docker run --rm -v "$PWD/bot:/app:ro" -v "$PWD/tests:/tests:ro" \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
    -e PROJECTS_FILE=/tests/fixtures/projects.json \
    -e LOG_DIR=/tmp/logs -e STATE_DIR=/tmp/state -e BOARD_DIR=/tmp/board \
    "$image" python /tests/run_all.py
