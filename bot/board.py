"""
Board: the page where sessions put in front of the user what does not work well by voice, that is
drafts to approve (merge request descriptions, commit messages), code, diffs, screenshots and audio.

Two halves, for safety:
- writing happens only through the bot's localhost API (POST /show, called by channel/radio.ts)
- reading happens here, in a separate HTTP server on the board port, published only on the
  Tailscale IP. Nothing is sent to the sessions from here: replies go through the radio, by voice

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
        "when": when(meta.get("ts")),
    }


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
<div class="row"><h1>Board</h1><span id="status">loading...</span></div>
<nav id="tabs" class="tabs" aria-label="Sessions"></nav>
</div>
<main>
<p id="summary" class="summary"></p>
<div id="list"></div>
<p id="empty" class="empty" hidden></p>
</main>
<script nonce="{nonce}">
const listEl = document.getElementById('list')
const emptyEl = document.getElementById('empty')
const statusEl = document.getElementById('status')
const tabsEl = document.getElementById('tabs')
const summaryEl = document.getElementById('summary')
const nodes = new Map()   // id -> element on the page (data-session says whose it is)
let lastId = null
let firstLoad = true
let timer = null
let sessions = []         // connected sessions, from the bot: name, topic, ticket, project, state
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

// The tabs: "All" and one per session, connected or with something on the board
function sessionNames() {
  const names = sessions.map(s => s.name)
  for (const n of nodes.values()) if (!names.includes(n.dataset.session)) names.push(n.dataset.session)
  return names
}

function latestOf(name) {   // the most recent id of a session: ids grow over time
  let max = ''
  for (const [id, n] of nodes) if (n.dataset.session === name && id > max) max = id
  return max
}

let tabsSignature = ''
function drawTabs() {
  const names = sessionNames()
  // Redraw only if something changed: otherwise the bar jumps back to the start every 4 seconds
  const signature = JSON.stringify([selected, sessions, names.map(n => [n, latestOf(n) > (readPref('seen:' + n) || '')])])
  if (signature === tabsSignature) return
  tabsSignature = signature
  const left = tabsEl.scrollLeft
  tabsEl.replaceChildren()
  const allTab = el('button', 'tab' + (selected === 'all' ? ' active' : ''))
  allTab.append(el('b', '', 'All'))
  allTab.onclick = () => selectTab('all')
  tabsEl.append(allTab)
  for (const name of names) {
    const s = sessions.find(x => x.name === name)
    const b = el('button', 'tab' + (selected === name ? ' active' : '') + (s ? '' : ' closed'))
    const head = el('span', 'name')
    head.append(el('span', 'dot ' + (s ? s.state : 'closed')), el('b', '', name))
    b.append(head)
    if (s && s.topic) b.append(el('small', '', s.topic))
    if (latestOf(name) > (readPref('seen:' + name) || '')) b.append(el('span', 'unseen'))
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

// Show only the selected tab's items, say whose they are, and mark what is visible as seen
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
      if (last && (selected === 'all' || selected === name)) writePref('seen:' + name, last)
    }
  }
  const s = sessions.find(x => x.name === selected)
  const states = { working: 'working', waiting: 'waiting for you', idle: 'idle' }
  if (selected === 'all') {
    summaryEl.textContent = sessions.length === 1 ? '1 session open' : (sessions.length || 'No') + ' sessions open'
  } else if (s) {
    summaryEl.textContent = [s.topic, s.ticket, s.project, states[s.state]].filter(Boolean).join(' · ')
  } else {
    summaryEl.textContent = 'session closed'
  }
  emptyEl.hidden = visible > 0
  emptyEl.textContent = selected === 'all'
    ? 'Nothing yet. When you ask a session to show you something, it arrives here.'
    : 'Nothing from ' + selected + ' yet.'
  drawTabs()
}

async function loadItems() {
  const r = await fetch('/api/items', { cache: 'no-store' })
  if (r.status === 401) { location.reload(); return false }
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const { items } = await r.json()
  const kept = new Set(items.map(i => i.id))
  for (const [id, node] of nodes) if (!kept.has(id)) { node.remove(); nodes.delete(id) }
  // New items are always the most recent: they go on top, in the order they arrive
  const added = document.createDocumentFragment()
  for (const item of items) {
    if (nodes.has(item.id)) continue
    const node = renderItem(item)
    nodes.set(item.id, node)
    added.append(node)
  }
  listEl.prepend(added)
  firstLoad = false
  return true
}

async function poll() {
  try {
    const r = await fetch('/api/latest', { cache: 'no-store' })
    if (r.status === 401) { location.reload(); return }
    if (!r.ok) throw new Error('HTTP ' + r.status)
    const data = await r.json()
    sessions = data.sessions || []
    if (data.latest !== lastId && await loadItems()) lastId = data.latest
    refreshView()
    statusEl.textContent = 'updated at ' + new Date().toLocaleTimeString()
    statusEl.classList.remove('off')
  } catch (e) {
    statusEl.textContent = 'Radio host unreachable'
    statusEl.classList.add('off')
  }
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
    """The reading side of the board, the one reachable from the phone."""

    board = None     # set by start()
    log = None
    sessions = None  # bot function that lists the sessions (name, topic, ticket, project, state)
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
            return self._json(200, {"latest": self.board.latest(), "sessions": sessions})
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

    def do_POST(self):
        if urlparse(self.path).path != "/pair":
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


def start(board, log, port, sessions=None):
    """Start the board server in a separate thread. sessions: function that lists the sessions."""
    BoardHandler.board = board
    BoardHandler.log = log
    BoardHandler.sessions = staticmethod(sessions) if sessions else None
    server = ThreadingHTTPServer(("0.0.0.0", port), BoardHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="board").start()
    return server
