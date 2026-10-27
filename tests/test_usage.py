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
ok({k: limits[0][k] for k in ("key", "label", "pct", "resets", "said")}
   == {"key": "session", "label": "session", "pct": 40, "resets": "11am", "said": "11am"},
   "the session resets today: only the hour")
ok(limits[1]["resets"] == "Nov 1, 5pm" and limits[1]["label"] == "week", "the week: day and hour, no time zone")
ok(limits[1]["said"] == "November 1 at 5pm", "said aloud with the whole month (Piper reads \"Nov 1\" as \"9th 1\")")
ok(limits[2]["label"] == "Fable" and limits[2]["pct"] == 4, "a model's own week")
ok(rb.parse_usage("Not logged in · Please run /login") == [], "no limits in an error")
ok(rb.short_reset("Nov 3 at 9am (Europe/Rome)", now=time.mktime((2026, 11, 3, 8, 0, 0, 0, 0, -1))) == "9am",
   "a single-digit day is today too")

# 2. the board sees them, and the warnings come once per threshold (the pace has its own tests, below: these
# fixed dates would give one that depends on today's date)
rb.USAGE_PACE_WARN = False
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

# 4. the pace: at the average rate since the window opened, is there enough until the reset?
from datetime import datetime
from zoneinfo import ZoneInfo
now = time.time()
warsaw = datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp()
ok(rb.reset_epoch("Oct 27 at 11am (Europe/Warsaw)", now=warsaw - 7200) == warsaw, "the reset in the zone the CLI names")
ok(rb.reset_epoch("Nov 1 at 4:59pm (Europe/Warsaw)", now=warsaw)
   == datetime(2026, 11, 1, 16, 59, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp(), "with minutes")
dec30 = datetime(2026, 12, 30, 12, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp()
ok(rb.reset_epoch("Jan 2 at 9am (Europe/Rome)", now=dec30)
   == datetime(2027, 1, 2, 9, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp(), "January read in December is next year")
noon = datetime(2026, 10, 27, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
ok(rb.reset_epoch("11am (UTC)", now=noon) == noon + 23 * 3600, "an hour without a day is the next one")
ok(rb.reset_epoch("soon") is None, "unreadable: no reset")

session = lambda pct, hours_left: {"key": "session", "pct": pct, "reset_at": now + hours_left * 3600}
p = rb.usage_pace(session(30, 2), now)
ok(p == {"elapsed": 60, "projected": 50, "pace": "easy"}, f"3 of 5 hours gone at 30%: 50% by the reset ({p})")
ok(rb.usage_pace(session(54, 2), now)["pace"] == "tight", "90% by the reset: tight")
p = rb.usage_pace(session(72, 2), now)
ok(p["pace"] == "over" and p["projected"] == 120 and abs(p["runs_out"] - (now + 2 * 3600 - 50 * 60)) <= 150,
   "72% after 3 hours: runs out 50 minutes before the reset")
ok(rb.usage_pace(session(20, 4.5), now) == {"elapsed": 10}, "half an hour in: too early for a forecast")
ok(rb.usage_pace(session(100, 2), now) == {} and rb.usage_pace({"key": "week", "pct": 5, "reset_at": None}, now) == {},
   "nothing when already out or without a reset")
week = rb.usage_pace({"key": "week:Fable", "pct": 40, "reset_at": now + 5 * 86400}, now)
ok(week["pace"] == "over" and abs(week["runs_out"] - (now + 3 * 86400)) <= 150, "a week: 40% in 2 days runs out on day 5")

t = time.mktime((2026, 10, 27, 10, 15, 0, 0, 0, -1))
thu = time.mktime((2026, 10, 29, 15, 0, 0, 0, 0, -1))
ok((rb.clock(t, now=t), rb.clock(t, now=t, spoken=True)) == ("10:15am", "around 10:15am"), "a time today")
ok((rb.clock(thu, now=t), rb.clock(thu, now=t, spoken=True)) == ("Thu 3pm", "on Thursday around 3pm"), "another day")


def at(ts):
    tm = time.localtime(ts)
    return f"{time.strftime('%b', tm)} {tm.tm_mday} at {tm.tm_hour % 12 or 12}:{tm.tm_min:02d}{'am' if tm.tm_hour < 12 else 'pm'}"


def reading(session_pct, session_left_h, week_pct, week_left_d):
    return (f"Current session: {session_pct}% used · resets {at(time.time() + session_left_h * 3600)}\n"
            f"Current week (all models): {week_pct}% used · resets {at(time.time() + week_left_d * 86400)}\n")


rb.USAGE_PACE_WARN = True
rb.BOARD_DIR = tempfile.mkdtemp()
bot, spoken = new_bot()
bot.on_usage(reading(30, 2, 10, 5))
ok(spoken == [], "on an easy pace nothing is said")
bot.on_usage(reading(30, 2, 29, 5))
ok(spoken == [] and bot.board_state()["usage"]["limits"][1]["pace"] == "over",
   "a week a little over pace (102%) shows on the board but is not said: it would end 3 hours early out of 7 days")
limits = bot.board_state()["usage"]["limits"]
ok(limits[0]["elapsed"] == 60 and limits[0]["pace"] == "easy" and "runs_out_at" not in limits[0], "the board gets the pace")
bot.on_usage(reading(72, 2, 10, 5))
ok(len(spoken) == 1 and spoken[0].startswith("Claude usage: at this pace the session runs out around ")
   and "before the reset at " in spoken[0], f"too fast: said once with both times ({spoken})")
ok(bot.board_state()["usage"]["limits"][0]["runs_out_at"][0].isdigit(), "the board gets the time it runs out")
bot.on_usage(reading(75, 1.9, 10, 5))
ok(len(spoken) == 1, "the same window: not said again")
bot2, spoken2 = new_bot()
bot2.on_usage(reading(75, 1.9, 10, 5))
ok(spoken2 == [], "nor after a bot restart")
bot.on_usage(reading(75, 1.9, 40, 5))
ok(len(spoken) == 2 and "the week runs out on " in spoken[1], f"the week has its own warning ({spoken[1:]})")
bot.pace_warned["session"] -= 5 * 3600   # as if that warning had been about the previous window
bot.on_usage(reading(75, 1.9, 40, 5))
ok(len(spoken) == 3 and "the session runs out" in spoken[2], "a new session window warns again")
