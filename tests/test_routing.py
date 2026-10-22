import importlib.util, queue
import radio_bot as rb
rb.VOICE_FX = ""; rb.SOUND_OPEN = rb.SOUND_CLOSE = rb.SOUND_NEW = b""
ok = lambda c, what: print(("OK  " if c else "NO  ") + what)
class Log:
    path = "/x"
    def write(self, *a): pass
bot = rb.RadioBot(Log(), transcriber=object(), speaker=None); bot.launcher_q = queue.Queue()
said = []
bot.speak = lambda t, *a, **k: said.append(t)
qs = {n: bot.sessions.connect(n) for n in ("charlie", "delta", "echo")}
def route_of(phrase):
    for q in qs.values():
        while not q.empty(): q.get_nowait()
    r = bot.on_text("user", phrase)
    got = [(n, q.get_nowait()["text"]) for n, q in qs.items() if not q.empty()]
    return r, got
s = bot.sessions
s.last_spoke = "charlie"
ok(route_of("Delta Open UI Library.")[1] == [("delta", "Open UI Library.")], "no comma: delta")
ok(route_of("Echo. Charlie, report when the pipeline is ready.")[1] == [("charlie", "report when the pipeline is ready.")], "corrected name: the last one counts")
ok(route_of("Okay Charlie, push.")[1] == [("charlie", "push.")], "okay before the name")
ok(route_of("Echo, no, Delta, run lint.")[1] == [("delta", "run lint.")], "echo, no, delta")
ok(route_of("Charlie, Delta is stuck.")[1] == [("charlie", "Delta is stuck.")], "talking to charlie about delta")
s.last_spoke = "charlie"
ok(route_of("Delta is stuck, check it.")[1][0][0] == "charlie", "delta is ...: it is about delta, goes to the last one")
ok(route_of("Charlie's branch is wrong.")[1][0][0] == "charlie" and route_of("Delta's branch is wrong.")[1][0][0] != "delta", "delta's: not an address")
r = route_of("Delta Status,")
ok(r[0] == "status" and r[1] == [], "Delta Status: delta's status")
r = route_of("Foxtrot, close.")
ok(r[0] == "off-air" and r[1] == [] and "foxtrot is not on the air" in said[-1], f"closed session: says so ({said[-1]})")
ok(route_of("Hotel rooms are cheaper now.")[0] == "off-air", "hotel without a comma: among the first eight (accepted)")
ok(route_of("Mike said it works.")[1][0][0] == s.last_spoke, "mike without a comma, closed: normal sentence")
# open questions
s.last_spoke = None; s.awaiting.add("echo")
bot.on_claude_reply("echo", "The simplest fix is to move. Shall I do that?", kind="answer")
bot.on_claude_reply("charlie", "The sprint pipeline is finished.", kind="done")
ok(s.last_spoke == "charlie" and s.askers() == ["echo"], "echo asks, charlie comes after")
ok(route_of("Yes.")[1] == [("echo", "Yes.")], "the yes goes to whoever asked (echo), not to the last one")
ok(s.askers() == [], "question closed after the answer")
bot.on_claude_reply("echo", "Move or copy?", kind="blocked")
bot.on_claude_reply("charlie", "Done.", kind="done")
r = route_of("Run the tests again.")
ok(r[0] == "who-for" and r[1] == [] and said[-1] == "Who is that for, echo or charlie?", f"ambiguous sentence: asks ({said[-1]})")
ok(route_of("Charlie.")[1] == [("charlie", "Run the tests again.")], "the name is enough: the held sentence goes")
ok(s.askers() == ["echo"], "echo is still waiting")
r = route_of("Do it.")
ok(r[1] == [("echo", "Do it.")], "\"do it\" is a yes: to echo")
bot.on_claude_reply("echo", "Which one?", kind="blocked")
bot.on_claude_reply("delta", "Merge or rebase?", kind="blocked")
r = route_of("Yes.")
ok(r[0] == "who-for" and "echo or delta" in said[-1], f"two open questions, yes: asks ({said[-1]})")
r = route_of("Echo, the first one.")
ok(r[1] == [("echo", "the first one.")], "name with a sentence: goes to echo, the held one is dropped")
ok(route_of("Delta.")[1] == [("delta", "Delta.")], "afterwards, the name alone does not bring back the old sentence")
s.asking.clear(); s.last_spoke = "delta"
ok(route_of("Push it.")[1] == [("delta", "Push it.")], "no question: to the last one that spoke")
ok(s.route("Zoom, push it.")[1] == "Zoom, push it.", "unknown topic: unchanged")
# unread update: it does not become "last that spoke", and its question is not open
s.asking.clear(); s.last_spoke = "charlie"; s.focus = "charlie"; s.awaiting.clear()
bot.on_claude_reply("delta", "Found it. Should I fix it too?", kind="answer")
ok(s.last_spoke == "charlie" and s.askers() == [], "delta has an update: does not count")
ok(route_of("Push it.")[1] == [("charlie", "Push it.")], "the sentence without a name stays with charlie")
# misheard names (October 16: "Foxfot," ended up at echo)
qs["foxtrot"] = s.connect("foxtrot"); s.asking.clear(); s.fresh.clear(); s.last_spoke = "echo"
ok(route_of("Foxfot, please write a draft for Sam.")[1] == [("foxtrot", "please write a draft for Sam.")], "Foxfot, -> foxtrot")
ok(route_of("Foxtrott, push.")[1] == [("foxtrot", "push.")], "Foxtrott -> foxtrot")
ok(route_of("Sharlie, push.")[1] == [("charlie", "push.")], "Sharlie -> charlie")
ok(route_of("Ecco, push.")[1] == [("echo", "push.")], "Ecco -> echo")
ok(route_of("Delete the old branch.")[1] == [("echo", "Delete the old branch.")], "delete: a word, not delta")
ok(route_of("Delay the push, please.")[1] == [("echo", "Delay the push, please.")], "delay: a word")
ok(route_of("Forgot, one more thing.")[1] == [("echo", "Forgot, one more thing.")], "forgot: a word, not foxtrot")
ok(route_of("Charge it to the sprint.")[1][0][0] == "echo", "charge: a word")
ok(route_of("Deltas look fine.")[1][0][0] == "echo", "without a comma it must sound closer")
ok(route_of("Perfect, push it.")[1] == [("echo", "Perfect, push it.")], "perfect, ...: normal sentence")
