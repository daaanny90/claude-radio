# statusline.sh: the radio sessions' status line keeps what Claude Code gives it for the board, and still draws the
# user's own status line
import json
import os
import shutil
import subprocess
import tempfile

ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "statusline.sh")
home = tempfile.mkdtemp()
state = os.path.join(home, "state")
os.makedirs(os.path.join(home, ".claude"))
with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
    json.dump({"statusLine": {"type": "command", "command": "cat >/dev/null; echo my own line"}}, f)
payload = json.dumps({"model": {"display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 1.5}})


def line(**env):
    r = subprocess.run(["bash", script], input=payload, capture_output=True, text=True,
                       env=dict(os.environ, HOME=home, **env))
    return r.returncode, r.stdout.strip()


code, out = line(RADIO_SESSION="bravo", RADIO_STATE_DIR=state)
saved = os.path.join(state, "status", "bravo.json")
ok(code == 0 and os.path.isfile(saved) and json.load(open(saved))["cost"]["total_cost_usd"] == 1.5,
   "a radio session: its status kept in state/status/<name>.json")
ok(out == "my own line", f"the user's own status line still drawn ({out!r})")
os.remove(saved)
code, out = line()
ok(code == 0 and not os.path.exists(saved) and out == "my own line", "outside the radio: nothing kept")
os.remove(os.path.join(home, ".claude", "settings.json"))
code, out = line(RADIO_SESSION="bravo", RADIO_STATE_DIR=state)
ok(code == 0 and out == "" and os.path.isfile(saved), "no status line of the user's: an empty one")
shutil.rmtree(home)
