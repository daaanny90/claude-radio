// Roger: Claude Code agents per ticket, each in its own git worktree, through the roger CLI (bin/roger).
// Opened as a dialog (keys.json: "cmd-shift-j": "mod.open:roger").
// - Open: Enter runs `roger open <key>` in the folder of the terminal you were in, so roger can tell the
//   project from it; when it can't, the projects or branches it lists become buttons.
// - Open tickets: the spaces roger opened, named after a key, on a <repo>--<branch> worktree. Close runs
//   `roger close <folder>`; when work would be lost roger refuses, and "Close anyway" adds --force.
// The messages read here are marked "parsed by herdr/roger/main.js" in bin/roger: change both sides together.
import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { props, closeOverlay, theme, focused, spaces, exec, home, notify, run } from "herdr";

const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const KEY = /^[A-Z][A-Z0-9]*-\d+$/;

// Why roger stopped: the last line of its stderr (stdout has the steps before it), without "roger: "
function reason(r) {
  const last = (text) => (text ?? "").trim().split("\n").filter(Boolean).at(-1);
  return (last(r.stderr) ?? last(r.stdout) ?? `roger exited with ${r.code}`).replace(/^roger: /, "");
}

async function roger(args, cwd, timeout) {
  try { return await exec("roger", args, { cwd, timeout }); }
  catch (error) { return { code: -1, stdout: "", stderr: `roger: ${error}` }; }   // timed out, or not on the PATH
}

// The spaces roger opened: named after a ticket key, on a local worktree next to its checkout
function tickets() {
  return spaces().filter((s) => s.machine == null && KEY.test(s.name ?? "") && (s.folder ?? "").split("/").pop().includes("--"));
}

export default class Roger extends View {
  init(_props, cx) {
    // Read before the dialog takes the focus: the terminal you were in. A remote space's folder is not on this Mac
    const f = focused();
    const space = spaces().find((s) => s.id === f?.space);
    this.cwd = f?.cwd && space && space.machine == null ? f.cwd : home();
    this.text = "";
    this.input = InputState.new({ placeholder: "SHOP-1234, a branch or a link" });
    this.input.on("change", (_e, cx) => { this.text = this.input.value(); this.error = ""; this.choices = null; cx.notify(); });
    this.input.on("submit", (_e, cx) => this.open(cx));
    this.inputFocused = true;
    this.input.on("focus", (_e, cx) => { this.inputFocused = true; cx.notify(); });
    this.input.on("blur", (_e, cx) => { this.inputFocused = false; cx.notify(); });
    this.input.focus();
    cx.timer.after(600, () => this.input.focus());
    this.handle = cx.focus_handle();
    this.busy = "";        // what roger open is doing, next to the spinner
    this.error = "";
    this.choices = null;   // { label, options: [{ text, args }] } when roger asks which project or branch
    this.rows = {};        // space id -> { stage: "confirm" | "busy" | "lost" | "force", error }
    this.closed = false;
  }

  // The overlay is going (Esc, the shortcut again): a roger still running only gets to notify
  close() { this.closed = true; }

  dismiss() { closeOverlay(props().overlay); }

  open(cx, args) {
    const key = this.text.trim();
    if (this.busy || !key) return;
    cx.spawn(async (cx) => this.launch(cx, args ?? ["open", key]));
  }

  async launch(cx, args) {
    this.busy = `roger ${args.join(" ")}`;
    this.error = "";
    this.choices = null;
    cx.notify();
    const r = await roger(args, this.cwd, 600_000);
    const ticket = args[1];
    if (r.code === 0) {
      const lines = (r.stdout ?? "").trim().split("\n");
      const shown = lines.some((l) => l.includes("is open already"));
      notify({ title: `${ticket} ${shown ? "shown" : "opened"}`, body: lines[0] ?? "", level: "success" });
      if (!this.closed) this.dismiss();
      return;
    }
    if (this.closed) return notify({ title: `roger open ${ticket}`, body: reason(r), level: "error" });
    this.busy = "";
    const why = reason(r);
    const project = why.match(/which project\? -p (.+)$/);
    const branch = why.match(/several branches, name the one to open: (.+)$/);
    const p = args.indexOf("-p");
    const keep = p < 0 ? [] : args.slice(p, p + 2);   // a project chosen before a branch is asked for
    if (project) {
      this.choices = { label: "Which project?", options: project[1].split(" | ").map((name) => ({ text: name, args: ["open", ticket, "-p", name] })) };
    } else if (branch) {
      this.choices = { label: "Which branch?", options: branch[1].split(", ").map((name) => ({ text: name, args: ["open", name, ...keep] })) };
    } else {
      this.error = why;
    }
    cx.notify();
  }

  stage(cx, id, state) {
    if (state) this.rows[id] = state;
    else delete this.rows[id];
    cx.notify();
  }

  shut(cx, space, force) {
    this.stage(cx, space.id, { stage: "busy" });
    cx.spawn(async (cx) => {
      // From home: roger refuses to close the worktree it runs in
      const r = await roger(["close", space.folder, ...(force ? ["--force"] : [])], home(), 120_000);
      if (r.code === 0) {
        notify({ title: `${space.name} closed`, body: "Agent, worktree and local branch are gone", level: "success" });
        if (!this.closed) this.stage(cx, space.id, null);
        return;
      }
      const why = reason(r);
      if (this.closed) return notify({ title: `roger close ${space.name}`, body: why, level: "error" });
      if (/--force closes anyway/.test(why)) return this.stage(cx, space.id, { stage: "lost", error: why });
      this.error = `${space.name}: ${why}`;   // its space may be gone already (a shell kept the worktree)
      this.stage(cx, space.id, null);
    });
  }

  key(e, cx) {
    if (e.key === "escape") { this.dismiss(); cx.stop_propagation(); }
  }

  row(t, s, button) {
    const state = this.rows[s.id];
    const color = { working: t.working, blocked: t.blocked, done: t.done, idle: t.idle }[s.status] ?? t.unknown;
    const repo = s.folder.split("/").pop().split("--")[0];
    const note = (text, color) => div().text_size(11.5).text_color(color).child(text);
    const keep = button(`keep-${s.id}`, "Keep", "plain", (_e, cx) => this.stage(cx, s.id, null));
    const actions = !state ? [button(`close-${s.id}`, "Close", "plain", (_e, cx) => this.stage(cx, s.id, { stage: "confirm" }))]
      : state.stage === "confirm" ? [note("Agent, worktree and local branch?", t.muted), keep,
        button(`yes-${s.id}`, "Close", "danger", (_e, cx) => this.shut(cx, s, false))]
      : state.stage === "busy" ? [div().text_color(t.working).frames(SPINNER, 90), note("closing…", t.dim)]
      : state.stage === "lost" ? [keep,
        button(`anyway-${s.id}`, "Close anyway", "danger", (_e, cx) => this.stage(cx, s.id, { stage: "force", error: state.error }))]
      : [note("That work is lost.", t.blocked), keep,
        button(`force-${s.id}`, "Close anyway", "danger", (_e, cx) => this.shut(cx, s, true))];
    return v_flex().id(`row-${s.id}`).py(6).px(8).rounded(8).hover((st) => st.bg(t.raised))
      .child(h_flex().items_center().gap(8)
        .child(h_flex().id(`go-${s.id}`).flex_1().min_w(0).items_center().gap(8).cursor_pointer()
          .on_click(() => { run("space.select", { id: s.id }); this.dismiss(); })
          .child(div().size(8).flex_none().rounded_full().bg(color))
          .child(div().flex_none().text_size(13).font_medium().child(s.name))
          .child(div().min_w(0).truncate().text_size(12).text_color(t.dim).child(repo)))
        .child(h_flex().flex_none().items_center().gap(6).children(actions)))
      .when(Boolean(state?.error), (el) => el.child(div().pt(4).pl(16).text_size(11.5).text_color(t.blocked).child(state.error)));
  }

  render() {
    const t = theme();
    const label = (text) => div().pt(14).pb(6).text_size(11.5).text_color(t.dim).child(text);
    const button = (id, text, kind, action) => {
      const bg = kind === "primary" ? t.solid : kind === "danger" ? t.blocked : t.raised;
      const fg = kind === "primary" ? t.solid_foreground : kind === "danger" ? t.background : t.foreground;
      return h_flex().id(id).h(28).px(11).flex_none().items_center().rounded(7).border_1()
        .border_color(kind === "plain" ? t.input : bg).bg(bg).text_color(fg).text_size(12).font_medium()
        .cursor_pointer().hover((s) => s.opacity(0.88)).on_click(action).child(text);
    };
    const open = tickets();
    return v_flex().id("roger").track_focus(this.handle).on_key_down((e, cx) => this.key(e, cx))
      .size_full().min_h(0).p(18).bg(t.popover).text_color(t.foreground)
      .child(h_flex().items_center()
        .child(div().flex_1().text_size(15).font_semibold().child("Tickets"))
        .child(div().text_size(11).text_color(t.dim).child("Esc")))
      .child(label(`Open a ticket, in ${this.cwd.replace(home(), "~")}`))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().min_w(0).child(Input.new(this.input).w_full().h(34).px(10).rounded(9).border_1()
          .border_color(this.inputFocused ? t.accent : t.input).bg(t.background).text_size(12.8).text_color(t.foreground)))
        .child(button("open", "Open", "primary", (_e, cx) => this.open(cx))))
      .when(Boolean(this.choices), (el) => el
        .child(label(this.choices.label))
        .child(h_flex().flex_wrap().gap(6).children(this.choices.options.map((o, i) =>
          button(`choice-${i}`, o.text, "plain", (_e, cx) => { if (!this.busy) cx.spawn(async (cx) => this.launch(cx, o.args)); })))))
      .when(Boolean(this.busy), (el) => el.child(h_flex().pt(10).gap(6).text_size(11.5).text_color(t.dim)
        .child(div().text_color(t.working).frames(SPINNER, 90))
        .child(div().min_w(0).truncate().child(`${this.busy}…`))))
      .when(Boolean(this.error), (el) => el.child(div().pt(10).text_size(12).text_color(t.blocked).child(this.error)))
      .child(label(open.length ? `Open tickets (${open.length})` : "No ticket open"))
      .child(v_flex().id("tickets").flex_1().min_h(0).overflow_y_scrollbar().children(open.map((s) => this.row(t, s, button))));
  }
}
