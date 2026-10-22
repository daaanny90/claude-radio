import importlib.util, queue, time, types
import radio_bot as rb
rb.VOICE_FX = ""; rb.ACK_MODE = "name"
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def __init__(self): self.rows = []
    def write(self, *a): self.rows.append(a)
texts = []
class Transcriber:
    def transcribe(self, pcm, dur): return texts.pop(0), "accurate"
log = Log()
bot = rb.RadioBot(log, transcriber=Transcriber(), speaker=None); bot.launcher_q = queue.Queue()
said = []; bot.speak = lambda t, *a, **k: said.append(t); bot.play = lambda *a: None
s = bot.sessions
qs = {n: s.connect(n) for n in ("alpha", "delta")}
def arrivals():
    out = []
    for n, q in qs.items():
        while not q.empty(): out.append((n, q.get_nowait()["text"]))
    return sorted(out)
user = {"session": 7, "name": "user"}
chunk = types.SimpleNamespace(pcm=b"\0" * rb.BYTES_PER_MS * 1000)
def talk(text_a, text_b, before=None):
    """One long transmission: cut at MAX_UTTERANCE_S, then it continues."""
    texts[:] = [text_a, text_b]
    bot.on_sound(user, chunk)
    bot.buffers[7]["first"] -= rb.MAX_UTTERANCE_S + 1      # has been talking for more than 60 s
    a = bot.finished_utterances()
    bot.on_sound(user, chunk)                               # and keeps going right away
    b = bot.buffers.pop(7)
    ok(a[0].get("cut") and b.get("continues"), "cut marked, the next piece is a continuation")
    if before: before()
    bot.handle(a[0]); bot.handle(b)

s.last_spoke = "delta"
talk("Alpha, yes, the new changes should be reviewed before merging.", "And yes, before opening the merge request, report on the board.")
ok(arrivals() == sorted([("alpha", "yes, the new changes should be reviewed before merging."), ("alpha", "And yes, before opening the merge request, report on the board.")]), "the October 20 case: everything to alpha")
ok(any(r[0] == "continuation" for r in log.rows), "in the log: continuation")
talk("Delta, yes. For the double tap your recommendation is right.", "Alpha, and you: push.")
ok(arrivals() == [("alpha", "and you: push."), ("delta", "yes. For the double tap your recommendation is right.")], "a name at the start of the continuation wins")
s.last_spoke = "delta"; s.asking.clear()
s.asking["alpha"] = time.monotonic(); s.asking["delta"] = time.monotonic()
talk("I need a research on the beta branch.", "So that QA can test it in sprint.")
ok(arrivals() == [] and bot.who_for and bot.who_for["text"] == "I need a research on the beta branch. So that QA can test it in sprint.", f"who-for: the continuation is appended to the held sentence ({bot.who_for})")
bot.handle({"name": "user", "session": 7, "pcm": bytearray(chunk.pcm * 2), "first": 0, "last": 0}) if texts.append("Alpha.") is None else None
ok(arrivals() == [("alpha", "I need a research on the beta branch. So that QA can test it in sprint.")], "once the name is said, the whole thing goes")
s.asking.clear()
talk("Open the ui library for ticket 1400 and fix the modal", "and also the tooltip on mobile.")
q = bot.questions[-1]
ok(q["kind"] == "open" and q["request"].endswith("fix the modal and also the tooltip on mobile."), f"open: the continuation goes into the request ({q['request']})")
# a normal sentence after a pause is not a continuation
bot.questions.clear(); s.last_spoke = "delta"
texts[:] = ["Push it."]
bot.on_sound(user, chunk); b = bot.buffers.pop(7)
ok(not b.get("continues"), "no cut before: new sentence")
bot.handle(b)
ok(arrivals() == [("delta", "Push it.")], "and it goes to the last one heard")
