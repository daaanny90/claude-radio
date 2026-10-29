# Saver mode, for when Claude's limits run short: the sessions keep their model, every message tells them to answer
# only at the end and keep their context small, until the limit that was fullest when it began resets
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


def new_bot():
    bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
    bot.launcher_q = queue.Queue()
    spoken = []
    bot.speak = lambda text, *a, **k: spoken.append(text)
    return bot, spoken


def queued(bot):
    out = []
    while not bot.work.empty():
        _priority, _seq, kind, payload = bot.work.get_nowait()
        out.append((kind, payload))
    return out


def limits(session, week, now=None):
    now = now or time.time()
    return {"limits": [{"key": "session", "label": "session", "pct": session, "reset_at": now + 2 * 3600},
                       {"key": "week", "label": "week", "pct": week, "reset_at": now + 3 * 86400}],
            "at": now, "every": 300, "tips": []}


def usage_text(session, week):
    # the output of `claude -p /usage`, trimmed: resets two days ahead, so that no window has gone by
    day = time.strftime("%b %d", time.localtime(time.time() + 2 * 86400)).replace(" 0", " ")
    return (f"Current session: {session}% used · resets {day} at 11am (Europe/Warsaw)\n"
            f"Current week (all models): {week}% used · resets {day} at 5pm (Europe/Warsaw)\n")


# 1. by voice, the whole utterance; with a name in front it is a message to that session
bot, spoken = new_bot()
q = bot.sessions.connect("bravo")
for phrase, on in (("Saver.", True), ("Saver off.", False), ("saver on", True), ("Risparmio spento.", False),
                   ("Save tokens.", True), ("Saver, stop.", False), ("Safer.", True), ("risparmio acceso", True),
                   ("saver mode off", False), ("Risparmio massimo.", True), ("Saver off!", False),
                   ("Modalità risparmio massimo.", True), ("modalita risparmio spento", False)):
    result = bot.on_text("user", phrase)
    ok(bot.saver == on and result == ("saver" if on else "saver-off"), f"\"{phrase}\": {'on' if on else 'off'}")
for phrase in ("Bravo, saver.", "Saver for the checkout page is broken."):
    bot.on_text("user", phrase)
    ok(not bot.saver, f"\"{phrase}\" is no command")
bot.on_typed("saver", "bravo")
ok(bot.saver, "typed in a session's tab it stays the global switch")
bot.on_text("user", "saver off")

# 2. it ends with the fullest limit: the session at 85% before the week at 40%, the week when it is the fuller
bot, spoken = new_bot()
bot.usage = limits(85, 40)
bot.on_text("user", "Saver.")
ok(bot.saver_until == bot.usage["limits"][0]["reset_at"] and bot.saver_window == "session"
   and spoken[-1].startswith("Saver on: the sessions answer only at the end, until the session resets"),
   f"until the session resets ({spoken[-1]})")
again, _ = new_bot()
ok(again.saver and again.saver_until == bot.saver_until and again.saver_window == "session", "kept across a restart")
bot.on_text("user", "Saver off.")
ok(spoken[-1] == "Saver off." and bot.saver_until is None and not new_bot()[0].saver, "off, and off after a restart")
bot.usage = limits(20, 92)
bot.on_text("user", "Saver.")
ok(bot.saver_window == "week" and bot.saver_until == bot.usage["limits"][1]["reset_at"], "the week, when it is fuller")
bot.on_text("user", "Saver off.")
bot.usage = None
bot.on_text("user", "Saver.")
ok(bot.saver_until is None and spoken[-1] == "Saver on: the sessions answer only at the end, until you turn it off.",
   "no reading of the limits: on until turned off")
bot.on_text("user", "Saver off.")

# 3. the messages to the sessions carry it: radio.ts swaps its reminder
bot, spoken = new_bot()
q = bot.sessions.connect("bravo")
bot.on_text("user", "Bravo, run the tests.")
ok(q.get_nowait() == {"type": "message", "text": "run the tests.", "user": "user", "saver": False}, "off: saver false")
bot.on_text("user", "Saver.")
bot.on_text("user", "Bravo, run the tests.")
ok(q.get_nowait()["saver"] is True, "on: saver true")
bot.first_message["bravo"] = "Read the ticket and send me the plan."
bot.deliver_first_message("bravo")
ok(q.get_nowait()["saver"] is True, "the first message of a new session too")
bot.on_text("user", "Saver off.")

# 4. at the reset it turns itself off, aloud
bot, spoken = new_bot()
bot.usage = limits(85, 40)
bot.on_text("user", "Saver.")
queued(bot)
bot.on_usage(usage_text(30, 41))
ok(queued(bot) == [], "before the reset: still on")
bot.saver_until = time.time() - 1
bot.on_usage(usage_text(2, 41))
work = queued(bot)
ok(work == [("saver", {"on": False, "source": "reset", "aloud": True})], f"after the reset: the worker turns it off ({work})")
bot.set_saver(False, "reset", aloud=True)
ok(not bot.saver and spoken[-1] == "Saver off, the session has reset.", f"said aloud ({spoken[-1]})")
bot.on_usage(usage_text(3, 41))
ok(queued(bot) == [], "off: nothing more")

# 5. the usage warnings suggest it, unless it is on
bot, spoken = new_bot()
bot.on_usage(usage_text(81, 30))
ok(spoken[-1].endswith("Say saver to spare tokens."), f"a threshold, saver off: suggested ({spoken[-1]})")
bot.on_text("user", "Saver.")
bot.on_usage(usage_text(91, 30))
ok("saver" not in spoken[-1].lower() and "91 percent" in spoken[-1], f"saver on: not suggested ({spoken[-1]})")
bot.on_text("user", "Saver off.")

# 6. the board: its switch goes through the worker, its state is in what the page polls
bot, spoken = new_bot()
bot.board_action("saver", {"on": True}, "100.64.0.2")
work = queued(bot)
ok(work == [("saver", {"on": True, "source": "board 100.64.0.2"})], f"the board's switch: queued ({work})")
bot.usage = limits(85, 40)
bot.set_saver(True, "board 100.64.0.2")
state = bot.board_state()
ok(state["saver"] is True and state["saver_until"] == rb.clock(bot.saver_until) and spoken == []
   and bot.chat.snapshot()[-1]["kind"] == "notice", "on the page: on, until when, and written, not said")
bot.set_saver(False, "board 100.64.0.2")
ok(bot.board_state()["saver"] is False and bot.board_state()["saver_until"] == "", "off on the page")
