# The day's todos: board/todos.json, written by an outside gathering, read by the radio for the board's Todo tab
import json
import os
import queue
import tempfile

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
ok(set(p1) == {"id", "group", "order", "title", "summary", "links", "mode", "state", "session"},
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
