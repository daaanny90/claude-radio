# Who does what: the board shows what you last asked each session, because names are generic and topics go stale
import queue
import tempfile
import time
import types

import radio_bot as rb

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
    for n in ("alpha", "bravo", "charlie"):
        bot.sessions.connect(n)
    return bot


def asked(bot, name):
    return next(s for s in bot.board_sessions() if s["name"] == name)["asked"]


# 1. what reaches a session, spoken or typed, without its name
bot = new_bot()
ok(asked(bot, "bravo") == "", "nothing asked yet")
bot.on_typed("fix the footer padding on small screens", "bravo")
ok(asked(bot, "bravo") == "fix the footer padding on small screens", "typed in bravo's tab")
bot.on_text("user", "Charlie, look at why the swipe jumps on iOS in the media viewer.")
ok(asked(bot, "charlie") == "look at why the swipe jumps on iOS in the media viewer.", "spoken, without \"Charlie,\"")
ok(asked(bot, "bravo") == "fix the footer padding on small screens", "each session keeps its own")
ts = next(s for s in bot.board_sessions() if s["name"] == "bravo")["asked_ts"]
ok(abs(ts - time.time()) < 5, "with the time it was asked")

# 2. an acknowledgement is not a new job
for ack in ("yes", "Yes, push it.", "push it", "ok vai", "thanks", "go on", "sì", "do it"):
    bot.on_typed(ack, "bravo")
ok(asked(bot, "bravo") == "fix the footer padding on small screens", "\"yes\", \"push it\", \"thanks\" keep the request")
bot.on_typed("now run the tests", "bravo")
ok(asked(bot, "bravo") == "now run the tests", "a short new request replaces it")

# 3. a long pasted markdown becomes one line
md = "## Spec\n\n```js\nconst x = 1\n```\n- the **footer** keeps its padding\n- the card has a shadow\n" + "- more detail\n" * 40
bot.on_typed(md, "alpha")
line = asked(bot, "alpha")
ok(line.startswith("Spec the footer keeps its padding the card has a shadow") and len(line) <= rb.ASKED_MAX
   and line.endswith("…") and "\n" not in line and "**" not in line and "const" not in line,
   f"markdown flattened, code dropped, cut ({line[:70]}...)")

# 4. the continuation of a cut utterance joins the request
bot.note_asked("charlie", "and then open the merge request", continues=True)
ok(asked(bot, "charlie") == "look at why the swipe jumps on iOS in the media viewer. and then open the merge request",
   "a continuation joins the first part")

# 5. a new session with an old name starts clean; a removed one is gone
bot.asked["delta"] = {"text": "old job", "ts": time.time()}
bot.launch({"name": "delta", "project": {"name": "shop-ui", "dir": "/x"}, "request": "", "user": "user",
            "branch": "radio-delta", "base_branch": "main"})
ok("delta" not in bot.asked, "opening delta again forgets the old delta's job")
q = bot.sessions.connect("echo")
bot.note_asked("echo", "review the login form validation")
bot.sessions.disconnect("echo", q)
ok(bot.forget_session("echo") == "forgot" and "echo" not in bot.asked, "removed from the board: its request goes too")

# 6. after a restart, from the CSV log
bot2 = new_bot()
rows = [
    ["2026-10-27T09:00:00", "to_claude", "user", "session=bravo", "fix the header"],
    ["2026-10-27T09:05:00", "to_claude", "user", "session=bravo", "yes"],
    ["2026-10-27T09:06:00", "to_claude", "radio", "session=charlie first=1", "Ticket SHOP-4242. Fix the login form."],
    ["2026-10-27T09:07:00", "to_claude", "user", "session=alpha", "write the release notes for this week"],
    ["2026-10-27T09:07:30", "to_claude", "user", "session=alpha continues=1", "and open the merge request"],
    ["2026-10-27T09:08:00", "to_claude", "user", "session=bravo", "old request of an old bravo"],
    ["2026-10-27T09:09:00", "open", "user", "project=shop-ui name=bravo branch=radio-bravo", ""],
]
for row in rows:
    bot2.restore_chat(row)
ok(asked(bot2, "charlie") == "Ticket SHOP-4242. Fix the login form.", "the radio's brief counts as the request")
ok(asked(bot2, "alpha") == "write the release notes for this week and open the merge request", "continuation restored")
ok(asked(bot2, "bravo") == "", "a later \"open\" of bravo clears the old bravo's job")
ok(bot2.asked["alpha"]["ts"] == time.mktime((2026, 10, 27, 9, 7, 0, 0, 0, -1)), "with the time of the first part")

# 7. a name used again: the tab starts at the new session's opening (27 October: charlie closed at 9:17 and a new
# charlie opened at 9:30 on another ticket, bravo the same at 11:13 and 11:15; both tabs mixed two sessions)
since = lambda bot, name: next(s for s in bot.board_sessions() if s["name"] == name)["since"]
bot = new_bot()
ok(since(bot, "bravo") == 0, "a session with no opening on record: the whole tab")
before = time.time()
bot.launch({"name": "bravo", "project": {"name": "shop-ui", "dir": "/x"}, "request": "", "user": "user",
            "branch": "radio-bravo", "base_branch": "main"})
ok(since(bot, "bravo") >= before, "opened again: its tab starts now")
bot3 = new_bot()
for row in [["2026-10-27T09:17:49", "closed", "charlie", "", ""],
            ["2026-10-27T09:30:12", "open", "operator", "project=portal name=charlie branch=SHOP-4242-charlie", ""]]:
    bot3.restore_chat(row)
ok(since(bot3, "charlie") == time.mktime((2026, 10, 27, 9, 30, 12, 0, 0, -1)), "after a restart, from the log's \"open\" row")
