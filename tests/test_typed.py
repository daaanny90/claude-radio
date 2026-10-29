# Typed on the board in any language: the session replies in that language, the board shows it and the radio, whose
# voices are English, only says where to read it. Spoken messages keep their spoken English replies
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
q = bot.sessions.connect("bravo")

# 1. the message says it was typed: radio.ts asks for a reply in its language
bot.on_typed("controlla il padding del footer su mobile", "bravo")
event = q.get_nowait()
ok(event["text"] == "controlla il padding del footer su mobile" and event["typed"] is True, f"typed in its tab ({event})")
bot.on_typed("Bravo, guarda anche l'header")
ok(q.get_nowait()["typed"] is True, "typed in the All tab with the name")

# 2. its replies: the text on the board, aloud only where to read it; progress stays written
del spoken[:]
bot.on_claude_reply("bravo", "Sto controllando il footer.", "progress")
ok(spoken == [], "progress: written only")
bot.on_claude_reply("bravo", "Il padding è 8 pixel su mobile e 16 su desktop. Lo porto a 12?", "answer")
ok(spoken == ["bravo replied, see the board."], f"an answer: where to read it ({spoken})")
ok(bot.chat.snapshot()[-1]["text"] == "Il padding è 8 pixel su mobile e 16 su desktop. Lo porto a 12?",
   "the whole reply on the board, in Italian")
bot.on_claude_reply("bravo", "Fatto, test verdi.", "done")
bot.on_claude_reply("bravo", "Mi serve il link di Figma.", "blocked")
ok(spoken[-2:] == ["bravo is done, see the board.", "bravo is blocked, see the board."], f"done and blocked ({spoken[-2:]})")

# 3. a spoken message to the same session: typed no more, the reply is read aloud as before
bot.on_text("user", "Bravo, run the tests.")
ok(q.get_nowait()["typed"] is False, "spoken: not typed")
bot.on_claude_reply("bravo", "The tests pass.", "done")
ok(spoken[-1] == "The tests pass.", f"read aloud ({spoken[-1]})")
