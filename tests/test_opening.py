import importlib.util, queue, time
import radio_bot as rb
rb.VOICE_FX = ""; rb.ACK_MODE = "name"; rb.SOUND_OPEN = rb.SOUND_CLOSE = rb.SOUND_NEW = b""
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def write(self, *a): pass
bot = rb.RadioBot(Log(), transcriber=object(), speaker=None); bot.launcher_q = queue.Queue()
said = []; bot.speak = lambda t, *a, **k: said.append(t); bot.sound = lambda *a: None
s = bot.sessions
qs = {n: s.connect(n) for n in ("alpha", "charlie", "echo", "operator")}
P = rb.load_projects()
portal = next(p for p in P if p["name"] == "portal")
def drain():
    for q in qs.values():
        while not q.empty(): q.get_nowait()
    while not bot.launcher_q.empty(): bot.launcher_q.get_nowait()
def arrivals():
    return [(n, q.get_nowait()["text"]) for n, q in qs.items() if not q.empty()]
def open_bravo():
    """'Open customer portal' + yes: bravo opens without a task."""
    s.fresh.clear(); bot.questions.clear()
    bot.opening["bravo"] = {"kind": "open", "name": "bravo", "project": portal, "ticket": None, "branch": "radio-bravo", "request": "", "user": "user"}
    s.project["bravo"] = "portal"
    qs["bravo"] = s.connect("bravo")
    bot.on_launcher_result({"name": "bravo", "ok": True, "dir": "/x/portal--bravo"})
def questions(*who):
    s.asking.clear()
    for n in who: s.asking[n] = time.monotonic()

ok(rb.parse_ticket("the ticket, My Shop 1794. The pipeline fails", "SHOP") == ("MYSHOP-1794", "The pipeline fails"), f"My Shop 1794 -> MYSHOP {rb.parse_ticket('the ticket, My Shop 1794. The pipeline fails', 'SHOP')}")
ok(rb.parse_ticket("ticket Shöp 1309 merge", "MYSHOP")[0] == "SHOP-1309", "Shöp 1309 -> SHOP")
ok(rb.parse_ticket("ticket 1794", "MYSHOP")[0] == "MYSHOP-1794", "no prefix: the project's own")

open_bravo(); questions("echo", "charlie"); drain()
ok(s.fresh_one() == "bravo" and s.last_spoke == "bravo", "bravo ready: without a task and last heard")
r = bot.on_text("user", "Open the ticket, My Shop 1794. The pipeline on that branch fails on Chromatic, report before doing anything.")
a = arrivals()
ok(r == "message" and a and a[0][0] == "bravo" and a[0][1].startswith("Ticket MYSHOP-1794. Open the ticket"), f"the real case: to bravo ({r} {a})")
ok(s.ticket.get("bravo") == "MYSHOP-1794" and s.fresh_one() is None and bot.launcher_q.empty(), "ticket on the board, bravo no longer fresh, nothing opened")

open_bravo(); questions("echo", "charlie"); drain()
r = bot.on_text("user", "Ticket My Shop 1794, check the review comments.")
ok(arrivals() == [("bravo", "Ticket MYSHOP-1794. Ticket My Shop 1794, check the review comments.")], "no name and no open: to bravo")

open_bravo(); questions("echo"); drain()
ok(bot.on_text("user", "Yes.") == "message" and arrivals() == [("echo", "Yes.")], "the yes goes to whoever asked, not to the fresh one")
ok(s.fresh_one() == "bravo", "bravo keeps waiting for the task")
questions("echo", "charlie"); drain()
ok(bot.on_text("user", "Yes.") == "who-for", "yes with two open questions: asks")

open_bravo(); questions(); drain()
ok(bot.on_text("user", "Open gardenlog.") == "open?" and bot.questions[-1].get("lobby"), "open gardenlog with bravo fresh: lobby")
open_bravo(); drain()
ok(bot.on_text("user", "Open customer portal, ticket 1800, fix the login.") == "message" and arrivals()[0][1].startswith("Ticket MYSHOP-1800."), "same project as the fresh one: to it")
open_bravo(); drain()
ok(bot.on_text("user", "Open ticket Shop 1309, merge the story.") == "open?" and not arrivals(), "SHOP prefix with bravo on the portal: not to bravo")

s.fresh.clear(); bot.questions.clear(); drain()
ok(bot.on_text("user", "Open the ticket, My Shop 1794, check the comments.") == "open?" and bot.questions[-1]["project"]["name"] == "portal"
   and bot.questions[-1]["branch"].startswith("MYSHOP-1794-"), f"no fresh one: opens the portal from the prefix ({bot.questions[-1].get('branch')}) {said[-1]}")
bot.questions.clear()
ok(bot.on_text("user", "Open ticket 1309 in the ui library and merge the story.") == "open?" and bot.questions[-1]["project"]["name"] == "shop-ui"
   and bot.questions[-1]["ticket"] == "SHOP-1309", f"project named after the ticket: ui library ({said[-1]})")
bot.questions.clear()
ok(bot.on_text("user", "Open gardenlog, it calls the customer portal API.") == "open?" and bot.questions[-1].get("lobby"), "without a ticket a project in the middle does not count: lobby")
# after bravo's "ready", echo speaks: a sentence without a name no longer goes straight to bravo
open_bravo(); questions(); drain()
bot.sessions.awaiting.add("echo"); bot.on_claude_reply("echo", "Both fixes are committed.", kind="done")
r = bot.on_text("user", "Push it.")
ok(r == "who-for" and "echo or bravo" in said[-1] and not arrivals(), f"echo speaks after the ready: asks ({said[-1]})")
ok(bot.on_text("user", "Bravo.") == "message" and arrivals() == [("bravo", "Push it.")], "the name is enough")
