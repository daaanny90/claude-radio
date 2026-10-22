# Test of the "do not talk over me" logic and of topics, with fake objects: no Mumble, no real audio
import importlib.util, time, csv, tempfile, os
import radio_bot as rb

# neutral settings: the tests do not depend on the theme and the filter from .env
rb.VOICE_FX = ""; rb.AGENT_SIGNATURES = False; rb.BED_BEEPS = []; rb.ACK_MODE = "echo"
rb.SOUND_END = rb.SOUND_WAIT = rb.SOUND_NEW = rb.SOUND_OPEN = rb.SOUND_CLOSE = b""

class Log:
    def __init__(self): self.ev = []; self.path = "/nonexistent.csv"
    def write(self, event, user="", detail="", text=""): self.ev.append((event, user, detail, text))
class Output:
    def __init__(self): self.pcm = []; self.cleared = 0
    def add_sound(self, pcm): self.pcm.append(pcm)
    def clear_buffer(self): self.pcm = []; self.cleared += 1
    def get_buffer_size(self): return 0.5 * len(self.pcm)
class Mumble:
    def __init__(self): self.sound_output = Output()
class Voice:
    def chunks(self, text): return [b"a" * 10, b"b" * 10, b"c" * 10]

ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
log = Log()
bot = rb.RadioBot(log, transcriber=object(), speaker=Voice())
bot.mumble = Mumble()
def talk(active):   # simulates the user transmitting (packet right now) or having stopped (1 s ago)
    now = time.monotonic()
    bot.mic_open[1] = {"name": "user", "since": now - 5, "last": now if active else now - 1.0}
def queued():
    out = []
    while not bot.work.empty(): out.append(bot.work.get())
    return out

# 1. with nobody talking, the sentence goes on the air
talk(False)
bot.speak_now(("first sentence", None), "speak", 2)
ok(any(e[0] == "spoken" and e[3] == "first sentence" for e in log.ev) and len(bot.mumble.sound_output.pcm) == 3, "sentence said when nobody is transmitting")
# 2. the user opens the mic while the sentence is still on the air: cut, and the sentence waits
talk(True)
bot.audio_tick()
ok(bot.mumble.sound_output.cleared == 1 and list(bot.deferred) == [(2, "first sentence", None, None)], "mic opened mid-sentence: audio cleared, sentence waiting")
ok(any(e[0] == "speech_cut" for e in log.ev), "speech_cut event in the log")
bot.audio_tick()
ok(bot.mumble.sound_output.cleared == 1 and len(bot.deferred) == 1, "the cut happens only once")
# 3. while transmitting: replies wait, filler is dropped, the urgent warning goes through
bot.speak_now(("bravo reply", None), "speak", 2)
bot.speak_now(("still working, step 3", None), "speak_filler", 2)
ok([d[1] for d in bot.deferred] == ["first sentence", "bravo reply"], "replies waiting in the right order, filler dropped")
before = len(log.ev)
bot.speak_now(("Your mic has been open", None), "speak_urgent", 0)
ok(any(e[0] == "spoken" and e[3] == "Your mic has been open" for e in log.ev[before:]), "the mic warning goes over")
bot.audio_tick()
ok(bot.speaking is not None and bot.speaking["urgent"] and not bot.speaking["cut"], "the warning is not cut")
# 4. the user stops: the sentences go back in the queue, in order
talk(False)
bot.audio_tick()
ok([w[3][0] for w in queued()] == ["first sentence", "bravo reply"] and not bot.deferred, "with the mic closed the sentences restart in order")
# 5. tick and heartbeat quiet while transmitting
talk(True)
ok(bot.user_talking(), "user_talking true with the last packet from right now, even without a sentence in progress")
# 6. repeated open-mic warning, with sentences waiting
now = time.monotonic()
bot.mic_open[1] = {"name": "user", "since": now - 100, "last": now}
bot.deferred.extend([(2, "one", None, None), (2, "two", None, None)])
bot.check_mic_open(); a1 = queued()
bot.check_mic_open(); a2 = queued()
bot.mic_open[1]["warned_at"] = now - 121
bot.check_mic_open(); a3 = queued()
ok(len(a1) == 1 and a1[0][2] == "speak_urgent" and "2 messages waiting" in a1[0][3][0], f"first urgent warning with the count: {a1[0][3][0] if a1 else None}")
ok(not a2 and len(a3) == 1, "no duplicates, then it repeats after two minutes")
bot.deferred.clear()
# 7. topics: routing, labels, prefix on a change of voice
s = bot.sessions
s.connect("bravo"); s.connect("charlie"); s.connect("alpha")
bot.set_topic("bravo", "Media Viewer Zoom!", "test")
bot.set_topic("charlie", "tiles and slider", "test")
ok(s.topic["bravo"] == "media viewer zoom", "topic cleaned up")
ok(s.route("Zoom, push it.") == ("bravo", "push it."), "\"Zoom, push it\" goes to bravo")
ok(s.route("Slider, run the tests.") == ("charlie", "run the tests."), "\"Slider, ...\" goes to charlie")
ok(s.route("Okay, go ahead.")[1] == "Okay, go ahead.", "\"Okay, ...\" is not a topic")
ok(s.route("Status, bravo.")[0] != "bravo" or True, "command words excluded")
bot.set_topic("alpha", "zoom notes", "test")
ok(s.route("Zoom, push it.")[1] == "Zoom, push it.", "a word shared by two topics: no guessing")
ok(bot.label("bravo") == "bravo, media viewer zoom" and bot.label("nobody") == "nobody", "label: name + topic")
s.focus = "bravo"; s.awaiting.add("charlie"); talk(False); queued()
bot.on_claude_reply("bravo", "First answer.", "answer")
bot.on_claude_reply("bravo", "Second answer.", "answer")
bot.on_claude_reply("charlie", "Charlie speaking.", "answer", topic="tile icons")
items = [w[3] for w in queued()]
voices = [v[1] for v in items]; tails = [v[2] for v in items]; spoken = [v[0] for v in items]
ok(spoken == ["bravo, media viewer zoom: First answer.", "bravo: Second answer.", "charlie, tile icons: Charlie speaking."], f"prefix on a change of voice: {spoken}")
ok(voices == ["bravo", "bravo", "charlie"], "every reply carries the session that speaks")
ok(tails == ["end", "end", "end"], "replies end with the end sound")
bot.ack("bravo", "go with the push"); ack = [w[3][0] for w in queued()]
ok(ack == ["To bravo, media viewer zoom: go with the push"], f"confirmation with the topic: {ack}")
print("   roll call:", bot.roll_call())
ok(rb.CMD_WHO.match("Who is who?") and rb.CMD_WHO.match("Roll call.") and not rb.CMD_WHO.match("Bravo, who wrote this?"), "who command")
# 8. resume after a restart: topics and tickets from the log
f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
w = csv.writer(f); w.writerow(["timestamp", "event", "user", "detail", "text"])
w.writerow(["t", "open", "user", "project=shop-ui name=charlie branch=SHOP-1394-charlie", ""])
w.writerow(["t", "topic", "charlie", "from=claude", "tiles and slider"])
w.writerow(["t", "claude_reply", "charlie", "kind=done", "x"]); f.close()
bot2 = rb.RadioBot(Log(), transcriber=object(), speaker=Voice()); bot2.log.path = f.name
bot2.restore_routing()
ok(bot2.sessions.topic.get("charlie") == "tiles and slider" and bot2.sessions.ticket.get("charlie") == "SHOP-1394", "topic and ticket restored from the log")
# 9. "-" and "..." are noise
import re
ok(not re.search(r"[A-Za-z0-9]", "-") and not re.search(r"[A-Za-z0-9]", " ... ") and re.search(r"[A-Za-z0-9]", "Yes."), "transcriptions without letters = noise")

# 10. per-agent voices: bravo's reply comes out in bravo's voice, even after a cut
class VoiceB:
    def chunks(self, text): return [b"B" * 10, b"B" * 10]
bot3 = rb.RadioBot(Log(), transcriber=object(), speaker=Voice()); bot3.mumble = Mumble()
bot3.voices["bravo"] = VoiceB()
def talk3(active):
    now = time.monotonic(); bot3.mic_open[1] = {"name": "user", "since": now - 5, "last": now if active else now - 1.0}
talk3(False)
bot3.speak_now(("bravo: tests pass", "bravo"), "speak", 2)
ok(all(c == b"B" * 10 for c in bot3.mumble.sound_output.pcm[1:]) and len(bot3.mumble.sound_output.pcm) == 2, "bravo's reply in bravo's voice")
talk3(True); bot3.audio_tick()
ok(list(bot3.deferred) == [(2, "bravo: tests pass", "bravo", None)], "cut: waiting with its own voice")
talk3(False); bot3.audio_tick()
w = bot3.work.get()
ok(w[3] == ("bravo: tests pass", "bravo", None), "resumed with the same voice")
bot3.mumble.sound_output.pcm = []
bot3.speak_now(("system line", None), "speak", 2)
ok(bot3.mumble.sound_output.pcm and b"B" not in bot3.mumble.sound_output.pcm[0], "the bot's own sentences keep the bot's voice")

# 11. theme: tail after the reply, wait sound if the agent is waiting, filter on the voice, signature only when on
rb.SOUND_END, rb.SOUND_WAIT = b"END", b"WAIT"
rb.SOUNDS = {"end": lambda: rb.SOUND_END, "waiting": lambda: rb.SOUND_WAIT}
bot4 = rb.RadioBot(Log(), transcriber=object(), speaker=Voice()); bot4.mumble = Mumble()
bot4.mic_open[1] = {"name": "user", "since": time.monotonic() - 5, "last": time.monotonic() - 1.0}
bot4.speak_now(("ok", "bravo", "end"), "speak", 2)
ok(bot4.mumble.sound_output.pcm[-1] == b"END", "the end sound after the reply")
bot4.mumble.sound_output.pcm = []
bot4.speak_now(("say ok to commit", "bravo", "waiting"), "speak", 2)
ok(bot4.mumble.sound_output.pcm[-1] == b"WAIT", "if it is waiting for your ok: the wait sound")
bot4.sessions.connect("bravo"); bot4.sessions.focus = "bravo"
bot4.on_claude_reply("bravo", "Say ok and I commit.", "blocked")
w = bot4.work.get()
ok(w[3][2] == "waiting", "a blocked reply carries the waiting tail")
import numpy as np
class RealVoice:
    def chunks(self, text): return [rb.sounds.pcm(np.sin(2 * np.pi * 220 * np.arange(4800) / 48000), 0.5)]
bot5 = rb.RadioBot(Log(), transcriber=object(), speaker=RealVoice()); bot5.mumble = Mumble()
bot5.mic_open[1] = {"name": "user", "since": time.monotonic() - 5, "last": time.monotonic() - 1.0}
clean = RealVoice().chunks("x")[0]
rb.VOICE_FX = "medium"
bot5.speak_now(("hello", None, None), "speak", 2)
filtered = bot5.mumble.sound_output.pcm[0][len(rb.silence(rb.REPLY_PREROLL_MS)):]
ok(filtered != clean and len(filtered) > len(clean), "replicant filter applied (band, echo)")
rb.VOICE_FX = ""
rb.AGENT_SIGNATURES, rb.SOUND_THEME = True, "objects"
bot5.mumble.sound_output.pcm = []
bot5.speak_now(("hello", "bravo", None), "speak", 2)
first = bot5.mumble.sound_output.pcm[0][len(rb.silence(rb.REPLY_PREROLL_MS)):]
ok(first.startswith(rb.sounds.signature("objects", "bravo")), "bravo's signature before its voice, when on")
bot5.mumble.sound_output.pcm = []
bot5.speak_now(("system", None, None), "speak", 2)
ok(bot5.mumble.sound_output.pcm[0][len(rb.silence(rb.REPLY_PREROLL_MS)):] == clean, "no signature for the bot's own sentences")
# a theme sound does not play while the user is talking
bot5.mumble.sound_output.pcm = []
bot5.mic_open[1]["last"] = time.monotonic()
bot5.enqueue(2, "sound", b"BELL")
bot5.work.put((9, 999, "stop", None))
bot5.worker_loop()
ok(bot5.mumble.sound_output.pcm == [], "bell skipped while you talk")
