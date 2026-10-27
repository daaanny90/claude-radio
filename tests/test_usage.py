# Claude's plan limits: read from `claude -p /usage`, shown on the board, said aloud past each threshold
import http.client
import json
import queue
import socket
import tempfile
import threading
import time
import types
from http.server import ThreadingHTTPServer

import radio_bot as rb

rb.VOICE_FX = ""
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
    spoken = []
    bot.speak = lambda text, *a, **k: spoken.append(text)
    return bot, spoken


def usage(session, week, fable=4):
    # the output of `claude -p /usage` on 2.1.310, trimmed
    return f"""You are currently using your subscription to power your Claude Code usage

Current session: {session}% used · resets Oct 27 at 11am (Europe/Warsaw)
Current week (all models): {week}% used · resets Nov 1 at 5pm (Europe/Warsaw)
Current week (Fable): {fable}% used · resets Nov 1 at 4:59pm (Europe/Warsaw)

What's contributing to your limits usage?
Last 24h · 1200 requests · 9 sessions
  81%  Start fresh when you switch tasks · run /clear
"""


# 1. reading the CLI's text
oct27 = time.mktime((2026, 10, 27, 9, 0, 0, 0, 0, -1))
limits = rb.parse_usage(usage(40, 21), now=oct27)
ok([l["key"] for l in limits] == ["session", "week", "week:Fable"], f"three limits ({limits})")
ok(limits[0] == {"key": "session", "label": "session", "pct": 40, "resets": "11am", "said": "11am"},
   "the session resets today: only the hour")
ok(limits[1]["resets"] == "Nov 1, 5pm" and limits[1]["label"] == "week", "the week: day and hour, no time zone")
ok(limits[1]["said"] == "November 1 at 5pm", "said aloud with the whole month (Piper reads \"Nov 1\" as \"9th 1\")")
ok(limits[2]["label"] == "Fable" and limits[2]["pct"] == 4, "a model's own week")
ok(rb.parse_usage("Not logged in · Please run /login") == [], "no limits in an error")
ok(rb.short_reset("Nov 3 at 9am (Europe/Rome)", now=time.mktime((2026, 11, 3, 8, 0, 0, 0, 0, -1))) == "9am",
   "a single-digit day is today too")

# 2. the board sees them, and the warnings come once per threshold
bot, spoken = new_bot()
ok(bot.board_state()["usage"] is None, "before the first reading the board shows nothing")
ok(bot.on_usage(usage(40, 21)) and spoken == [], "40%: nothing said")
state = bot.board_state()["usage"]
ok([l["pct"] for l in state["limits"]] == [40, 21, 4] and state["stale"] is False, "the board gets the limits")
bot.on_usage(usage(81, 21))
ok(len(spoken) == 1 and "session is at 81 percent" in spoken[0] and "resets" in spoken[0], f"81%: said once ({spoken})")
bot.on_usage(usage(84, 22))
ok(len(spoken) == 1, "still past 80: not said again")
bot.on_usage(usage(96, 22))
ok(len(spoken) == 2 and "96 percent" in spoken[1], "past 90 and 95 at once: one sentence")
bot.on_usage(usage(97, 85, fable=99))
ok(len(spoken) == 3 and "week is at 85 percent, resets November 1 at 5pm" in spoken[2],
   "the week has its own thresholds, a model's week is only shown")
ok(any(r[0] == "usage_warning" for r in bot.log.rows), "the warnings are in the log")
bot2, spoken2 = new_bot()
bot2.on_usage(usage(97, 85))
ok(spoken2 == [], "after a bot restart what was already said is not said again (board/.settings.json)")
bot.on_usage(usage(2, 85))
bot.on_usage(usage(82, 85))
ok(len(spoken) == 4 and "82 percent" in spoken[3], "a new session window: the thresholds start over")
bot.usage["at"] -= 3600
ok(bot.board_state()["usage"]["stale"] is True, "an hour without readings: the board greys it out")
ok(bot.on_usage("garbage") is False and bot.board_state()["usage"]["stale"] is True, "a bad reading keeps the last good one")

# 3. the launcher posts it to the API
bot, spoken = new_bot()
rb.ApiHandler.bot = bot
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
server = ThreadingHTTPServer(("127.0.0.1", port), rb.ApiHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def post(path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=json.dumps(body), headers={"content-type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


ok(post("/usage", {"text": usage(55, 30), "every": 300})[0] == 200 and bot.usage["limits"][0]["pct"] == 55, "POST /usage")
ok(post("/usage", {"text": "Not logged in"})[0] == 422, "a text without limits is refused")
server.shutdown()
