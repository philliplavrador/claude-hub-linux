# claude-hub, part 1: the desktop module, constants, the popup's record, its KWin window, show / start / hide.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


def _load_desktop():
    path = Path(__file__).resolve().parent / "claude-desks"
    if not path.exists() and shutil.which("claude-desks"):
        path = Path(shutil.which("claude-desks")).resolve()
    loader = importlib.machinery.SourceFileLoader("claude_desks", str(path))
    spec = importlib.util.spec_from_loader("claude_desks", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod, path


DK, DESKTOP_CMD = _load_desktop()


STATE = DK.HUB_STATE
WORKDIR = os.environ.get("CLAUDE_HUB_WORKDIR") or DK.WORKDIR
REFRESH = float(os.environ.get("CLAUDE_HUB_REFRESH") or 0.5)
SWEEP = float(os.environ.get("CLAUDE_HUB_SWEEP") or 2.0)
FRESH_FOR = 5.0
FOCUS_GRACE = 0.5
SHOW_CONFIRM = 0.6
OPENING_FOR = 15.0
PAGE = 10
TITLE = DK.HUB_TITLE
SERVICE = ("org.claudehub.Desktops", "/Desktops", "org.claudehub.Desktops")

BOLD, DIM, OFF, REV = "\x1b[1m", "\x1b[2m", "\x1b[0m", "\x1b[7m"
STRIKE, NOSTRIKE = "\x1b[9m", "\x1b[29m"
HAND_NAMED, NOHAND_NAMED = "\x1b[38;5;111m", "\x1b[39m"
GREEN, YELLOW, BLUE, CYAN, RED = "\x1b[32m", "\x1b[33m", "\x1b[34m", "\x1b[36m", "\x1b[31m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
REFRESH_FAILED = f"{RED}refresh failed: "
NOT_ANSWERING = "can't see your screens right now: window tiling isn't answering — try again in a moment"
SERVICE_DOWN = "can't see your screens: the desktop service isn't running"



def write_state(fields: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(fields, indent=1))
    os.replace(tmp, STATE)


@contextmanager
def show_lock():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_name(STATE.name + ".lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def starting(st: dict) -> bool:
    opening = float(st.get("opening") or 0)
    return bool(opening) and time.time() - opening < OPENING_FOR and DK.pid_alive(st.get("opening_pid"))



POPUP_JS = r"""
function lsPopupFlags(w) {
  w.skipTaskbar = true;
  w.skipSwitcher = true;
  w.skipPager = true;
  w.keepAbove = true;
  w.noBorder = false;   // a title bar like every other Konsole (as requested)
}
function lsPopupFrame(a) {
  var width = Math.min(a.width, Math.max(900, Math.min(1400, Math.floor(a.width * 3 / 5))));
  var height = Math.min(a.height, Math.max(480, Math.min(900, Math.floor(a.height * 3 / 5))));
  return {x: a.x + Math.floor((a.width - width) / 2), y: a.y + Math.floor((a.height - height) / 2),
          width: width, height: height};
}
"""


def script_show(konsole_pid: int) -> str:
    return DK.JS_LIB + POPUP_JS + (
        "var w = lsWindow(%d, 'Claude hub');\n"
        "if (w) {\n"
        "  w.desktops = [workspace.currentDesktop];\n"
        "  if (w.minimized) w.minimized = false;\n"
        "  lsPopupFlags(w);\n"
        "  w.frameGeometry = lsPopupFrame(workspace.clientArea(KWin.PlacementArea, w));\n"
        "  workspace.activeWindow = w;\n"
        "}\n" % int(konsole_pid or 0))


def script_hide(konsole_pid: int) -> str:
    return DK.JS_LIB + POPUP_JS + (
        "var w = lsWindow(%d, 'Claude hub');\n"
        "if (w) { w.minimized = true; lsPopupFlags(w); }\n" % int(konsole_pid or 0))


WATCH_FOR = 60


def script_watch(konsole_pid: int) -> str:
    return DK.JS_LIB + POPUP_JS + (
        "var lsPopupPid = %d, lsHidden = false, lsUntil = Date.now() + %d;\n"
        "function lsHideNew(w) {\n"
        "  if (lsHidden || Date.now() > lsUntil || !w || !w.normalWindow || w.pid !== lsPopupPid) return;\n"
        "  lsHidden = true;\n"
        "  w.minimized = true;\n"
        "  lsPopupFlags(w);\n"
        "}\n"
        "workspace.windowAdded.connect(lsHideNew);\n"
        "var lsThere = workspace.windowList();\n"
        "for (var i = 0; i < lsThere.length; i++) lsHideNew(lsThere[i]);\n" % (int(konsole_pid), WATCH_FOR * 1000))


def watch_hidden(konsole_pid: int) -> str:
    if int(konsole_pid or 0) <= 0:
        return ""
    name = f"claude-hub-watch-{int(konsole_pid)}-{time.time_ns()}"
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"{name}.js"
    try:
        path.write_text(script_watch(konsole_pid))
    except OSError:
        return ""
    try:
        with DK.kwin_script_turn():
            sid = DK.load_script(path, name)
            if not sid:
                return ""
            ran = DK.qdbus("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run")
            return name if ran and ran.returncode == 0 else ""
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def watch_konsole(name: str) -> int:
    m = re.fullmatch(r"claude-hub-watch-(\d+)-\d+", str(name or ""))
    return int(m.group(1)) if m else 0


def record_konsole(st: dict) -> int:
    return int(st.get("konsole_pid") or st.get("opening_pid") or 0)


def ensure_watch(st: dict, konsole_pid: int) -> str:
    pid = int(konsole_pid or 0)
    name = str(st.get("watch") or "")
    if name and watch_konsole(name) == pid and record_konsole(st) == pid and DK.pid_alive(pid):
        return name
    return watch_hidden(pid)


def script_pin(konsole_pid: int) -> str:
    return DK.JS_LIB + (
        "var w = lsWindow(%d, 'Claude hub');\n"
        "if (w) w.onAllDesktops = true;\n" % int(konsole_pid or 0))


def keep_out_of_taskbar() -> None:
    wid = os.environ.get("WINDOWID", "")
    if not wid.isdigit() or os.environ.get("WAYLAND_DISPLAY") or not os.environ.get("DISPLAY"):
        return
    try:
        subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "add,skip_taskbar,skip_pager"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:                                    # noqa: BLE001
        pass


def set_tab_title(title: str) -> None:
    svc, ses = os.environ.get("KONSOLE_DBUS_SERVICE"), os.environ.get("KONSOLE_DBUS_SESSION")
    if svc and ses:
        DK.qdbus(svc, ses, "org.kde.konsole.Session.setTitle", "1", title, timeout=3)


def hide_menu_bar() -> None:
    svc = os.environ.get("KONSOLE_DBUS_SERVICE")
    if not svc:
        return
    win, actions = "/konsole/MainWindow_1", "/konsole/MainWindow_1/actions"
    DK.qdbus(svc, f"{actions}/options_show_menubar", "org.qtproject.Qt.QAction.setChecked", "true", timeout=3)
    DK.qdbus(svc, win, "org.kde.KMainWindow.activateAction", "options_show_menubar", timeout=3)
    DK.qdbus(svc, f"{actions}/hamburger_menu", "org.qtproject.Qt.QAction.setVisible", "false", timeout=3)


def open_popup(env: dict) -> int:
    cmd = ["konsole", "--separate", "--workdir", WORKDIR, "-p", "ShowTerminalSizeHint=false",
           "-p", f"tabtitle={TITLE}", "-e", "claude-hub", "run"]
    if shutil.which("systemd-run", path=env.get("PATH")):
        cmd = ["systemd-run", "--user", "--scope", "--slice=app.slice", "-q", "--", *cmd]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, env=env).pid


def poke() -> None:
    def call() -> None:
        try:
            subprocess.run(["gdbus", "call", "--session", "--dest", SERVICE[0], "--object-path", SERVICE[1],
                            "--method", f"{SERVICE[2]}.Poke"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:                            # noqa: BLE001
            pass
    threading.Thread(target=call, daemon=True).start()



def from_the_hidden_screen() -> bool:
    return not os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY", "").split(".")[0] == DK.HIDDEN


def signal_popup(st: dict, sig: int) -> int:
    try:
        os.kill(int(st["pid"]), sig)
    except (OSError, KeyError, TypeError, ValueError) as e:
        print(f"claude-hub: could not reach the popup: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_show() -> int:
    if from_the_hidden_screen():
        print("claude-hub: no desktop to show the popup on (this is the hidden screen)", file=sys.stderr)
        return 1
    with show_lock():
        st = DK.hub_state()
        if DK.hub_alive(st):
            return signal_popup(st, signal.SIGUSR1)
        if starting(st):
            st["toggle_pending"] = not st.get("toggle_pending")
            if bool(st.get("hidden")) != st["toggle_pending"]:
                st["watch"] = ensure_watch(st, int(st.get("opening_pid") or 0))
            write_state(st)
            return 0
        env = DK.desktop_env()
        if DK.on_hidden_screen(env):
            print("claude-hub: no desktop to open the popup on", file=sys.stderr)
            return 1
        write_state({"opening": time.time(), "opening_pid": open_popup(env)})
    return 0


def cmd_start() -> int:
    if from_the_hidden_screen():
        print("claude-hub: no desktop to open the popup on (this is the hidden screen)", file=sys.stderr)
        return 1
    with show_lock():
        st = DK.hub_state()
        if DK.hub_alive(st) or starting(st):
            return 0
        env = DK.desktop_env()
        if DK.on_hidden_screen(env):
            print("claude-hub: no desktop to open the popup on", file=sys.stderr)
            return 1
        env["CLAUDE_HUB_HUB_HIDDEN"] = "1"
        pid = open_popup(env)
        write_state({"opening": time.time(), "opening_pid": pid, "hidden": True,
                     "watch": ensure_watch(st, pid)})
    return 0


def cmd_hide() -> int:
    if from_the_hidden_screen():
        print("claude-hub: no popup on the hidden screen", file=sys.stderr)
        return 1
    with show_lock():
        st = DK.hub_state()
        if DK.hub_alive(st):
            return signal_popup(st, signal.SIGUSR2)
        if starting(st):
            st["toggle_pending"] = not st.get("hidden")
            st["watch"] = ensure_watch(st, int(st.get("opening_pid") or 0))
            write_state(st)
            return 0
    print("claude-hub: no popup is running", file=sys.stderr)
    return 1
