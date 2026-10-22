import importlib.util, queue
import radio_bot as rb
rb.VOICE_FX = ""; rb.ACK_MODE = "name"
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def write(self, *a): pass
bot = rb.RadioBot(Log(), transcriber=object(), speaker=None)
said = []; bot.speak = lambda t, *a, **k: said.append(t)
bot.sessions.connect("charlie")
bot.on_text("user", "Run the tests and tell me.")
ok(said == [], f"one session: no voice ({said})")
bot.sessions.connect("echo"); bot.sessions.topic["charlie"] = "sprint pipeline watch"
bot.on_text("user", "Charlie, run the tests and tell me what fails.")
ok(said == ["To charlie."], f"more sessions: only the name ({said})")
