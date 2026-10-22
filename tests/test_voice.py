import importlib.util, queue, threading, time
import radio_bot as rb
rb.VOICE_FX = ""; rb.AGENT_SIGNATURES = False; rb.AGENT_VOICES = {"echo": "x"}
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def write(self, *a): pass
used = []
class Voice:
    def __init__(self, name): self.name = name
    def chunks(self, text):
        used.append(self.name); return iter(())
bot = rb.RadioBot(Log(), transcriber=object(), speaker=Voice("radio"))
bot.voices_loading.add("echo")
def arrive():
    time.sleep(0.6); bot.voices["echo"] = Voice("echo"); bot.voices_loading.discard("echo")
threading.Thread(target=arrive).start()
t = time.monotonic(); bot.speak_now(("echo: done.", "echo", None), "reply", 1)
ok(used == ["echo"] and 0.5 < time.monotonic() - t < 3, f"waits for the voice that is loading ({used}, {time.monotonic() - t:.1f} s)")
used.clear(); bot.voices.clear()
t = time.monotonic(); bot.speak_now(("echo: done.", "echo", None), "reply", 1)
ok(used == ["radio"] and time.monotonic() - t < 0.5, "voice not loading (missing): immediately with the radio voice")
