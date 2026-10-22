import importlib.util, queue, time
import radio_bot as rb
rb.VOICE_FX = ""; rb.SOUND_OPEN = rb.SOUND_CLOSE = rb.SOUND_NEW = b""; rb.HOST_HOME = "/home/user"
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def write(self, *a): pass
bot = rb.RadioBot(Log(), transcriber=object(), speaker=None); bot.launcher_q = queue.Queue()
said = []; bot.speak = lambda t, *a, **k: said.append(t); bot.sound = lambda *a: None
s = bot.sessions; q = s.connect("delta"); s.project["delta"] = "lobby"; s.topic["delta"] = "finding the project"
bot.on_move("delta", "~/work/portal", "The user: review the comments on My Shop 1794 and the Chromatic failure, report before touching code.")
time.sleep(2.3)
e = bot.launcher_q.get_nowait()
ok(e["type"] == "move" and e.get("project") and e["dir"] == "/home/user/work/portal" and e["branch"] == "MYSHOP-1794-delta" and e["install"] is False, f"to the launcher: worktree of the portal ({e})")
ok(bot.first_message["delta"].startswith("You are now in your own worktree of customer portal, moved here from the radio lobby. Ticket MYSHOP-1794."), bot.first_message["delta"][:120])
ok(s.project["delta"] == "portal" and s.ticket["delta"] == "MYSHOP-1794" and "delta" not in s.topic, "project and ticket set, topic cleared")
ok(said[-1] == "delta is moving to customer portal, ticket MYSHOP 1794.", said[-1])
bot.on_launcher_result({"name": "delta", "ok": True, "moved": True, "dir": "/home/user/work/portal--delta"})
ok(q.get_nowait()["text"].startswith("You are now in your own worktree") and s.fresh_one() is None, "reopened: gets the brief, is not 'fresh'")
# plain folder: the usual move
bot.on_move("delta", "/home/user/code/todo-app", "run the tests")
time.sleep(2.3)
e = bot.launcher_q.get_nowait()
ok(e == {"type": "move", "name": "delta", "dir": "/home/user/code/todo-app"}, f"folder not registered: plain move ({e})")
# worktree of a registered project as the destination: a new worktree named after the session there too
bot.on_move("delta", "/home/user/work/shop-ui--echo", "ticket 1309")
time.sleep(2.3)
e = bot.launcher_q.get_nowait()
ok(e.get("project") and e["dir"] == "/home/user/work/shop-ui" and e["branch"] == "SHOP-1309-delta", f"someone else's worktree: its own ({e.get('branch')})")
