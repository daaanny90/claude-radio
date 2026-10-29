# session.sh: the command typed into a new radio session. The operator and the lobby get the helpers' model and effort,
# every session gets the radio's own settings (channel/settings.json). A fake tmux records the command
import os
import shutil
import subprocess
import tempfile

ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
radio = os.path.realpath(tempfile.mkdtemp())   # a copy of the radio folder: its own .env, not the real one
shutil.copy(os.path.join(repo, "session.sh"), radio)
for d in ("operator", "lobby", "bin"):
    os.makedirs(os.path.join(radio, d))
project = os.path.realpath(tempfile.mkdtemp())
log = os.path.join(radio, "tmux.log")
with open(os.path.join(radio, "bin", "tmux"), "w") as f:
    f.write(f"""#!/usr/bin/env bash
echo "$*" >> {log}
case "$1" in has-session) exit 1 ;; capture-pane) echo '? for shortcuts' ;; esac
exit 0
""")
os.chmod(os.path.join(radio, "bin", "tmux"), 0o755)


def typed(folder, env_file):
    with open(os.path.join(radio, ".env"), "w") as f:
        f.write(env_file)
    open(log, "w").close()
    subprocess.run(["bash", os.path.join(radio, "session.sh"), "alpha", folder], capture_output=True, text=True,
                   env=dict(os.environ, PATH=os.path.join(radio, "bin") + os.pathsep + os.environ["PATH"]), timeout=60)
    return next((l for l in open(log).read().splitlines() if l.startswith("send-keys") and " claude " in l), "")


line = typed(project, "RADIO_MODEL=opus\nRADIO_EFFORT=max\n")
ok("--model opus --effort max" in line and f"--settings {radio}/channel/settings.json" in line,
   f"a project session: the sessions' model and effort, the radio's settings ({line[-160:]})")
line = typed(os.path.join(radio, "operator"), "RADIO_MODEL=opus\nRADIO_EFFORT=max\n")
ok("--model sonnet --effort medium" in line, f"the operator: sonnet and medium when not set ({line[-120:]})")
line = typed(os.path.join(radio, "lobby"), "RADIO_MODEL=opus\nRADIO_EFFORT=max\nRADIO_HELPER_MODEL=haiku\nRADIO_HELPER_EFFORT=low\n")
ok("--model haiku --effort low" in line, "the lobby: RADIO_HELPER_MODEL and RADIO_HELPER_EFFORT")
shutil.rmtree(radio)
shutil.rmtree(project)
