# The board as a text console: typed text, answers by id, silent mode, the conversation, and the HTTP checks
import http.client
import json
import queue
import socket
import tempfile
import time
import types

import radio_bot as rb
import board

rb.VOICE_FX = ""
rb.ACK_MODE = "name"
rb.SOUND_OPEN = rb.SOUND_CLOSE = rb.SOUND_NEW = b""
rb.BOARD_DIR = tempfile.mkdtemp()
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)


class Log:
    path = "/nonexistent.csv"
    def __init__(self):
        self.rows = []
    def write(self, *a):
        self.rows.append(a)


class Mumble:
    """Collects what would go on the air."""
    def __init__(self):
        self.sent = []
        self.sound_output = types.SimpleNamespace(add_sound=self.sent.append, clear_buffer=lambda: self.sent.clear(),
                                                  get_buffer_size=lambda: 0.0)


def new_bot():
    bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
    bot.launcher_q = queue.Queue()
    bot.mumble = Mumble()
    qs = {n: bot.sessions.connect(n) for n in ("alpha", "bravo", "charlie")}
    return bot, qs


def arrivals(qs):
    out = []
    for n, q in qs.items():
        while not q.empty():
            out.append((n, q.get_nowait()["text"]))
    return sorted(out)


def chat(bot, who=None):
    return [e for e in bot.chat.snapshot() if who is None or e["who"] == who]


# 1. typed text in a session's tab
bot, qs = new_bot()
ok(bot.on_typed("run the tests", "bravo") == "message" and arrivals(qs) == [("bravo", "run the tests")],
   "in bravo's tab plain text goes to bravo")
you = chat(bot, "you")[-1]
ok(you["session"] == "bravo" and you["via"] == "board" and you["text"] == "run the tests", "the typed message is in bravo's tab")
ok(bot.on_typed("Charlie, push it.", "bravo") == "message" and arrivals(qs) == [("charlie", "push it.")],
   "a name typed in bravo's tab wins")
ok(bot.on_typed("open the ui library", "bravo") in ("open?", "open-failed") and arrivals(qs) == [],
   "\"open ...\" in bravo's tab is a command, not a message to bravo")
bot.questions.clear()
r = bot.on_typed("stop", "bravo")
events = []
while not bot.launcher_q.empty():
    events.append(bot.launcher_q.get_nowait())
ok(r and any(e.get("type") == "interrupt" and e.get("names") == ["bravo"] for e in events),
   f"\"stop\" in bravo's tab stops only bravo ({events})")
bot.last_stop = None
r = bot.on_typed("status", "bravo")
notice = chat(bot, "radio")[-1]
ok(r == "status" and notice["session"] == "bravo", "\"status\" in bravo's tab: bravo's status, in bravo's tab")
bot.on_typed("close", "charlie")
ok(len(bot.questions) == 1 and bot.questions[-1]["kind"] == "close" and bot.questions[-1]["name"] == "charlie",
   "\"close\" in charlie's tab asks to close charlie")
ok(bot.on_typed("yes", "bravo") == "message" and arrivals(qs) == [("bravo", "yes")] and len(bot.questions) == 1,
   "\"yes\" in bravo's tab is a message to bravo: the radio's questions have their own buttons")
ok(bot.on_typed("   ", "bravo") == "empty", "empty text is ignored")
ok(bot.on_typed("Bravo, run lint.", "") == "message" and arrivals(qs) == [("bravo", "run lint.")],
   "in the All tab typed text is routed like speech")

# 2. answers by id
bot, qs = new_bot()
bot.on_typed("close", "alpha")
bot.on_typed("close", "bravo")
first, second = list(bot.questions)
ok(first["id"] != second["id"] and first["said"].startswith("Closing alpha"), "each question has an id and its text")
ok(bot.on_board_answer(first["id"], "allow") == "close", "the first question can be answered after a second one came")
e = bot.launcher_q.get_nowait()
ok(e == {"type": "close", "name": "alpha"} and list(bot.questions) == [second], f"alpha closes, bravo's question stays ({e})")
ok(bot.on_board_answer(first["id"], "allow") == "gone" and chat(bot, "radio")[-1]["text"].startswith("That question"),
   "an answer to a question already answered only says so")
ok(bot.on_board_answer(second["id"], "deny") == "close" and bot.launcher_q.empty() and not bot.questions,
   "No cancels")
state = bot.board_state()
ok(state["questions"] == [] and state["write"] is True and state["silent"] is False, "board state without questions")
bot.on_typed("close", "charlie")
ok([q["session"] for q in bot.board_state()["questions"]] == ["charlie"], "the board sees the question and whose it is")

# 3. silent mode
bot, qs = new_bot()
bot.play(b"\x00\x01")
ok(bot.mumble.sent == [b"\x00\x01"], "with the voice on, sounds go on the air")
bot.mumble.sent.clear()
bot.deferred.append((2, "waiting sentence", None, None))
bot.held = [("held reply", "bravo", "end")]
bot.speaking = {"text": "x", "cut": False, "done": False}
ok(bot.set_silent(True, "test") == "silent", "silent on")
ok(not bot.deferred and not bot.held and bot.speaking["cut"], "silent drops what was waiting and cuts the sentence on the air")
bot.play(b"\x00\x01")
ok(bot.mumble.sent == [], "silent: nothing goes on the air")
while not bot.work.empty():
    bot.work.get_nowait()
bot.speak("Opening delta.")
ok(bot.work.empty() and chat(bot, "radio")[-1]["text"] == "Opening delta.", "silent: the radio's sentences are only written")
bot.on_claude_reply("bravo", "Tests pass.", kind="done")
ok(bot.work.empty() and chat(bot, "session")[-1]["text"] == "Tests pass.", "silent: the replies are only written")
ok(json.load(open(f"{rb.BOARD_DIR}/.settings.json"))["silent"] is True, "silent survives a restart (board/.settings.json)")
ok(rb.RadioBot(Log(), transcriber=object(), speaker=None).silent is True, "a new bot starts silent")
bot.set_silent(False, "test")
ok(bot.work.empty() and not bot.held and not bot.deferred, "voice back on: nothing already read is replayed")
bot.set_silent(True, "test")
ok(bot.on_text("user", "Speak.") == "resume" and bot.silent is False, "saying \"speak\" brings the voice back")

# 4. who your message reached: who-for and continuation
bot, qs = new_bot()
bot.speak = lambda *a, **k: None
texts = []
bot.transcriber = types.SimpleNamespace(transcribe=lambda pcm, dur: (texts.pop(0), "accurate"))
bot.play = lambda *a: None
pcm = bytearray(b"\x00" * rb.BYTES_PER_MS * 1500)
bot.sessions.asking.update({"alpha": time.monotonic(), "bravo": time.monotonic()})
texts[:] = ["Run the tests again."]
bot.handle({"name": "user", "session": 7, "pcm": pcm, "first": 0, "last": 0})
held = chat(bot, "you")[-1]
ok(held["session"] == "" and bot.who_for, "unnamed with two questions open: \"Who is that for?\", not in a tab yet")
texts[:] = ["Alpha."]
bot.handle({"name": "user", "session": 7, "pcm": pcm, "first": 0, "last": 0})
mine = chat(bot, "you")
ok([e["session"] for e in mine[-2:]] == ["alpha", "alpha"] and arrivals(qs) == [("alpha", "Run the tests again.")],
   "after the name both messages are in alpha's tab")
texts[:] = ["Bravo, review the merge request", "and tell me what you think."]
bot.handle({"name": "user", "session": 7, "pcm": pcm, "first": 0, "last": 0, "cut": True})
bot.handle({"name": "user", "session": 7, "pcm": pcm, "first": 0, "last": 0, "continues": True})
ok([e["session"] for e in chat(bot, "you")[-2:]] == ["bravo", "bravo"], "a continuation is in the same tab as the first part")

# 5. the conversation after a restart, from the CSV log
bot, qs = new_bot()
rows = [["2026-10-20T10:00:00+02:00", "to_claude", "user", "session=bravo", "Run the tests."],
        ["2026-10-20T10:00:05+02:00", "to_claude", "radio", "session=bravo first=1", "Ticket SHOP-1."],
        ["2026-10-20T10:01:00+02:00", "claude_reply", "bravo", "kind=done spoken=1", "All green."],
        ["2026-10-20T10:02:00+02:00", "to_claude", "board", "session=alpha", "Push it."],
        ["2026-10-20T10:03:00+02:00", "transcript", "user", "dur_ms=900", "noise"]]
for row in rows:
    bot.restore_chat(row)
got = [(e["who"], e["session"], e["kind"], e["via"], e["text"]) for e in bot.chat.snapshot()]
ok(got == [("you", "bravo", "", "voice", "Run the tests."), ("radio", "bravo", "task", "", "Ticket SHOP-1."),
           ("session", "bravo", "done", "", "All green."), ("you", "alpha", "", "board", "Push it.")],
   f"the conversation is rebuilt from the log ({got})")

# 6. the HTTP side: who may act
class FakeRadio:
    def __init__(self):
        self.actions = []
        self.write = True
    def board_state(self):
        return {"chat": 3, "questions": [{"id": 1, "text": "Closing bravo. Say yes to confirm.", "session": "bravo"}],
                "who_for": [], "silent": False, "write": self.write}
    def board_chat(self):
        return [{"id": 1, "ts": time.time(), "who": "you", "session": "bravo", "kind": "", "via": "board", "text": "hi"}]
    def board_action(self, action, body, client):
        if not self.write:
            raise PermissionError("the board is read only (BOARD_WRITE=0)")
        self.actions.append((action, body))


store = board.Board(tempfile.mkdtemp())
radio = FakeRadio()
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
board.start(store, Log(), port, lambda: [], radio)
time.sleep(0.3)
cookie = f"{board.COOKIE}={store.make_cookie()}"
good = {"Content-Type": "application/json", "X-Board-Action": "1", "Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"}


def call(method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    return r.status, json.loads(data) if data.startswith(b"{") else data


ok(call("POST", "/api/send", {k: v for k, v in good.items() if k != "Cookie"}, {"text": "hi"})[0] == 401, "no cookie: 401")
ok(call("POST", "/api/send", {k: v for k, v in good.items() if k != "X-Board-Action"}, {"text": "hi"})[0] == 403,
   "no X-Board-Action header (a form from another site): 403")
ok(call("POST", "/api/send", dict(good, Origin="http://evil.example"), {"text": "hi"})[0] == 403, "another origin: 403")
ok(call("POST", "/api/send", dict(good, **{"Content-Type": "text/plain"}), {"text": "hi"})[0] == 403, "not JSON: 403")
status, _ = call("POST", "/api/send", good, {"text": "Bravo, run the tests", "session": ""})
ok(status == 200 and radio.actions == [("send", {"text": "Bravo, run the tests", "session": ""})], "from the page: accepted")
ok(call("POST", "/api/answer", good, {"id": 1, "verdict": "yes"})[0] == 200 and radio.actions[-1][0] == "answer", "answer accepted")
ok(call("POST", "/api/silent", good, {"on": True})[0] == 200 and radio.actions[-1] == ("silent", {"on": True}), "silent accepted")
radio.write = False
ok(call("POST", "/api/send", good, {"text": "hi"})[0] == 403, "BOARD_WRITE=0: read only")
status, latest = call("GET", "/api/latest", {"Cookie": cookie})
ok(status == 200 and latest["chat"] == 3 and latest["questions"][0]["id"] == 1 and "latest" in latest, "/api/latest carries the radio's state")
status, data = call("GET", "/api/chat", {"Cookie": cookie})
ok(status == 200 and data["events"][0]["text"] == "hi" and data["events"][0]["when"], "/api/chat lists the conversation")
ok(call("GET", "/api/chat")[0] == 401, "the conversation needs the cookie too")

# 7. the bot's own guard on actions
bot, qs = new_bot()
bot.board_action("send", {"text": "hello", "session": "bravo"}, "100.64.0.2")
_, _, kind, payload = bot.work.get_nowait()
ok(kind == "typed" and payload == {"text": "hello", "session": "bravo", "client": "100.64.0.2"}, "send goes to the worker's queue")
try:
    bot.board_action("send", {"text": "x", "session": "../etc"}, "")
    ok(False, "a bad session name must be refused")
except ValueError:
    ok(True, "a bad session name is refused")
rb.BOARD_WRITE = False
try:
    bot.board_action("send", {"text": "x"}, "")
    ok(False, "BOARD_WRITE=0 must refuse")
except PermissionError:
    ok(True, "BOARD_WRITE=0: the bot refuses too")
rb.BOARD_WRITE = True

# 8. "all stop" typed in a session's tab still stops everyone; items and conversation share one clock
bot, qs = new_bot()
bot.on_typed("all stop", "bravo")
e = bot.launcher_q.get_nowait()
ok(e.get("type") == "interrupt" and sorted(e.get("names")) == ["alpha", "bravo", "charlie"], f"\"all stop\" in a tab stops everyone ({e})")
item = board.public({"id": "x", "ts": "2026-10-20T10:00:00", "title": "t"})
chat_event = board.public_chat({"id": 1, "ts": item["ts"] + 60, "who": "you", "text": "hi"})
ok(isinstance(item["ts"], float) and chat_event["ts"] - item["ts"] == 60 and chat_event["when"], "items and messages sort on the same clock")
