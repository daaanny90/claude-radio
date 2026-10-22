# claude-radio

Notes for Claude Code sessions that develop this repository. If your working directory is `lobby/` or `operator/`, you are a radio session, not a developer: ignore this file and follow your own `CLAUDE.md`.

- Read `README.md` first: architecture, setup and configuration.
- The bot (`bot/radio_bot.py`) is deterministic: routing, commands and feedback are plain Python, no second language model. Reasoning happens in the Claude Code sessions.
- Run the tests with `./tests/run.sh` (they run in a throwaway bot container with fake Mumble, sessions and speakers, against `tests/fixtures/projects.json`). Add a test for every routing or feedback bug you fix, using the phrase that went wrong.
- Comments explain why, not what. Code, comments and docs are in English. The Italian (and German) words inside the voice-command regexes are features for Italian speakers: keep them.
- Never commit personal data: `.env`, `projects.json`, `local/`, `logs/`, `board/`, `state/` and `models/` are git-ignored for a reason. Examples use neutral names (`SHOP-1234`, "the ui library").
- Several components talk to each other by name: the bot's HTTP API (used by `channel/radio.ts`, `launcher.py`, `tools/operator_tool.py`), the launcher events, the CSV log event names (parsed by `bot/summary.py` and on restart), the MCP server name `radio`, and the `## Radio` heading of `CLAUDE-radio.md`. Change both sides together.
