#!/usr/bin/env python3
"""
Tools for the radio operator (the "operator" session, which the user calls with "Operator, ...").
Runs on the host. Usage:
    operator_tool.py sessions                      open sessions: project, folder, branch, state, unsaved work
    operator_tool.py open <project|folder> [request...]   open a session (registered project: its own worktree; folder: there)
    operator_tool.py close <name...> | all         close sessions (all = every session except the operator)
    operator_tool.py restart <name>                restart a session keeping its conversation
    operator_tool.py worktrees                     worktrees of the registered projects: session, changes, unpushed commits
    operator_tool.py remove-worktree <path> [--force] [--branch]   remove a worktree (and its branch if already merged)
    operator_tool.py storybook [stop <port>]       running Storybooks (port, folder, memory) and stopping one
    operator_tool.py memory                        swap, load and the heaviest processes
Without --force it refuses to remove a worktree with uncommitted changes, unpushed commits or a session inside.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

RADIO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.getenv("RADIO_BOT_URL", "http://127.0.0.1:8790")


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def api(path, body=None):
    req = urllib.request.Request(BOT + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def projects():
    with open(os.path.join(RADIO, "projects.json"), encoding="utf-8") as f:
        return json.load(f).get("projects", [])


def panes():
    """tmux session -> folder it is working in."""
    _, out = sh("tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}")
    return dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)


def git_state(folder):
    """branch, uncommitted changes, unpushed commits (None if there is no upstream)."""
    rc, branch = sh("git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return None, 0, None
    _, st = sh("git", "-C", folder, "status", "--porcelain")
    rc, ahead = sh("git", "-C", folder, "rev-list", "--count", "@{u}..HEAD")
    return branch, len(st.splitlines()) if st else 0, int(ahead) if rc == 0 and ahead.isdigit() else None


def sessions():
    opened = api("/sessions")
    dirs = panes()
    for name in opened.get("sessions", []):
        folder = dirs.get(name, "?")
        try:
            with open(os.path.join(RADIO, "state", f"{name}.json")) as f:
                state = json.load(f).get("state", "?")
        except (OSError, ValueError):
            state = "?"
        branch, changes, ahead = git_state(folder) if folder != "?" else (None, 0, None)
        pushed = "no upstream" if ahead is None else f"{ahead} commits to push"
        print(f"{name}: {opened.get('projects', {}).get(name, '-')} | {folder} | {branch or '-'} | {state} | "
              f"{changes} uncommitted changes | {pushed}")
    if not opened.get("sessions"):
        print("no sessions open")


def open_session(target, request):
    print(api("/operator/open", {"target": target, "request": request}))


def close_sessions(names):
    if names == ["all"]:
        names = [n for n in api("/sessions").get("sessions", []) if n != "operator"]
    print(api("/operator/close", {"names": names}))


def worktrees():
    dirs = panes()
    by_folder = {v: k for k, v in dirs.items()}
    for p in projects():
        main = os.path.realpath(os.path.expanduser(p["dir"]))
        _, out = sh("git", "-C", main, "worktree", "list", "--porcelain")
        for block in out.split("\n\n"):
            found = re.search(r"^worktree (.+)$", block, re.M)
            if not found or os.path.realpath(found.group(1)) == main:
                continue
            w = found.group(1)
            branch, changes, ahead = git_state(w)
            merged = sh("git", "-C", w, "merge-base", "--is-ancestor", "HEAD", f"origin/{p.get('base_branch', 'master')}")[0] == 0
            print(f"{w} | {branch} | session: {by_folder.get(w, 'none')} | {changes} uncommitted changes | "
                  f"{'no upstream' if ahead is None else str(ahead) + ' commits to push'} | "
                  f"{'already merged into ' + p.get('base_branch', 'master') if merged else 'not merged'}")


def remove_worktree(path, force, with_branch):
    w = os.path.realpath(os.path.expanduser(path))
    mains = {os.path.realpath(os.path.expanduser(p["dir"])) for p in projects()}
    if w in mains or w == RADIO:
        sys.exit("refusing: this is a project's main checkout (or the radio)")
    if w in {os.path.realpath(v) for v in panes().values()}:
        sys.exit("refusing: a session is open in that worktree, close it first")
    branch, changes, ahead = git_state(w)
    if branch is None:
        sys.exit("refusing: not a git worktree")
    if not force and (changes or (ahead or 0) > 0):
        sys.exit(f"refusing: {changes} uncommitted changes and {ahead or 0} unpushed commits (with --force they are lost)")
    _, common = sh("git", "-C", w, "rev-parse", "--git-common-dir")
    main = os.path.dirname(os.path.realpath(os.path.join(w, common)))
    rc, out = sh("git", "-C", main, "worktree", "remove", *(["--force"] if force else []), w)
    if rc != 0:
        sys.exit(f"git worktree remove: {out}")
    print(f"removed {w}")
    if with_branch and branch != "HEAD":
        rc, out = sh("git", "-C", main, "branch", "-d", branch)   # -d: only if already merged
        print(f"branch {branch}: {'deleted' if rc == 0 else 'kept (' + out.splitlines()[-1] + ')'}")


def storybook(args):
    if args[:1] == ["stop"] and len(args) == 2:
        rc, pid = sh("lsof", "-nP", f"-iTCP:{args[1]}", "-sTCP:LISTEN", "-t")
        if not pid:
            sys.exit(f"nothing listening on port {args[1]}")
        for p in pid.split():
            os.kill(int(p), 15)
        print(f"stopped the Storybook on port {args[1]}")
        return
    _, out = sh("lsof", "-nP", "-iTCP", "-sTCP:LISTEN")
    seen = set()
    for line in out.splitlines()[1:]:
        c = line.split()
        if c[0] != "node" or c[1] in seen:
            continue
        _, cmd = sh("ps", "-o", "command=", "-p", c[1])
        if "storybook" not in cmd:
            continue
        seen.add(c[1])
        _, cwd = sh("lsof", "-a", "-p", c[1], "-d", "cwd", "-Fn")
        _, rss = sh("ps", "-o", "rss=", "-p", c[1])
        _, age = sh("ps", "-o", "etime=", "-p", c[1])
        print(f"port {c[8].split(':')[-1]} | {cwd.splitlines()[-1][1:] if cwd else '?'} | {int(rss or 0) // 1024} MB | up {age.strip()}")
    if not seen:
        print("no Storybook")


def memory():
    print(sh("sysctl", "vm.swapusage")[1])
    print(sh("uptime")[1])
    _, out = sh("ps", "-axo", "rss=,comm=", "-m")
    for line in out.splitlines()[:12]:
        rss, comm = line.strip().split(None, 1)
        print(f"{int(rss) // 1024} MB  {os.path.basename(comm)}")


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "sessions":
        sessions()
    elif cmd == "open" and args:
        open_session(args[0], " ".join(args[1:]))
    elif cmd == "close" and args:
        close_sessions(args)
    elif cmd == "restart" and len(args) == 1:
        os.execv(os.path.join(RADIO, "restart-session.sh"), ["restart-session.sh", args[0]])
    elif cmd == "worktrees":
        worktrees()
    elif cmd == "remove-worktree" and args:
        remove_worktree(args[0], "--force" in args, "--branch" in args)
    elif cmd == "storybook":
        storybook(args)
    elif cmd == "memory":
        memory()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
