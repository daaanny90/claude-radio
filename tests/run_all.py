"""Runs every tests/test_*.py script and fails if any check prints NO or a script crashes.

The scripts use fake Mumble, sessions and speakers: no audio, no network. Run them with ./tests/run.sh,
which mounts the working copy of bot/ into a throwaway bot container (numpy and piper live there).
"""
import glob
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
failed = 0
for path in sorted(glob.glob(os.path.join(here, "test_*.py"))):
    r = subprocess.run([sys.executable, path], capture_output=True, text=True, cwd=here)
    out = r.stdout + r.stderr
    oks = sum(line.startswith("OK") for line in out.splitlines())
    bad = [line for line in out.splitlines() if line.startswith("NO")]
    status = "ok" if r.returncode == 0 and not bad else "FAIL"
    print(f"{status:4} {os.path.basename(path)}: {oks} ok, {len(bad)} failed")
    if status != "ok":
        failed += 1
        for line in bad or out.splitlines()[-15:]:
            print("     " + line)
sys.exit(1 if failed else 0)
