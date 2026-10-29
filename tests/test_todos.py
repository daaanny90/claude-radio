# The day's todos: board/todos.json, written by an outside gathering, read by the radio for the board's Todo tab
import http.client
import json
import os
import queue
import socket
import tempfile
import time

import radio_bot as rb
import board

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
    bot.speak = lambda text, *a, **k: None
    return bot


def write(data, name="todos.json"):
    # as the gathering does it: a new file renamed over the old one
    path = os.path.join(rb.BOARD_DIR, name)
    with open(path + ".tmp", "wb") as f:
        f.write(data if isinstance(data, bytes) else (data if isinstance(data, str) else json.dumps(data)).encode())
    os.replace(path + ".tmp", path)
    return path


def todo(id, group="today", mode="none", **more):
    return dict({"id": id, "group": group, "title": f"Title of {id}", "mode": mode}, **more)


def gathered(*items, groups=None):
    return {"version": 1, "gathered_at": "2026-10-29T09:45:00+01:00",
            "groups": groups if groups is not None else [{"id": "today", "label": "Today"}, {"id": "next", "label": "Tomorrow"}],
            "items": list(items)}


ids = lambda todos: [t["id"] for t in todos["items"]]

# 1. nothing gathered yet
bot = new_bot()
t = bot.board_todos()
ok(t == {"gathered_at": None, "groups": [], "items": [], "skipped": [], "error": None}, f"no file: no todos, no error ({t})")
ok(bot.board_state()["todos"] == t, "the board's poll carries them")

# 2. a good file: groups in their order, todos by order, the brief kept away from the board
write(gathered(
    todo("PROJ-2", group="next", order=1),
    todo("PROJ-1", order=2, mode="branch", target="shop-ui", ticket="PROJ-1", branch="PROJ-1-logo",
         summary="Approved by design.", brief="Todo PROJ-1: bring shop-ui!487 to main.",
         links=[{"label": "shop-ui!487", "url": "https://git.example.com/shop/-/merge_requests/487"},
                {"label": "evil", "url": "javascript:alert(1)"}, {"url": "ftp://example.com/x"},
                {"url": "https://example.com/no-label"}, "not a link"]),
    todo("mail:event-reply", order=1, summary="Cancel by 30.10 if not going.", extra="ignored"),
    todo("PROJ-3", group="later"),
    todo("PROJ-4")))
t = bot.board_todos()
ok(t["error"] is None and t["skipped"] == [] and t["gathered_at"] == "2026-10-29T09:45:00+01:00", "read without errors")
ok(ids(t) == ["mail:event-reply", "PROJ-1", "PROJ-4", "PROJ-2", "PROJ-3"],
   f"today before tomorrow, by order, without one last; an unlisted group at the end ({ids(t)})")
ok([g["id"] for g in t["groups"]] == ["today", "next", "later"] and t["groups"][2]["label"] == "later",
   "an unlisted group is added after the others, named by its id")
p1 = t["items"][1]
ok(set(p1) == {"id", "group", "order", "title", "summary", "links", "mode", "state", "session", "error", "can_start"},
   f"only what the board shows: no brief, target, ticket or branch ({sorted(p1)})")
ok(p1["links"] == [{"label": "shop-ui!487", "url": "https://git.example.com/shop/-/merge_requests/487"},
                   {"label": "https://example.com/no-label", "url": "https://example.com/no-label"}],
   "links: only http(s), a missing label is the address")
ok(p1["state"] == "open" and p1["session"] == "", "nothing started yet")
ok(bot.todos["items"][1]["brief"] == "Todo PROJ-1: bring shop-ui!487 to main." and bot.todos["items"][1]["branch"] == "PROJ-1-logo",
   "the radio keeps the brief and the branch for Start")

# 3. todos that break the contract are left out, each with its reason
write(gathered(
    todo("PROJ-10"),
    {"id": "PROJ-11", "group": "today", "mode": "none"},
    todo("PROJ-12", mode="fix"),
    todo("PROJ-13", mode="new"),
    todo("PROJ-14", mode="branch", ticket="PROJ-14"),
    todo("PROJ-15", mode="new", ticket="PROJ-15", brief="x" * 4001),
    todo("PROJ-10", order=0),
    "just a string",
    todo("PROJ-16", mode="new", ticket="PROJ-16", brief="x" * 4000, title="T" * 250, summary="S" * 600),
    dict(todo("PROJ-17"), title=42)))
t = bot.board_todos()
ok(ids(t) == ["PROJ-10", "PROJ-16"], f"only the good ones ({ids(t)})")
ok(t["skipped"] == ["PROJ-11: no title", 'PROJ-12: unknown mode "fix"', 'PROJ-13: mode "new" needs a ticket',
                    'PROJ-14: mode "branch" needs a branch', "PROJ-15: brief over 4000 characters",
                    "PROJ-10: a second todo with this id", "item 8: not a JSON object", "PROJ-17: no title"],
   f"each with its reason, the first of two with one id stays ({t['skipped']})")
p16 = t["items"][1]
ok(len(p16["title"]) == 200 and p16["title"].endswith("…") and len(p16["summary"]) == 500,
   "a long title or summary is cut for the board")
ok(len(bot.todos["items"][1]["brief"]) == 4000, "a brief at the limit is kept whole")
write(gathered(*[todo(f"PROJ-{n}", order=n) for n in range(1, 106)]))
t = bot.board_todos()
ok(len(t["items"]) == 100 and t["items"][-1]["id"] == "PROJ-100" and t["skipped"] == ["5 more past the limit of 100"],
   "at most 100 todos, the rest counted")

# 4. the radio's own state of each todo, which a new gathering does not reset
write(gathered(todo("PROJ-1", mode="new", ticket="PROJ-1"), todo("PROJ-2"), todo("PROJ-3"), todo("PROJ-4")))
write({"PROJ-1": {"state": "started", "session": "echo", "ts": 1}, "PROJ-2": {"state": "done", "session": "bravo"},
       "PROJ-3": {"state": "lost"}, "PROJ-4": "started", "PROJ-99": {"state": "started", "session": "alpha"}},
      "todos-state.json")
t = bot.board_todos()
ok([(i["state"], i["session"]) for i in t["items"]] == [("started", "echo"), ("done", "bravo"), ("open", ""), ("open", "")],
   f"started with its session, done; an unknown state is open ({[(i['state'], i['session']) for i in t['items']]})")
write(gathered(todo("PROJ-1", mode="new", ticket="PROJ-1", title="Gathered again")))
ok(bot.board_todos()["items"][0]["state"] == "started", "gathered again: still started")
write("{broken", "todos-state.json")
ok(bot.board_todos()["items"][0]["state"] == "open", "a broken state file: everything open, nothing raised")
os.remove(os.path.join(rb.BOARD_DIR, "todos-state.json"))

# 5. a broken file keeps the last good todos on screen, with the error
write(gathered(todo("PROJ-1"), todo("PROJ-2")))
bot.board_todos()
write('{"version": 1, "items": [')
t = bot.board_todos()
ok(ids(t) == ["PROJ-1", "PROJ-2"] and t["error"].startswith("todos.json is not valid JSON"),
   f"not JSON: the last good todos and the error ({t['error']})")
write(dict(gathered(todo("PROJ-9")), version=2))
t = bot.board_todos()
ok(ids(t) == ["PROJ-1", "PROJ-2"] and t["error"] == "todos.json has version 2, this radio reads version 1",
   "an unknown version: the same")
for broken, error in (([1, 2], "todos.json is not a JSON object"), ({"version": 1}, "todos.json has no list of items")):
    write(broken)
    ok(bot.board_todos()["error"] == error, error)
write(b'{"version": 1, "items": [], "gathered_at": "\xff"}')
ok(bot.board_todos()["error"].startswith("todos.json is not valid JSON"), "not UTF-8 either")
write(gathered(todo("PROJ-3")))
t = bot.board_todos()
ok(ids(t) == ["PROJ-3"] and t["error"] is None, "fixed: the new todos, no error")
ok(new_bot().board_todos()["items"][0]["id"] == "PROJ-3", "a restarted bot reads it again")
os.remove(os.path.join(rb.BOARD_DIR, "todos.json"))
t = bot.board_todos()
ok(t["items"] == [] and t["error"] is None, "removed: nothing gathered")

# 6. read again only when the file changes, also when the new one has the same size and time
calls = []
real = rb.parse_todos
rb.parse_todos = lambda data: calls.append(1) or real(data)
path = write(gathered(todo("PROJ-1", title="AAAA")))
for _ in range(3):
    bot.board_todos()
ok(len(calls) == 1, f"polled three times, parsed once ({len(calls)})")
before = os.stat(path)
write(gathered(todo("PROJ-1", title="BBBB")))
os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
ok(bot.board_todos()["items"][0]["title"] == "BBBB", "same size and time, renamed over the old one: read again")

# 7. whatever goes wrong, the rest of the board's state stays
rb.parse_todos = lambda data: 1 / 0
write(gathered(todo("PROJ-2")))
bot.usage = {"limits": [], "at": 0, "every": 300}
state = bot.board_state()
ok(state["todos"]["error"] == "todos.json not read: division by zero" and ids(state["todos"]) == ["PROJ-1"]
   and state["usage"] is not None and "chat" in state, "an unexpected error: shown, and the board keeps everything else")
rb.parse_todos = real

# 8. the board's page: the poll carries the todos without the brief, the page has their tab
write(gathered(todo("PROJ-1", mode="new", ticket="PROJ-1", brief="Secret plan.", links=[{"url": "https://example.com/1"}])))
store = board.Board(tempfile.mkdtemp())
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
board.start(store, Log(), port, bot.board_sessions, bot)
time.sleep(0.3)
cookie = f"{board.COOKIE}={store.make_cookie()}"


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Cookie": cookie})
    r = c.getresponse()
    return r.status, r.read()


status, data = get("/api/latest")
todos = json.loads(data)["todos"]
ok(status == 200 and ids(todos) == ["PROJ-1"] and todos["items"][0]["links"] == [{"label": "https://example.com/1", "url": "https://example.com/1"}],
   "GET /api/latest: the todos")
ok(b"Secret plan." not in data and b"brief" not in data, "the brief never reaches the page")
status, page = get("/")
ok(status == 200 and b'id="todos"' in page and b"VIEWS = ['all', 'todo', 'operator']" in page and b"todoRow" in page,
   "the page has the Todo tab")
ok(b"innerHTML" not in page, "the page builds todos from text nodes only")
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("POST", "/api/start", body=json.dumps({"id": "PROJ-1"}), headers={
    "Content-Type": "application/json", "X-Board-Action": "1", "Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"})
status = c.getresponse().status
_, _, kind, payload = bot.work.get_nowait()
ok(status == 200 and kind == "start_todo" and payload["todo_id"] == "PROJ-1", "POST /api/start reaches the radio's queue")
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("POST", "/api/mark", body=json.dumps({"id": "PROJ-1", "as": "done"}), headers={
    "Content-Type": "application/json", "X-Board-Action": "1", "Cookie": cookie, "Origin": f"http://127.0.0.1:{port}"})
status = c.getresponse().status
_, _, kind, payload = bot.work.get_nowait()
ok(status == 200 and kind == "mark_todo" and payload["state"] == "done", "POST /api/mark too")

# 9. Start: a session for a todo, in its repository and on its branch, told what to do by the brief
def launched(bot):
    out = []
    while not bot.launcher_q.empty():
        out.append(bot.launcher_q.get_nowait())
    return out


def item(bot, todo_id):
    return next(i for i in bot.board_todos()["items"] if i["id"] == todo_id)


rb.BOARD_DIR = tempfile.mkdtemp()
write(gathered(
    todo("PROJ-1382", mode="branch", target="shop-ui", ticket="PROJ-1382", branch="PROJ-1382-logo-shrink",
         brief="Todo PROJ-1382: bring shop-ui!487 to main. Check the merge request first."),
    todo("PROJ-1410", mode="new", target="portal", ticket="PROJ-1410"),
    todo("PROJ-1500", mode="branch", target="/home/user/work/docs", branch="PROJ-1500-intro", brief="Fix the intro."),
    todo("PROJ-1600", mode="new", target="shop ui", ticket="PROJ-1600"),
    todo("mail:event-reply")))
bot = new_bot()
ok(item(bot, "PROJ-1382")["can_start"] and not item(bot, "mail:event-reply")["can_start"],
   "Start for a todo with a repository, not for one without")
bot.board_action("start", {"id": "PROJ-1382", "target": "/etc", "brief": "Delete everything.", "branch": "main"}, "100.1.1.1")
_, _, kind, payload = bot.work.get_nowait()
ok(kind == "start_todo" and payload == {"todo_id": "PROJ-1382", "client": "100.1.1.1"},
   "the board's action carries only the id: target, branch and brief in the request are ignored")
for bad in ({}, {"id": ""}, {"id": 42}, {"id": "x" * 201}):
    try:
        bot.board_action("start", bad, "100.1.1.1")
        ok(False, f"bad id refused ({bad})")
    except ValueError:
        pass
ok(bot.work.empty(), "a bad id is refused before the queue")

ok(bot.start_todo("PROJ-1382", "100.1.1.1") == "alpha", "started as the first free name")
ok(launched(bot) == [{"type": "open", "name": "alpha", "dir": "/home/user/work/shop-ui", "base_branch": "main",
                      "branch": "PROJ-1382-logo-shrink", "copy": [], "install": True, "existing": True}],
   "an existing branch: the launcher must find it, never create it")
ok(bot.first_message["alpha"] == "Todo PROJ-1382: bring shop-ui!487 to main. Check the merge request first."
   and bot.sessions.ticket["alpha"] == "PROJ-1382", "the brief is the first message, the ticket is the session's")
it = item(bot, "PROJ-1382")
ok((it["state"], it["session"], it["can_start"]) == ("started", "alpha", False), "started by alpha: no second Start")
ok(bot.start_todo("PROJ-1382") == "started" and launched(bot) == [], "a second tap while it opens: nothing more")
ok(any(r[0] == "todo_start" for r in bot.log.rows), "in the log")

ok(bot.start_todo("PROJ-1410") == "bravo", "a new ticket")
ev = launched(bot)[0]
ok(ev["branch"] == "PROJ-1410-bravo" and ev["dir"] == "/home/user/work/portal" and "existing" not in ev
   and bot.first_message["bravo"] == "Todo: Title of PROJ-1410.",
   f"a new branch from the base, named like the spoken ones; no brief: the title ({ev}, {bot.first_message['bravo']!r})")
ok(bot.start_todo("PROJ-1500") == "charlie", "an absolute folder")
ok(launched(bot) == [{"type": "open", "name": "charlie", "dir_direct": "/home/user/work/docs"}]
   and bot.first_message["charlie"] == "Work on branch PROJ-1500-intro: check it out here first. Fix the intro.",
   "no registered project: straight in the folder, told to check the branch out")

# 10. what goes wrong is written on the todo
ok(bot.start_todo("PROJ-1600") == "failed" and launched(bot) == [], "\"shop ui\" is no project name: nothing opens")
it = item(bot, "PROJ-1600")
ok(it["state"] == "open" and it["error"] == '"shop ui" is no registered project nor an absolute folder' and it["can_start"],
   f"the target is matched exactly, never approximately, and the board says why ({it['error']})")
ok(bot.start_todo("mail:event-reply") == "failed" and item(bot, "mail:event-reply")["error"] == "this todo has no repository to open",
   "a todo without a repository does not start")
ok(bot.start_todo("PROJ-404") == "unknown", "an id that is not in todos.json")
bot.on_launcher_result({"name": "alpha", "ok": False, "detail": "branch PROJ-1382-logo-shrink not found: couldn't find remote ref"})
it = item(bot, "PROJ-1382")
ok((it["state"], it["can_start"]) == ("open", True) and it["error"] == "could not start: branch PROJ-1382-logo-shrink not found: couldn't find remote ref",
   f"the launcher failed: open again, with the reason ({it['error']})")
bot.on_launcher_result({"name": "bravo", "ok": True, "dir": "/home/user/work/portal--PROJ-1410-bravo"})
q = bot.sessions.connect("bravo")
ok(item(bot, "PROJ-1410")["state"] == "started" and not item(bot, "PROJ-1410")["can_start"], "opened: started")
bot.sessions.disconnect("bravo", q)
ok(item(bot, "PROJ-1410")["can_start"], "its session closed: it can start again")
bot.on_launcher_result({"name": "bravo", "ok": False, "detail": "later failure"})
ok(item(bot, "PROJ-1410")["error"] == "", "a later result for the same name no longer touches the todo")
bot.start_todo("PROJ-1382")
ok(item(bot, "PROJ-1382")["error"] == "" and item(bot, "PROJ-1382")["state"] == "started", "started again: the old error is gone")
bot.launcher_q = None
ok(bot.start_todo("PROJ-1410") == "failed" and item(bot, "PROJ-1410")["error"] == "the launcher is not running",
   "no launcher: said on the todo")

# 11. a session opened some other way (by voice, by the operator) on the todo's ticket: the todo is started by it.
# 29 October: bravo had worked on the first todo's ticket since the 27th, and the board offered Start
rb.BOARD_DIR = tempfile.mkdtemp()
write(gathered(
    todo("shop!1513", mode="branch", target="shop-ui", ticket="PROJ-1411", branch="PROJ-1411-cards"),
    todo("site!20", mode="branch", target="shop-site", ticket="PROJ-1412", branch="PROJ-1412-more"),
    todo("docs!3", mode="new", target="/home/user/work/docs", ticket="PROJ-7"),
    todo("mail:x")))
bot = new_bot()
queues = {}
for name, project, ticket in (("bravo", "shop-ui", "PROJ-1411"), ("delta", "shop-ui", "PROJ-1412"), ("echo", "docs", "PROJ-7")):
    queues[name] = bot.sessions.connect(name)
    bot.sessions.project[name], bot.sessions.ticket[name] = project, ticket
it = item(bot, "shop!1513")
ok((it["state"], it["session"], it["can_start"]) == ("started", "bravo", False), f"bravo is on its ticket: started by bravo ({it})")
ok(item(bot, "site!20")["state"] == "open" and item(bot, "site!20")["can_start"],
   "the same ticket in another project is another job: delta works on shop-ui, the todo is for shop-site")
ok(item(bot, "docs!3")["session"] == "echo", "a folder target: matched by the folder's name")
ok(bot.start_todo("shop!1513") == "started" and launched(bot) == [], "no second session on it")
bot.set_todo_state("shop!1513", "open", error="could not start: no network")
ok(item(bot, "shop!1513")["error"] == "", "an old Start error is not shown while a session is on it")
bot.sessions.disconnect("bravo", queues["bravo"])
ok(item(bot, "shop!1513")["state"] == "open" and item(bot, "shop!1513")["can_start"], "bravo closed: open again")

# 12. done and dropped from the board, kept by id
for bad in ({"id": "site!20", "as": "gone"}, {"id": "", "as": "done"}, {"as": "done"}):
    try:
        bot.board_action("mark", bad, "100.1.1.1")
        ok(False, f"refused: {bad}")
    except ValueError:
        pass
bot.board_action("mark", {"id": "site!20", "as": "done"}, "100.1.1.1")
_, _, kind, payload = bot.work.get_nowait()
ok(kind == "mark_todo" and payload == {"todo_id": "site!20", "state": "done", "client": "100.1.1.1"} and bot.work.empty(),
   "only a good id and state reach the queue")
ok(bot.mark_todo(**payload) == "done" and item(bot, "site!20")["state"] == "done" and not item(bot, "site!20")["can_start"],
   "done: no Start")
bot.mark_todo("mail:x", "dropped")
bot.mark_todo("docs!3", "done")
ok(item(bot, "mail:x")["state"] == "dropped" and (item(bot, "docs!3")["state"], item(bot, "docs!3")["session"]) == ("done", "echo"),
   "dropped; done keeps the session that was on it")
write(gathered(todo("site!20", mode="branch", target="shop-site", ticket="PROJ-1412", branch="PROJ-1412-more", title="Again"),
               todo("mail:x"), todo("docs!3", mode="new", target="/home/user/work/docs", ticket="PROJ-7")))
ok([item(bot, i)["state"] for i in ("site!20", "mail:x", "docs!3")] == ["done", "dropped", "done"],
   "a new gathering does not bring them back")
bot.mark_todo("site!20", "open")
ok(item(bot, "site!20")["state"] == "open" and item(bot, "site!20")["can_start"], "reopened: Start again")
ok(bot.mark_todo("PROJ-404", "done") == "unknown" and "PROJ-404" not in bot.todo_states(), "an id not in todos.json is not written")
ok(any(r[0] == "todo_mark" for r in bot.log.rows), "in the log")
