"""
Board: the page where sessions put in front of the user what does not work well by voice, that is
drafts to approve (merge request descriptions, commit messages), code, diffs, screenshots and audio.

It is also a text console, for when the user cannot talk: the conversation with the sessions, a box that
takes every voice command as text, buttons for the radio's questions, and a silent switch.

Two halves, for safety:
- items are written only through the bot's localhost API (POST /show, called by channel/radio.ts)
- the page lives here, in a separate HTTP server on the board port, published only on the Tailscale IP.
  Its actions (typed text, answers, silent mode) go to the bot's work queue like a spoken utterance, and
  only from the page itself: see BoardHandler._action. BOARD_WRITE=0 keeps the board read-only

Access: the first time, the page asks for a six-digit code that the bot reads out loud ("board
code"), valid for a few minutes and only once. After that a signed cookie remains, with a secret
stored in the board folder, so it survives bot restarts.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 20261015-114203-00a1b2: date and time, a counter for items within the same second, four random hex digits.
# Alphabetical order is arrival order: the page notices something new by looking at the latest id
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
FILE_RE = re.compile(r"^\d{2}-[A-Za-z0-9._-]{1,80}$")       # 00-screenshot.png
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
AUDIO_TYPES = {"audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/mp4": ".m4a"}
MEDIA_BY_EXT = {ext: mime for mime, ext in {**IMAGE_TYPES, **AUDIO_TYPES}.items()}
FORMATS = ("text", "code", "diff")

MAX_TITLE = 200
MAX_TEXT = 512 * 1024            # bytes of text per item
MAX_FILES = 10
MAX_IMAGE = 15 * 1024 * 1024
MAX_TEXT_FILE = 512 * 1024
LIST_LIMIT = 30                  # items shown on the page

CODE_TTL_S = 300                 # the spoken code is valid for five minutes...
CODE_MAX_FAILURES = 5            # ...and after five wrong attempts within ten minutes a new code is needed
CODE_FAILURE_WINDOW_S = 600
COOKIE = "bacheca"              # name kept from before the translation: changing it would log paired phones out
COOKIE_DAYS = 365


def safe_name(name, index):
    """Presentable, harmless file name: no paths, only safe characters."""
    base = os.path.basename(str(name or "")).lstrip(".")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:80]
    return base or f"file{index}"


def when(ts):
    """Time shown on the card: "11:42" if it is today, "yesterday 11:42", otherwise "15 Oct 11:42"."""
    try:
        moment = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    today = datetime.now().date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if moment.date() == today - timedelta(days=1):
        return moment.strftime("yesterday %H:%M")
    return moment.strftime("%d %b %H:%M")


class Board:
    """The board's items on disk: one folder per item with meta.json and its images and audio."""

    def __init__(self, root, keep=100):
        self.root = root
        self.keep = max(1, int(keep))
        os.makedirs(root, exist_ok=True)
        self.lock = threading.Lock()
        self.secret = self._secret()
        self.code = None            # (code, expiry) of the last spoken code
        self.failures = deque()     # times of the wrong attempts

    def _next_id(self):
        """Id always greater than the latest one on disk, even within the same second or if the clock
        goes backwards (call with the lock held)."""
        latest = self.latest()
        stamp = max(time.strftime("%Y%m%d-%H%M%S"), latest[:15])
        n = int(latest[16:18], 16) + 1 if latest.startswith(stamp) else 0
        return f"{stamp}-{min(n, 255):02x}{secrets.token_hex(2)}"

    def _secret(self):
        path = os.path.join(self.root, ".secret")
        old = os.path.join(self.root, ".segreto")   # name before the translation: keeps phones paired
        if not os.path.exists(path) and os.path.exists(old):
            os.rename(old, path)
        try:
            with open(path, "rb") as f:
                data = f.read()
            if len(data) >= 32:
                return data
        except FileNotFoundError:
            pass
        data = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return data

    # --- items --------------------------------------------------------------

    def add(self, session, title, text="", fmt="text", files=()):
        """Store an item and return it. ValueError if something is wrong (the message goes back to the agent)."""
        title = " ".join(str(title or "").split())[:MAX_TITLE]
        if not title:
            raise ValueError("title is empty")
        text = str(text or "")
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise ValueError(f"text is longer than {MAX_TEXT // 1024} KB: attach it as a file or trim it")
        fmt = fmt if fmt in FORMATS else "text"
        files = list(files or [])
        if not text.strip() and not files:
            raise ValueError("nothing to show: give text or files")
        if len(files) > MAX_FILES:
            raise ValueError(f"at most {MAX_FILES} files")

        entries, blobs = [], []
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                raise ValueError("bad file entry")
            name = safe_name(f.get("name"), i)
            try:
                data = base64.b64decode(f.get("data") or "", validate=True)
            except ValueError:
                raise ValueError(f"{name}: data is not valid base64") from None
            mime = str(f.get("mime") or "")
            if mime == "audio/x-wav":
                mime = "audio/wav"
            if mime in IMAGE_TYPES or mime in AUDIO_TYPES:
                if len(data) > MAX_IMAGE:
                    raise ValueError(f"{name}: file larger than {MAX_IMAGE // 1024 // 1024} MB")
                kind, ext = ("image", IMAGE_TYPES[mime]) if mime in IMAGE_TYPES else ("audio", AUDIO_TYPES[mime])
                stem = os.path.splitext(name)[0][:60] or kind
                stored = f"{i:02d}-{stem}{ext}"
                entries.append({"kind": kind, "name": name, "file": stored})
                blobs.append((stored, data))
            else:
                if len(data) > MAX_TEXT_FILE:
                    raise ValueError(f"{name}: text file larger than {MAX_TEXT_FILE // 1024} KB")
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError(f"{name}: neither text nor a png, jpg, gif, webp image or wav, mp3, ogg audio") from None
                entries.append({"kind": "text", "name": name, "text": content})

        with self.lock:
            item_id = self._next_id()
            meta = {
                "id": item_id, "session": str(session or "?"), "title": title, "format": fmt, "text": text,
                "files": entries, "ts": datetime.now().isoformat(timespec="seconds"),
            }
            folder = os.path.join(self.root, item_id)
            os.makedirs(folder)
            for stored, data in blobs:
                with open(os.path.join(folder, stored), "wb") as out:
                    out.write(data)
            tmp = os.path.join(folder, "meta.json.tmp")
            with open(tmp, "w", encoding="utf-8") as out:
                json.dump(meta, out, ensure_ascii=False)
            os.replace(tmp, os.path.join(folder, "meta.json"))
            self._prune()
        return meta

    def _ids(self):
        return sorted(d for d in os.listdir(self.root) if ID_RE.match(d))

    def _prune(self):
        ids = self._ids()
        for old in ids[:max(0, len(ids) - self.keep)]:
            shutil.rmtree(os.path.join(self.root, old), ignore_errors=True)

    def forget(self, session):
        """Deletes every item of a session (a closed session removed from the board). Returns how many."""
        removed = 0
        for item_id in self._ids():
            try:
                with open(os.path.join(self.root, item_id, "meta.json"), encoding="utf-8") as f:
                    owner = json.load(f).get("session")
            except (OSError, ValueError):
                continue
            if owner == session:
                shutil.rmtree(os.path.join(self.root, item_id), ignore_errors=True)
                removed += 1
        return removed

    def latest(self):
        ids = self._ids()
        return ids[-1] if ids else ""

    def items(self, limit=LIST_LIMIT):
        out = []
        for item_id in reversed(self._ids()):
            try:
                with open(os.path.join(self.root, item_id, "meta.json"), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue   # half-written or corrupted item: skip it
            if len(out) >= limit:
                break
        return out

    def media_path(self, item_id, name):
        """Path and MIME type of a stored image or audio file, or (None, None). Checks the names before touching the disk."""
        if not ID_RE.match(item_id) or not FILE_RE.match(name):
            return None, None
        mime = MEDIA_BY_EXT.get(os.path.splitext(name)[1].lower())
        path = os.path.join(self.root, item_id, name)
        if mime is None or not os.path.isfile(path):
            return None, None
        return path, mime

    # --- access -------------------------------------------------------------

    def new_code(self):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self.lock:
            self.code = (code, time.monotonic() + CODE_TTL_S)
            self.failures.clear()   # whoever asks for a code by voice is already inside the radio
        return code

    def try_code(self, attempt):
        """True if the code is right (and consume it), False if not, None if the attempts are used up."""
        now = time.monotonic()
        with self.lock:
            while self.failures and now - self.failures[0] > CODE_FAILURE_WINDOW_S:
                self.failures.popleft()
            if len(self.failures) >= CODE_MAX_FAILURES:
                self.code = None
                return None
            if self.code and now < self.code[1] and hmac.compare_digest(attempt.encode(), self.code[0].encode()):
                self.code = None
                return True
            self.failures.append(now)
            return False

    def make_cookie(self):
        expiry = str(int(time.time()) + COOKIE_DAYS * 86400)
        signature = hmac.new(self.secret, expiry.encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{signature}"

    def cookie_ok(self, value):
        expiry, _, signature = (value or "").partition(".")
        if not (expiry.isascii() and expiry.isdigit()) or len(expiry) > 12 or int(expiry) < time.time():
            return False
        good = hmac.new(self.secret, expiry.encode(), hashlib.sha256).hexdigest()
        # in bytes: compare_digest on non-ASCII strings raises an exception instead of returning False
        return hmac.compare_digest(signature.encode("utf-8", "replace"), good.encode())


def public(meta):
    """The item as the page sees it: images and audio become URLs, the timestamp becomes readable."""
    files = []
    for f in meta.get("files", []):
        if f.get("kind") in ("image", "audio"):
            files.append({"kind": f["kind"], "name": f.get("name", ""), "url": f"/f/{meta['id']}/{f.get('file', '')}"})
        else:
            files.append({"kind": "text", "name": f.get("name", ""), "text": f.get("text", "")})
    return {
        "id": meta["id"], "session": meta.get("session", ""), "title": meta.get("title", ""),
        "format": meta.get("format", "text"), "text": meta.get("text", ""), "files": files,
        "when": when(meta.get("ts")), "ts": epoch(meta.get("ts")),
    }


def epoch(iso):
    """The item's ISO time as seconds, to sort items and conversation together on the page."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0


def public_chat(event):
    """A conversation event as the page sees it (the text is rendered with textContent, never as HTML)."""
    return {"id": event["id"], "ts": event["ts"], "when": when(datetime.fromtimestamp(event["ts"]).isoformat()), "who": event["who"],
            "session": event.get("session", ""), "kind": event.get("kind", ""), "via": event.get("via", ""),
            "text": event["text"]}


# --- the pages ---------------------------------------------------------------

STYLE = """
/* Las Vegas, 2049: one warm color family in an orange haze, hairlines and corner marks instead of frames, small tracked
   labels over light text, a faint glow on what matters. Red-orange only for stop and blocked. No web fonts: the page
   never calls anything off the tailnet */
:root { --bg: #0b0806; --deep: #070504; --fg: #f3e6d0; --soft: #c9b79b; --dim: #8c7b65; --amber: #ffa94d; --red: #ff4b3a;
  --op: #a3c4cc; --op-glow: 0 0 10px rgba(140, 200, 215, .45);
  --hair: rgba(255, 169, 77, .22); --hair-soft: rgba(255, 169, 77, .10); --glow: 0 0 10px rgba(255, 150, 60, .45);
  --sans: "Helvetica Neue", "Avenir Next", system-ui, -apple-system, Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, "Roboto Mono", monospace;
  color-scheme: dark; }
* { box-sizing: border-box; }
[hidden] { display: none !important; }   /* the classes below set display: hidden must still win */
html { -webkit-text-size-adjust: 100%; background: var(--bg); }
/* a column as tall as the screen: the line to type in stays at the bottom even with a short log */
body { margin: 0; min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column; color: var(--fg); font: 300 16px/1.55 var(--sans);
  background: radial-gradient(130% 70% at 50% -12%, rgba(255, 122, 40, .30), rgba(255, 122, 40, .07) 48%, transparent 72%) fixed, var(--bg); }
button, textarea, input { font: inherit; color: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 1px solid var(--amber); outline-offset: 2px; }
a.link { color: var(--amber); text-decoration-color: var(--hair); text-underline-offset: 3px; }
/* the labels: small, tracked, upper case, like the readouts on the films' screens */
.brand, .nick, .time, .target, .kind, .tab, .prompt, .toggle, #status, .key, .composer button, .file {
  font: 500 11px/1.4 var(--sans); letter-spacing: .2em; text-transform: uppercase; }
/* corner marks: four short hairlines instead of a frame */
.ask, .line.item .box {
  --mark: var(--hair); background:
  linear-gradient(var(--mark), var(--mark)) top left / 12px 1px no-repeat, linear-gradient(var(--mark), var(--mark)) top left / 1px 12px no-repeat,
  linear-gradient(var(--mark), var(--mark)) top right / 12px 1px no-repeat, linear-gradient(var(--mark), var(--mark)) top right / 1px 12px no-repeat,
  linear-gradient(var(--mark), var(--mark)) bottom left / 12px 1px no-repeat, linear-gradient(var(--mark), var(--mark)) bottom left / 1px 12px no-repeat,
  linear-gradient(var(--mark), var(--mark)) bottom right / 12px 1px no-repeat, linear-gradient(var(--mark), var(--mark)) bottom right / 1px 12px no-repeat; }

/* top: name, voice, clock; the sessions; the selected one's topic */
.bar { position: sticky; top: 0; z-index: 2; background: linear-gradient(rgba(11, 8, 6, .96), rgba(11, 8, 6, .88));
  border-bottom: 1px solid var(--hair-soft); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
.row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px 6px; }
.brand { color: var(--amber); font-size: 13px; letter-spacing: .5em; text-shadow: var(--glow); }
.right { display: flex; align-items: center; gap: 16px; }
#status { color: var(--dim); font-family: var(--mono); letter-spacing: .12em; }
#status.off { color: var(--red); }
.toggle { padding: 0; border: 0; background: none; color: var(--soft); }
.toggle::before { content: ""; display: inline-block; width: 14px; height: 2px; margin: 0 8px 3px 0; background: var(--amber);
  box-shadow: var(--glow); vertical-align: middle; }
.toggle.off { color: var(--red); }
.toggle.muted { color: var(--dim); }
.toggle.muted::before { background: var(--dim); box-shadow: none; }
.toggle.off::before { background: var(--red); box-shadow: 0 0 10px rgba(255, 75, 58, .5); }
.tabs { display: flex; gap: 22px; overflow-x: auto; scrollbar-width: none; padding: 8px 16px 0; }
.tab { flex: 0 0 auto; position: relative; display: flex; flex-direction: column; align-items: flex-start; padding: 0 0 10px;
  border: 0; background: none; color: var(--dim); text-align: left; }
.tab .tt { margin-top: 2px; max-width: 14em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--dim);
  font: 300 12px/1.3 var(--sans); letter-spacing: .02em; text-transform: none; }
.tab.active { color: var(--fg); }
.tab.active .nick { color: var(--amber); text-shadow: var(--glow); }
.tab.active::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 1px; background: var(--amber); box-shadow: var(--glow); }
.tab.unseen .nick::after { content: ""; display: inline-block; width: 4px; height: 4px; margin: 0 0 2px 6px; border-radius: 50%;
  background: var(--fg); box-shadow: 0 0 6px var(--fg); vertical-align: middle; }
.tab.closed { opacity: .45; }
/* the operator: the one cool color in the warm haze, set apart from the agents by a hairline */
.tab.op { padding-right: 22px; margin-right: 0; }
.tab.op::before { content: ""; position: absolute; right: 0; top: 2px; bottom: 12px; width: 1px; background: var(--hair); }
.tab.op .nick { color: var(--op); }
.tab.op.active .nick { color: var(--op); text-shadow: var(--op-glow); }
.tab.op.active::after { background: var(--op); box-shadow: var(--op-glow); }
.tab.op .state.working { filter: hue-rotate(150deg) saturate(.6); }
.line > .nick.n-op, .live > .nick.n-op, .prompt.op { color: var(--op); text-shadow: var(--op-glow); }
.line.agent:has(> .n-op) > .body { color: #dfe9ea; }
/* markdown from the agents */
.body strong, .text strong { font-weight: 600; color: var(--fg); }
.body em, .text em { font-style: italic; }
.md-h { display: inline-block; margin-top: 4px; font-weight: 600; color: var(--amber); }
.md-code { padding: 1px 5px; background: var(--deep); border: 1px solid var(--hair-soft); font: 13px var(--mono); color: var(--fg); }
.body pre.code { margin: 6px 0; }
.state { display: block; width: 18px; height: 2px; margin-top: 6px; background: var(--hair); }
.state.working { background: var(--amber); box-shadow: var(--glow); }
.state.waiting { background: var(--fg); box-shadow: 0 0 8px rgba(243, 230, 208, .6); }
.topic { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 16px; padding: 8px 16px 10px;
  border-top: 1px solid var(--hair-soft); color: var(--soft); font-size: 13px; }
.actions { display: flex; gap: 8px; }

/* the log */
main { flex: 1 0 auto; width: 100%; max-width: 760px; margin: 0 auto; padding: 18px 16px 24px; }
.empty { color: var(--dim); }
.line { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 14px; margin: 0 0 22px; }
.line > .nick { order: 1; color: var(--amber); }
.line > .time { order: 2; color: var(--dim); font-family: var(--mono); letter-spacing: .12em; }
.line > .target { order: 3; color: var(--dim); }
.line > .body { order: 4; flex: 1 0 100%; margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.line.you > .nick { color: var(--fg); }
.line.you > .body { color: var(--soft); }
.line.agent > .nick { text-shadow: var(--glow); }
.line.notice > .nick { color: var(--dim); }
.line.notice > .body { color: var(--dim); font-size: 14px; }
.kind { margin-right: 10px; color: var(--dim); font-size: 10px; }
.k-blocked { color: var(--red); }
.line.agent.blocked > .nick { color: var(--red); text-shadow: 0 0 10px rgba(255, 75, 58, .5); }
.title { font-weight: 400; font-size: 18px; line-height: 1.35; letter-spacing: .01em; }
.fresh .title { color: var(--amber); text-shadow: var(--glow); }
.line.item .box { margin-top: 10px; padding: 14px; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--soft); }
/* on a phone long lines wrap: scrolling sideways line by line is worse */
pre { margin: 8px 0 0; padding: 10px 12px; background: var(--deep); border-left: 1px solid var(--hair); white-space: pre-wrap;
  overflow-wrap: anywhere; font: 13px/1.5 var(--mono); color: var(--soft); }
pre.diff span { display: block; min-height: 1.5em; padding: 0 12px; margin: 0 -12px; }
.add { color: #e9c58a; background: rgba(255, 169, 77, .08); }
.del { color: #ff8f7f; background: rgba(255, 75, 58, .08); }
.hunk { color: var(--dim); }
.diffhead { color: var(--dim); }
.file { margin-top: 10px; color: var(--dim); text-transform: none; letter-spacing: .08em; overflow-wrap: anywhere; }
audio { display: block; width: 100%; margin-top: 6px; filter: sepia(.6) saturate(1.4) hue-rotate(-12deg) brightness(.9); }
img { display: block; max-width: 100%; height: auto; margin-top: 10px; }

/* bottom: the radio's questions and the line to type in */
.dock { position: sticky; bottom: 0; z-index: 2; padding: 10px 16px 12px; background: linear-gradient(rgba(11, 8, 6, .9), rgba(11, 8, 6, .98));
  border-top: 1px solid var(--hair-soft); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
.ask { --mark: var(--amber); display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 0 0 10px; padding: 12px 14px; }
.ask .nick { flex: 1 0 100%; color: var(--amber); text-shadow: var(--glow); }
.asktext { flex: 1 1 18ch; color: var(--fg); }
.keys { display: flex; gap: 8px; }
.key { padding: 7px 14px; border: 1px solid var(--hair); background: none; color: var(--soft); }
.key.yes { border-color: var(--amber); color: var(--amber); box-shadow: inset 0 0 12px rgba(255, 150, 60, .15); }
.key.stop { border-color: var(--red); color: var(--red); }
button:disabled { opacity: .45; }
.composer { display: flex; align-items: flex-end; gap: 12px; }
.prompt { padding-bottom: 11px; color: var(--amber); text-shadow: var(--glow); white-space: nowrap; }
.composer textarea { flex: 1; min-height: 40px; max-height: 140px; padding: 9px 0; resize: none; border: 0;
  border-bottom: 1px solid var(--hair); background: none; color: var(--fg); caret-color: var(--amber); font: 300 16px/1.4 var(--sans); }
.composer textarea:focus { outline: none; border-bottom-color: var(--amber); box-shadow: 0 1px 0 0 var(--amber); }
.composer textarea::placeholder { color: var(--dim); }
.composer button { padding: 9px 16px; border: 1px solid var(--amber); background: none; color: var(--amber); }

.login { max-width: 420px; margin: 0 auto; padding: 56px 20px; }
.login h1 { margin: 0 0 18px; color: var(--amber); font: 500 13px var(--sans); letter-spacing: .5em; text-transform: uppercase; text-shadow: var(--glow); }
.login p { color: var(--soft); }
.login input { width: 100%; margin: 18px 0; padding: 10px 0; border: 0; border-bottom: 1px solid var(--amber); background: none;
  color: var(--amber); font: 300 34px var(--mono); letter-spacing: .5em; text-align: center; caret-color: var(--amber); text-shadow: var(--glow); }
.login input:focus { outline: none; }
.login button { width: 100%; padding: 12px; border: 1px solid var(--amber); background: none; color: var(--amber);
  font: 500 12px var(--sans); letter-spacing: .3em; text-transform: uppercase; }
.error { color: var(--red); }
/* keyboard: the list of keys, and its "?" only where there is a keyboard and a pointer */
.keyhint { width: 18px; text-align: center; }
.keyhint::before { content: none; }
@media (hover: none) { .keyhint { display: none; } }
.help { position: fixed; z-index: 5; right: 16px; top: 64px; width: min(360px, calc(100vw - 32px)); padding: 16px 18px;
  background: rgba(11, 8, 6, .97); border: 1px solid var(--hair); box-shadow: 0 0 40px rgba(255, 122, 40, .12); }
.help .nick { margin: 0 0 10px; color: var(--amber); text-shadow: var(--glow); }
.help dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0; }
.help dt { color: var(--fg); font: 500 12px/1.5 var(--mono); letter-spacing: .08em; white-space: nowrap; }
.help dd { margin: 0; color: var(--soft); font-size: 14px; }
@media (prefers-reduced-transparency: reduce) { .bar, .dock { backdrop-filter: none; -webkit-backdrop-filter: none; } }

/* motion that says something: a light runs along the bar of a session at work, the bar of one waiting for you
   breathes, the live line scans, a new message comes in with a short flare on its label */
.state.working { background: linear-gradient(90deg, rgba(255, 169, 77, .18) 0%, rgba(255, 169, 77, .18) 35%, #ffd9a8 50%,
  rgba(255, 169, 77, .18) 65%, rgba(255, 169, 77, .18) 100%); background-size: 300% 100%; animation: run 1.8s linear infinite; }
.state.waiting { animation: breathe 2.6s ease-in-out infinite; }
@keyframes run { from { background-position: 100% 0; } to { background-position: 0 0; } }
@keyframes breathe { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
#live { margin-top: 4px; }
.live { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 14px; margin: 0 0 18px; }
.live > .nick { color: var(--amber); text-shadow: var(--glow); }
.live > .kind { margin: 0; color: var(--amber); animation: breathe 2.2s ease-in-out infinite; }
.live > .doing { flex: 1 1 auto; color: var(--soft); font-size: 14px; }
.live > .scan { flex: 1 0 100%; height: 1px; margin-top: 8px; overflow: hidden;
  background: linear-gradient(90deg, transparent, rgba(255, 169, 77, .25), transparent); position: relative; }
.live > .scan::after { content: ""; position: absolute; top: 0; left: -30%; width: 30%; height: 1px;
  background: linear-gradient(90deg, transparent, #ffd9a8, transparent); box-shadow: 0 0 8px rgba(255, 169, 77, .8);
  animation: scan 2.4s cubic-bezier(.45, 0, .55, 1) infinite; }
@keyframes scan { from { left: -30%; } to { left: 100%; } }
.arrive { animation: arrive .7s ease-out both; }
.arrive > .nick { animation: flare 1.4s ease-out both; }
@keyframes arrive { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes flare { 0% { text-shadow: 0 0 18px rgba(255, 190, 120, 1); color: #ffe2bd; } 100% { } }
@media (prefers-reduced-motion: reduce) {
  .state.working, .state.waiting, .live > .kind, .live > .scan::after, .arrive, .arrive > .nick { animation: none; }
  .state.working { background: var(--amber); }
}
"""

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0b0806">
<meta name="robots" content="noindex">
<title>Board</title>
<style>""" + STYLE + """</style>
</head>
"""

LOGIN = HEAD + """<body>
<main class="login">
<h1>Board</h1>
<p>Say <b>board code</b> to the radio and type the six digits you hear here. They are valid for five minutes and work once.</p>
<form method="post" action="/pair">
<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="9" required autofocus aria-label="Code">
<button>Enter</button>
</form>
<p class="error">{error}</p>
</main>
</body>
</html>
"""

PAGE = HEAD + """<body>
<header class="bar">
<div class="row"><span class="brand">radio</span><span class="right"><button id="keys" class="toggle keyhint" type="button" title="Keyboard shortcuts" aria-label="Keyboard shortcuts">?</button><button id="sound" class="toggle" type="button"></button><button id="silent" class="toggle" type="button" hidden></button><span id="status">connecting</span></span></div>
<nav id="tabs" class="tabs" role="tablist" aria-label="Sessions"></nav>
<div class="topic"><span id="summary"></span><span id="actions" class="actions" hidden></span></div>
</header>
<main>
<div id="list"></div>
<p id="empty" class="empty" hidden></p>
<div id="live" aria-live="polite"></div>
</main>
<footer class="dock">
<div id="asks"></div>
<form id="composer" class="composer" hidden>
<label for="input" id="prompt" class="prompt">radio</label>
<textarea id="input" rows="1" maxlength="4000" autocomplete="off" autocapitalize="sentences"></textarea>
<button id="send" type="submit">send</button>
</form>
</footer>
<div id="help" class="help" role="dialog" aria-label="Keyboard shortcuts" hidden>
<p class="nick">keyboard</p>
<dl>
<dt>/ &nbsp;i</dt><dd>type a message (Esc leaves the box)</dd>
<dt>Enter</dt><dd>send, Shift+Enter for a new line</dd>
<dt>&larr; &rarr; &nbsp;h l</dt><dd>previous and next tab</dd>
<dt>0 &ndash; 9</dt><dd>tab by position: 0 all, 1 operator</dd>
<dt>j k &nbsp;g G</dt><dd>scroll down and up, top and bottom</dd>
<dt>y &nbsp;n</dt><dd>answer the radio's question</dd>
<dt>m</dt><dd>voice on or silent (the radio)</dd>
<dt>s</dt><dd>sounds on or muted (this page)</dd>
<dt>X</dt><dd>stop the session of this tab</dd>
<dt>?</dt><dd>this list (Esc closes it)</dd>
</dl>
</div>
<script nonce="{nonce}">
const listEl = document.getElementById('list')
const emptyEl = document.getElementById('empty')
const statusEl = document.getElementById('status')
const tabsEl = document.getElementById('tabs')
const summaryEl = document.getElementById('summary')
const asksEl = document.getElementById('asks')
const actionsEl = document.getElementById('actions')
const composerEl = document.getElementById('composer')
const promptEl = document.getElementById('prompt')
const inputEl = document.getElementById('input')
const sendEl = document.getElementById('send')
const silentEl = document.getElementById('silent')
const liveEl = document.getElementById('live')
const soundEl = document.getElementById('sound')

// Sounds on this page: the radio's own (the theme heard on the air), only for what arrives while it is open,
// one per update at most. On or muted per device. Browsers play them only after the first tap on the page
let soundOn = readPref('sound') !== 'off'
const players = {}
const SOUND_RANK = { end: 1, new: 2, waiting: 3, error: 4 }
let pendingSound = null
function want(name) { if (!pendingSound || SOUND_RANK[name] > SOUND_RANK[pendingSound]) pendingSound = name }
function playSound(name) {
  if (!soundOn) return
  const a = players[name] || (players[name] = new Audio('/sounds/' + name + '.wav'))
  a.currentTime = 0
  a.play().catch(() => {})   // no tap on the page yet: the browser refuses, and that is fine
}
function drawSound() {
  soundEl.textContent = soundOn ? 'sounds' : 'muted'
  soundEl.classList.toggle('muted', !soundOn)
  soundEl.title = soundOn ? 'Sounds on this page: tap to mute' : 'Tap to hear new replies and questions'
}
soundEl.onclick = () => {
  soundOn = !soundOn
  writePref('sound', soundOn ? 'on' : 'off')
  drawSound()
  if (soundOn) playSound('end')
}
const seenQuestions = new Set()
let reachable = true
const nodes = new Map()   // "i:<id>" items, "c:<id>" conversation events -> element (data-session, data-ts, data-who)
const NATO = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet', 'kilo', 'lima']
let lastId = null
let opened = false
let chatVersion = null
let firstLoad = true
let timer = null
let state = { sessions: [], questions: [], who_for: [], silent: false, write: false, operator: 'off' }
let selected = decodeURIComponent(location.hash.slice(1)) || readPref('tab') || 'all'

// localStorage may be missing or throw (private browsing): it is only a convenience
function readPref(k) { try { return localStorage.getItem(k) } catch (e) { return null } }
function writePref(k, v) { try { localStorage.setItem(k, v) } catch (e) {} }

function el(tag, cls, text) {
  const e = document.createElement(tag)
  if (cls) e.className = cls
  if (text !== undefined) e.textContent = text
  return e
}

// Every session has its nick color, like in an IRC client: the same name, the same color
function nickClass(name) {
  if (name === 'operator') return 'nick n-op'
  const i = NATO.indexOf(name)
  return 'nick n' + (i < 0 ? 0 : i % 8)
}

// A little markdown, as the agents write it: **bold**, *italic*, `code`, headings, lists, ``` blocks.
// Built node by node with textContent: no HTML coming from the sessions ever reaches the page
function markdown(node, text) {
  const parts = text.split(/```[^\\n]*\\n?([\\s\\S]*?)```/g)   // odd parts are the inside of fenced code blocks
  parts.forEach((part, i) => {
    if (i % 2) { node.append(el('pre', 'code', part.replace(/\\n$/, ''))); return }
    part.split('\\n').forEach((raw, j, lines) => {
      let line = raw
      const heading = /^#{1,6}\\s+/.exec(line)
      const bullet = /^(\\s*)[-*+]\\s+/.exec(line)
      let target = node
      if (heading) { target = el('strong', 'md-h'); node.append(target); line = line.slice(heading[0].length) }
      else if (bullet) { node.append(document.createTextNode(bullet[1] + '• ')); line = line.slice(bullet[0].length) }
      inline(target, line)
      if (j < lines.length - 1) node.append(document.createTextNode('\\n'))
    })
  })
}

function inline(node, text) {
  const re = /(`[^`\\n]+`)|(\\*\\*[^*\\n]+?\\*\\*)|(__[^_\\n]+?__)|(\\*[^*\\s\\n][^*\\n]*?\\*)|(\\b_[^_\\n]+?_\\b)/g
  let last = 0
  let m
  while ((m = re.exec(text))) {
    linkify(node, text.slice(last, m.index))
    const t = m[0]
    if (m[1]) node.append(el('code', 'md-code', t.slice(1, -1)))
    else {
      const strong = m[2] || m[3]
      const e = el(strong ? 'strong' : 'em')
      inline(e, t.slice(strong ? 2 : 1, strong ? -2 : -1))
      node.append(e)
    }
    last = m.index + t.length
  }
  linkify(node, text.slice(last))
}

// Text and code always go through textContent: no HTML coming from the sessions
function block(text, format) {
  if (format === 'diff') {
    const pre = el('pre', 'diff')
    for (const line of text.replace(/\\n$/, '').split('\\n')) {
      let cls = ''
      if (/^(diff |index |--- |\\+\\+\\+ |new file|deleted file|similarity|rename )/.test(line)) cls = 'diffhead'
      else if (line.startsWith('@@')) cls = 'hunk'
      else if (line.startsWith('+')) cls = 'add'
      else if (line.startsWith('-')) cls = 'del'
      pre.append(el('span', cls, line))
    }
    return pre
  }
  if (format === 'code') return el('pre', 'code', text)
  const p = el('div', 'text')
  markdown(p, text)
  return p
}

// http(s) addresses in the text become links (for example Storybook ones on the tailnet).
// Only text nodes and <a> built here: no HTML coming from the sessions
function linkify(node, text) {
  const re = /https?:\\/\\/[^\\s<>"'`]+/g
  let last = 0
  let m
  while ((m = re.exec(text))) {
    const url = m[0].replace(/[.,;:!?)\\]}]+$/, '')
    node.append(document.createTextNode(text.slice(last, m.index)))
    const a = el('a', 'link', url)
    a.href = url
    a.target = '_blank'
    a.rel = 'noopener noreferrer'
    node.append(a)
    last = m.index + url.length
    re.lastIndex = last
  }
  node.append(document.createTextNode(text.slice(last)))
}

// One log line: time, nick, text. Long text wraps under the text column, as in irssi
function line(cls, when, nick, nickCls) {
  const d = el('div', 'line ' + cls)
  d.append(el('span', 'time', when), el('span', nickCls, nick))
  const body = el('div', 'body')
  d.append(body)
  return [d, body]
}

function renderItem(item) {
  const [d, body] = line('item', item.when, item.session, nickClass(item.session))
  d.dataset.session = item.session
  d.dataset.ts = item.ts
  d.dataset.who = 'session'
  if (!firstLoad) d.classList.add('fresh')
  body.append(el('div', 'title', item.title))
  const box = el('div', 'box')
  if (item.text) box.append(block(item.text, item.format))
  for (const f of item.files) {
    if (f.kind === 'image') {
      const link = el('a')
      link.href = f.url
      link.target = '_blank'
      const img = el('img')
      img.src = f.url
      img.alt = f.name
      img.loading = 'lazy'
      img.onload = () => { if (following) toBottom() }   // an image grows the page after the scroll to the bottom
      link.append(img)
      box.append(link, el('div', 'file', f.name))
    } else if (f.kind === 'audio') {
      const player = el('audio')
      player.controls = true
      player.preload = 'none'   // downloaded only when you press play: it counts against the phone's data
      player.src = f.url
      box.append(el('div', 'file', f.name), player)
    } else {
      box.append(el('div', 'file', f.name), block(f.text, /\\.(diff|patch)$/i.test(f.name) ? 'diff' : 'code'))
    }
  }
  if (box.childNodes.length) body.append(box)
  return d
}

// Your message (you, to bravo), a session's reply (bravo), a notice of the radio
function target(ev) { return ev.session ? 'to ' + ev.session : '' }

function renderChat(ev) {
  let parts
  if (ev.who === 'you') parts = line('you', ev.when, 'you', 'nick n-you')
  else if (ev.who === 'session') parts = line('agent ' + (ev.kind || ''), ev.when, ev.session, nickClass(ev.session))
  else parts = line('notice', ev.when, 'radio', 'nick n-notice')
  const [d, body] = parts
  d.dataset.session = ev.session
  d.dataset.ts = ev.ts
  d.dataset.who = ev.who
  if (ev.who === 'session' && ev.kind && ev.kind !== 'answer') body.append(el('span', 'kind k-' + ev.kind, ev.kind + ' '))
  if (ev.who === 'you') d.querySelector('.time').after(el('span', 'target', target(ev)))
  if (ev.who === 'you' && ev.via === 'board') d.title = 'typed on the board'
  markdown(body, ev.text)
  return d
}

// The log reads top to bottom like a terminal: a new line goes after the last older one
function insertSorted(node) {
  if (opened) node.classList.add('arrive')   // only what arrives while you look, not the history on opening
  const ts = parseFloat(node.dataset.ts) || 0
  for (const child of listEl.children) {
    if ((parseFloat(child.dataset.ts) || 0) > ts) { listEl.insertBefore(node, child); return }
  }
  listEl.append(node)
}

function atBottom() { return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120 }
let following = true   // you are reading the bottom of the log: new lines and late images keep you there
window.addEventListener('scroll', () => { following = atBottom() }, { passive: true })
function toBottom() { window.scrollTo(0, document.documentElement.scrollHeight) }

// The tabs: "all" and one per session, connected or with something on the board
function sessionNames() {   // the agents: the operator has its own fixed tab
  const names = state.sessions.map(s => s.name).filter(n => n !== 'operator')
  for (const n of nodes.values()) {
    const name = n.dataset.session
    if (name && name !== 'operator' && !names.includes(name)) names.push(name)
  }
  return names
}

function latestOf(name) {   // when something last came from a session (your own messages do not count)
  let max = 0
  for (const n of nodes.values()) {
    if (n.dataset.session === name && n.dataset.who !== 'you') max = Math.max(max, parseFloat(n.dataset.ts) || 0)
  }
  return max
}

function unseen(name) { return latestOf(name) > (parseFloat(readPref('seen:' + name)) || 0) }

let tabsSignature = ''
function drawTabs() {
  const names = sessionNames()
  // Redraw only if something changed: otherwise the bar jumps back to the start every 4 seconds
  const signature = JSON.stringify([selected, state.sessions, state.operator, unseen('operator'), names.map(n => [n, unseen(n)])])
  if (signature === tabsSignature) return
  tabsSignature = signature
  const left = tabsEl.scrollLeft
  tabsEl.replaceChildren()
  const allTab = el('button', 'tab' + (selected === 'all' ? ' active' : ''), 'all')
  allTab.setAttribute('role', 'tab')
  allTab.setAttribute('aria-selected', String(selected === 'all'))
  allTab.type = 'button'
  allTab.onclick = () => selectTab('all')
  tabsEl.append(allTab)
  // The operator: always there, set apart from the agents. Typing in its tab calls it if it is not up
  const op = state.sessions.find(x => x.name === 'operator')
  const opTab = el('button', 'tab op' + (selected === 'operator' ? ' active' : '') + (unseen('operator') ? ' unseen' : ''))
  opTab.setAttribute('role', 'tab')
  opTab.setAttribute('aria-selected', String(selected === 'operator'))
  opTab.type = 'button'
  opTab.append(el('span', 'nick n-op', 'operator'),
               el('span', 'tt', op ? (op.state === 'working' ? 'working' : 'ready') : state.operator === 'starting' ? 'starting' : 'off'),
               el('span', 'state ' + (op ? op.state : state.operator === 'starting' ? 'working' : 'closed')))
  opTab.onclick = () => selectTab('operator')
  tabsEl.append(opTab)
  for (const name of names) {
    const s = state.sessions.find(x => x.name === name)
    const b = el('button', 'tab' + (selected === name ? ' active' : '') + (s ? '' : ' closed') + (unseen(name) ? ' unseen' : ''))
    b.setAttribute('role', 'tab')
    b.setAttribute('aria-selected', String(selected === name))
    b.type = 'button'
    b.append(el('span', nickClass(name), name))
    if (s && s.topic) b.append(el('span', 'tt', s.topic))
    b.append(el('span', 'state ' + (s ? s.state : 'closed')))   // a thin bar: working, waiting for you, idle
    b.onclick = () => selectTab(name)
    tabsEl.append(b)
  }
  tabsEl.scrollLeft = left
  // Newly selected tab (also from the address, #charlie): the bar scrolls until it is shown
  if (selected !== revealed) {
    revealed = selected
    const active = tabsEl.querySelector('.active')
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }
}
let revealed = null

function selectTab(name) {
  selected = name
  writePref('tab', name)
  history.replaceState(null, '', name === 'all' ? location.pathname : '#' + encodeURIComponent(name))
  refreshView()
  toBottom()
}

// Sends an action to the radio. The X-Board-Action header tells the board it comes from this page
async function act(path, body) {
  try {
    const r = await fetch(path, {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Board-Action': '1' },
      body: JSON.stringify(body),
    })
    if (r.status === 401) { location.reload(); return false }
    if (!r.ok) {
      const data = await r.json().catch(() => ({}))
      statusEl.textContent = data.error || ('HTTP ' + r.status)
      statusEl.classList.add('off')
      return false
    }
    setTimeout(poll, 600)   // the radio needs a moment to route it: look again soon
    return true
  } catch (e) {
    statusEl.textContent = 'radio host unreachable'
    statusEl.classList.add('off')
    return false
  }
}

function button(label, cls, onclick) {
  const b = el('button', 'key ' + cls, label)
  b.type = 'button'
  b.onclick = async () => { b.disabled = true; await onclick(); b.disabled = false }
  return b
}

// The radio's questions waiting for yes or no, and "Who is that for?", right above the prompt
function drawAsks() {
  asksEl.replaceChildren()
  if (!state.write) return
  for (const q of state.questions) {
    if (selected !== 'all' && q.session !== selected) continue
    const row = el('div', 'ask')
    row.append(el('span', 'nick n-ask', 'radio asks'), el('span', 'asktext', q.text))
    const keys = el('span', 'keys')
    keys.append(button('yes', 'yes', () => act('/api/answer', { id: q.id, verdict: 'yes' })),
                button('no', '', () => act('/api/answer', { id: q.id, verdict: 'no' })))
    row.append(keys)
    asksEl.append(row)
  }
  if (state.who_for.length && selected === 'all') {
    const row = el('div', 'ask')
    row.append(el('span', 'nick n-ask', 'radio asks'), el('span', 'asktext', 'Who is that for?'))
    const keys = el('span', 'keys')
    for (const name of state.who_for) keys.append(button(name, '', () => act('/api/send', { text: name, session: '' })))
    row.append(keys)
    asksEl.append(row)
  }
}

// In a session's tab: stop it (like saying "Bravo, stop."), its status, close it (the radio asks to confirm)
function drawActions(s) {
  actionsEl.replaceChildren()
  const closed = !s && selected !== 'all' && selected !== 'operator'
  actionsEl.hidden = !(state.write && (s || closed))
  if (actionsEl.hidden) return
  if (closed) {
    // A closed session: its messages and items can go, a new session with the same name starts clean
    actionsEl.append(button('remove from board', 'stop', async () => {
      if (!confirm('Remove ' + selected + "'s messages and items from the board?")) return
      if (await act('/api/forget', { session: selected })) {
        for (const [key, node] of nodes) if (node.dataset.session === selected) { node.remove(); nodes.delete(key) }
        selectTab('all')
      }
    }))
    return
  }
  actionsEl.append(button('stop', 'stop', () => act('/api/send', { text: 'stop', session: s.name })),
                   button('status', '', () => act('/api/send', { text: 'status', session: s.name })))
  if (s.name !== 'operator') actionsEl.append(button('close', '', () => act('/api/send', { text: 'close', session: s.name })))
}

// At the end of the log, one live line per session at work: its name, what it is doing, a scanning line
function drawLive() {
  liveEl.replaceChildren()
  for (const s of state.sessions) {
    if (s.state !== 'working' || (selected !== 'all' && selected !== s.name)) continue
    const row = el('div', 'live')
    row.append(el('span', nickClass(s.name), s.name), el('span', 'kind', 'working'),
               el('span', 'doing', s.doing || 'thinking'), el('span', 'scan'))
    liveEl.append(row)
  }
}

// Show only the selected tab, say whose it is, and mark what is visible as seen
function refreshView() {
  if (selected !== 'all' && selected !== 'operator' && !sessionNames().includes(selected)) selected = 'all'
  let visible = 0
  for (const n of nodes.values()) {
    n.hidden = !(selected === 'all' || n.dataset.session === selected)
    if (!n.hidden) visible++
  }
  if (document.visibilityState === 'visible') {
    for (const name of sessionNames().concat('operator')) {
      const last = latestOf(name)
      if (last && (selected === 'all' || selected === name)) writePref('seen:' + name, String(last))
    }
  }
  const s = state.sessions.find(x => x.name === selected)
  const states = { working: 'working', waiting: 'waiting for you', idle: 'idle' }
  const n = state.sessions.length
  const agents = state.sessions.filter(x => x.name !== 'operator').length
  if (selected === 'all') summaryEl.textContent = agents === 1 ? '1 session open' : (agents || 'no') + ' sessions open'
  else if (selected === 'operator' && !s) summaryEl.textContent = state.operator === 'starting'
    ? 'the operator is starting: what you type reaches it as soon as it is up'
    : 'the operator is off: type to call it'
  else if (s && selected === 'operator') summaryEl.textContent = 'opens, closes and checks the sessions  /  ' + states[s.state]
  else if (s) summaryEl.textContent = [s.topic, s.ticket, s.project, states[s.state]].filter(Boolean).join('  /  ')
  else summaryEl.textContent = selected + ' is closed'
  emptyEl.hidden = visible > 0
  emptyEl.textContent = selected === 'all'
    ? 'Nothing yet. Talk or type to a session: its replies and what it shows you appear here.'
    : selected === 'operator'
      ? 'Ask the operator to open a session, close them all, clean up worktrees or check memory.'
      : 'Nothing from ' + selected + ' yet.'
  // a closed session's tab: nothing to type to (the text would end up elsewhere), only "remove from board"
  composerEl.hidden = !state.write || (!s && selected !== 'all' && selected !== 'operator')
  promptEl.textContent = selected === 'all' ? 'radio' : selected
  promptEl.classList.toggle('op', selected === 'operator')
  inputEl.placeholder = selected === 'all' ? 'message or command'
    : selected === 'operator' ? 'ask the operator' : (s ? 'message to ' + selected : '')
  drawSound()
  silentEl.hidden = !state.write
  silentEl.textContent = state.silent ? 'silent' : 'voice on'
  silentEl.classList.toggle('off', state.silent)
  silentEl.title = state.silent ? 'The radio says nothing: tap to bring the voice back'
    : 'Tap to silence the radio: everything is written here'
  drawActions(s)
  drawAsks()
  drawLive()
  drawTabs()
}

async function loadItems() {
  const r = await fetch('/api/items', { cache: 'no-store' })
  if (r.status === 401) { location.reload(); return false }
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const { items } = await r.json()
  const kept = new Set(items.map(i => 'i:' + i.id))
  for (const [key, node] of nodes) if (key.startsWith('i:') && !kept.has(key)) { node.remove(); nodes.delete(key) }
  for (const item of items) {
    const key = 'i:' + item.id
    if (nodes.has(key)) continue
    const node = renderItem(item)
    nodes.set(key, node)
    insertSorted(node)
    if (opened) want('new')
  }
  firstLoad = false
  return true
}

async function loadChat() {
  const r = await fetch('/api/chat', { cache: 'no-store' })
  if (r.status === 401) { location.reload(); return false }
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const { events } = await r.json()
  const kept = new Set(events.map(e => 'c:' + e.id))
  for (const [key, node] of nodes) if (key.startsWith('c:') && !kept.has(key)) { node.remove(); nodes.delete(key) }
  for (const ev of events) {
    const key = 'c:' + ev.id
    const old = nodes.get(key)
    if (old) {
      // your message reached a session after it was drawn: it moves to that tab too
      if (old.dataset.session !== ev.session) {
        old.dataset.session = ev.session
        const to = old.querySelector('.target')
        if (to) to.textContent = target(ev)
      }
      continue
    }
    const node = renderChat(ev)
    nodes.set(key, node)
    insertSorted(node)
    if (opened && ev.who === 'session') want(ev.kind === 'blocked' ? 'waiting' : 'end')
  }
  return true
}

async function poll() {
  try {
    const follow = following   // stay at the bottom only if you were already there
    const r = await fetch('/api/latest', { cache: 'no-store' })
    if (r.status === 401) { location.reload(); return }
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const data = await r.json()
    state = { sessions: data.sessions || [], questions: data.questions || [], who_for: data.who_for || [],
              silent: !!data.silent, write: !!data.write, operator: data.operator || 'off' }
    if (data.latest !== lastId && await loadItems()) lastId = data.latest
    if (data.chat !== undefined && data.chat !== chatVersion && await loadChat()) chatVersion = data.chat
    for (const q of state.questions) {
      if (!seenQuestions.has(q.id)) { if (opened) want('waiting'); seenQuestions.add(q.id) }
    }
    refreshView()
    if (follow) toBottom()
    opened = true
    reachable = true
    if (pendingSound) { playSound(pendingSound); pendingSound = null }
    statusEl.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    statusEl.classList.remove('off')
  } catch (e) {
    statusEl.textContent = 'radio host unreachable'
    statusEl.classList.add('off')
    if (reachable && opened) playSound('error')   // once, when the radio stops answering
    reachable = false
  }
}

composerEl.addEventListener('submit', async e => {
  e.preventDefault()
  const text = inputEl.value.trim()
  if (!text) return
  sendEl.disabled = true
  if (await act('/api/send', { text, session: selected === 'all' ? '' : selected })) {
    inputEl.value = ''
    inputEl.style.height = ''
    toBottom()
  }
  sendEl.disabled = false
})
// Enter (or Ctrl/Cmd+Enter) sends, Shift+Enter starts a new line (on a phone keyboard, the send key); Esc leaves the box
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); composerEl.requestSubmit() }
  else if (e.key === 'Escape') { e.preventDefault(); inputEl.blur() }
})
// Keyboard: single keys while you are not typing in the box, like a pager or vim. "?" lists them
const helpEl = document.getElementById('help')
const keysEl = document.getElementById('keys')
function tabOrder() { return ['all', 'operator'].concat(sessionNames()) }
function moveTab(step) {
  const order = tabOrder()
  const i = Math.max(0, order.indexOf(selected))
  selectTab(order[(i + step + order.length) % order.length])
}
function toggleHelp(show) { helpEl.hidden = show === undefined ? !helpEl.hidden : !show }
keysEl.onclick = () => toggleHelp()
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return
  const typing = e.target === inputEl || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)
  if (typing) return
  if (e.key === 'Escape') { if (!helpEl.hidden) { toggleHelp(false); e.preventDefault() } return }
  const s = state.sessions.find(x => x.name === selected)
  const visibleQuestion = state.questions.find(q => selected === 'all' || q.session === selected)
  let handled = true
  switch (e.key) {
    case '/': case 'i': if (!composerEl.hidden) inputEl.focus(); break
    case 'ArrowLeft': case 'h': moveTab(-1); break
    case 'ArrowRight': case 'l': moveTab(1); break
    case 'j': window.scrollBy(0, 120); break
    case 'k': window.scrollBy(0, -120); break
    case 'g': window.scrollTo(0, 0); break
    case 'G': toBottom(); break
    case 'y': case 'n':
      if (state.write && visibleQuestion) act('/api/answer', { id: visibleQuestion.id, verdict: e.key === 'y' ? 'yes' : 'no' })
      else handled = false
      break
    case 'm': if (state.write) silentEl.click(); break
    case 's': soundEl.click(); break
    case 'X': if (state.write && s) act('/api/send', { text: 'stop', session: s.name }); else handled = false; break
    case '?': toggleHelp(); break
    default:
      if (/^[0-9]$/.test(e.key) && tabOrder()[+e.key]) selectTab(tabOrder()[+e.key])
      else handled = false
  }
  if (handled) e.preventDefault()
})
// The box grows with the text, up to a few lines
inputEl.addEventListener('input', () => {
  inputEl.style.height = ''
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px'
})
silentEl.onclick = async () => {
  silentEl.disabled = true
  if (await act('/api/silent', { on: !state.silent })) { state.silent = !state.silent; refreshView() }
  silentEl.disabled = false
}

// In the background Chrome suspends the page: poll only while it is visible, and immediately on return
function schedule() {
  clearInterval(timer)
  if (document.visibilityState === 'visible') {
    poll()
    timer = setInterval(poll, 4000)
  }
}
document.addEventListener('visibilitychange', schedule)
window.addEventListener('hashchange', () => { selected = decodeURIComponent(location.hash.slice(1)) || 'all'; refreshView() })
schedule()
</script>
</body>
</html>
"""


class BoardHandler(BaseHTTPRequestHandler):
    """The board's HTTP server, the one reachable from the phone: the page, its data, and its actions."""

    board = None     # set by start()
    log = None
    sessions = None  # bot function that lists the sessions (name, topic, ticket, project, state)
    radio = None     # the bot: conversation, questions, silent mode, and the actions sent from the board
    server_version = "board"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _html(self, code, page, **fields):
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
               "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        body = page.replace("{nonce}", nonce)
        for key, value in fields.items():
            body = body.replace("{" + key + "}", html.escape(value))
        self._send(code, body.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp, "Cache-Control": "no-store"})

    def _authed(self):
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        morsel = jar.get(COOKIE)
        return morsel is not None and self.board.cookie_ok(morsel.value)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            if self._authed():
                return self._html(200, PAGE)
            return self._html(200, LOGIN, error="")
        if not self._authed():
            return self._json(401, {"error": "pairing code required: say \"board code\" to the radio"})
        if path == "/api/latest":
            sessions = []
            if self.sessions:
                try:
                    sessions = self.sessions()
                except Exception:
                    sessions = []
            state = {}
            if self.radio is not None:
                try:
                    state = self.radio.board_state()
                except Exception:
                    state = {}
            return self._json(200, dict(state, latest=self.board.latest(), sessions=sessions))
        if path == "/api/chat":
            events = self.radio.board_chat() if self.radio is not None else []
            return self._json(200, {"events": [public_chat(e) for e in events]})
        if path == "/api/items":
            return self._json(200, {"items": [public(m) for m in self.board.items()]})
        m = re.match(r"^/sounds/([a-z]+)\.wav$", path)
        if m and self.radio is not None:
            data = self.radio.board_sound(m.group(1))
            if data:
                return self._send(200, data, "audio/wav", {"Cache-Control": "private, max-age=86400",
                                                            "Content-Security-Policy": "default-src 'none'"})
        m = re.match(r"^/f/([^/]+)/([^/]+)$", path)
        if m:
            file_path, mime = self.board.media_path(m.group(1), m.group(2))
            if file_path:
                with open(file_path, "rb") as f:
                    data = f.read()
                return self._send(200, data, mime, {"Cache-Control": "private, max-age=604800, immutable",
                                                    "Content-Security-Policy": "default-src 'none'"})
        return self._json(404, {"error": "not found"})

    def _action(self, action):
        """A POST from the page that acts on the sessions. A paired browser controls agents that run commands on
        this machine, so besides the cookie (HttpOnly, SameSite=Strict) the request must come from the page itself:
        JSON with the X-Board-Action header (another site cannot send it without a CORS preflight, which this
        server never answers) and, when the browser says where it comes from, an Origin equal to this host."""
        if not self._authed():
            return self._json(401, {"error": "pairing code required: say \"board code\" to the radio"})
        if self.headers.get("X-Board-Action") != "1" or \
                not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(403, {"error": "actions come only from the board page"})
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host', '')}":
            return self._json(403, {"error": "cross-origin request"})
        if self.radio is None:
            return self._json(503, {"error": "the radio is not connected to the board"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16384:
            return self._json(413, {"error": "too long"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            self.radio.board_action(action, body, self.client_address[0])
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/send", "/api/answer", "/api/silent", "/api/forget"):
            return self._action(path.rsplit("/", 1)[1])
        if path != "/pair":
            return self._json(404, {"error": "not found"})
        length = min(int(self.headers.get("Content-Length") or 0), 1024)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        attempt = re.sub(r"\D", "", (form.get("code") or [""])[0])[:6]
        ok = self.board.try_code(attempt)
        who = self.client_address[0]
        if ok:
            self.log.write("board_paired", "board", f"from={who}")
            cookie = (f"{COOKIE}={self.board.make_cookie()}; Path=/; Max-Age={COOKIE_DAYS * 86400}; "
                      "HttpOnly; SameSite=Strict")   # no Secure: it is plain http on the tailnet, a Secure cookie would not be stored
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.log.write("board_pair_failed", "board", f"from={who} locked={int(ok is None)}")
        if ok is None:
            return self._html(429, LOGIN, error="Too many wrong attempts: ask the radio for a new code.")
        return self._html(401, LOGIN, error="Wrong or expired code.")


def start(board, log, port, sessions=None, radio=None):
    """Start the board server in a separate thread. sessions: function that lists the sessions.
    radio: the bot (board_state, board_chat, board_action), for the conversation and the actions."""
    BoardHandler.board = board
    BoardHandler.log = log
    BoardHandler.sessions = staticmethod(sessions) if sessions else None
    BoardHandler.radio = radio
    server = ThreadingHTTPServer(("0.0.0.0", port), BoardHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="board").start()
    return server
