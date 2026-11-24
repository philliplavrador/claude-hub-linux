# claude-hub, part 15: class Popup (the core: __init__, terminal, draw, keys, act, end, relay, pump, run) and main.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class Popup(PopupRows, PopupShown, PopupSession, PopupScreens, PopupPick):
    SEQUENCES = {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down", b"[H": "home", b"[F": "end",
                 b"OH": "home", b"OF": "end", b"[1~": "home", b"[4~": "end", b"[7~": "home", b"[8~": "end",
                 b"[5~": "pgup", b"[6~": "pgdn", b"[3~": "delete", b"[C": "right", b"OC": "right",
                 b"[D": "left", b"OD": "left",
                 b"[I": "focus-in", b"[O": "focus-out"}

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.out = sys.stdout.fileno()
        self.cooked = None
        self.lock = threading.Lock()
        self.kwin_lock = threading.Lock()
        self.rows: list[dict] = []
        self.pick = ""
        self.av_text = ""
        self.av_seq = 0
        self.av_busy = False
        self.av_preview: dict | None = None
        self.mode = "list"
        self.skill: dict | None = None
        self.info_top = 0
        self.new_account = ""
        self.new_model = ""
        self.new_auto = False
        self.new_auto_pick = ""
        self.service: dict | None = None
        self.unread: set[str] = set()
        self.kwin = True
        self.sel_key: str | None = None
        self.filter = ""
        self.number = ""
        self.pinned: dict | None = None
        self.message = ""
        self.confirm: dict | None = None
        self.closes: dict[str, dict] = {}
        self.new_desktop_said = ""
        self.renaming: dict | None = None
        self.dirty = True
        self.pending = b""
        self.top = 0
        self.visible = False
        self.shown_at = 0.0
        self.showing = False
        self.ending = 0
        self.shown_on: str | None = None
        self.busy: dict[str, str] = {}
        self.focus_due = 0.0
        self.konsole_pid = 0
        self.started = int(time.time())
        self.signals: collections.deque[str] = collections.deque()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_w, False)
        self.wake = threading.Event()
        self.sweep = None
        self.swept = -SWEEP
        self.want_sweep = True
        self.sweeping = False
        self.fresh = False
        self.watch = ""
        self.touched = False

    def write(self, s: str) -> None:
        os.write(self.out, s.encode())

    def size(self) -> tuple[int, int]:
        try:
            cols, rows = os.get_terminal_size(self.out)
            return rows, cols
        except OSError:
            return 30, 150

    def enter_screen(self) -> None:
        tty.setraw(self.fd)
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[?1004h")
        self.dirty = True

    def leave_screen(self) -> None:
        try:
            self.write("\x1b[?1004l\x1b[?25h\x1b[?1049l")
            if self.cooked is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.cooked)
        except (OSError, termios.error):
            pass

    def on_signal(self, what: str) -> None:
        self.signals.append(what)
        try:
            os.write(self.wake_w, b".")
        except OSError:
            pass

    def fill(self, timeout: float) -> bool:
        if self.pending:
            return True
        try:
            r, _, _ = select.select([self.fd, self.wake_r], [], [], timeout)
        except InterruptedError:
            return False
        if self.wake_r in r:
            try:
                os.read(self.wake_r, 512)
            except OSError:
                pass
        if self.fd in r:
            data = os.read(self.fd, 4096)
            if not data:
                raise EOFError
            self.pending += data
        return bool(self.pending)

    def read_key(self, timeout: float) -> str:
        if self.signals:
            return "signal"
        if not self.fill(timeout):
            return "signal" if self.signals else "tick"
        k, self.pending = self.pending[:1], self.pending[1:]
        if k == b"\x1b":
            if not self.pending:
                self.fill(0.05)
            for seq, name in self.SEQUENCES.items():
                if self.pending.startswith(seq):
                    self.pending = self.pending[len(seq):]
                    return name
            if self.pending[:1] in (b"[", b"O"):
                m = re.match(rb"[\[O][0-9;]*[@-~]", self.pending)
                self.pending = self.pending[m.end():] if m else b""
                return "other"
            return "esc"
        if k in (b"\r", b"\n"):
            return "enter"
        if k in (b"\x7f", b"\x08"):
            return "backspace"
        if k[0] >= 0xC0:
            need = 1 if k[0] < 0xE0 else 2 if k[0] < 0xF0 else 3
            if len(self.pending) < need:
                self.fill(0.05)
            k, self.pending = k + self.pending[:need], self.pending[need:]
        ch = k.decode(errors="replace")
        return ch if ch.isprintable() else "other"

    def draw(self) -> None:
        rows_n, cols = self.size()
        body_rows = self.shown_rows()
        with self.lock:
            msg, kwin, unread, confirm = self.message, self.kwin, set(self.unread), self.confirm
            renaming = dict(self.renaming) if self.renaming else None
            mode, service = self.mode, self.service
            self.dirty = False
        keys = [r.get("key") for r in body_rows]
        self.keep_a_session_row(keys)
        sel = self.sel_key if self.sel_key in keys else None
        clock = time.strftime("%H:%M")
        bold = {"session": "Tools", "screens": "Screens", "model": "New Claude Code", "effort": "New Claude Code",
                "skills": "Skills", "skill-info": "Skills"}.get(mode, TITLE)
        title = bold
        if mode == "model":
            title = f"{bold} · {self.new_account} · which model?"
        elif mode == "effort":
            title = f"{bold} · {self.new_account} · {model_label(self.new_model)} · which effort?"
        elif mode == "screens":
            title = "Screens · a sticky screen follows your Main Desktop"
        elif mode == "skills":
            title = f"{bold} · reusable prompts, each on a desktop of its own"
        elif mode == "skill-info":
            self.refresh_skill()
            title = f"{bold} · {(self.skill or {}).get('name') or ''}"
        title = clip(title, max(len(bold), cols - len(clock) - 4))
        head = [f" {BOLD}{bold}{OFF}{title[len(bold):]}{' ' * max(1, cols - len(title) - len(clock) - 3)}{DIM}{clock}{OFF}", ""]
        widths = columns(body_rows, cols)
        body = [format_row(r, cols, r.get("key") == sel, widths) for r in body_rows]
        if mode == "skill-info":
            body = self.skill_info_lines(cols)
        elif self.filter and not body_rows:
            body = [f"   {DIM}nothing matches{OFF}"]
        elif mode == "skills" and not body_rows:
            body = [f"   {DIM}no skills saved yet{OFF}"]
        elif mode == "screens" and not body_rows:
            if service is None:
                said = SERVICE_DOWN
            elif DK.screens_known(service):
                said = DK.NO_SCREENS
            else:
                said = NOT_ANSWERING
            body = [f"   {DIM}{clip(said, cols - 4)}{OFF}"]
        foot = [""]
        if self.number:
            none = "" if sel else f"  {DIM}nothing here is numbered {self.number}{OFF}"
            foot.append(f" {CYAN}number:{OFF} {self.number}▏{none}")
        elif self.filter and mode != "skill-info":
            foot.append(f" {CYAN}filter:{OFF} {self.filter}▏")
        if mode == "session":
            if msg:
                foot.append(f" {msg}")
            foot.append(f" {DIM}number+enter open · → account · type to filter · esc back{OFF}")
        elif mode in ("model", "effort"):
            if msg:
                foot.append(f" {msg}")
            foot.append(f" {DIM}number+enter pick · type to filter · esc back{OFF}")
        elif mode == "skills":
            unreadable = hub_skills()[1]
            if unreadable:
                foot.append(f" {YELLOW}{clip('could not read: ' + ' · '.join(unreadable), cols - 2)}{OFF}")
            if msg:
                foot.append(f" {msg}")
            foot.append(f" {DIM}number+enter start · → what it does · type to filter · esc back{OFF}")
        elif mode == "skill-info":
            if msg:
                foot.append(f" {msg}")
            foot.append(f" {DIM}enter start · esc back{OFF}")
        elif mode == "screens":
            if DK.all_sticky(body_rows):
                foot.append(f" {YELLOW}{clip(DK.ALL_STICKY, cols - 2)}{OFF}")
            if msg:
                foot.append(f" {msg}")
            foot.append(f" {DIM}number+enter sticky on/off · esc back{OFF}")
        else:
            if not kwin:
                foot.append(f" {RED}KWin is not answering: desktops cannot be read{OFF}")
            if unread:
                foot.append(f" {DIM}{', '.join(sorted(unread))}: session list not answering{OFF}")
            if renaming:
                foot.append(f" {CYAN}rename {clip(display_name(renaming['row']), 30)}:{OFF} "
                            f"{clip(renaming['text'], max(10, cols - 60))}▏")
                foot.append(f" {DIM}enter keeps it as typed · enter on nothing lets Claude name it · esc cancels{OFF}")
            elif confirm:
                n = list_number(confirm.get("number"))
                if confirm.get("yours"):
                    where = f"desktop {n}" if n else "it"
                    foot.append(f" {YELLOW}Close {clip(main_name(confirm), 40)}? This closes every window on {where} "
                                f"and asks to save first. y/n{OFF}")
                else:
                    what = (f"This stops the Claude and closes every window on desktop {n}." if n else
                            "This stops the Claude; its desktop is already gone.")
                    foot.append(f" {YELLOW}End {clip(display_name(confirm), 40)}? {what} y/n{OFF}")
            elif msg:
                foot.append(f" {msg}")
            if not renaming:
                foot.append(f" {DIM}number+enter go · r rename · type to filter · ↑↓ · del end · esc close{OFF}")
        foot = [clip_ansi(l, cols - 1) for l in foot]
        room = max(1, rows_n - len(head) - len(foot))
        if mode == "skill-info":
            self.info_top = max(0, min(self.info_top, max(0, len(body) - room)))
            shown = body[self.info_top:self.info_top + room]
            below = len(body) - self.info_top - len(shown)
            how = (f"↓ {below} more line{'' if below == 1 else 's'} · " if below
                   else "↑ scroll up · " if self.info_top else "")
            foot[-1] = clip_ansi(f" {DIM}enter start · {how}esc back{OFF}", cols - 1)
        else:
            at = keys.index(sel) if sel in keys else 0
            if at < self.top:
                self.top = at
            elif at >= self.top + room:
                self.top = at - room + 1
            self.top = max(0, min(self.top, max(0, len(body) - room)))
            shown = body[self.top:self.top + room]
        lines = (head + shown + foot)[:rows_n]
        self.write("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J")

    def on_key(self, k: str) -> None:
        if k == "focus-in":
            self.focus_due = 0.0
            if not self.visible:
                self.apply()
            return
        if k == "focus-out":
            if self.hides_on_focus_out():
                self.hide()
            return
        if not self.visible:
            return
        self.dirty = True
        if self.renaming:
            self.on_rename_key(k)
            return
        if self.confirm:
            row, self.confirm = self.confirm, None
            if k in ("y", "Y", "delete"):
                self.end(row)
            else:
                self.message = f"{DIM}nothing ended{OFF}"
            return
        took = None
        if self.mode != "skill-info":
            took = self.on_number_key(k)
        if took is True:
            return
        self.pinned = took or None
        try:
            self.on_mode_key(k)
        finally:
            self.pinned = None

    def on_mode_key(self, k: str) -> None:
        if self.mode == "session":
            self.on_session_key(k)
            return
        if self.mode == "model":
            self.on_pick_key(k, self.pick_model, self.leave_model)
            return
        if self.mode == "effort":
            self.on_pick_key(k, self.pick_effort, self.leave_effort)
            return
        if self.mode == "screens":
            self.on_screens_key(k)
            return
        if self.mode == "skills":
            self.on_skills_key(k)
            return
        if self.mode == "skill-info":
            self.on_skill_info_key(k)
            return
        if k == "esc":
            if self.filter:
                self.set_filter("")
            else:
                self.hide()
        elif k == "backspace":
            self.set_filter(self.filter[:-1])
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.move(k)
        elif k == "enter":
            row = self.selected()
            if row:
                self.act(row)
        elif k == "right":
            row = self.selected()
            if row and row.get("type") == "session":
                self.enter_session()
            elif row and row.get("yours"):
                self.enter_screens()
        elif k == "delete":
            row = self.selected()
            if row and row.get("type") == "panel":
                self.message = f"{DIM}the Panel is not a Claude's desktop: it cannot be ended{OFF}"
            elif row and row.get("type") == "aux":
                self.message = f"{DIM}the Aux Desktop is not a Claude's desktop: it closes when the aux is switched off{OFF}"
            elif row and row.get("yours") and self.endable(row) and self.stop_main_close(row):
                pass
            elif row and self.endable(row):
                key = str(row.get("desktop_id") or row.get("key") or "")
                with self.lock:
                    doing = self.busy.get(key, "")
                if doing:
                    self.message = f"{DIM}still {doing}…{OFF}"
                else:
                    self.confirm = row
        elif k in ("r", "R") and not self.filter and not self.number and self.renamable(self.selected()):
            self.renaming = {"row": self.selected(), "text": ""}
        elif len(k) == 1 and k.isprintable():
            self.set_filter(self.filter + k)

    @classmethod
    def renamable(cls, r: dict | None) -> bool:
        return bool(r) and cls.endable(r) and bool(r.get("session_id")) and bool(r.get("account"))

    def on_rename_key(self, k: str) -> None:
        ask = self.renaming
        if k == "esc":
            self.renaming, self.message = None, f"{DIM}name unchanged{OFF}"
        elif k == "enter":
            self.renaming = None
            self.rename_row(ask["row"], ask["text"].strip())
        elif k == "backspace":
            ask["text"] = ask["text"][:-1]
        elif len(k) == 1 and k.isprintable() and len(ask["text"]) < 80:
            ask["text"] += k

    def rename_row(self, r: dict, text: str) -> None:
        account, sid, old = str(r["account"]), str(r["session_id"]), display_name(r)
        cmd = [title_cmd(), "set", account, sid, text] if text else [title_cmd(), "rename", account, sid]
        self.message = f"{DIM}{'renaming' if text else 'asking Claude to name'} {clip(old, 40)}…{OFF}"

        def run() -> None:
            res = run_quiet(cmd, 90)
            name = ANSI.sub("", res.stdout).strip()
            if res.returncode == 0 and name:
                self.message = f"{DIM}named {clip(name, 60)}{'' if text else ' by Claude'}{OFF}"
            elif text:
                self.message = f"{RED}that name cannot be used (3 to 80 characters, one line): {clip(old, 40)} keeps its name{OFF}"
            else:
                self.message = f"{RED}Claude could not name it: {clip(old, 40)} keeps its name{OFF}"
            try:
                self.look()
            except Exception:                            # noqa: BLE001
                pass
            self.dirty = True
            self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def on_number_key(self, k: str) -> bool | dict:
        if self.filter:
            return False
        if len(k) == 1 and k in "0123456789":
            if len(self.number) < NUMBER_DIGITS:
                self.number += k
            self.select_number()
            return True
        if not self.number:
            return False
        typed, self.number = self.number, ""
        if k == "backspace":
            self.number = typed[:-1]
            if self.number:
                self.select_number()
            return True
        if k == "esc":
            return True
        if k in ("enter", "right"):
            key = self.sel_key
            row = self.selected()
            if not row or row.get("num") != int(typed) or row.get("key") != key:
                self.message = f"{DIM}nothing here is numbered {typed}{OFF}"
                return True
            return row
        if len(k) == 1 and k.isprintable() and self.mode in FILTER_MODES:
            self.filter = typed
        return False

    def select_number(self) -> None:
        n = int(self.number)
        row = next((r for r in self.shown_rows() if r.get("num") == n), None)
        self.sel_key = row.get("key") if row else None
        self.message = ""

    def desktop_cmd(self, args: list[str], done=None, after=None, busy: tuple[str, str] | None = None) -> None:
        cmd = [str(DESKTOP_CMD), *args]
        if busy:
            with self.lock:
                self.busy[busy[0]] = busy[1]

        def run() -> None:
            try:
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                except Exception as e:                   # noqa: BLE001
                    res = subprocess.CompletedProcess(cmd, 1, "", str(e))
                with self.lock:
                    if done:
                        said = done(res)
                        if said is not None:
                            self.message = said
                    elif res.returncode != 0:
                        why = in_list_numbers(ANSI.sub("", (res.stderr or res.stdout)).strip().replace("\n", " "))
                        self.message = f"{RED}{clip(why, 200) or 'claude-desks ' + args[0] + ' failed'}{OFF}"
                    self.dirty = True
            finally:
                if busy:
                    with self.lock:
                        self.busy.pop(busy[0], None)
                if after:
                    after()
                poke()
                self.ask_sweep()

        threading.Thread(target=run, daemon=True).start()

    def go(self, args: list[str], busy: tuple[str, str] | None = None, done=None) -> None:
        self.hide()
        self.desktop_cmd(args, done=done, busy=busy)

    def act(self, r: dict) -> None:
        if r.get("type") == "session":
            self.enter_session()
            return
        if r.get("type") == "usage":
            self.show_usages()
            return
        if r.get("type") == "skills":
            self.enter_skills()
            return
        if r.get("type") == "desktop" and r.get("key") == DESKTOP_ROW["key"]:
            if self.mode == "session" and self.visible:
                self.new_main_desktop()
            return
        if r.get("type") == "new":
            pick = autoswap_pick()
            if pick:
                self.enter_model(pick, auto=True)
            else:
                self.new_claude()
            return
        key, name = str(r.get("desktop_id") or r.get("key") or ""), display_name(r)
        with self.lock:
            doing = self.busy.get(key)
        if doing:
            self.message = f"{DIM}still {doing}…{OFF}"
            return
        state = r.get("state")
        if r.get("yours"):
            self.go(["goto", r.get("desktop_id") or "1"], busy=(key, f"going to {main_name(r)}"))
        elif r.get("type") == "panel":
            self.go(["goto", r["desktop_id"]], busy=(key, "going to the Panel"))
        elif r.get("type") == "aux":
            self.go(["goto", r["desktop_id"]], busy=(key, "going to the Aux Desktop"))
        elif r.get("desktop_id"):
            if (r.get("desktop_gone") or state in ("stopped", "no-window")
                    or (r.get("session_id") and not r.get("konsole_pid")
                        and state in ("working", "parked", "waiting", "idle", "needs-you"))):
                self.go(["resume", r["desktop_id"]], busy=(key, f"resuming {name}"))
            else:
                self.go(["goto", r["desktop_id"]], busy=(key, f"going to {name}"))
        elif r.get("type") != "claude":
            self.message = f"{DIM}nothing to go to{OFF}"
        elif r.get("konsole_pid") and r.get("session_id"):
            self.go(["adopt", "--session", r["session_id"], "--goto"], busy=(key, f"giving {name} a desktop"),
                    done=adopt_said)
        elif r.get("kind") == "background" and r.get("id"):
            self.go(["new", r["account"], "--attach", r["id"]], busy=(key, f"opening {name}"))
        else:
            self.message = f"{DIM}this Claude runs in a terminal that is not a Konsole: nothing to move{OFF}"

    @staticmethod
    def endable(r: dict) -> bool:
        if r.get("type") == "desktop" and r.get("yours"):
            return bool(r.get("desktop_id")) and main_number(r) >= 2
        return (r.get("type") == "desktop" and not r.get("yours") and bool(r.get("desktop_id"))
                and r.get("state") != "no-claude")

    def new_main_desktop(self) -> None:
        self.new_desktop_said = ""

        def done(res) -> str | None:
            said = main_new_said(res)
            self.new_desktop_said = ANSI.sub("", said) if said else ""
            return said

        self.go(["main-desktop", "new", "--json"], done=done)

    def stop_main_close(self, r: dict) -> bool:
        did = str(r.get("desktop_id") or "")
        c = close_state(did, self.closes.get(did))
        if not c or c.get("state") not in CLOSE_LIVE:
            return False
        self.message = f"{DIM}stopping the close of {main_name(r)}…{OFF}"
        name = main_name(r)

        def done(res) -> str:
            if res.returncode == 3:
                return f"{DIM}no close running for {name}{OFF}"
            if res.returncode != 0:
                return f"{RED}{clip(last_line(res.stderr) or last_line(res.stdout) or 'could not stop it', 200)}{OFF}"
            return f"{DIM}close stopped: {name} kept open{OFF}"

        self.desktop_cmd(["main-desktop", "close", did, "--stop"], done=done)
        return True

    def close_main(self, r: dict) -> None:
        did, name = str(r["desktop_id"]), main_name(r)
        with self.lock:
            mine = self.closes.get(did)
            if mine and mine.get("proc") is not None and mine["proc"].poll() is None:
                self.message = f"{DIM}still closing {name}…{OFF}"
                return
        cmd = [str(DESKTOP_CMD), "main-desktop", "close", did, "--json"]
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        except Exception as e:                            # noqa: BLE001
            with self.lock:
                self.closes[did] = {"proc": None, "failed": clip(str(e), 200), "start": started, "ended": time.time()}
            return
        rec = {"proc": proc, "failed": "", "start": started, "ended": None}
        with self.lock:
            self.closes[did] = rec
        self.message = f"{DIM}closing {name}…{OFF}"

        def watch() -> None:
            try:
                out, err = proc.communicate()
            except Exception as e:                        # noqa: BLE001
                out, err = "", str(e)
            if proc.returncode == 7:
                rec["failed"] = NOT_FOR_CLAUDES
            elif proc.returncode not in (0, 3):
                why = last_line(err) or last_line(out)
                rec["failed"] = clip(why, 200) or f"claude-desks main-desktop close failed ({proc.returncode})"
            rec["ended"] = time.time()
            with self.lock:
                self.dirty = True
            poke()

        threading.Thread(target=watch, daemon=True).start()

    def end(self, r: dict) -> None:
        if r.get("yours"):
            self.close_main(r)
            return
        did, name, here = r["desktop_id"], display_name(r), bool(r.get("current"))
        self.message = f"{DIM}ending {name}…{OFF}"
        if here:
            with self.lock:
                self.ending += 1
            with self.kwin_lock:
                DK.kwin_script(script_pin(self.konsole_pid), "hub-pin")

        def summary(res) -> str:
            lines = [l for l in ANSI.sub("", res.stdout).splitlines() if l.strip()]
            said = lines[-1] if lines else ANSI.sub("", res.stderr).strip() or f"ended {name}"
            return f"{DIM if res.returncode == 0 else RED}{clip(in_list_numbers(said), 300)}{OFF}"

        def after() -> None:
            if here:
                with self.lock:
                    self.ending -= 1
                    last = not self.ending
                if last:
                    self.apply()
            try:
                self.look()
            except Exception:                            # noqa: BLE001
                pass
            self.select_current_if_listing(only_if_gone=True)
            self.dirty = True
        self.desktop_cmd(["end", did], done=summary, after=after, busy=(did, f"ending {name}"))
        self.select_current_if_listing()

    def relay(self, argv: list[str]) -> tuple[int | None, bool]:
        master, slave = pty.openpty()
        proc, taken = None, False
        try:
            match_size(slave, self.out)
            quiet_pty(slave)
            proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            slave = -1
            os.set_blocking(master, False)
            taken = self.pump(proc, master)
        finally:
            if slave >= 0:
                os.close(slave)
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            drain(master, self.out)
            os.close(master)
        return (proc.returncode if proc is not None else None), taken

    def pump(self, proc: subprocess.Popen, master: int) -> bool:
        tap = FocusTap()
        to_tool = bytearray()
        taken = False

        def his(data: bytes) -> None:
            nonlocal taken
            through, reports = tap.feed(data, time.monotonic())
            to_tool.extend(through)
            for report in reports:
                if report == "focus-out" and self.hides_on_focus_out():
                    self.on_signal("hide")
                    taken = True

        his(self.pending)
        self.pending = b""
        while not taken and proc.poll() is None:
            try:
                r, w, _ = select.select([self.fd, master, self.wake_r], [master] if to_tool else [], [], 0.05)
            except InterruptedError:
                continue
            if self.wake_r in r:
                try:
                    os.read(self.wake_r, 512)
                except OSError:
                    pass
                match_size(master, self.out)
            if master in r:
                try:
                    out = os.read(master, 65536)
                except OSError:
                    out = b""
                if out:
                    os.write(self.out, out)
            if self.fd in r:
                data = os.read(self.fd, 4096)
                if not data:
                    raise EOFError
                his(data)
            to_tool.extend(tap.due(time.monotonic()))
            if to_tool:
                try:
                    del to_tool[:os.write(master, bytes(to_tool))]
                except BlockingIOError:
                    pass
                except OSError:
                    to_tool.clear()
            if not taken and any(s in ("toggle", "hide") for s in list(self.signals)):
                taken = True
        return taken

    def new_claude(self, title: str = "Open Claude as which account?") -> None:
        pick = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"claude-hub-pick-{os.getpid()}"
        pick.unlink(missing_ok=True)
        account = ""
        try:
            try:
                rc, taken = self.relay(["claude-account-menu", "--out", str(pick), "--title", title])
            except OSError as e:
                rc, taken = None, False
                self.message = f"{RED}no account menu: {e}{OFF}"
            if not taken and rc == 0 and pick.exists():
                account = pick.read_text().strip()
        finally:
            pick.unlink(missing_ok=True)
            self.enter_screen()
            self.shown_at = time.monotonic()
        if not account:
            return
        if account not in DK.accounts():
            self.message = f"{RED}no such account: {account}{OFF}"
            return
        self.enter_model(account)

    def run(self) -> int:
        if not sys.stdin.isatty():
            print("claude-hub run: needs a terminal", file=sys.stderr)
            return 2
        try:
            os.chdir(WORKDIR)
        except OSError:
            pass
        hidden = os.environ.pop("CLAUDE_HUB_HUB_HIDDEN", "") == "1"
        self.konsole_pid = DK.konsole_above(os.getpid())

        def bye(*_):
            sys.exit(0)
        signal.signal(signal.SIGUSR1, lambda *_: self.on_signal("toggle"))
        signal.signal(signal.SIGUSR2, lambda *_: self.on_signal("hide"))
        signal.signal(signal.SIGWINCH, lambda *_: self.on_signal("resize"))
        signal.signal(signal.SIGHUP, bye)
        signal.signal(signal.SIGTERM, bye)
        with show_lock():
            st = DK.hub_state()
            if DK.hub_alive(st) and int(st.get("pid") or 0) != os.getpid():
                print("claude-hub run: the popup already runs", file=sys.stderr)
                return 0
            self.visible = (not hidden) != bool(st.get("opening") and st.get("toggle_pending"))
            self.watch = str(st.get("watch") or "") if watch_konsole(st.get("watch")) else ""
            self.write_record()
        self.cooked = termios.tcgetattr(self.fd)
        try:
            self.enter_screen()
            set_tab_title(TITLE)
            hide_menu_bar()
            if self.visible:
                self.shown_at = time.monotonic()
                self.prepare()
            threading.Thread(target=self.settle, daemon=True).start()
            threading.Thread(target=self.refresher, daemon=True).start()
            while True:
                if self.visible and self.dirty:
                    try:
                        self.draw()
                    except EOFError:
                        raise
                    except Exception as e:                 # noqa: BLE001 — a bad board must not kill the popup
                        with self.lock:
                            self.message, self.dirty = f"{REFRESH_FAILED}{e}{OFF}", True
                k = self.read_key(0.2)
                if k == "signal":
                    self.take_signals()
                elif k != "tick":
                    try:
                        self.on_key(k)
                    except EOFError:
                        raise
                    except Exception as e:                 # noqa: BLE001 — a bad key or board must not kill it
                        with self.lock:
                            self.message, self.dirty = f"{REFRESH_FAILED}{e}{OFF}", True
                self.confirm_show()
        except EOFError:
            return 0
        finally:
            self.leave_screen()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "show":
        return cmd_show()
    if cmd == "start":
        return cmd_start()
    if cmd == "hide":
        return cmd_hide()
    if cmd == "run":
        return Popup().run()
    if cmd == "list":
        return cmd_list("--json" in argv)
    print(next(p for p in __doc__.split("\n\n") if p.lstrip().startswith("claude-hub show")))
    return 2
