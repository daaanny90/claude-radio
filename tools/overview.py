#!/usr/bin/env python3
"""
Visual overview of the work in a folder, to put on the board with the show tool:
    tools/overview.py [file ...] [--dir DIR] [--base BRANCH] [--max N]

1. starts the folder's Storybook on the tailnet (tools/storybook.sh), or reuses the one already running
2. picks the stories: those of the files passed, otherwise those touched by the uncommitted changes
   (if there are none, by the diff against --base, default master). First the stories the diff adds
   or changes, then the others in the same file, then those next to the modified components
3. mobile and desktop screenshots of each one ("...Mobile" stories mobile only, "...Desktop" stories
   desktop only), at most --max stories (default 5: the board takes 10 files per item)
4. prints the links (Storybook UI and full screen, with the Tailscale IP) and the image paths

Images go to /tmp/radio-screenshots/, never into the folder: it is the project's repository.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
STORY_SUFFIX = re.compile(r"\.stories\.(ts|tsx|js|jsx|mjs|vue|svelte)$")


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"{' '.join(cmd[:3])}...: {(r.stdout + r.stderr).strip()[-500:]}")
    return r.stdout


def changed_files(top, base):
    """Touched files (relative to the repository root) and the diff text, to spot the new exports."""
    status = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=top)
    files, untracked = [], set()
    for line in status.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        files.append(path)
        if line.startswith("??"):
            untracked.add(path)
    if files:
        return files, run(["git", "diff", "HEAD"], cwd=top), untracked
    # nothing uncommitted: the branch's work against the base
    rng = f"{base}...HEAD"
    files = [f for f in run(["git", "diff", "--name-only", rng], cwd=top, check=False).splitlines() if f]
    return files, run(["git", "diff", rng], cwd=top, check=False), set()


def new_exports(diff_text):
    """For each file in the diff, the exports added or touched ('export const Zoom' on + lines)."""
    out, current = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif current and (m := re.match(r"^\+\s*export\s+const\s+(\w+)", line)):
            out.setdefault(current, set()).add(m.group(1))
    return out


def main():
    ap = argparse.ArgumentParser(description="Visual overview for the board")
    ap.add_argument("files", nargs="*", help="story or component files to show (default: the modified ones)")
    ap.add_argument("--dir", default=".", help="folder with the Storybook (default: the current one)")
    ap.add_argument("--base", default="master", help="branch to compare against when there are no uncommitted changes")
    ap.add_argument("--max", type=int, default=5, help="maximum number of stories (default 5)")
    args = ap.parse_args()

    # 1. Storybook on the tailnet
    sb = subprocess.run([os.path.join(TOOLS, "storybook.sh"), "start", args.dir], capture_output=True, text=True)
    if sb.returncode != 0:
        sys.exit(sb.stdout + sb.stderr)
    lines = sb.stdout.splitlines()
    sb_dir = lines[0].removeprefix("Storybook for ").strip()
    base_url = next(l for l in lines if l.startswith("http://"))

    top = run(["git", "rev-parse", "--show-toplevel"], cwd=sb_dir).strip()
    prefix = os.path.relpath(sb_dir, top)                      # "." or "apps/vue" in a monorepo
    to_import = lambda p: "./" + (p if prefix == "." else os.path.relpath(p, prefix))

    # 2. the stories to show
    if args.files:
        cwd = os.getcwd()
        wanted = [os.path.relpath(os.path.abspath(os.path.join(cwd, f)), top) for f in args.files]
        added, untracked = {}, set()
    else:
        wanted, diff_text, untracked = changed_files(top, args.base)
        added = new_exports(diff_text)
    with urllib.request.urlopen(base_url + "/index.json", timeout=30) as r:
        entries = [e for e in json.load(r)["entries"].values() if e.get("type") == "story"]

    ranked = []   # (priority, position in the index, entry)
    for pos, e in enumerate(entries):
        imp = e["importPath"]
        for f in wanted:
            if prefix != "." and not f.startswith(prefix + "/"):
                continue
            if STORY_SUFFIX.search(f) and imp == to_import(f):
                fresh = f in untracked or e.get("exportName") in added.get(f, set())
                ranked.append((0 if fresh else 1, pos, e))
            elif not STORY_SUFFIX.search(f) and os.path.dirname(imp) == os.path.dirname(to_import(f)):
                ranked.append((2, pos, e))
    chosen, seen = [], set()
    for _, _, e in sorted(ranked, key=lambda t: (t[0], t[1])):
        if e["id"] not in seen:
            seen.add(e["id"])
            chosen.append(e)
    skipped = len(chosen) - args.max
    chosen = chosen[:args.max]

    print(f"Storybook: {base_url}")
    if not chosen:
        print("No story linked to the modified files: pass the story or component files as arguments.")
        return

    # 3. screenshots. On the first load after startup Vite optimizes the dependencies and reloads the
    # page: a throwaway shot before the series avoids blank images
    session = os.environ.get("RADIO_SESSION") or os.path.basename(sb_dir)
    out = f"/tmp/radio-screenshots/{session}-{time.strftime('%Y%m%d-%H%M%S')}"
    env = dict(os.environ, RADIO_SCREENSHOTS=out)
    frame = lambda e: f"{base_url}/iframe.html?id={e['id']}&viewMode=story"
    subprocess.run([os.path.join(TOOLS, "screenshots.sh"), frame(chosen[0]), "warmup", "desktop"],
                   env=dict(env, RADIO_SCREENSHOTS=out + "/.warmup"), capture_output=True)
    images = []
    print()
    for e in chosen:
        export = e.get("exportName") or e["name"]
        which = "mobile" if re.search(r"mobile$", export, re.I) else "desktop" if re.search(r"desktop$", export, re.I) else "both"
        name = f"{e['title'].split('/')[-1]}-{e['id'].split('--')[-1]}".lower().replace(" ", "-")
        shot = subprocess.run([os.path.join(TOOLS, "screenshots.sh"), frame(e), name, which], env=env, capture_output=True, text=True)
        images += [p for p in shot.stdout.splitlines() if p.endswith(".png")]
        print(f"{e['title']}: {e['name']}")
        print(f"{base_url}/?path=/story/{e['id']}")
        print(frame(e))
        if shot.returncode != 0:
            print(f"(screenshot failed: {shot.stderr.strip()})")
        print()
    if skipped > 0:
        print(f"{skipped} more touched stories not photographed (--max {args.max}).")
    print("Images to pass to show:")
    for p in images:
        print(p)


if __name__ == "__main__":
    main()
