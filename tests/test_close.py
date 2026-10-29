# Closing a session: the launcher deletes its worktree, and when that would lose work the radio asks again
import queue
import tempfile

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


bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
bot.launcher_q = queue.Queue()
spoken = []
bot.speak = lambda text, *a, **k: spoken.append(text)
qs = {name: bot.sessions.connect(name) for name in ("alpha", "bravo")}

# 1. close, yes: the order as before
bot.on_text("user", "Bravo, close.")
bot.on_text("user", "Yes.")
order = bot.launcher_q.get_nowait()
ok(order == {"type": "close", "name": "bravo"} and spoken[-2:] == ["Closing bravo. Say yes to confirm.", "Closing bravo."],
   f"close, yes: the order ({order})")

# 2. 29 October: the launcher's answer about a close used to be spoken as "Could not open"
bot.on_launcher_result({"name": "bravo", "ok": False, "action": "close", "lost": "3 uncommitted files and 2 unpushed commits"})
ok(spoken[-1] == "bravo has 3 uncommitted files and 2 unpushed commits: closing deletes them. Say yes to close anyway."
   and not any("open" in s for s in spoken), f"work that would be lost: asked again ({spoken[-1]})")
bot.on_text("user", "Yes.")
order = bot.launcher_q.get_nowait()
ok(order == {"type": "close", "name": "bravo", "force": True}, f"yes again: closed anyway ({order})")
bot.on_launcher_result({"name": "bravo", "ok": True, "closed": True, "removed": "shop--PROJ-1-bravo", "detail": ""})
ok(spoken[-1] == "bravo closed, worktree removed." and ("closed", "bravo", "removed=shop--PROJ-1-bravo", "") in bot.log.rows,
   f"said and logged ({spoken[-1]})")

bot.on_text("user", "Alpha, close.")
bot.on_text("user", "Yes.")
bot.launcher_q.get_nowait()
bot.on_launcher_result({"name": "alpha", "ok": False, "action": "close", "lost": "1 uncommitted file"})
bot.on_text("user", "No.")
ok(spoken[-1] == "Cancelled." and bot.launcher_q.empty() and not bot.questions, "no: alpha stays open, nothing deleted")

# 3. the other outcomes
bot.on_launcher_result({"name": "alpha", "ok": True, "closed": True, "removed": "", "detail": "the terminal desk is open in it"})
ok(spoken[-1] == "alpha closed. Its worktree stays: the terminal desk is open in it.", f"kept, and why ({spoken[-1]})")
bot.on_launcher_result({"name": "alpha", "ok": True, "closed": True, "removed": "", "detail": ""})
ok(spoken[-1] == "alpha closed.", "no worktree (the lobby, a plain folder)")
bot.on_launcher_result({"name": "alpha", "ok": False, "action": "close", "detail": "no such session"})
ok(spoken[-1] == "Could not close alpha: no such session.", f"an error is a close error ({spoken[-1]})")
