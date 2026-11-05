# Speech recorded on the board: the same Whisper, filters and tab rules as typed text, never a beep on the channel
import http.client
import queue
import socket
import tempfile
import time

import radio_bot as rb
import board

rb.VOICE_FX = ""
rb.ACK_MODE = "name"
rb.SOUND_OPEN = rb.SOUND_CLOSE = rb.SOUND_NEW = b""
rb.BOARD_DIR = tempfile.mkdtemp()
rb.BOARD_WRITE = True
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)


class Log:
    path = "/nonexistent.csv"
    def __init__(self):
        self.rows = []
    def write(self, *a):
        self.rows.append(a)


class Whisper:
    """Says what it is told to, or fails."""
    def __init__(self):
        self.said, self.calls = [], 0
    def transcribe(self, pcm48k, duration_ms):
        self.calls += 1
        text = self.said.pop(0)
        if isinstance(text, Exception):
            raise text
        return text, "accurate"


whisper = Whisper()
bot = rb.RadioBot(Log(), transcriber=whisper, speaker=None)
bot.launcher_q = queue.Queue()
played = []
bot.play = played.append
bot.speak = lambda *a, **k: None
qs = {n: bot.sessions.connect(n) for n in ("alpha", "bravo", "operator")}
second = b"\x10\x00" * 16000   # one second at 16 kHz


def utterance():
    _priority, _seq, kind, buf = bot.work.get_nowait()
    return kind, buf


def heard(text, tab, pcm=second, rate=16000):
    whisper.said.append(text)
    bot.board_voice(pcm, rate, tab, "100.64.0.2")
    return bot.handle(utterance()[1])


def arrived(name):
    return qs[name].get_nowait() if not qs[name].empty() else None


def last_you():
    return [e for e in bot.chat.snapshot() if e["who"] == "you"][-1]


# 1. HTTP: who may send speech, and what reaches the worker
store = board.Board(tempfile.mkdtemp())
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
board.start(store, Log(), port, bot.board_sessions, bot)
time.sleep(0.3)
good = {"Content-Type": "application/octet-stream", "X-Board-Action": "1",
        "Cookie": f"{board.COOKIE}={store.make_cookie()}", "Origin": f"http://127.0.0.1:{port}"}


def post(path, headers, body=second, length=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("POST", path)
    for k, v in headers.items():
        c.putheader(k, v)
    c.putheader("Content-Length", str(len(body) if length is None else length))
    c.endheaders()
    c.send(body)
    return c.getresponse().status


path = "/api/voice?session=bravo&rate=16000"
ok(post(path, {k: v for k, v in good.items() if k != "Cookie"}) == 401, "no cookie: 401")
ok(post(path, {k: v for k, v in good.items() if k != "X-Board-Action"}) == 403, "no X-Board-Action header: 403")
ok(post(path, dict(good, Origin="http://evil.example")) == 403, "another origin: 403")
ok(post(path, dict(good, **{"Content-Type": "application/json"})) == 403, "not raw audio: 403")
ok(bot.work.empty(), "none of them reached the worker")
ok(post("/api/voice?session=bravo&rate=16000", good, b"", length=board.MAX_VOICE_S * 2 * 16000 + 2) == 413,
   "longer than three minutes: 413, before reading it")
ok(post("/api/voice?session=bravo&rate=4000", good) == 400 and post("/api/voice?session=Bravo!&rate=16000", good) == 400,
   "a rate or a tab that makes no sense: 400")
ok(post(path, dict(good, Origin=f"https://127.0.0.1:{port}")) == 200, "from the page, also through tailscale serve: 200")
kind, buf = utterance()
ok(kind == "utterance" and buf["board"] and buf["tab"] == "bravo" and buf["name"] == "board-voice"
   and len(buf["pcm"]) == len(second) * 3, "an utterance for the worker, brought to 48 kHz")
rb.BOARD_WRITE = False
ok(post(path, good) == 403 and bot.work.empty(), "BOARD_WRITE=0: 403")
rb.BOARD_WRITE = True

# 2. the tab decides, like typed text; the reply is spoken English, not a typed one
heard("Run the tests.", "bravo")
m = arrived("bravo")
ok(m and m["text"] == "Run the tests." and m["typed"] is False and m["user"] == "board-voice",
   f"in bravo's tab: to bravo, answered aloud in English ({m})")
ok(last_you()["session"] == "bravo" and last_you()["via"] == "voice", "the transcript is in bravo's tab")
heard("Bravo, push.", "alpha")
ok(arrived("bravo")["text"] == "push." and arrived("alpha") is None, "a name said in another tab picks that session")
heard("Bravo non lavora sulla ui?", "operator")
ok(arrived("operator")["text"] == "Bravo non lavora sulla ui?" and arrived("bravo") is None,
   "in the operator's tab a name is what it is told about, as typed (5 November)")
heard("Alpha, run the linter.", "all")
ok(arrived("alpha")["text"] == "run the linter.", "in All: the name picks the session")
logged = [r[0] for r in bot.log.rows]
ok("transcript" in logged and "typed" not in logged, "logged as speech, not as typed text")

# 3. nothing heard: a line in the tab, never a beep on the channel
whisper.calls = 0
heard("", "bravo")
ok(bot.chat.snapshot()[-1]["text"] == "I heard nothing." and bot.chat.snapshot()[-1]["session"] == "bravo",
   "noise: said in the tab")
heard("Sottotitoli a cura di Amara.org", "bravo")
ok("makes up" in bot.chat.snapshot()[-1]["text"], "one of Whisper's inventions: said in the tab")
heard(RuntimeError("connection refused"), "bravo")
ok(bot.chat.snapshot()[-1]["text"].startswith("Whisper failed"), "Whisper down: said in the tab")
calls = whisper.calls
bot.board_voice(second[:3200], 16000, "bravo", "100.64.0.2")
bot.handle(utterance()[1])
ok(whisper.calls == calls and bot.chat.snapshot()[-1]["text"] == "Too short, I heard nothing.", "a tap: not even sent to Whisper")
ok(played == [] and all(arrived(n) is None for n in qs), "no beep, nothing to the sessions")
