#!/usr/bin/env python3
"""
Radio launcher for Claude Code sessions. It runs on the host, outside Docker, because tmux
and Claude Code live on the host while the bot lives in a container. On the Mac it is a
launchd agent (mac/radio.launcher.plist, installed by install-mac.sh).

It connects to the bot (GET /launcher/events, SSE) and waits for orders:
  open  {name, dir, base_branch, branch, copy, install, existing}
        -> the branch's git worktree: the one it already has, or a new <dir>--<branch> with a copy of the
           git-ignored files and pnpm install if there is a lockfile; then session.sh <name> <worktree>.
           existing: the branch must already be there, locally or on origin
  open  {name, lobby: true}
        -> session in the lobby (lobby/): finds the unregistered project and moves there
  move  {name, dir}
        -> closes the session and reopens it in dir, which must be under PROJECT_ROOTS (.env)
  interrupt {names}  -> one Esc in the terminal of each session: the emergency word
  close {name}  -> closes the tmux session; the worktree stays until it is merged
  clear {name}  -> /clear in an idle session: a fresh start, the conversation stays on disk
  effort {name, level, restore}
        -> saver mode: the effort of an idle session for this session only, down to level (RADIO_SAVER_EFFORT
           when missing), or with restore back to the level it had; answers with the level before and after
Every outcome goes back to the bot with POST /launcher/result.
Every USAGE_EVERY_S (.env, 0 = off) it also reads Claude's plan limits with `claude -p /usage` and posts them
to the bot (POST /usage), for the board and the warnings.
"""
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request

BOT = os.getenv("RADIO_BOT_URL", "http://127.0.0.1:8790")
RADIO_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_SCRIPT = os.path.join(RADIO_DIR, "session.sh")
LOBBY_DIR = os.path.join(RADIO_DIR, "lobby")
OPERATOR_DIR = os.path.join(RADIO_DIR, "operator")
DEFAULT_ROOTS = ""   # PROJECT_ROOTS in .env
NATO = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
        "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
        "uniform", "victor", "whiskey", "xray", "yankee", "zulu")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")


def post(path, body):
    req = urllib.request.Request(BOT + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def run(cmd, timeout=120, cwd=None, env=None):
    logging.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def tail(text, n=3):
    return " ".join((text or "").strip().splitlines()[-n:])[:300]


def has_ref(main, ref):
    return run(["git", "-C", main, "rev-parse", "--verify", "--quiet", ref]).returncode == 0


def worktree_of(main, branch):
    """Where git has the branch checked out (the main checkout or a worktree), or None."""
    r = run(["git", "-C", main, "worktree", "list", "--porcelain"])
    for block in r.stdout.split("\n\n"):
        path, checked_out = re.search(r"^worktree (.+)$", block, re.M), re.search(r"^branch refs/heads/(.+)$", block, re.M)
        if path and checked_out and checked_out.group(1) == branch:
            return os.path.realpath(path.group(1))
    return None


def fetch(main, branch):
    """The branch from origin: None, or why not. No prompts and a short timeout: the launcher handles one order at a
    time, and a fetch hanging on the network (a VPN that is off) would hold up "all stop" too."""
    try:
        r = run(["git", "-C", main, "fetch", "origin", branch], timeout=30, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except subprocess.TimeoutExpired:
        return "git fetch timed out"
    return None if r.returncode == 0 else ((r.stderr or "").strip().splitlines() or ["git fetch failed"])[0][:200]


def open_session(ev, before_start=None):
    """Find or create the branch's worktree and start the session in it. The worktree belongs to the branch, not to
    the session's name: names come back (on 29 October the next free name, echo, had old worktrees on other branches,
    and a new echo would have worked in one of them), the branch is the work. Git keeps a branch in one worktree at
    most: the same branch resumes its own, with a note, a new one gets <dir>--<branch>. existing: the branch must be
    there already, locally or on origin (someone's merge request), and is never created from the base under that
    name. before_start: called once the worktree is ready, before claude starts (moving out of the lobby closes the
    old session there)."""
    name, branch = ev["name"], ev["branch"]
    main = os.path.expanduser(ev["dir"])
    base = ev.get("base_branch") or "master"
    notes = []
    if not os.path.isdir(main):
        raise RuntimeError(f"project folder not found: {main}")
    run(["git", "-C", main, "worktree", "prune"])   # a worktree deleted by hand no longer holds its branch
    fetch_error = fetch(main, branch) if ev.get("existing") else None
    if fetch_error:
        notes.append(f"{branch} not fetched ({fetch_error})")
    worktree = worktree_of(main, branch)
    if ev.get("existing") and not fetch_error and has_ref(main, f"refs/heads/{branch}"):
        behind = run(["git", "-C", main, "rev-list", "--count", f"{branch}..origin/{branch}"]).stdout.strip()
        ahead = run(["git", "-C", main, "rev-list", "--count", f"origin/{branch}..{branch}"]).stdout.strip()
        if behind not in ("", "0") and ahead == "0" and not worktree:   # a review on yesterday's copy: bring it up to date
            run(["git", "-C", main, "branch", "-f", branch, f"origin/{branch}"])
        elif behind not in ("", "0"):
            notes.append(f"{behind} commits behind origin")
    if worktree == os.path.realpath(main):
        raise RuntimeError(f"{branch} is checked out in the main folder {main}: switch it there first")
    if worktree:
        other = session_in(worktree, name)
        if other:
            raise RuntimeError(f"{other} is already working on {branch}")
        changes = len(run(["git", "-C", worktree, "status", "--porcelain"]).stdout.splitlines())
        notes.append(f"resumed the worktree of {branch}" + (f" with {changes} uncommitted changes" if changes else ""))
    else:
        worktree = f"{main}--{branch.replace('/', '-')}"
        if has_ref(main, f"refs/heads/{branch}"):
            cmd = ["worktree", "add", worktree, branch]
        elif has_ref(main, f"refs/remotes/origin/{branch}"):
            cmd = ["worktree", "add", "--track", "-b", branch, worktree, f"origin/{branch}"]
        elif ev.get("existing"):
            raise RuntimeError(f"branch {branch} not found" + (f": {fetch_error}" if fetch_error else ""))
        else:
            cmd = ["worktree", "add", "-b", branch, worktree, base]
        r = run(["git", "-C", main, *cmd])
        if r.returncode != 0:
            raise RuntimeError("git worktree failed: " + tail(r.stderr))
        # Git-ignored files that the worktree does not bring along (CLAUDE.md, .claude, ...)
        for item in ev.get("copy", []):
            src, dst = os.path.join(main, item), os.path.join(worktree, item)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            elif os.path.exists(src) or os.path.islink(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst, follow_symlinks=False)
        if ev.get("install", True) and os.path.exists(os.path.join(worktree, "pnpm-lock.yaml")):
            r = run(["bash", "-lc", f"cd {shlex.quote(worktree)} && pnpm install --frozen-lockfile --prefer-offline"], timeout=900)
            if r.returncode != 0:
                notes.append("dependencies not installed")
                logging.warning("pnpm install failed: %s", tail(r.stderr))
    if before_start:
        before_start()
    r = run([SESSION_SCRIPT, name, worktree], timeout=180)
    if r.returncode != 0:
        raise RuntimeError("Claude Code did not start: " + tail(r.stdout + r.stderr))
    return worktree, notes


def close_session(ev):
    r = run(["tmux", "kill-session", "-t", ev["name"]])
    if r.returncode != 0:
        raise RuntimeError("no such session")
    # The session's Storybook (tools/storybook.sh): each one holds about a GB of memory
    run(["tmux", "kill-session", "-t", f"storybook-{ev['name']}"])


def clear_session(ev):
    """A fresh start: /clear typed at the session's prompt. The conversation stays on disk (/resume brings it back),
    the context starts empty. Only between two turns: typed during one, it would wait there as the next message."""
    name = ev["name"]
    try:
        with open(os.path.join(RADIO_DIR, "state", f"{name}.json"), encoding="utf-8") as f:
            working = json.load(f).get("state") == "working"
    except (OSError, ValueError):
        working = False
    if working:
        raise RuntimeError(f"{name} is working: stop it first")
    if run(["tmux", "has-session", "-t", name]).returncode != 0:
        raise RuntimeError("no such session")
    run(["tmux", "send-keys", "-t", name, "/clear"])
    time.sleep(0.5)   # the slash command menu opens first, as in restart-session.sh
    run(["tmux", "send-keys", "-t", name, "Enter"])


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_LABEL = re.compile(r"\b(low|medium|high|xhigh|max)\b")


def slider_level(screen):
    """The level the /effort slider points at: the label nearest under its ▲ (None: no slider on screen)."""
    lines = screen.splitlines()
    for line, below in zip(lines, lines[1:]):
        col = line.find("▲")
        labels = [(abs(m.start() + len(m.group(1)) / 2 - col), m.group(1)) for m in EFFORT_LABEL.finditer(below)]
        if col >= 0 and len(labels) == len(EFFORT_LEVELS):
            return min(labels)[1]
    return None


def set_effort(ev):
    """Saver mode: the effort of an idle session, through the /effort slider and its "s" (this session only). A
    level typed after /effort would also become the default of every new session, the desk's too, and /model would
    break the cache. Down only, unless restore gives back the level the session had. Keys go only to a session at its
    prompt: typed into a permission dialog, the Enter after /effort could answer it."""
    name = ev["name"]
    target = ev.get("level") or radio_env("RADIO_SAVER_EFFORT", "high")
    if target not in EFFORT_LEVELS:
        raise RuntimeError(f"unknown effort {target}")
    try:
        with open(os.path.join(RADIO_DIR, "state", f"{name}.json"), encoding="utf-8") as f:
            state = json.load(f).get("state")
    except (OSError, ValueError):
        state = None
    if state != "idle":
        raise RuntimeError(f"{name} is not idle")
    pane = lambda: run(["tmux", "capture-pane", "-p", "-t", name]).stdout
    keys = lambda *k: run(["tmux", "send-keys", "-t", name, *k])
    screen = pane()
    if "shift+tab to cycle" not in screen or "Esc to cancel" in screen:
        raise RuntimeError(f"{name} is not at its prompt")

    def open_slider():
        keys("/effort")
        time.sleep(0.5)   # the slash command menu opens first
        keys("Enter")
        time.sleep(1.2)
        level = slider_level(pane())
        if level is None:
            keys("Escape")
            raise RuntimeError("the effort slider did not open")
        return level

    before = open_slider()
    if not ev.get("restore") and EFFORT_LEVELS.index(before) <= EFFORT_LEVELS.index(target):
        keys("Escape")   # already that low (the operator, the lobby): nothing to save
        return before, before
    keys(*["Left"] * len(EFFORT_LEVELS), *["Right"] * EFFORT_LEVELS.index(target))   # Left stops at low
    time.sleep(0.3)
    keys("s")
    time.sleep(1.5)
    after = open_slider()
    keys("Escape")
    if after != target:
        raise RuntimeError(f"the effort is {after}, not {target}")
    return before, after


def radio_env(key, default=""):
    """One line of the radio's .env: the launcher runs outside Docker and reads it itself."""
    try:
        with open(os.path.join(RADIO_DIR, ".env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"') or default
    except OSError:
        pass
    return default


def project_roots():
    return [os.path.realpath(os.path.expanduser(r)) for r in radio_env("PROJECT_ROOTS", DEFAULT_ROOTS).split(":") if r.strip()]


def registered_dirs():
    try:
        with open(os.path.join(RADIO_DIR, "projects.json"), encoding="utf-8") as f:
            return {os.path.realpath(os.path.expanduser(p["dir"])) for p in json.load(f).get("projects", [])}
    except (OSError, ValueError, KeyError):
        return set()


def session_in(folder, exclude):
    """The name of another radio session already working in that folder (or below it, cd'd into a package), if any."""
    r = run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"])
    for line in r.stdout.splitlines():
        session_name, _, path = line.partition("\t")
        path = os.path.realpath(path)
        if session_name in NATO and session_name != exclude and (path == folder or path.startswith(folder + os.sep)):
            return session_name
    return None


def open_lobby(ev):
    os.makedirs(LOBBY_DIR, exist_ok=True)
    r = run([SESSION_SCRIPT, ev["name"], LOBBY_DIR], timeout=180)
    if r.returncode != 0:
        raise RuntimeError("Claude Code did not start: " + tail(r.stdout + r.stderr))
    return LOBBY_DIR


def allowed_folder(requested_dir, name):
    """Only folders under the project roots, never the radio itself nor the main checkout of a registered
    project (those work in their own worktrees), never where another session is already working."""
    folder = os.path.realpath(os.path.expanduser(requested_dir or ""))
    if not os.path.isdir(folder):
        raise RuntimeError(f"folder not found: {requested_dir}")
    if folder in project_roots() or not any(folder.startswith(r + os.sep) for r in project_roots()):
        raise RuntimeError("only project folders under " + ", ".join(project_roots()) if project_roots()
                           else "no project roots: set PROJECT_ROOTS in the radio's .env")
    if folder == RADIO_DIR or folder.startswith(RADIO_DIR + os.sep):
        raise RuntimeError("the radio folder itself is off limits")
    if folder in registered_dirs():
        raise RuntimeError("it is a registered project: ask to open it by name, it gets its own worktree")
    other = session_in(folder, name)
    if other:
        raise RuntimeError(f"{other} is already working in that folder")
    return folder


def open_in(ev):
    """Open a session directly in a folder (requested by the operator)."""
    folder = allowed_folder(ev.get("dir_direct"), ev["name"])
    r = run([SESSION_SCRIPT, ev["name"], folder], timeout=180)
    if r.returncode != 0:
        raise RuntimeError("Claude Code did not start: " + tail(r.stdout + r.stderr))
    return folder


def move_session(ev):
    """Close the session and reopen it in the project folder (see allowed_folder)."""
    name = ev["name"]
    folder = allowed_folder(ev.get("dir"), name)
    notes = []
    r = run(["git", "-C", folder, "status", "--porcelain"])
    if r.returncode == 0 and r.stdout.strip():
        notes.append(f"{len(r.stdout.splitlines())} uncommitted changes there")
    run(["tmux", "kill-session", "-t", name])
    run(["tmux", "kill-session", "-t", f"storybook-{name}"])
    r = run([SESSION_SCRIPT, name, folder], timeout=180)
    if r.returncode != 0:
        raise RuntimeError("Claude Code did not start: " + tail(r.stdout + r.stderr))
    return folder, notes


def handle(ev):
    name = ev.get("name", "?")
    try:
        if ev["type"] == "open" and ev.get("lobby"):
            post("/launcher/result", {"name": name, "ok": True, "dir": open_lobby(ev)})
        elif ev["type"] == "open" and ev.get("operator"):
            # The bot asks for the operator only when it does not see it connected: a tmux session with that name
            # is dead (Claude Code exited, or it sits in a folder that no longer exists) and is replaced
            run(["tmux", "kill-session", "-t", name])
            r = run([SESSION_SCRIPT, name, OPERATOR_DIR], timeout=180)
            if r.returncode != 0:
                raise RuntimeError("Claude Code did not start: " + tail(r.stdout + r.stderr))
            post("/launcher/result", {"name": name, "ok": True, "dir": OPERATOR_DIR})
        elif ev["type"] == "open" and ev.get("dir_direct"):
            post("/launcher/result", {"name": name, "ok": True, "dir": open_in(ev)})
        elif ev["type"] == "interrupt":
            # emergency word: one Esc stops whatever Claude Code is doing (and closes its dialogs)
            for target in ev.get("names", []):
                run(["tmux", "send-keys", "-t", target, "Escape"])
        elif ev["type"] == "move" and ev.get("project"):
            # from the lobby to a registered project: reopen in its worktree, like "open". The lobby closes
            # only once the worktree is ready: if git fails the session stays where it is and hears why
            def close_lobby():
                run(["tmux", "kill-session", "-t", name])
                run(["tmux", "kill-session", "-t", f"storybook-{name}"])
            worktree, notes = open_session(ev, before_start=close_lobby)
            post("/launcher/result", {"name": name, "ok": True, "moved": True, "dir": worktree, "detail": ", ".join(notes)})
        elif ev["type"] == "move":
            folder, notes = move_session(ev)
            post("/launcher/result", {"name": name, "ok": True, "moved": True, "dir": folder, "detail": ", ".join(notes)})
        elif ev["type"] == "open":
            worktree, notes = open_session(ev)
            post("/launcher/result", {"name": name, "ok": True, "dir": worktree, "detail": ", ".join(notes)})
        elif ev["type"] == "close":
            close_session(ev)
            post("/launcher/result", {"name": name, "ok": True, "closed": True})
        elif ev["type"] == "clear":
            clear_session(ev)
            post("/launcher/result", {"name": name, "ok": True, "action": "clear"})
        elif ev["type"] == "effort":
            before, after = set_effort(ev)
            post("/launcher/result", {"name": name, "ok": True, "action": "effort", "from": before, "level": after})
    except Exception as e:   # the error must be spoken aloud, it must not stop the launcher
        logging.exception("order failed")
        post("/launcher/result", {"name": name, "ok": False, "detail": str(e)[:200],
                                  **({"action": ev["type"]} if ev.get("type") in ("clear", "effort") else {})})


# A session whose state has said "working" this long with no hook since is checked against its terminal
STUCK_S = 90
# Claude Code's status line while it works: a verb with an ellipsis and the seconds, "✢ Enchanting… (7s · ↓ 232
# tokens)" (checked on 2.1.310, which no longer says "esc to interrupt"; that one is still accepted)
WORKING_RE = re.compile(r"…\s*\(\d+[smh]|esc to interrupt")


def unstick(name, state_file):
    """The Claude Code hooks can leave a session "working" after its turn ended: on 26 October echo ended a turn
    with a ScheduleWakeup call and no Stop hook came, so the board showed it at work while it sat at the prompt.
    The terminal is the truth: without Claude Code's working line in it, the session is idle."""
    try:
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    if state.get("state") != "working" or time.time() - (state.get("ts") or 0) < STUCK_S:
        return False
    pane = run(["tmux", "capture-pane", "-p", "-t", name])
    if pane.returncode != 0 or WORKING_RE.search(pane.stdout):
        return False
    state.update(state="idle", event="PaneIdle", ts=int(time.time()))
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, state_file)
    logging.info("%s: the hooks said working, the terminal says idle", name)
    return True


def watch_panes():
    state_dir = os.path.join(RADIO_DIR, "state")
    while True:
        time.sleep(30)
        try:
            for entry in os.listdir(state_dir):
                if entry.endswith(".json"):
                    unstick(entry[:-5], os.path.join(state_dir, entry))
        except Exception:
            logging.exception("pane check failed")


# Set when the launcher (re)connects to the bot: a restarted bot has lost the last reading, read it again at once
usage_now = threading.Event()


def watch_usage():
    """/usage answers from the CLI itself, without a model call: no tokens spent. No user settings (no hooks), no
    transcript, and no API key from the environment, which would replace the subscription being measured."""
    every = float(radio_env("USAGE_EVERY_S", "300"))
    if every <= 0:
        return
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = ["claude", "-p", "/usage", "--setting-sources", "", "--no-session-persistence"]
    while True:
        try:
            out = subprocess.run(cmd, cwd=RADIO_DIR, env=env, capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                post("/usage", {"text": out.stdout, "every": every})
            else:
                logging.warning("claude /usage failed: %s", tail(out.stderr or out.stdout))
        except Exception as e:
            logging.warning("usage not read: %s", e)
        usage_now.wait(every)
        usage_now.clear()


def listen():
    while True:
        try:
            with urllib.request.urlopen(BOT + "/launcher/events", timeout=None) as res:
                logging.info("connected to the bot")
                usage_now.set()
                for raw in res:
                    line = raw.decode("utf-8").rstrip("\n")
                    if line.startswith("data:"):
                        handle(json.loads(line[5:].strip()))
            logging.info("the bot closed the connection")
        except Exception as e:
            logging.warning("bot unreachable: %s", e)
        time.sleep(3)


if __name__ == "__main__":
    threading.Thread(target=watch_panes, daemon=True, name="panes").start()
    threading.Thread(target=watch_usage, daemon=True, name="usage").start()
    listen()
