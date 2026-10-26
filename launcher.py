#!/usr/bin/env python3
"""
Radio launcher for Claude Code sessions. It runs on the host, outside Docker, because tmux
and Claude Code live on the host while the bot lives in a container. On the Mac it is a
launchd agent (mac/radio.launcher.plist, installed by install-mac.sh).

It connects to the bot (GET /launcher/events, SSE) and waits for orders:
  open  {name, dir, base_branch, branch, copy, install}
        -> git worktree <dir>--<name> on the branch, copy of the git-ignored files,
           pnpm install if there is a lockfile, then session.sh <name> <worktree>
  open  {name, lobby: true}
        -> session in the lobby (lobby/): finds the unregistered project and moves there
  move  {name, dir}
        -> closes the session and reopens it in dir, which must be under PROJECT_ROOTS (.env)
  interrupt {names}  -> one Esc in the terminal of each session: the emergency word
  close {name}  -> closes the tmux session; the worktree stays until it is merged
Every outcome goes back to the bot with POST /launcher/result.
"""
import json
import logging
import os
import shlex
import shutil
import subprocess
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


def run(cmd, timeout=120, cwd=None):
    logging.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def tail(text, n=3):
    return " ".join((text or "").strip().splitlines()[-n:])[:300]


def open_session(ev, before_start=None):
    """Create (or resume) the worktree and start the session in it. before_start: called once the worktree
    is ready, before claude starts (moving out of the lobby closes the old session there)."""
    name, branch = ev["name"], ev["branch"]
    main = os.path.expanduser(ev["dir"])
    base = ev.get("base_branch") or "master"
    worktree = f"{main}--{name}"
    notes = []
    if not os.path.isdir(main):
        raise RuntimeError(f"project folder not found: {main}")
    if not os.path.isdir(worktree):
        r = run(["git", "-C", main, "worktree", "add", "-b", branch, worktree, base])
        if r.returncode != 0:   # the branch already exists: resume it
            r = run(["git", "-C", main, "worktree", "add", worktree, branch])
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
    """The name of another radio session already working in that folder, if any."""
    r = run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"])
    for line in r.stdout.splitlines():
        session_name, _, path = line.partition("\t")
        if session_name in NATO and session_name != exclude and os.path.realpath(path) == folder:
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
    except Exception as e:   # the error must be spoken aloud, it must not stop the launcher
        logging.exception("order failed")
        post("/launcher/result", {"name": name, "ok": False, "detail": str(e)[:200]})


def listen():
    while True:
        try:
            with urllib.request.urlopen(BOT + "/launcher/events", timeout=None) as res:
                logging.info("connected to the bot")
                for raw in res:
                    line = raw.decode("utf-8").rstrip("\n")
                    if line.startswith("data:"):
                        handle(json.loads(line[5:].strip()))
            logging.info("the bot closed the connection")
        except Exception as e:
            logging.warning("bot unreachable: %s", e)
        time.sleep(3)


if __name__ == "__main__":
    listen()
