# The operator always on, its tab on the board, and closed sessions removed from the board
import json
import queue
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


def new_bot():
    bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
    bot.launcher_q = queue.Queue()
    bot.mumble = types.SimpleNamespace(sound_output=types.SimpleNamespace(add_sound=lambda pcm: None,
                                                                          clear_buffer=lambda: None,
                                                                          get_buffer_size=lambda: 0.0))
    spoken = []
    bot.speak = lambda text, *a, **k: spoken.append(text)
    return bot, spoken


def launched(bot):
    out = []
    while not bot.launcher_q.empty():
        out.append(bot.launcher_q.get_nowait())
    return out


# 1. keepalive: the operator is started on its own, after the grace period, silently, with a growing backoff
bot, spoken = new_bot()
bot.operator_keepalive()
ok(launched(bot) == [], "right after a bot restart it waits for the sessions to reconnect")
bot.started_at -= rb.OPERATOR_GRACE_S + 1
bot.operator_keepalive()
ok(launched(bot) == [{"type": "open", "name": "operator", "operator": True}] and bot.opening["operator"]["quiet"],
   "after the grace period the operator is started")
ok(spoken == [], "without a word on the air")
bot.operator_keepalive()
ok(launched(bot) == [], "not twice while it is starting")
bot.on_launcher_result({"name": "operator", "ok": False, "detail": "Claude Code did not start"})
ok(spoken == [] and "could not start" in bot.chat.snapshot()[-1]["text"], "a failed quiet start: one line on the board, nothing on the air")
bot.operator_keepalive()
ok(launched(bot) == [], "after a failure it waits before trying again")
bot.operator_next_try = 0
bot.operator_keepalive()
ok(len(launched(bot)) == 1 and bot.operator_failures == 2, "then it tries again")
q = bot.sessions.connect("operator")
bot.on_launcher_result({"name": "operator", "ok": True, "dir": "/x/operator"})
bot.on_session_connected("operator")
ok(spoken == [] and bot.operator_failures == 0 and "operator" not in bot.sessions.fresh,
   "connected: no \"is ready\", no fresh session, the backoff starts over")
rb.OPERATOR_ALWAYS = False
bot.sessions.disconnect("operator", q)
bot.operator_next_try = 0
bot.operator_keepalive()
ok(launched(bot) == [], "OPERATOR_ALWAYS=0: it is not started on its own")
rb.OPERATOR_ALWAYS = True

# 2. typing in the operator's tab
bot, spoken = new_bot()
r = bot.on_typed("I need a new session for ticket MYSHOP-4242", "operator")
ok(r == "operator-call" and launched(bot) == [{"type": "open", "name": "operator", "operator": True}]
   and bot.first_message["operator"] == "I need a new session for ticket MYSHOP-4242",
   "the operator is down: typing in its tab calls it and the text waits for it")
you = [e for e in bot.chat.snapshot() if e["who"] == "you"][-1]
ok(you["session"] == "operator" and you["via"] == "board", "the message is in the operator's tab")
r = bot.on_typed("and close charlie too", "operator")
ok(r == "operator-opening" and bot.first_message["operator"].endswith("\n\nand close charlie too"),
   "while it starts, more requests queue up instead of being lost")
q = bot.sessions.connect("operator")
bot.opening.pop("operator")
bot.deliver_first_message("operator")
first = q.get_nowait()["text"]
ok("MYSHOP-4242" in first and "close charlie" in first, "both reach it when it connects")
r = bot.on_typed("close everyone except bravo", "operator")
ok(r == "message" and q.get_nowait()["text"] == "close everyone except bravo", "up: the text goes to the operator")
r = bot.on_text("user", "Operator, stop.")
e = launched(bot)
ok(any(x.get("type") == "interrupt" and x.get("names") == ["operator"] for x in e), f"\"Operator, stop.\" stops it ({e})")
bot.last_stop = None
ok(bot.on_text("user", "Operator, status.") == "status", "\"Operator, status.\" is its status")
ok(bot.on_text("user", "Operator, status of alpha?") == "message", "\"Operator, status of alpha?\" is a request to it")
state = bot.board_state()
ok(state["operator"] == "on", "the board sees the operator on")

# 3. a closed session removed from the board
bot, spoken = new_bot()
bot.board = board.Board(tempfile.mkdtemp())
bot.board.add("charlie", "Charlie's diff", text="+ a")
bot.board.add("bravo", "Bravo's notes", text="b")
bot.chat.add("session", "done", session="charlie", kind="done")
bot.chat.add("you", "charlie, push", session="charlie")
bot.chat.add("session", "bravo here", session="bravo")
qc = bot.sessions.connect("charlie")
try:
    bot.board_action("forget", {"session": "charlie"}, "")
    ok(False, "an open session must not be removed")
except ValueError:
    ok(True, "an open session cannot be removed: close it first")
bot.sessions.disconnect("charlie", qc)
bot.board_action("forget", {"session": "charlie"}, "100.64.0.2")
_, _, kind, payload = bot.work.get_nowait()
ok(kind == "forget" and payload == {"session": "charlie", "client": "100.64.0.2"}, "the removal goes to the worker")
ok(bot.forget_session(**payload) == "forgot", "removed")
ok([m["session"] for m in bot.board.items()] == ["bravo"], "charlie's items are deleted, bravo's stay")
ok([e["session"] for e in bot.chat.snapshot()] == ["bravo"], "charlie's conversation is gone, bravo's stays")
saved = json.load(open(f"{rb.BOARD_DIR}/.settings.json"))["forgotten"]
ok("charlie" in saved, "the removal survives a restart (board/.settings.json)")
bot2, _ = new_bot()
old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(saved["charlie"] - 60))
new = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(saved["charlie"] + 60))
bot2.restore_chat([old, "claude_reply", "charlie", "kind=done", "old reply"])
bot2.restore_chat([new, "claude_reply", "charlie", "kind=done", "a new charlie"])
bot2.restore_chat([old, "to_claude", "user", "session=charlie", "old message"])
ok([e["text"] for e in bot2.chat.snapshot()] == ["a new charlie"], "after a restart the old conversation stays removed, a new charlie shows")
for bad in ("operator", "../x"):
    try:
        bot.board_action("forget", {"session": bad}, "")
        ok(False, f"{bad} must be refused")
    except ValueError:
        ok(True, f"forget refuses {bad!r}")
