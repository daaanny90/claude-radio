# The launcher's worktrees: one per branch, never an old one on another branch in silence. Runs on the host, like the
# launcher itself (git and tmux live there), with throwaway repositories
import os
import shutil
import subprocess
import sys
import tempfile
import types

# no global or system git config (hooks, signing, templates of this machine), a fixed identity
os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                  GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import launcher  # noqa: E402

launcher.SESSION_SCRIPT = "true"   # no Claude Code: the session "starts" at once
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr}")
    return r.stdout.strip()


def head(path):
    return sh("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD")


def open_(name, branch, existing=False):
    ev = {"type": "open", "name": name, "dir": main, "base_branch": "main", "branch": branch, "copy": ["CLAUDE.md"],
          "install": False}
    if existing:
        ev["existing"] = True
    try:
        return launcher.open_session(ev)
    except RuntimeError as e:
        return None, str(e)


root = os.path.realpath(tempfile.mkdtemp())
origin, main, other = (os.path.join(root, d) for d in ("origin.git", "shop", "colleague"))
sh("git", "init", "-q", "--bare", "-b", "main", origin)
sh("git", "init", "-q", "-b", "main", main)
with open(os.path.join(main, ".gitignore"), "w") as f:
    f.write("CLAUDE.md\n")
with open(os.path.join(main, "CLAUDE.md"), "w") as f:
    f.write("rules\n")
sh("git", "add", ".gitignore", cwd=main)
sh("git", "commit", "-q", "-m", "start", cwd=main)
sh("git", "remote", "add", "origin", origin, cwd=main)
sh("git", "push", "-q", "origin", "main", cwd=main)

# 1. a new branch: its own folder, named after it, from the base, with the ignored files
wt, notes = open_("echo", "PROJ-1-echo")
ok(wt == f"{main}--PROJ-1-echo" and head(wt) == "PROJ-1-echo" and notes == [], f"a new branch gets <dir>--<branch> ({wt}, {notes})")
ok(os.path.isfile(os.path.join(wt, "CLAUDE.md")), "the git-ignored files are copied in")

# 2. the same branch again: its worktree, resumed, and said
with open(os.path.join(wt, "notes.txt"), "w") as f:
    f.write("half done\n")
again, notes = open_("charlie", "PROJ-1-echo")
ok(again == wt and notes == ["resumed the worktree of PROJ-1-echo with 1 uncommitted changes"],
   f"the same branch resumes its worktree, with a note ({notes})")

# 3. 29 October: the next free name had an old worktree on another branch, and the new session would have worked there
sh("git", "-C", main, "worktree", "add", "-q", "-b", "PROJ-0-delta", f"{main}--delta", "main")   # as the launcher did before
wt, notes = open_("delta", "PROJ-2-delta")
ok(wt == f"{main}--PROJ-2-delta" and head(wt) == "PROJ-2-delta" and head(f"{main}--delta") == "PROJ-0-delta",
   "a name used again on another ticket: a new folder, the old one untouched")
wt, notes = open_("foxtrot", "PROJ-0-delta")
ok(wt == f"{main}--delta" and notes and notes[0].startswith("resumed"), "an old <dir>--<name> worktree is found by its branch")

# 4. a branch that exists only here, in no worktree
sh("git", "-C", main, "branch", "PROJ-3-golf", "main")
wt, _ = open_("golf", "PROJ-3-golf")
ok(wt == f"{main}--PROJ-3-golf" and head(wt) == "PROJ-3-golf", "a local branch gets its worktree")

# 5. someone's merge request, only on origin: fetched and tracked
sh("git", "clone", "-q", origin, other)
sh("git", "-C", other, "switch", "-q", "-c", "PROJ-5-datepicker")
sh("git", "-C", other, "commit", "-q", "--allow-empty", "-m", "date picker")
sh("git", "-C", other, "push", "-q", "origin", "PROJ-5-datepicker")
wt, notes = open_("hotel", "PROJ-5-datepicker", existing=True)
ok(wt == f"{main}--PROJ-5-datepicker" and sh("git", "-C", wt, "rev-parse", "--abbrev-ref", "@{u}") == "origin/PROJ-5-datepicker"
   and sh("git", "-C", wt, "log", "-1", "--format=%s") == "date picker", f"a branch only on origin: fetched and tracked ({notes})")

# 6. a review on yesterday's copy of the branch: brought up to date
sh("git", "-C", other, "switch", "-q", "-c", "PROJ-6-modal", "origin/main")
sh("git", "-C", other, "commit", "-q", "--allow-empty", "-m", "first round")
sh("git", "-C", other, "push", "-q", "origin", "PROJ-6-modal")
sh("git", "-C", main, "fetch", "-q", "origin", "PROJ-6-modal")
sh("git", "-C", main, "branch", "PROJ-6-modal", "origin/PROJ-6-modal")
sh("git", "-C", other, "commit", "-q", "--allow-empty", "-m", "second round")
sh("git", "-C", other, "push", "-q", "origin", "PROJ-6-modal")
wt, notes = open_("india", "PROJ-6-modal", existing=True)
ok(wt and sh("git", "-C", wt, "log", "-1", "--format=%s") == "second round" and notes == [],
   f"a local copy behind origin is fast-forwarded before the worktree ({notes})")

# 7. what fails says why, and leaves nothing behind
wt, error = open_("juliet", "PROJ-7-nowhere", existing=True)
ok(wt is None and error.startswith("branch PROJ-7-nowhere not found") and not os.path.exists(f"{main}--PROJ-7-nowhere"),
   f"an existing branch that is nowhere: no new branch from the base under its name ({error})")
wt, error = open_("kilo", "main")
ok(wt is None and error.startswith("main is checked out in the main folder"), f"the main checkout's branch ({error})")
os.makedirs(f"{main}--PROJ-8-lima")
with open(f"{main}--PROJ-8-lima/left.txt", "w") as f:   # git takes an empty folder, not one with files
    f.write("left behind\n")
wt, error = open_("lima", "PROJ-8-lima")
ok(wt is None and "already exists" in error, f"a folder in the way that is no worktree: git's error ({error})")

# 8. a session already at work on that branch, also from a subfolder of it
real_run = launcher.run
busy = f"{main}--PROJ-1-echo"
launcher.run = lambda cmd, **k: (types.SimpleNamespace(returncode=0, stdout=f"bravo\t{busy}/packages/ui\n", stderr="")
                                 if cmd[0] == "tmux" else real_run(cmd, **k))
wt, error = open_("mike", "PROJ-1-echo")
ok(wt is None and error == "bravo is already working on PROJ-1-echo", f"someone at work there ({error})")
wt, _ = open_("bravo", "PROJ-1-echo")
ok(wt == busy, "the same session reopening its own branch is not in the way")
launcher.run = real_run

# 9. a branch with a slash, and origin out of reach with a local copy
wt, _ = open_("november", "feature/login")
ok(wt == f"{main}--feature-login" and head(wt) == "feature/login", "a slash in the branch: a dash in the folder")
sh("git", "-C", main, "remote", "set-url", "origin", os.path.join(root, "gone.git"))
sh("git", "-C", main, "branch", "PROJ-9-oscar", "main")
wt, notes = open_("oscar", "PROJ-9-oscar", existing=True)
ok(wt == f"{main}--PROJ-9-oscar" and len(notes) == 1 and notes[0].startswith("PROJ-9-oscar not fetched"),
   f"origin out of reach: the local copy, and a note ({notes})")

shutil.rmtree(root)   # on the host the throwaway repositories would stay behind
