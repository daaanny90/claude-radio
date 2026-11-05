# A session restarted by restart-session.sh: its messages wait for the new Claude, a failed restart says why
import queue
import tempfile
import time

import radio_bot as rb

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


bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
bot.launcher_q = queue.Queue()
spoken = []
bot.speak = lambda text, *a, **k: spoken.append(text)
q = bot.sessions.connect("charlie")
bot.sessions.connect("alpha")


def drained(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# 1. 5 November: typed while charlie was closing for its restart, the message reached the old Claude and was lost
bot.work.put((0, 1, "restart", {"name": "charlie", "phase": "start", "detail": ""}))
bot.work.put((9, 2, "stop", None))
bot.worker_loop()
ok("charlie" in bot.restarting, "restart-session.sh says start: through the worker")
r = bot.on_typed("anche website forse ne ha bisogno?", "charlie")
ok(r == "held" and drained(q) == [] and spoken[-1] == "charlie is restarting: it gets this when it is back.",
   "typed in its tab while it closes: held, and said")
bot.on_session_gone("charlie", q)
r = bot.on_text("user", "Charlie, run the tests.")
ok(r == "held" and not any("not on the air" in s for s in spoken), "said to it while it is down: held, not \"off the air\"")
q2 = bot.sessions.connect("charlie")
bot.on_session_connected("charlie")
got = drained(q2)
ok([(m["text"], m["typed"]) for m in got] == [("anche website forse ne ha bisogno?", True), ("run the tests.", False)]
   and spoken[-1] == "charlie is back with your messages." and "charlie" not in bot.restarting,
   f"the new Claude gets them in order, typed as typed ({[m['text'] for m in got]})")
bot.on_session_connected("charlie")
ok(drained(q2) == [], "a later reconnection sends nothing again")

# 2. a restart that fails: the end of the terminal on the board, and what did not reach it
bot.on_restart("charlie", "start")
bot.on_typed("and the website?", "charlie")
bot.on_restart("charlie", "failed", "You have 1 unsent feedback draft\nEnter to review & send · Esc to discard and exit")
note = bot.chat.snapshot()[-1]
ok(note["session"] == "charlie" and "unsent feedback draft" in note["text"] and "- and the website?" in note["text"]
   and spoken[-1] == "charlie did not restart, see the board.", "failed: the question and the unsent message in its tab")
ok(bot.on_typed("and now?", "charlie") == "message" and drained(q2)[-1]["text"] == "and now?", "then messages go again")

# 3. a script that never says how it ended: after RESTART_MAX_S the messages are not held any longer
bot.on_restart("charlie", "start")
bot.restarting["charlie"]["at"] -= rb.RESTART_MAX_S + 1
ok(bot.on_typed("still there?", "charlie") == "message" and any("no news from restart-session.sh" in e["text"] for e in bot.chat.snapshot()[-2:]),
   "no news: said on the board, and the message goes")
ok(drained(q2)[-1]["text"] == "still there?" and drained(bot.sessions.connect("alpha")) == [], "nothing for the others")
