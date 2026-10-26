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
:root { --bg: #f6f5f2; --card: #fff; --fg: #1d1d1f; --muted: #6b6b70; --line: #e3e1dc; --accent: #c2410c;
  --code-bg: #f1efea; --add-bg: #e3f3e7; --add-fg: #14532d; --del-bg: #fbe9e9; --del-fg: #7f1d1d; --hunk: #6d28d9; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #141414; --card: #1e1e1e; --fg: #ececec; --muted: #9a9a9f; --line: #333; --accent: #fb923c;
    --code-bg: #171717; --add-bg: #10301b; --add-fg: #a7f3c0; --del-bg: #3a1515; --del-fg: #fecaca; --hunk: #c4b5fd; }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, Roboto, sans-serif; }
.bar { position: sticky; top: 0; z-index: 1; padding: 10px 12px 8px; background: var(--bg); border-bottom: 1px solid var(--line); }
.row { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 0 4px 8px; }
.bar h1 { margin: 0; font-size: 18px; }
.tabs { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; }
.tab { flex: 0 0 auto; position: relative; display: flex; flex-direction: column; justify-content: center; align-items: flex-start;
  min-height: 40px; padding: 4px 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--card);
  color: var(--fg); font: inherit; font-size: 14px; text-align: left; }
.tab .name { display: flex; align-items: center; gap: 6px; }
.tab small { max-width: 9em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.tab.active { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.tab.closed { opacity: .6; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.dot.working { background: #16a34a; }
.dot.waiting { background: var(--accent); }
.dot.closed { background: transparent; border: 1px solid var(--muted); }
.unseen { position: absolute; top: 1px; right: 3px; width: 8px; height: 8px; border-radius: 50%; background: #2563eb; }
.summary { margin: 2px 4px 10px; color: var(--muted); font-size: 14px; }
#status { font-size: 13px; color: var(--muted); }
#status.off { color: var(--accent); font-weight: 600; }
main { max-width: 900px; margin: 0 auto; padding: 12px 12px 48px; }
.empty { color: var(--muted); padding: 24px 4px; }
article { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin: 0 0 12px; }
article.fresh { border-left: 4px solid var(--accent); }
.meta { display: flex; gap: 8px; font-size: 13px; color: var(--muted); }
.session { font-weight: 600; color: var(--accent); text-transform: capitalize; }
h2 { margin: 2px 0 6px; font-size: 17px; line-height: 1.3; overflow-wrap: anywhere; }
/* on a phone long lines wrap: scrolling sideways line by line is worse */
pre { margin: 8px 0 0; padding: 10px; border-radius: 8px; background: var(--code-bg); white-space: pre-wrap;
  overflow-wrap: anywhere; font: 13px/1.45 ui-monospace, Menlo, Consolas, "Roboto Mono", monospace; }
pre.text { padding: 0; background: none; font: inherit; white-space: pre-wrap; overflow-wrap: anywhere; }
pre.diff { padding: 10px 0; }
.lines span { display: block; padding: 0 10px; min-height: 1.45em; white-space: pre-wrap; overflow-wrap: anywhere; }
.add { background: var(--add-bg); color: var(--add-fg); }
.del { background: var(--del-bg); color: var(--del-fg); }
.hunk { color: var(--hunk); }
.diffhead { color: var(--muted); font-weight: 600; }
a.link { color: var(--accent); }
.file { margin-top: 10px; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
audio { display: block; width: 100%; margin-top: 6px; }
img { display: block; max-width: 100%; height: auto; margin-top: 8px; border-radius: 8px; border: 1px solid var(--line); }
.login { max-width: 420px; margin: 0 auto; padding: 32px 16px; }
.login input { width: 100%; margin: 12px 0; padding: 12px; font-size: 28px; letter-spacing: 6px; text-align: center;
  border: 1px solid var(--line); border-radius: 10px; background: var(--card); color: var(--fg); }
.login button { width: 100%; padding: 12px; font-size: 17px; border: 0; border-radius: 10px; background: var(--accent); color: #fff; }
.error { color: var(--accent); font-weight: 600; }
[hidden] { display: none !important; }   /* the classes below set display: hidden must still win */
.right { display: flex; align-items: center; gap: 10px; }
.pill { padding: 3px 10px; border: 1px solid var(--line); border-radius: 999px; background: var(--card); color: var(--fg); font: inherit; font-size: 13px; }
.pill.on { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.composer { display: flex; gap: 8px; align-items: flex-end; margin: 8px 0 0; }
.composer textarea { flex: 1; min-height: 40px; max-height: 160px; padding: 9px 12px; resize: none; border: 1px solid var(--line);
  border-radius: 12px; background: var(--card); color: var(--fg); font: inherit; font-size: 16px; line-height: 1.35; }
.composer button, .buttons button, .actions button { padding: 8px 14px; border: 1px solid var(--line); border-radius: 10px;
  background: var(--card); color: var(--fg); font: inherit; font-size: 15px; }
.composer button, .buttons button.primary { border-color: var(--accent); background: var(--accent); color: #fff; }
.actions { display: flex; gap: 8px; margin: -4px 4px 12px; }
.actions button.danger { border-color: #b91c1c; color: #b91c1c; font-weight: 600; }
.ask { background: var(--card); border: 1px solid var(--accent); border-radius: 12px; padding: 10px 14px; margin: 0 0 12px; }
.ask p { margin: 0 0 8px; }
.buttons { display: flex; flex-wrap: wrap; gap: 8px; }
button:disabled { opacity: .5; }
.msg { margin: 0 0 12px; display: flex; flex-direction: column; }
.msg .bubble { max-width: 88%; padding: 8px 12px; border-radius: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.msg.you { align-items: flex-end; }
.msg.you .bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
.msg.you .bubble a.link { color: #fff; }
.msg.agent .bubble { background: var(--card); border: 1px solid var(--line); border-bottom-left-radius: 4px; }
.msg.agent.blocked .bubble { border-color: var(--accent); }
.msgmeta { font-size: 12px; color: var(--muted); margin: 0 4px 3px; }
.msg.radio { align-items: center; text-align: center; color: var(--muted); font-size: 13px; padding: 0 12px; }
.msg.radio .when { font-size: 11px; }
"""

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
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
<div class="bar">
<div class="row"><h1>Board</h1><span class="right"><button id="silent" class="pill" type="button" hidden></button><span id="status">loading...</span></span></div>
<nav id="tabs" class="tabs" aria-label="Sessions"></nav>
<form id="composer" class="composer" hidden>
<textarea id="input" rows="1" maxlength="4000" aria-label="Message"></textarea>
<button id="send" type="submit">Send</button>
</form>
</div>
<main>
<p id="summary" class="summary"></p>
<div id="actions" class="actions" hidden></div>
<div id="asks"></div>
<div id="list"></div>
<p id="empty" class="empty" hidden></p>
</main>
<script nonce="{nonce}">
const listEl = document.getElementById('list')
const emptyEl = document.getElementById('empty')
const statusEl = document.getElementById('status')
const tabsEl = document.getElementById('tabs')
const summaryEl = document.getElementById('summary')
const asksEl = document.getElementById('asks')
const actionsEl = document.getElementById('actions')
const composerEl = document.getElementById('composer')
const inputEl = document.getElementById('input')
const sendEl = document.getElementById('send')
const silentEl = document.getElementById('silent')
const nodes = new Map()   // "i:<id>" items, "c:<id>" conversation events -> element (data-session, data-ts, data-who)
let lastId = null
let chatVersion = null
let firstLoad = true
let timer = null
let state = { sessions: [], questions: [], who_for: [], silent: false, write: false }
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

// Text and code always go through textContent: no HTML coming from the sessions
function block(text, format) {
  if (format === 'diff') {
    const pre = el('pre', 'diff')
    const lines = el('div', 'lines')
    for (const line of text.replace(/\\n$/, '').split('\\n')) {
      let cls = ''
      if (/^(diff |index |--- |\\+\\+\\+ |new file|deleted file|similarity|rename )/.test(line)) cls = 'diffhead'
      else if (line.startsWith('@@')) cls = 'hunk'
      else if (line.startsWith('+')) cls = 'add'
      else if (line.startsWith('-')) cls = 'del'
      lines.append(el('span', cls, line))
    }
    pre.append(lines)
    return pre
  }
  if (format === 'code') return el('pre', 'code', text)
  const pre = el('pre', 'text')
  linkify(pre, text)
  return pre
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

function renderItem(item) {
  const a = el('article')
  a.dataset.session = item.session
  a.dataset.ts = item.ts
  a.dataset.who = 'session'
  if (!firstLoad) a.classList.add('fresh')
  const meta = el('div', 'meta')
  meta.append(el('span', 'session', item.session), el('span', '', item.when))
  a.append(meta, el('h2', '', item.title))
  if (item.text) a.append(block(item.text, item.format))
  for (const f of item.files) {
    if (f.kind === 'image') {
      const link = el('a')
      link.href = f.url
      link.target = '_blank'
      const img = el('img')
      img.src = f.url
      img.alt = f.name
      img.loading = 'lazy'
      link.append(img)
      a.append(link, el('div', 'file', f.name))
    } else if (f.kind === 'audio') {
      const player = el('audio')
      player.controls = true
      player.preload = 'none'   // downloaded only when you press play: it counts against the phone's data
      player.src = f.url
      a.append(el('div', 'file', f.name), player)
    } else {
      a.append(el('div', 'file', f.name), block(f.text, /\\.(diff|patch)$/i.test(f.name) ? 'diff' : 'code'))
    }
  }
  return a
}

// A conversation event: your message (right), a session's reply (left), a notice of the radio (small, centered)
function chatMeta(ev) {
  if (ev.who === 'you') {
    return 'you' + (ev.session ? ' → ' + ev.session : '') + ' · ' + ev.when + (ev.via === 'board' ? ' · typed' : ' · said')
  }
  if (ev.who === 'session') return ev.when + (ev.kind && ev.kind !== 'answer' ? ' · ' + ev.kind : '')
  return ev.when
}

function renderChat(ev) {
  const d = el('div', 'msg ' + (ev.who === 'you' ? 'you' : ev.who === 'session' ? 'agent ' + (ev.kind || '') : 'radio'))
  d.dataset.session = ev.session
  d.dataset.ts = ev.ts
  d.dataset.who = ev.who
  if (ev.who === 'radio') {
    const line = el('span', '', ev.text)
    d.append(line, el('small', 'when', ' ' + ev.when))
    return d
  }
  const meta = el('div', 'msgmeta')
  if (ev.who === 'session') meta.append(el('span', 'session', ev.session), document.createTextNode(' '))
  meta.append(el('span', 'metatext', chatMeta(ev)))
  const bubble = el('div', 'bubble')
  linkify(bubble, ev.text)
  d.append(meta, bubble)
  return d
}

// Nodes are kept newest first: a new one goes before the first older one
function insertSorted(node) {
  const ts = parseFloat(node.dataset.ts) || 0
  for (const child of listEl.children) {
    if ((parseFloat(child.dataset.ts) || 0) < ts) { listEl.insertBefore(node, child); return }
  }
  listEl.append(node)
}

// The tabs: "All" and one per session, connected or with something on the board
function sessionNames() {
  const names = state.sessions.map(s => s.name)
  for (const n of nodes.values()) if (n.dataset.session && !names.includes(n.dataset.session)) names.push(n.dataset.session)
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
  const signature = JSON.stringify([selected, state.sessions, names.map(n => [n, unseen(n)])])
  if (signature === tabsSignature) return
  tabsSignature = signature
  const left = tabsEl.scrollLeft
  tabsEl.replaceChildren()
  const allTab = el('button', 'tab' + (selected === 'all' ? ' active' : ''))
  allTab.type = 'button'
  allTab.append(el('b', '', 'All'))
  allTab.onclick = () => selectTab('all')
  tabsEl.append(allTab)
  for (const name of names) {
    const s = state.sessions.find(x => x.name === name)
    const b = el('button', 'tab' + (selected === name ? ' active' : '') + (s ? '' : ' closed'))
    b.type = 'button'
    const head = el('span', 'name')
    head.append(el('span', 'dot ' + (s ? s.state : 'closed')), el('b', '', name))
    b.append(head)
    if (s && s.topic) b.append(el('small', '', s.topic))
    if (unseen(name)) b.append(el('span', 'unseen'))
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
  window.scrollTo(0, 0)
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
    statusEl.textContent = 'Radio host unreachable'
    statusEl.classList.add('off')
    return false
  }
}

function button(label, cls, onclick) {
  const b = el('button', cls, label)
  b.type = 'button'
  b.onclick = async () => { b.disabled = true; await onclick(); b.disabled = false }
  return b
}

// The radio's questions waiting for yes or no, and "Who is that for?": answered with a tap
function drawAsks() {
  asksEl.replaceChildren()
  if (!state.write) return
  for (const q of state.questions) {
    if (selected !== 'all' && q.session !== selected) continue
    const card = el('div', 'ask')
    card.append(el('p', '', q.text))
    const row = el('div', 'buttons')
    row.append(button('Yes', 'primary', () => act('/api/answer', { id: q.id, verdict: 'yes' })),
               button('No', '', () => act('/api/answer', { id: q.id, verdict: 'no' })))
    card.append(row)
    asksEl.append(card)
  }
  if (state.who_for.length && selected === 'all') {
    const card = el('div', 'ask')
    card.append(el('p', '', 'Who is that for?'))
    const row = el('div', 'buttons')
    for (const name of state.who_for) row.append(button(name, 'primary', () => act('/api/send', { text: name, session: '' })))
    card.append(row)
    asksEl.append(card)
  }
}

// In a session's tab: stop it (like saying "Bravo, stop."), its status, close it (the radio asks to confirm)
function drawActions(s) {
  actionsEl.replaceChildren()
  actionsEl.hidden = !(state.write && s)
  if (actionsEl.hidden) return
  actionsEl.append(button('Stop', 'danger', () => act('/api/send', { text: 'stop', session: s.name })),
                   button('Status', '', () => act('/api/send', { text: 'status', session: s.name })),
                   button('Close', '', () => act('/api/send', { text: 'close', session: s.name })))
}

// Show only the selected tab, say whose it is, and mark what is visible as seen
function refreshView() {
  if (selected !== 'all' && !sessionNames().includes(selected)) selected = 'all'
  let visible = 0
  for (const n of nodes.values()) {
    n.hidden = !(selected === 'all' || n.dataset.session === selected)
    if (!n.hidden) visible++
  }
  if (document.visibilityState === 'visible') {
    for (const name of sessionNames()) {
      const last = latestOf(name)
      if (last && (selected === 'all' || selected === name)) writePref('seen:' + name, String(last))
    }
  }
  const s = state.sessions.find(x => x.name === selected)
  const states = { working: 'working', waiting: 'waiting for you', idle: 'idle' }
  const n = state.sessions.length
  if (selected === 'all') summaryEl.textContent = n === 1 ? '1 session open' : (n || 'No') + ' sessions open'
  else if (s) summaryEl.textContent = [s.topic, s.ticket, s.project, states[s.state]].filter(Boolean).join(' · ')
  else summaryEl.textContent = 'session closed'
  emptyEl.hidden = visible > 0
  emptyEl.textContent = selected === 'all'
    ? 'Nothing yet. Talk or type to a session: its replies and what it shows you arrive here.'
    : 'Nothing from ' + selected + ' yet.'
  composerEl.hidden = !state.write
  inputEl.placeholder = selected === 'all' ? 'Message or command'
    : (s ? 'Message to ' + selected : selected + ' is closed')
  silentEl.hidden = !state.write
  silentEl.textContent = state.silent ? 'Silent' : 'Voice on'
  silentEl.classList.toggle('on', state.silent)
  silentEl.title = state.silent ? 'The radio says nothing: tap to bring the voice back'
    : 'Tap to silence the radio: everything is written here'
  drawActions(s)
  drawAsks()
  drawTabs()
}

async function loadItems() {
  const r = await fetch('/api/items', { cache: 'no-store' })
  if (r.status === 401) { location.reload(); return false }
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const { items } = await r.json()
  const kept = new Set(items.map(i => 'i:' + i.id))
  for (const [key, node] of nodes) if (key.startsWith('i:') && !kept.has(key)) { node.remove(); nodes.delete(key) }
  // Oldest first: each new one ends up on top of the previous ones
  for (const item of items.slice().reverse()) {
    const key = 'i:' + item.id
    if (nodes.has(key)) continue
    const node = renderItem(item)
    nodes.set(key, node)
    insertSorted(node)
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
        const meta = old.querySelector('.metatext')
        if (meta) meta.textContent = chatMeta(ev)
      }
      continue
    }
    const node = renderChat(ev)
    nodes.set(key, node)
    insertSorted(node)
  }
  return true
}

async function poll() {
  try {
    const r = await fetch('/api/latest', { cache: 'no-store' })
    if (r.status === 401) { location.reload(); return }
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const data = await r.json()
    state = { sessions: data.sessions || [], questions: data.questions || [], who_for: data.who_for || [],
              silent: !!data.silent, write: !!data.write }
    if (data.latest !== lastId && await loadItems()) lastId = data.latest
    if (data.chat !== undefined && data.chat !== chatVersion && await loadChat()) chatVersion = data.chat
    refreshView()
    statusEl.textContent = 'updated at ' + new Date().toLocaleTimeString()
    statusEl.classList.remove('off')
  } catch (e) {
    statusEl.textContent = 'Radio host unreachable'
    statusEl.classList.add('off')
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
  }
  sendEl.disabled = false
})
// The box grows with the text, up to a few lines
inputEl.addEventListener('input', () => {
  inputEl.style.height = ''
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px'
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
        if path in ("/api/send", "/api/answer", "/api/silent"):
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
