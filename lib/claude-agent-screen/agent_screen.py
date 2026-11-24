"""claude-agent-screen: agents never put a window on your screen.

The hidden screen (:20) is where agents work. Routing alone kept failing open (a move to Wayland once sent Chromium to
the visible screen for a day without anyone noticing), so this adds three enforcement layers on top of it, each
independent of the others:

  session-env   SessionStart hook. Writes $CLAUDE_ENV_FILE so every Bash command a
                Claude session runs has no route to your screen: WAYLAND_DISPLAY gone,
                DISPLAY=:20, toolkits forced to X11, and agent-bin/ first on PATH so
                xdg-open, firefox and friends refuse instead of opening, and so do
                your panel and window programs run by name (the
                list under `guard`). Claude's own
                process keeps your Wayland socket, so image paste still works. The
                commands also run at nice 10, so an agent's tests and builds give
                the processor to the apps you are using first.
  guard         PreToolUse hook on Bash. Denies a command that explicitly aims at your
                screen (DISPLAY=:0, WAYLAND_DISPLAY=wayland-0), runs a desktop opener, or runs
                one of your programs that open a panel or a window where you are
                looking (the table SCREEN_TOOLS, empty here: one entry per program
                of yours; the verbs that open nothing and a run sent to the hidden
                screen on the command itself pass; a run by path, and one inside `bash -c '...'`, `bash -lc`, `sh -c` or `eval '...'`,
                which a login shell sends past the shims, are read as well). It also denies a
                command that could signal more than the command started: `ps -e`
                plus a selector (lists everything), `kill -1`, a kill fed by `xargs` or `$(...)`,
                and a script or inline code the command runs that sends a signal
                (read from the file, pattern matching, see "signals" below). The
                advice is to run it from a terminal of your own, since it could end
                processes outside the agent's own. A command that runs one of
                the two tools loading code from a lib/<name>/ folder by path also has
                that folder compared with HEAD, file by file, and is refused when it
                cannot be read completely. Codex has no PreToolUse hook
                here, so none of this covers Codex; its own
                shell_environment_policy covers the screen part only.
  watch         The backstop, run by claude-agent-screen.service. The KWin script
                kwin/agent-screen-guard asks it about every new window; a window whose
                process descends from a Claude Code or Codex agent is minimized and
                focus goes back where it was. Minimized, not closed: nothing is lost,
                and it is still on the taskbar if you want it.

  kill [-f] NAME  kill only what is the agent's own -- on the hidden screen or
                descended from an agent. Anything on your session is spared and
                named. The sanctioned replacement for killing a desktop app by
                name, which has no idea whose window it is closing.

  verdict PID   what `watch` would say about a process (for testing)
  log           the last windows it tucked away
"""
from __future__ import annotations
import bisect, io, json, os, re, signal, stat, sys, time, tokenize
from pathlib import Path
from typing import NamedTuple

HOME = Path.home()
_HERE = Path(__file__).resolve()
REPO = _HERE.parents[2]
WORKSPACE = REPO
HIDDEN = os.environ.get("CLAUDE_HUB_HIDDEN_DISPLAY", ":20")
AGENT_BIN = _HERE.parents[2] / "agent-bin"
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "claude-hub"
LOG = STATE / "agent-screen.log"
BUS_NAME = "org.claudehub.AgentScreen"
OBJ_PATH = "/AgentScreen"


ENV_LINES = f"""\
# claude-agent-screen: this session's commands have no route to his screen
unset WAYLAND_DISPLAY WAYLAND_SOCKET
export DISPLAY={HIDDEN}
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb SDL_VIDEODRIVER=x11
export MOZ_ENABLE_WAYLAND=0 MOZ_NO_REMOTE=1 ELECTRON_OZONE_PLATFORM_HINT=x11
export BROWSER={AGENT_BIN}/xdg-open
export CLAUDE_HUB_AGENT_SCREEN=1
case ":$PATH:" in *":{AGENT_BIN}:"*) ;; *) export PATH="{AGENT_BIN}:$PATH" ;; esac
# agents' commands (tests, builds, servers) yield the processor to the user's own apps, so those stay
# responsive. No disk priority: under BFQ it measured no better for the user's apps.
renice -n 10 -p $$ >/dev/null 2>&1 || true
"""


def session_env() -> int:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    target = os.environ.get("CLAUDE_ENV_FILE")
    if not target:
        return 0
    try:
        with open(target, "a+") as f:
            f.seek(0)
            if ENV_LINES in f.read():
                return 0
            f.write(ENV_LINES)
    except OSError as e:
        print(f"claude-agent-screen: could not write {target}: {e}", file=sys.stderr)
    return 0


OPENERS = (r"xdg-open|sensible-browser|x-www-browser|www-browser|gnome-www-browser|"
           r"kde-open5?|kfmclient|gtk-launch|firefox(?:-esr)?|google-chrome(?:-stable)?|"
           r"chromium(?:-browser)?|brave-browser|microsoft-edge|konqueror|falkon|"
           r"gio\s+open|kioclient5?\s+exec|kstart5?|plasma-open-settings")
CMD_START = r"(?:^|[;&|(`{\n]|\$\(|\b(?:exec|nohup|setsid|env|sudo|command|timeout\s+\S+|xargs(?:\s+-\S+)*)\s)"
CMD_GAP = r"[^\S\n]*"
CMD_ASSIGN = (r"[A-Za-z_][A-Za-z0-9_]*=(?:[^\s;&|(`{]|[;&|(`{](?![A-Za-z_][A-Za-z0-9_]*=|\s|$))*+"
              r"(?<!\bexec)(?<!\bnohup)(?<!\bsetsid)(?<!\benv)(?<!\bsudo)(?<!\bcommand)[^\S\n]+")
CMD_PATH = r"(?:[^\s/;&|(`{]*+/)*"
ADVICE = ("Agents never put a window on the user's screen. For a browser check use "
          "the Playwright or Chrome DevTools tools, or a plain `npx playwright` run: they open on "
          "the hidden screen automatically. To show him something, print the URL as text.")

DBUS_TOOL = r"\b(?:gdbus|qdbus6?|dbus-send|busctl)\b"
ACCEL_WRITE = (r"setShortcut|setForeignShortcut|setInactive|doRegister|unRegister|unregister|"
               r"blockGlobalShortcuts|activateGlobalShortcutContext|yourShortcut|cleanUp|"
               r"Properties\.Set")
ACCEL_ADVICE = ("A D-Bus write to org.kde.kglobalaccel crashes kwin_wayland and leaves the window "
                "keys dead until the user logs out. Bind a key with "
                "`kwriteconfig6 --file kglobalshortcutsrc ...`, and say it needs a re-login. Reads "
                "of kglobalaccel are fine.")

GUI_APPS = (r"konsole|yakuake|dolphin|kate|kwrite|okular|gwenview|spectacle|krunner|"
            r"plasmashell|kwin_wayland|kwin_x11|kded6?|firefox|chromium|chrome|brave|"
            r"code|codium|codex|thunderbird|vlc|obs")
KILLERS = r"pkill|killall|kquitapp[56]?|xkill"
KILL_ADVICE = ("A kill by name has no idea which screen a process is on, so it takes his "
               "windows too. Use `claude-agent-screen kill <name>`: it kills only what is "
               "on the hidden screen or descends from an agent, and says what it refused. "
               "Killing something by the PID you started yourself is always fine.")

RAW_RULES = [
    (re.compile(CMD_START + CMD_GAP + CMD_PATH + r"(?:" + KILLERS + r")\b(?:\s+-\S++)*\s++[^;&|\n]*"
                r"\b(?:" + GUI_APPS + r")\b", re.M),
     "kills a desktop app by name, which reaches the user's windows as well as the agent's",
     KILL_ADVICE),
]

RULES = [
    (re.compile(CMD_START + CMD_GAP + r"(?:" + CMD_ASSIGN + r")*" + CMD_PATH + r"(?:" + OPENERS + r")(?=\s|$|[;&|)])", re.M),
     "runs a desktop opener, which puts a window on the user's screen", ADVICE),
    (re.compile(r"\bDISPLAY=['\"]?:(?!" + re.escape(HIDDEN.lstrip(":")) + r"\b)\d"),
     "points DISPLAY at the user's screen", ADVICE),
    (re.compile(r"\bWAYLAND_DISPLAY=['\"]?(?:\S*/)?wayland-\d"),
     "points WAYLAND_DISPLAY at the user's screen", ADVICE),
    (re.compile(r"(?s)^(?=.*" + DBUS_TOOL + r")(?=.*kglobalaccel)(?=.*(?:" + ACCEL_WRITE + r"))"),
     "writes to org.kde.kglobalaccel over D-Bus, which aborts the compositor", ACCEL_ADVICE),
]

_UNREAD = object()


class _Tool(NamedTuple):
    why: str
    advice: str
    opens: frozenset[str] = frozenset()
    passes: frozenset[str] | None = None
    bare: bool = True
    unreadable: bool = True
    always: bool = False
    var: str | None = None
    own_runtime: bool = False


def _screen_advice(instead: str, trial: str) -> str:
    lead = "Nothing sends that window to your own desktop: it leaves your shell before it draws."
    return " ".join(part for part in (lead, instead, trial) if part)


_ON_THE_RUN = "on the same command, in front of the program (an earlier `export` is not read)"


def _trial(var: str, name: str, what: str, verb: str) -> str:
    return f"To {what}, send it to the hidden screen {_ON_THE_RUN}: `{var}={HIDDEN} {name}{verb}`."


# One entry per program of yours that opens a panel or window where you are looking: the key is the command
# name after "claude-", the value says why a run is refused and what to do instead. None ships.
SCREEN_TOOLS: dict[str, _Tool] = {}

_SCREEN_NAMES = "|".join(re.escape(name) for name in SCREEN_TOOLS) or "(?!)"
_AFTER_NAME = r"(?=[\s;&|)<>`]|$)"
SCREEN_START = r"(?:" + CMD_START + r"|(?<![\w.\-/])time(?:[^\S\n]++-[^\s;&|()`]++){0,4}+[^\S\n]++)"
SCREEN_KEYWORDS = r"(?:(?:then|do|else|elif|if|while|until|!)[^\S\n]++)*+"
_SCREEN_WRAPPER = r"(?:exec|nohup|setsid|env|sudo|command|timeout|xargs|time)\s"
SCREEN_ASSIGN = (r"[A-Za-z_][A-Za-z0-9_]*=(?:\$\([^()\n]*+\)|`[^`\n]*+`|[^\s;&|(`{]"
                 r"|[;&|(`{](?![A-Za-z_][A-Za-z0-9_]*=|\s|$))*+"
                 r"(?<!\bexec)(?<!\bnohup)(?<!\bsetsid)(?<!\benv)(?<!\bsudo)(?<!\bcommand)[^\S\n]+")
_SCREEN_ASSIGNS = r"(?:" + SCREEN_ASSIGN + r"){0,16}+"
_SUBST_HEAD = r"(?:\$\([^()\n]*+\))?+"
_SCREEN_INTERPRETER = (r"(?P<interp>python[0-9.]*+|bash|sh|dash|zsh)"
                       r"(?P<iflags>(?:[^\S\n]++-[^\s;&|()`]++){0,8}+)[^\S\n]++" + _SUBST_HEAD + CMD_PATH)
_SCREEN_SYSTEMD_RUN = (r"(?P<sd>systemd-run)(?=\s)(?P<sdopts>(?:[^\S\n]++(?!" + _SCREEN_WRAPPER + r"|" + CMD_PATH
                       + r"claude-(?:" + _SCREEN_NAMES + r")" + _AFTER_NAME
                       + r")(?:\$\([^()\n]*+\)|[^\s;&|()`{])++){0,24}+)[^\S\n]++" + CMD_PATH)
_NOT_A_VALUE = r"(?![A-Za-z_][A-Za-z0-9_]*=)"
SCREEN_TOOL = re.compile(SCREEN_START + CMD_GAP + SCREEN_KEYWORDS + _SCREEN_ASSIGNS + _NOT_A_VALUE + _SUBST_HEAD + CMD_PATH
                         + r"(?:" + _SCREEN_INTERPRETER + r"|" + _SCREEN_SYSTEMD_RUN + r")?"
                         + r"(?P<name>claude-(?:" + _SCREEN_NAMES + r"))" + _AFTER_NAME, re.M)

_SCREEN_SHELL_FLAG = r"(?:-[A-Za-z]++(?<=[oO])[^\S\n]++[\w-]++|-[A-Za-z]++|--[a-z][a-z-]*+)"
_SCREEN_INLINE_SHELL = (r"(?:(?P<shell>bash|sh|dash|zsh)(?P<shflags>(?:[^\S\n]++" + _SCREEN_SHELL_FLAG + r"){1,8}+)|eval)"
                        r"[^\S\n]++(?P<body>'(?P<index>\d++)')(?=[\s;&|)<>`]|$)")
_SCREEN_INLINE_SYSTEMD_RUN = (r"(?P<sd>systemd-run)(?=\s)(?P<sdopts>(?:[^\S\n]++(?!" + _SCREEN_WRAPPER + r"|" + CMD_PATH
                              + r"(?:bash|sh|dash|zsh|eval)[^\S\n])(?:\$\([^()\n]*+\)|[^\s;&|()`{])++){0,24}+)[^\S\n]++"
                              + CMD_PATH)
SCREEN_INLINE = re.compile(SCREEN_START + CMD_GAP + SCREEN_KEYWORDS + _SCREEN_ASSIGNS + CMD_PATH
                           + r"(?:" + _SCREEN_INLINE_SYSTEMD_RUN + r")?" + _SCREEN_INLINE_SHELL, re.M)
_MAX_INLINE_DEPTH = 3

QUOTED_OR_COMMENT = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"|(?<![^\s;&|(])#[^\n]*")
CONTINUED = re.compile(r"(?<!\\)((?:\\\\)*+)\\\n")

_BLANKS = re.compile(r"[^\S\n]*+")
_REDIRECT = re.compile(r"(?:\d*+(?:<<<|<<-?|>>|>&|>\||<&|<>|>|<)|&>>?)[^\S\n]*+(?:[^\s;&|()<>`]++)?")
_ARGUMENT = re.compile(r"[^\s;&|()<>`]++")
_PLAIN_ARGUMENT = re.compile(r"[\w./:=+@%,-]+")
_SCREEN_SHELLS = {"bash", "sh", "dash", "zsh"}
_NOEXEC = re.compile(r"(?:^|\s)-[A-Za-z]*n[A-Za-z]*(?=\s|$)")
_ASSIGNED = re.compile(r"(?:(?<![\w=-])|(?<=--setenv=))([A-Za-z_]\w*)=(\S*)")


def _first_argument(text: str, pos: int):
    while True:
        pos = _BLANKS.match(text, pos).end()
        if pos >= len(text) or text[pos] in "\n;|)`":
            return None
        if text[pos] == "&" and not text.startswith("&>", pos):
            return None
        redirect = _REDIRECT.match(text, pos)
        if redirect:
            pos = redirect.end()
            continue
        word = _ARGUMENT.match(text, pos)
        if not word:
            return None
        return word.group() if _PLAIN_ARGUMENT.fullmatch(word.group()) else _UNREAD


def _opens(tool: _Tool, first) -> bool:
    if tool.always:
        return True
    if first is None:
        return tool.bare
    if first is _UNREAD:
        return tool.unreadable
    if tool.passes is not None:
        return first not in tool.passes
    return first in tool.opens


def _sent_to_hidden(tool: _Tool, before_name: str, inherited=()) -> bool:
    if tool.var is None:
        return False
    given = dict(inherited)
    given.update(_ASSIGNED.findall(before_name))
    if given.get(tool.var) != HIDDEN:
        return False
    if tool.own_runtime:
        folder = given.get("XDG_RUNTIME_DIR", "")
        return bool(folder) and not folder.startswith("/run/user/")
    return True


_PASSES_ENV = frozenset({"env", "nohup", "setsid", "exec", "command", "time", "timeout", "nice"})
_SHELL_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!"})
_PASSED_WORD = re.compile(r"[A-Za-z_]\w*=\S*+"
                          r"|--(?![iu])\S*+"
                          r"|-(?:(?![iuc])[A-Za-z0-9])++"
                          r"|\d[\d.]*+[smhd]?")
_LETTERS = re.compile(r"[a-z]*+")
_LOOK_BACK = 1024


def _handed_on_from(text: str, pos: int) -> int:
    if _LETTERS.match(text, pos).group() not in _PASSES_ENV:
        return pos
    low = max(0, pos - _LOOK_BACK)
    begin = max(text.rfind(mark, low, pos) for mark in ";&|()`{}\n") + 1
    if begin == 0 and low > 0:
        return pos
    front = text[begin:pos].split()
    if front and not text[pos - 1].isspace() and not front.pop().endswith("/"):
        return pos
    for word in reversed(front):
        if (word.rpartition("/")[2] not in _PASSES_ENV and word not in _SHELL_KEYWORDS
                and not _PASSED_WORD.fullmatch(word)):
            return pos
    return begin


_BUNDLE_C = re.compile(r"(?:^|\s)-[A-Za-z]*c[A-Za-z]*(?=\s|$)")
_DQ_ESCAPE = re.compile(r"\\(?:([$`\"\\])|\n)")


_QUOTED_PATH = re.compile(r"(?:[\w$~{}.+@%-]*+/)++claude-(?:" + _SCREEN_NAMES + r")")


def _quoted_path(found: re.Match) -> re.Match | None:
    quote, text = found.group(), found.string
    if found.start() and text[found.start() - 1] not in " \t\n;&|":
        return None
    if text[found.end():found.end() + 1] not in ("", " ", "\t", "\n", ";", "&", "|", ")", "<", ">"):
        return None
    return _QUOTED_PATH.fullmatch(quote, 1, len(quote) - 1)


def _unquoted(quote: str) -> str:
    inside = quote[1:-1]
    return inside if quote[0] == "'" else _DQ_ESCAPE.sub(lambda m: m.group(1) or "", inside)


def _screen_tool_verdict(written: str, inherited=(), depth: int = 0) -> tuple[str, str] | None:
    if "claude-" not in written:
        return None
    quotes: list[str] = []
    quoted_path = False

    def blank(found: re.Match) -> str:
        nonlocal quoted_path
        quote = found.group()
        if quote[0] not in "'\"":
            return ""
        if "claude-" not in quote:
            return "''"
        quoted_path = quoted_path or bool(_quoted_path(found))
        quotes.append(quote)
        return f"'{len(quotes) - 1}'"

    def bare(found: re.Match) -> str:
        quote = found.group()
        if quote[0] not in "'\"":
            return ""
        path = _quoted_path(found)
        return path.group().replace("{", "").replace("}", "") if path else "''"

    text = CONTINUED.sub(r"\1 ", QUOTED_OR_COMMENT.sub(blank, written))
    readings = [text]
    if quoted_path:
        readings.append(CONTINUED.sub(r"\1 ", QUOTED_OR_COMMENT.sub(bare, written)))
    for reading in readings:
        for found in SCREEN_TOOL.finditer(reading):
            tool = SCREEN_TOOLS[found.group("name")[len("claude-"):]]
            if found.group("interp") in _SCREEN_SHELLS and _NOEXEC.search(found.group("iflags")):
                continue
            if not _opens(tool, _first_argument(reading, found.end())):
                continue
            behind_unit = bool(found.group("sd"))
            handed = () if behind_unit else inherited
            first_word = found.start("sdopts") if behind_unit else found.start()
            if _sent_to_hidden(tool, reading[first_word:found.start("name")], handed):
                continue
            earlier = first_word if behind_unit else _handed_on_from(reading, first_word)
            if earlier < first_word and _sent_to_hidden(tool, reading[earlier:found.start("name")], handed):
                continue
            return tool.why, tool.advice
    if quotes and depth < _MAX_INLINE_DEPTH:
        return _inline_verdict(text, quotes, inherited, depth)
    return None


def _inline_verdict(text: str, quotes: list[str], inherited, depth: int) -> tuple[str, str] | None:
    try:
        for found in SCREEN_INLINE.finditer(text):
            if found.group("shell"):
                flags = found.group("shflags")
                if not _BUNDLE_C.search(flags) or _NOEXEC.search(flags):
                    continue
            behind_unit = bool(found.group("sd"))
            given = {} if behind_unit else dict(inherited)
            first_word = found.start("sdopts") if behind_unit else _handed_on_from(text, found.start())
            given.update(_ASSIGNED.findall(text[first_word:found.start("body")]))
            body = HEREDOC.sub("", _unquoted(quotes[int(found.group("index"))]))
            hit = _screen_tool_verdict(body, given, depth + 1)
            if hit:
                return hit
    except Exception as e:
        _log(f"guard inline-shell rule error: {e!r}")
    return None


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.S | re.M)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


GUARD_BUDGET = 3.0
TOO_SLOW = ("is too long or too repetitive for the guard to read within its time limit",
            "A guard that runs out of time lets a command through unread, so this one is refused instead. "
            "Split it into shorter commands, or write the long part to a file and pass the file.")


class _OverBudget(BaseException):
    ...


def guard_verdict(cmd: str, cwd: str | None = None) -> tuple[str, str] | None:
    done = False

    def late(signum, frame):
        nonlocal done
        if not done:
            done = True
            raise _OverBudget

    before = signal.getsignal(signal.SIGALRM)
    caller = (0.0, 0.0)
    start = time.monotonic()
    try:
        try:
            signal.signal(signal.SIGALRM, late)
        except ValueError:
            return _guard_verdict(cmd, cwd)
        caller = signal.setitimer(signal.ITIMER_REAL, GUARD_BUDGET)
        hit = _guard_verdict(cmd, cwd)
        done = True
        return hit
    except _OverBudget:
        _log(f"guard over its time limit ({GUARD_BUDGET:g} s), refused: {len(cmd)} chars: {cmd[:200]!r}")
        return TOO_SLOW
    finally:
        done = True
        if signal.getsignal(signal.SIGALRM) is late:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before if before is not None else signal.SIG_DFL)
            if caller[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, max(caller[0] - (time.monotonic() - start), 1e-6), caller[1])


def _guard_verdict(cmd: str, cwd: str | None = None) -> tuple[str, str] | None:
    written = HEREDOC.sub("", cmd)
    for rx, why, advice in RAW_RULES:
        if rx.search(written):
            return why, advice
    bare = QUOTED.sub("''", written)
    for rx, why, advice in RULES:
        if rx.search(bare):
            return why, advice
    try:
        hit = _screen_tool_verdict(written)
    except Exception as e:
        _log(f"guard screen-tool rule error: {e!r}")
        hit = None
    if hit:
        return hit
    try:
        return _signal_verdict(cmd, cwd)
    except Exception as e:
        _log(f"guard rule error: {e!r}")
        return None



_MAX_FILES = 10
_MAX_BYTES = 1 << 20
_MAX_CMD = 1 << 20
_MAX_DEPTH = 4
_BUDGET = 1.0
_SNIPPET = 120
_LINE_CAP = 4000
_LINE_STRIDE = 3600
_PS_WINDOW = 32
_PS_MATCHES = 50

_LIB_TOOLS = ("claude-hub", "claude-agent-screen")
_PACKAGE_TOOLS = ()
_LIB_BUDGET = 1.0
_MAX_PART_READS = 40
_MAX_LIB_ENTRIES = 1000
_MAX_LIB_RUNS = 64
_MAX_VERSIONS = 16
_MAX_UNKNOWN = 64

_PS_ADVICE = ("`-e` selects every process and procps adds selections together, so a selector does not "
              "narrow it: a probe once killed each pid such a listing printed and ended a whole "
              "session. To list one session's processes, drop `-e`, e.g. "
              "`ps -o pid= --sid X`.")
_BOX_ADVICE = ("Run it from a terminal of your own, since it could end processes outside your own. "
               "To stop a process you started earlier in another command, kill it by its "
               "recorded PID from the Bash tool.")
_CMD_WHY = {
    "ps": "lists every process of his (`ps -e` plus a selector selects everything)",
    "all": "signals every process it can reach, or a whole process group (kill -1, kill -$$)",
    "xargs": "kills whatever pids are piped to it, which can include his whole session",
    "computed": "kills a pid list worked out at run time, which can include his whole session",
}

_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_PLAIN = re.compile(r"[^ \t\r\f\v\n\\'\"`$;&|()<>]+")
_DQ_PLAIN = re.compile(r'[^"\\$`]+')
_REDIR = re.compile(r">>|>&|>\||<&|<>|>|<")
_DUP_WORD = re.compile(r"\d+-?|-")
_HEREDOC_WORD = re.compile(r"""(?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"[^"]*")+""", re.S)
_EXPANSION = re.compile(r"\\.|\$\(\(|\$\(|\$\{|`", re.S)
_PARAM_SPECIAL = re.compile(r"\\.|\$\(\(|\$\(|\$\{|`|'|\"|\}", re.S)
_QUOTED_SPECIAL = re.compile(r"\\.|\$\(\(|\$\(|\$\{|`|\"", re.S)
_ARITH_SPECIAL = re.compile(r"[()\\'\"]")


def _heredoc_delim(word: str) -> tuple[str, bool]:
    out: list[str] = []
    quoted = False
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "\\":
            quoted = True
            if i + 1 < n:
                out.append(word[i + 1])
            i += 2
        elif c in "'\"":
            quoted = True
            j = word.find(c, i + 1)
            j = n if j < 0 else j
            out.append(word[i + 1:j])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), quoted


_ASSIGN_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")


class _Seg:
    __slots__ = ("words", "pos", "end", "bodies", "strings", "outs", "fd_outs", "ins", "body_at", "spans",
                 "string_spans", "marks", "tails", "drops")

    def __init__(self, pos: int):
        self.words: list[tuple[str, bool]] = []
        self.pos = pos
        self.end = pos
        self.bodies: list[str] = []
        self.strings: list[str] = []
        self.body_at: list[int] = []
        self.spans: list[tuple[int, int]] = []
        self.string_spans: list[tuple[int, int]] = []
        self.marks: list[int] = []
        self.tails: list[int] = []
        self.drops: frozenset[int] | set[int] = frozenset()
        self.outs: list[str] = []
        self.fd_outs: list[str] = []
        self.ins: list[str] = []


class _Lexer:

    def __init__(self, text: str, deadline: float):
        self.t = text
        self.out: list[_Seg] = []
        self.pending: list[tuple[str, bool, bool, _Seg]] = []
        self.dropped: set[int] = set()
        self.deadline = deadline
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks % 64 == 0 and time.monotonic() > self.deadline:
            raise TimeoutError("reading the command took too long")

    def backtick_end(self, i: int, end: int) -> int:
        t = self.t
        while i < end:
            if t[i] == "\\":
                i += 2
                continue
            if t[i] == "`":
                return i
            i += 1
        return end

    def heredocs(self, i: int, end: int, depth: int) -> int:
        t = self.t
        pending, self.pending = self.pending, []
        for delim, strip, quoted, seg in pending:
            tabs = r"\t*" if strip else ""
            m = re.compile("^" + tabs + re.escape(delim) + "$", re.M).search(t, i, end)
            stop = m.start() if m else end
            body, nxt = t[i:stop], (min(m.end() + 1, end) if m else end)
            if not quoted:
                self.expansions(i, stop, depth)
            if strip:
                body = re.sub(r"(?m)^\t+", "", body)
            seg.bodies.append(body)
            seg.body_at.append(i)
            i = nxt
        return i

    def deeper(self, depth: int) -> None:
        if depth > 12:
            raise ValueError("command nested too deep")

    def expansions(self, i: int, end: int, depth: int) -> None:
        t = self.t
        self.deeper(depth)
        while i < end:
            m = _EXPANSION.search(t, i, end)
            if not m:
                return
            self.tick()
            tok = m.group()
            if tok in ("`", "$((", "$(", "${"):
                i, _ = self.inner(tok, m.start(), m.end(), end, depth, True)
            else:
                i = m.end()
            self.pending = []

    def inner(self, tok: str, start: int, i: int, end: int, depth: int, in_dq: bool) -> tuple[int, bool]:
        t = self.t
        if tok == "`":
            j = self.backtick_end(i, end)
            self.scan(i, j, depth + 1, None)
            return j + 1, True
        if tok == "$((":
            e = self.arith(i, end, depth)
            if e >= 0:
                return e, False
            return self.scan(start + 2, end, depth + 1, ")"), True
        if tok == "$(":
            return self.scan(i, end, depth + 1, ")"), True
        return self.param(i, end, depth + 1, in_dq)

    def arith_end(self, i: int, end: int) -> int:
        t = self.t
        d = 0
        while i < end:
            m = _ARITH_SPECIAL.search(t, i, end)
            if not m:
                return -1
            self.tick()
            c = m.group()
            i = m.end()
            if c == "\\":
                i += 1
            elif c in "'\"":
                j = i
                while j < end and t[j] != c:
                    j += 2 if (c == '"' and t[j] == "\\") else 1
                i = j + 1
            elif c == "(":
                d += 1
            elif d == 0:
                return i + 1 if i < end and t[i] == ")" else -1
            else:
                d -= 1
        return -1

    def arith(self, i: int, end: int, depth: int) -> int:
        e = self.arith_end(i, end)
        if e >= 0:
            saved, self.pending = self.pending, []
            try:
                self.expansions(i, e - 2, depth + 1)
            finally:
                self.pending = saved
        return e

    def param(self, i: int, end: int, depth: int, in_dq: bool) -> tuple[int, bool]:
        self.deeper(depth)
        t = self.t
        ran = False
        while i < end:
            m = _PARAM_SPECIAL.search(t, i, end)
            if not m:
                return end, ran
            self.tick()
            tok = m.group()
            if tok == "}":
                return m.end(), ran
            if tok == "'":
                if in_dq:
                    i = m.end()
                else:
                    j = t.find("'", m.end(), end)
                    i = end if j < 0 else j + 1
            elif tok == '"':
                i, r = self.quoted(m.end(), end, depth + 1)
                ran = ran or r
            elif tok in ("`", "$((", "$(", "${"):
                i, r = self.inner(tok, m.start(), m.end(), end, depth, in_dq)
                ran = ran or r
            else:
                i = m.end()
        return end, ran

    def quoted(self, i: int, end: int, depth: int) -> tuple[int, bool]:
        self.deeper(depth)
        t = self.t
        ran = False
        while i < end:
            m = _QUOTED_SPECIAL.search(t, i, end)
            if not m:
                return end, ran
            self.tick()
            tok = m.group()
            if tok == '"':
                return m.end(), ran
            if tok in ("`", "$((", "$(", "${"):
                i, r = self.inner(tok, m.start(), m.end(), end, depth, True)
                ran = ran or r
            else:
                i = m.end()
        return end, ran

    def scan(self, i: int, end: int, depth: int, stop: str | None) -> int:
        if depth > 12:
            raise ValueError("command nested too deep")
        t = self.t
        seg: _Seg | None = None
        word: list[str] | None = None
        wpos = 0
        computed = False
        redir: str | None = None
        rfd: str | None = None
        nest = 0

        def flush() -> None:
            nonlocal seg, word, computed, redir, rfd
            if word is None:
                return
            w, c = "".join(word), computed
            word, computed = None, False
            if seg is None:
                seg = _Seg(wpos)
            seg.end = i
            if "\n" not in w:
                seg.marks.append(wpos)
                if t.find("\n", wpos, i) >= 0:
                    seg.marks.append(i - 1)
            elif not _ASSIGN_WORD.match(w):
                seg.tails.append(i)
            if redir is None:
                seg.words.append((w, c))
                seg.spans.append((wpos, i))
                return
            kind, fd = redir, rfd
            redir = rfd = None
            if kind == "dup":
                if _DUP_WORD.fullmatch(w):
                    return
                kind = "out"
            if kind == "out" and fd in (None, "1"):
                seg.outs.append(w)
            elif kind == "out":
                seg.fd_outs.append(w)
            elif kind == "rw":
                seg.fd_outs.append(w)
                if fd in (None, "0"):
                    seg.ins.append(w)
            elif kind == "in" and fd in (None, "0"):
                seg.ins.append(w)
            elif kind == "str":
                seg.strings.append(w)
                seg.string_spans.append((wpos, i))

        def finish() -> None:
            nonlocal seg, redir, rfd
            flush()
            redir = rfd = None
            if seg is not None and seg.words:
                self.out.append(seg)
            seg = None
            self.tick()

        while i < end:
            c = t[i]
            if c in " \t\r\f\v":
                flush()
                i += 1
            elif c == "\n":
                finish()
                i += 1
                if self.pending:
                    i = self.heredocs(i, end, depth)
            elif c == "#" and word is None:
                j = t.find("\n", i, end)
                i = end if j < 0 else j
            elif c == "\\":
                if i + 1 < end and t[i + 1] == "\n":
                    self.dropped.add(i + 1)
                    i += 2
                    continue
                if word is None:
                    word, wpos = [], i
                if i + 1 < end:
                    word.append(t[i + 1])
                i += 2
            elif c == "'":
                if word is None:
                    word, wpos = [], i
                j = t.find("'", i + 1, end)
                j = end if j < 0 else j
                word.append(t[i + 1:j])
                i = j + 1
            elif c == '"':
                if word is None:
                    word, wpos = [], i
                i += 1
                while i < end:
                    d = t[i]
                    if d == '"':
                        i += 1
                        break
                    if d == "\\":
                        nxt = t[i + 1] if i + 1 < end else ""
                        if nxt in ('"', "\\", "$", "`"):
                            word.append(nxt)
                        elif nxt != "\n":
                            word.append("\\" + nxt)
                        else:
                            self.dropped.add(i + 1)
                        i += 2
                    elif d == "$" and t.startswith("(", i + 1):
                        s = i
                        e = self.arith(i + 3, end, depth) if t.startswith("(", i + 2) else -1
                        i = e if e >= 0 else self.scan(i + 2, end, depth + 1, ")")
                        word.append(t[s:i])
                        computed = True
                    elif d == "`":
                        j = self.backtick_end(i + 1, end)
                        self.scan(i + 1, j, depth + 1, None)
                        word.append(t[i:j + 1])
                        i = j + 1
                        computed = True
                    elif d == "$" and t.startswith("{", i + 1):
                        s = i
                        i, ran = self.param(i + 2, end, depth + 1, True)
                        word.append(t[s:i])
                        computed = computed or ran
                    elif d == "$":
                        word.append("$")
                        i += 1
                    else:
                        m = _DQ_PLAIN.match(t, i, end)
                        word.append(m.group())
                        i = m.end()
            elif c == "$":
                if word is None:
                    word, wpos = [], i
                if t.startswith("(", i + 1):
                    s = i
                    e = self.arith(i + 3, end, depth) if t.startswith("(", i + 2) else -1
                    i = e if e >= 0 else self.scan(i + 2, end, depth + 1, ")")
                    word.append(t[s:i])
                    computed = True
                elif t.startswith("'", i + 1):
                    j = i + 2
                    while j < end and t[j] != "'":
                        j += 2 if t[j] == "\\" else 1
                    word.append(t[i + 2:j])
                    i = j + 1
                elif t.startswith("{", i + 1):
                    s = i
                    i, ran = self.param(i + 2, end, depth + 1, False)
                    word.append(t[s:i])
                    computed = computed or ran
                else:
                    word.append("$")
                    i += 1
            elif c == "`":
                if word is None:
                    word, wpos = [], i
                j = self.backtick_end(i + 1, end)
                self.scan(i + 1, j, depth + 1, None)
                word.append(t[i:j + 1])
                i = j + 1
                computed = True
            elif c == ";":
                finish()
                i += 1
            elif c == "&":
                if t.startswith(">", i + 1):
                    flush()
                    redir, rfd = "out", None
                    i += 3 if t.startswith(">>", i + 1) else 2
                else:
                    finish()
                    i += 2 if t.startswith("&", i + 1) else 1
            elif c == "|":
                finish()
                i += 2 if t[i + 1:i + 2] in ("|", "&") else 1
            elif c == "(":
                finish()
                nest += 1
                i += 1
            elif c == ")":
                finish()
                if stop == ")" and nest == 0:
                    return i + 1
                nest = max(0, nest - 1)
                i += 1
            elif c in "<>":
                if word is not None and not computed and "".join(word).isdigit():
                    rfd, word = "".join(word), None
                else:
                    flush()
                    rfd = None
                if t.startswith("<<<", i):
                    redir = "str"
                    i += 3
                elif t.startswith("<<", i):
                    j = i + 2
                    strip = t.startswith("-", j)
                    j += 1 if strip else 0
                    while j < end and t[j] in " \t":
                        j += 1
                    m = _HEREDOC_WORD.match(t, j, end)
                    delim, quoted = _heredoc_delim(m.group()) if m else ("", False)
                    j = m.end() if m else j
                    if seg is None:
                        seg = _Seg(i)
                    seg.marks.append(i)
                    self.pending.append((delim, strip, quoted, seg))
                    i = j
                elif t.startswith(("<(", ">("), i):
                    s = i
                    i = self.scan(i + 2, end, depth + 1, ")")
                    word, wpos = ["<()"], s
                else:
                    m = _REDIR.match(t, i)
                    tok = m.group()
                    i = m.end()
                    redir = {">&": "dup", "<&": "skip", "<>": "rw"}.get(tok) or ("out" if tok[0] == ">" else "in")
            else:
                if word is None:
                    word, wpos = [], i
                m = _PLAIN.match(t, i, end)
                if m:
                    word.append(m.group())
                    i = m.end()
                else:
                    word.append(c)
                    i += 1
        finish()
        return end


def _lex(text: str, deadline: float) -> list[_Seg]:
    lx = _Lexer(text, deadline)
    lx.scan(0, len(text), 0, None)
    lx.out.sort(key=lambda s: s.pos)
    for seg in lx.out:
        seg.drops = lx.dropped
    return lx.out


_RESERVED = frozenset(("!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until",
                       "time", "coproc"))
_WRAPPERS = {
    "env": (frozenset(("-u", "-C", "-S", "--unset", "--chdir", "--split-string")), 0),
    "nice": (frozenset(("-n", "--adjustment")), 0),
    "timeout": (frozenset(("-s", "-k", "--signal", "--kill-after")), 1),
    "nohup": (frozenset(), 0),
    "setsid": (frozenset(), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset(("-a",)), 0),
    "sudo": (frozenset(("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U")), 0),
    "doas": (frozenset(("-u", "-C")), 0),
    "stdbuf": (frozenset(("-i", "-o", "-e")), 0),
    "ionice": (frozenset(("-c", "-n", "-p", "-P", "-u")), 0),
    "xargs": (frozenset(("-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--eof",
                         "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars")), 0),
}


_RUNNERS = {"uv": ("run",), "npx": (), "bunx": (), "pnpm": ("exec", "dlx"), "npm": ("exec", "x"),
            "yarn": ("exec", "dlx"), "bun": ("x",)}
_RUNNER_TAKES = frozenset((
    "--with", "--with-editable", "--with-requirements", "--python", "-p", "--project", "--directory",
    "--env-file", "--group", "--only-group", "--extra", "--package", "--index", "--default-index",
    "--index-url", "-i", "--extra-index-url", "--find-links", "-f", "--exclude-newer", "--config-file",
    "--cache-dir", "--python-platform", "--refresh-package", "--reinstall-package", "--upgrade-package", "-P",
    "--prefix", "--cache", "--userconfig", "--workspace", "-w", "--filter", "--call", "-c", "--cwd", "--dir"))


def _runner_command(base: str, words: list[tuple[str, bool]], i: int, chdirs: list):
    n = len(words)
    j = i + 1
    last = None
    subs = _RUNNERS[base]
    if subs:
        while j < n and words[j][0].startswith("-") and len(words[j][0]) > 1 and not words[j][1]:
            t, c = words[j]
            if base == "uv" and t == "--directory" and j + 1 < n:
                last = words[j + 1]
            elif base == "uv" and t.startswith("--directory="):
                last = (t.split("=", 1)[1], c)
            j += 2 if t in _RUNNER_TAKES else 1
        if j >= n or words[j][1] or words[j][0] not in subs:
            return None
        j += 1
    while j < n:
        t, c = words[j]
        if t == "--":
            j += 1
            break
        if not (t.startswith("-") and len(t) > 1 and not c):
            break
        if base == "uv" and t == "--directory" and j + 1 < n:
            last = words[j + 1]
        elif base == "uv" and t.startswith("--directory="):
            last = (t.split("=", 1)[1], c)
        j += 2 if t in _RUNNER_TAKES else 1
    if last is not None:
        chdirs.append(last)
    if j >= n:
        return None
    return j, base == "uv" and not words[j][1] and words[j][0].endswith(".py")


def _chdir_option(base: str, t: str, c: bool, words: list[tuple[str, bool]], i: int):
    short, long_ = ("-C", "--chdir") if base == "env" else ("-D", "--chdir") if base == "sudo" else (None, None)
    if short is None:
        return None
    if t in (short, long_):
        return (words[i + 1], 2) if i + 1 < len(words) else ((".", True), 1)
    if t.startswith(long_ + "="):
        return (t.split("=", 1)[1], c), 1
    if len(t) > 2 and t.startswith(short):
        return (t[2:], c), 1
    return None


def _resolve(words: list[tuple[str, bool]]):
    i, n = 0, len(words)
    xargs = False
    chdirs: list[tuple[str, bool]] = []
    while i < n:
        text, comp = words[i]
        if comp or not text:
            return None
        if _ASSIGN.match(text) or text in _RESERVED:
            i += 1
            continue
        base = text.rsplit("/", 1)[-1]
        spec = _WRAPPERS.get(base)
        if spec is None:
            if base in _RUNNERS:
                found = _runner_command(base, words, i, chdirs)
                if found is not None:
                    j, as_python = found
                    if as_python:
                        return "python3", words[j:], xargs, chdirs
                    i = j
                    continue
            return text, words[i + 1:], xargs, chdirs
        takes, lead = spec
        i += 1
        mine = None
        while i < n:
            t, c = words[i]
            if t == "--":
                i += 1
                break
            if t.startswith("-") and len(t) > 1 and not c:
                where = _chdir_option(base, t, c, words, i)
                if where is not None:
                    mine = where[0]
                    i += where[1]
                else:
                    i += 2 if t in takes else 1
            elif base == "env" and _ASSIGN.match(t):
                i += 1
            elif lead:
                lead -= 1
                i += 1
            else:
                break
        if mine is not None:
            chdirs.append(mine)
        if base == "xargs":
            xargs = True
    return None


_PS_SELECT = frozenset("pgGsuUtCq")
_PS_LONG_SELECT = frozenset(("--sid", "--ppid", "--pid", "--group", "--Group", "--user", "--User", "--tty",
                             "--quick-pid"))
_PS_LONG_ARG = frozenset(("--sort", "--format", "--cols", "--columns", "--width", "--lines", "--rows"))


def _ps_lists_all(tokens: list[str]) -> bool:
    everything = selected = False
    i, n = 0, len(tokens)
    first = True
    while i < n:
        t = tokens[i]
        i += 1
        if t.startswith("--"):
            name, eq, _ = t.partition("=")
            if name in _PS_LONG_SELECT:
                selected = True
                i += 0 if eq else 1
            elif name in _PS_LONG_ARG and not eq:
                i += 1
        elif t.startswith("-") and len(t) > 1:
            for j, ch in enumerate(t[1:], 2):
                if ch in "eA":
                    everything = True
                elif ch in _PS_SELECT:
                    selected = True
                    i += 1 if j >= len(t) else 0
                    break
                elif ch in "oO":
                    i += 1 if j >= len(t) else 0
                    break
        else:
            if first and t.isalpha() and "a" in t and "x" in t:
                everything = True
            first = False
    return everything and selected


def _kill_info(args: list[tuple[str, bool]], xargs: bool) -> tuple[bool, bool, bool]:
    sig: str | None = None
    dd = False
    targets: list[tuple[str, bool]] = []
    i, n = 0, len(args)
    while i < n:
        t, comp = args[i]
        i += 1
        if dd or comp or not t.startswith("-") or t == "-":
            targets.append((t, comp))
        elif t == "--":
            dd = True
        elif _SELF_GROUP.fullmatch(t):
            targets.append((t, comp))
        elif t in ("-l", "-L", "--list", "--table", "-t"):
            return False, False, False
        elif t in ("-s", "-n", "--signal"):
            sig = args[i][0] if i < n else ""
            i += 1
        elif t.startswith("--signal="):
            sig = t[9:]
        elif t.startswith("-s") and len(t) > 2:
            sig = t[2:]
        elif re.fullmatch(r"-(?:\d+|[A-Za-z][A-Za-z0-9]*)", t):
            if sig is None:
                sig = t[1:]
            elif t[1:].isdigit():
                targets.append((t, comp))
    if sig is not None and sig.lstrip("-") in ("0", "EXIT"):
        return False, False, False
    targets = [(t, c) for t, c in targets if t not in _SELF_PIDS]
    if not targets and not xargs:
        return False, False, False
    everything = any(t == "-1" or _SELF_GROUP.fullmatch(t) for t, _ in targets)
    return True, everything, any(c and not _recorded_pid_source(t) for t, c in targets)


_SELF_PIDS = frozenset(("$$", "$BASHPID", "${BASHPID}", "${$}"))
_SELF_GROUP = re.compile(r"-(?:\$\$|\$BASHPID|\$\{BASHPID\}|\$\{\$\})")
_PIDFILE_SUBST = re.compile(r"""(?:\$\(|`)\s*(?:cat\s+(["']?)[^\s"'`$()|;&<>*?\[\]{}]*(?:\$\{?\w+\}?[^\s"'`$()|;&<>*?\[\]{}]*)*\1"""
                            r"""(?:\s+2>/dev/null)?|<\s*[^\s"'`$()|;&<>*?\[\]{}]+)\s*(?:\)|`)$""")


def _recorded_pid_source(word: str) -> bool:
    return bool(_PIDFILE_SUBST.fullmatch(word))


_PY_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?$")
_SHELLS = frozenset(("bash", "sh", "dash", "zsh", "ksh", "ash"))
_JS = frozenset(("node", "nodejs", "bun", "deno", "tsx", "ts-node", "ts-node-esm"))
_JS_ARG = frozenset(("-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions",
                     "--tsconfig", "--env-file", "-P", "--project", "-O", "--compiler-options"))


def _family(name: str) -> str | None:
    if _PY_NAME.match(name):
        return "py"
    if name in _SHELLS:
        return "sh"
    if name in _JS:
        return "js"
    if name in ("perl", "ruby"):
        return name
    return None


def _shebang_family(text: str) -> str | None:
    if not text.startswith("#!"):
        return None
    first = text.split("\n", 1)[0][2:].split()
    if not first:
        return None
    prog = first[0].rsplit("/", 1)[-1]
    if prog == "env":
        rest = [w for w in first[1:] if not w.startswith("-") and "=" not in w]
        prog = rest[0].rsplit("/", 1)[-1] if rest else ""
    return _family(prog)


class _Cut(tuple):
    ...


def _cut(w: tuple[str, bool], text: str) -> _Cut:
    c = _Cut((text, False))
    c.of = w
    return c


def _interp_target(fam: str, name: str, args: list[tuple[str, bool]]):
    n = len(args)
    i = 0
    code = False
    if fam == "js" and n and name in ("bun", "deno") and args[0][0] in ("run", "eval"):
        code = args[0][0] == "eval"
        i = 1
    elif fam == "js" and n and name == "tsx" and args[0][0] == "watch":
        i = 1
    while i < n:
        t, comp = args[i]
        i += 1
        if t == "--":
            return (("code" if code else "file"), args[i]) if i < n else None
        if t == "-":
            return "stdin", None
        if not (len(t) > 1 and not comp and (t[0] == "-" or (fam == "sh" and t[0] == "+"))):
            return ("code" if code else "file"), args[i - 1]
        if fam == "py":
            if t.startswith("--"):
                continue
            body = t[1:]
            for k, ch in enumerate(body):
                if ch == "c":
                    if k + 1 < len(body):
                        return "code", _cut(args[i - 1], body[k + 1:])
                    code = True
                    break
                if ch == "m":
                    return None
                if ch in "WX":
                    i += 1 if k + 1 >= len(body) else 0
                    break
        elif fam == "sh":
            if t in ("--rcfile", "--init-file"):
                i += 1
            elif not t.startswith("--"):
                for ch in t[1:]:
                    if ch == "c":
                        code = True
                    elif ch in "oO":
                        i += 1
                        break
        elif fam == "js":
            if t in _JS_ARG:
                i += 1
            elif t in ("--eval", "--print"):
                code = True
            elif t.startswith(("--eval=", "--print=")):
                return "code", _cut(args[i - 1], t.split("=", 1)[1])
            elif re.fullmatch(r"-[a-zA-Z]+", t) and t[-1] in "ep":
                code = True
        else:
            if t in ("-I", "-r"):
                i += 1
            elif re.fullmatch(r"-[a-zA-Z]+", t) and t[-1] in "eE":
                code = True
    return None if code else ("stdin", None)


def _home_expanded(text: str, ctx: "_Ctx") -> str | None:
    if text.startswith("~"):
        head, slash, rest = text.partition("/")
        if head == "~":
            return str(HOME) + slash + rest
        if head == "~+" and ctx.cwd:
            return ctx.cwd + slash + rest
        return None
    if text.startswith("$HOME/"):
        return str(HOME) + text[5:]
    if text.startswith("${HOME}/"):
        return str(HOME) + text[7:]
    return text


def _resolve_path(text: str, computed: bool, ctx: "_Ctx") -> str | None:
    if computed or not text:
        return None
    text = _home_expanded(text, ctx)
    if text is None or any(ch in text for ch in "$`*?[{<>|;&()"):
        return None
    if not text.startswith("/"):
        if not ctx.cwd:
            return None
        text = ctx.cwd + "/" + text
    return os.path.normpath(text)


_UNKNOWN_CHAR = re.compile(r"[$`*?\[{<>|;&()]")
_GLOB_CHAR = re.compile(r"[*?\[]")


def _partial_path(text: str, ctx: "_Ctx") -> str | None:
    if not text:
        return None
    text = _home_expanded(text, ctx)
    if text is None or any(ch in text for ch in "$`{"):
        return None
    found = _UNKNOWN_CHAR.search(text)
    lit = text if found is None else text[:found.start()]
    if found is not None:
        parts = text[lit.rfind("/") + 1:].split("/")
        if any(part == ".." or (part.startswith(".") and _GLOB_CHAR.search(part)) for part in parts):
            return None
    if not lit:
        return None
    if not lit.startswith("/"):
        if not ctx.cwd:
            return None
        lit = ctx.cwd + "/" + lit
    return os.path.normpath(lit)


def _cd_target(ctx: "_Ctx", args: list[tuple[str, bool]]) -> str | None:
    rest = [a for a in args if not (a[0].startswith("-") and a[0] != "-")]
    if not rest:
        return str(HOME)
    t, comp = rest[0]
    return None if comp or t == "-" else _resolve_path(t, comp, ctx)


_INSTALLED_RX = re.compile(r"^%s/(?:bin|toolkit)/" % re.escape(str(REPO)))
_GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(HOME),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
_GIT_WAIT = 0.5
_MAX_HASH = 8 << 20
_MAX_SOURCES = 20


def _listed_dir(path: str) -> bool:
    return bool(_INSTALLED_RX.match(path))


def _git_out(directory: str, argv: list[str], ctx: "_Ctx | None") -> bytes | None:
    left = _GIT_WAIT if ctx is None else min(_GIT_WAIT, ctx.deadline - time.monotonic())
    if left <= 0.01:
        return None
    import subprocess
    try:
        out = subprocess.run(["git", "-C", directory, *argv], capture_output=True, timeout=left,
                             stdin=subprocess.DEVNULL, env=_GIT_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _head_entry(real: str, ctx: "_Ctx | None") -> tuple[str, int] | None:
    if ctx is not None and real in ctx.head:
        return ctx.head[real]
    name = os.path.basename(real)
    entry = None
    out = _git_out(os.path.dirname(real), ["ls-tree", "-z", "-l", "HEAD", "--", name], ctx)
    for item in (out or b"").split(b"\0"):
        meta, _, got = item.partition(b"\t")
        fields = meta.split()
        if len(fields) == 4 and fields[1] == b"blob" and got.decode("utf-8", "replace") == name:
            try:
                entry = (fields[2].decode("ascii"), int(fields[3]))
            except ValueError:
                entry = None
            break
    if ctx is not None:
        ctx.head[real] = entry
    return entry


def _matches_blob(real: str, entry: tuple[str, int]) -> bool:
    blob, size = entry
    if size > _MAX_HASH:
        return False
    data = _file_bytes(real, size + 1)
    if data is None or len(data) != size:
        return False
    return _blob_id(data, blob) == blob


def _blob_id(data: bytes, like: str) -> str:
    import hashlib
    make = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return make(b"blob %d\0" % len(data) + data).hexdigest()


def _head_lines(real: str, ctx: "_Ctx") -> dict[str, int] | None:
    if real in ctx.head_lines:
        return ctx.head_lines[real]
    lines = None
    entry = _head_entry(real, ctx)
    if entry is not None and entry[1] <= _MAX_HASH:
        out = _git_out(os.path.dirname(real), ["cat-file", "blob", entry[0]], ctx)
        if out is not None:
            lines = {}
            for line in out.decode("utf-8", "replace").split("\n"):
                lines[line] = lines.get(line, 0) + 1
    ctx.head_lines[real] = lines
    return lines


def _new_lines(path: str, text: str, ctx: "_Ctx") -> set[int] | None:
    real = os.path.realpath(path)
    if path in ctx.written or _touched(ctx, path, real) or not _listed_dir(real):
        return None
    left = _head_lines(real, ctx)
    if left is None:
        return None
    return _lines_not_in(text, left)


def _lines_not_in(text: str, left: dict[str, int]) -> set[int]:
    left = dict(left)
    new: set[int] = set()
    for no, line in enumerate(text.split("\n"), 1):
        if left.get(line, 0) > 0:
            left[line] -= 1
        else:
            new.add(no)
    return new


def _installed_tool(path: str, ctx: "_Ctx | None" = None) -> bool:
    real = os.path.realpath(path)
    if ctx is not None:
        if _touched(ctx, path, real):
            return False
        if real in ctx.exempt:
            return ctx.exempt[real]
    verdict = _installed_verdict(real, ctx)
    if ctx is not None:
        ctx.exempt[real] = verdict
    return verdict


def _installed_verdict(real: str, ctx: "_Ctx | None") -> bool:
    try:
        st = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid == 0 and not st.st_mode & 0o002:
        return True
    if "node_modules" in real.split("/"):
        return True
    if not _listed_dir(real):
        return False
    entry = _head_entry(real, ctx)
    return entry is not None and _matches_blob(real, entry)


def _touched(ctx: "_Ctx", *paths: str) -> bool:
    if not ctx.touched:
        return False
    for p in paths:
        while True:
            if p in ctx.touched:
                return True
            parent = os.path.dirname(p)
            if parent == p or not parent:
                break
            p = parent
    return False


def _copy_sources(ctx: "_Ctx", path: str) -> list[str]:
    if not ctx.copies:
        return []
    found: list[str] = []
    for spelled in dict.fromkeys((path, os.path.realpath(path))):
        p = spelled
        while True:
            for src in ctx.copies.get(p, ()):
                found.append(src + spelled[len(p):])
            parent = os.path.dirname(p)
            if parent == p or not parent:
                break
            p = parent
    return found


def _texts(path: str, ctx: "_Ctx", whole: bool = True) -> list[tuple[str, str, set[int] | None]]:
    out: list[tuple[str, str, set[int] | None]] = []
    seen: set[str] = set()
    todo = [path]
    while todo and len(seen) < _MAX_SOURCES:
        p = todo.pop(0)
        if p in seen:
            continue
        seen.add(p)
        ctx.tick()
        ctx.note_run(os.path.realpath(p))
        ctx.visit(p)
        todo.extend(_copy_sources(ctx, p))
        if _installed_tool(p, ctx):
            continue
        text = ctx.written.get(p)
        if text is not None:
            out.append((p, text, None if whole else ctx.written_new.get(p)))
            out.extend((p, old, None) for old in ctx.older(p))
            continue
        text = ctx.read(p)
        if text is not None:
            out.append((p, text, _new_lines(p, text, ctx)))
        else:
            ctx.unreadable(p)
    if any(t not in seen for t in todo):
        ctx.cut = True
    return out


def _file_bytes(path: str, limit: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        got = 0
        while got < limit:
            b = os.read(fd, min(65536, limit - got))
            if not b:
                break
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_script(path: str, limit: int = _MAX_BYTES) -> str | None:
    data = _file_bytes(path, limit)
    if data is None or b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


class _Ctx:

    def __init__(self, cwd: str | None):
        self.cwd = cwd
        self.written: dict[str, str] = {}
        self.written_new: dict[str, set[int]] = {}
        self.touched: set[str] = set()
        self.copies: dict[str, list[str]] = {}
        self.exempt: dict[str, bool] = {}
        self.head: dict[str, tuple[str, int] | None] = {}
        self.head_lines: dict[str, dict[str, int] | None] = {}
        self.cache: dict[str, str | None] = {}
        self.reads = 0
        self.deadline = time.monotonic() + _BUDGET
        self.ticks = 0
        self.versions: dict[str, list[str]] = {}
        self.versions_over: set[str] = set()
        self.blind: set[str] = set()
        self.lib_runs: dict[str, None] = {}
        self.lib_over = False
        self.cut = False
        self.unknown: dict[tuple[str | None, bool], str] = {}

    def add_unknown(self, prefix: str | None, exact: bool, word: str = "") -> None:
        if (None, False) in self.unknown:
            return
        if prefix is None or len(self.unknown) >= _MAX_UNKNOWN:
            self.unknown = {(None, False): word}
        else:
            self.unknown.setdefault((prefix, exact), word)

    def visit(self, path: str) -> None:
        ...

    def unreadable(self, path: str) -> None:
        ...

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise TimeoutError("reading the command took too long")

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks % 32 == 0:
            self.check()

    def touch(self, path: str) -> None:
        self.touched.add(path)
        self.touched.add(os.path.realpath(path))

    def read(self, path: str) -> str | None:
        if path in self.cache:
            return self.cache[path]
        if self.reads >= _MAX_FILES:
            return None
        self.reads += 1
        text = _read_script(path)
        self.cache[path] = text
        return text

    def add_version(self, path: str, text: str) -> None:
        got = self.versions.setdefault(path, [])
        if len(got) >= _MAX_VERSIONS:
            self.versions_over.add(path)
        else:
            got.append(text)

    def note_run(self, real: str) -> None:
        if os.path.basename(real) not in _LIB_TOOLS or real in self.lib_runs:
            return
        if not _listed_dir(real) or os.path.basename(os.path.dirname(real)) != "bin":
            return
        if len(self.lib_runs) >= _MAX_LIB_RUNS:
            self.lib_over = True
        else:
            self.lib_runs[real] = None

    def older(self, path: str) -> list[str]:
        return []


_COPIERS = frozenset(("cp", "mv", "install", "ln", "rsync"))


def _record_copy(name: str, words: list[str], ctx: _Ctx) -> None:
    target = None
    rest: list[str] = []
    k = 0
    while k < len(words):
        t = words[k]
        k += 1
        if t in ("-t", "--target-directory") and k < len(words):
            target = words[k]
            k += 1
        elif t.startswith("--target-directory="):
            target = t.split("=", 1)[1]
        elif name != "rsync" and t.startswith("-") and not t.startswith("--") and len(t) > 1 and "t" in t[1:]:
            i = t.index("t", 1)
            if i + 1 < len(t):
                target = t[i + 1:]
            elif k < len(words):
                target = words[k]
                k += 1
        elif not t.startswith("-"):
            rest.append(t)
    if target is None:
        if not rest:
            return
        target, sources = rest[-1], rest[:-1]
    else:
        sources = rest
    dest = _resolve_path(target, False, ctx)
    if not dest:
        ctx.add_unknown(_partial_path(target, ctx), False, target)
        return
    ctx.touch(dest)
    symbolic = name == "ln" and any(w == "--symbolic" or (w.startswith("-") and not w.startswith("--") and "s" in w)
                                    for w in words)
    is_file = os.path.isfile(dest)
    for word in sources:
        src = _resolve_path(word, False, ctx)
        if not src:
            ctx.add_unknown(dest, True, word)
            continue
        keys = (dest,) if is_file else (dest, os.path.join(dest, os.path.basename(src)))
        for key in dict.fromkeys(keys + tuple(os.path.realpath(k) for k in keys)):
            found = ctx.copies.setdefault(key, [])
            found.append(src)
            if symbolic and not word.startswith("/"):
                found.append(os.path.normpath(os.path.join(os.path.dirname(key), word)))


def _record_writes(seg: _Seg, name: str, args: list[tuple[str, bool]], ctx: _Ctx,
                   fresh: "_Fresh | None" = None) -> None:
    for out in (*seg.outs, *seg.fd_outs):
        path = _resolve_path(out, False, ctx)
        if path:
            ctx.touch(path)
        else:
            ctx.add_unknown(_partial_path(out, ctx), False, out)
    words = [t for t, c in args if not c]
    plain = [t for t in words if not t.startswith("-")]
    marks: list[str] = []
    writes = False
    if name in _COPIERS:
        writes = True
        _record_copy(name, words, ctx)
    elif name == "tee":
        writes = True
        marks = plain
    elif name == "sed" and any(t.startswith("-i") or t == "--in-place" or t.startswith("--in-place=") for t in words):
        writes = True
        scripted = any(t in ("-e", "-f", "--expression", "--file") or t.startswith(("--expression=", "--file="))
                       for t in words)
        marks = plain if scripted else plain[1:]
    elif name == "dd":
        writes = True
        marks = [t[3:] for t in words if t.startswith("of=")]
    if writes and len(words) < len(args):
        ctx.add_unknown(None, False, next(t for t, c in args if c))
    for target in marks:
        path = _resolve_path(target, False, ctx)
        if path:
            ctx.touch(path)
            if name != "tee":
                ctx.blind.update((path, os.path.realpath(path)))
        else:
            ctx.add_unknown(_partial_path(target, ctx), False, target)
    if name not in ("cat", "tee", "echo", "printf"):
        return
    if name == "tee":
        targets = [t for t, c in args if not t.startswith("-") and not c]
    else:
        targets = seg.outs[-1:]
    if seg.bodies:
        content = seg.bodies[0]
        new = fresh and fresh.body(0)
    elif seg.strings:
        content = seg.strings[0]
        new = fresh and fresh.string(0)
    elif name in ("echo", "printf"):
        content = " ".join(t for t, _ in args)
        if name == "printf":
            content = content.replace("\\n", "\n")
        one = name == "echo" and len(args) == 1
        new = fresh and (fresh.word(args[0], content) if one else fresh.command(content))
    else:
        return
    for target in targets:
        path = _resolve_path(target, False, ctx)
        if path:
            ctx.written[path] = content
            ctx.add_version(path, content)
            if fresh is None:
                ctx.written_new.pop(path, None)
            else:
                ctx.written_new[path] = new



def _analyze(text: str, ctx: _Ctx, script: bool, depth: int, only: set[int] | None = None):
    breaks = None
    for seg in _lex(text, ctx.deadline):
        ctx.tick()
        res = _resolve(seg.words)
        if res is None:
            continue
        raw, args, xargs, chdirs = res
        fresh = None
        if only is not None:
            if breaks is None:
                breaks = [m.start() for m in re.finditer("\n", text)]
            fresh = _Fresh(text, breaks, only, _command_is_new(seg, breaks, only))
        saved = ctx.cwd
        try:
            for word, comp in chdirs:
                ctx.cwd = _resolve_path(word, comp, ctx)
            found = _analyze_segment(seg, raw, args, xargs, ctx, script, depth, fresh)
        finally:
            if chdirs:
                ctx.cwd = saved
        if found:
            if fresh is not None and not fresh.all and found[0] != "nested" and raw.rsplit("/", 1)[-1] != "eval":
                continue
            return found
    return None


def _command_is_new(seg: _Seg, breaks: list[int], only: set[int]) -> bool:
    if bisect.bisect_left(breaks, seg.pos) + 1 in only:
        return True
    tails = {bisect.bisect_left(breaks, at) + 1 for at in seg.tails}
    for at in seg.marks:
        row = bisect.bisect_left(breaks, at) + 1
        if row in only and row not in tails:
            return True
    return False


class _Fresh:
    __slots__ = ("text", "breaks", "only", "all", "seg")

    def __init__(self, text: str, breaks: list[int], only: set[int], all_new: bool):
        self.text, self.breaks, self.only, self.all = text, breaks, only, all_new
        self.seg: _Seg | None = None

    def lines(self, at: int, part: str) -> set[int]:
        n = part.count("\n") + 1
        if self.all:
            return set(range(1, n + 1))
        first = bisect.bisect_left(self.breaks, at) + 1
        return {no - first + 1 for no in self.only if first <= no < first + n}

    def command(self, part: str) -> set[int]:
        if self.all:
            return set(range(1, part.count("\n") + 2))
        lo = bisect.bisect_left(self.breaks, self.seg.pos) + 1
        hi = bisect.bisect_left(self.breaks, self.seg.end) + 1
        return set(range(1, part.count("\n") + 2)) if any(no in self.only for no in range(lo, hi + 1)) else set()

    def _span(self, start: int, stop: int, part: str) -> set[int] | None:
        breaks = self.breaks
        lo, hi = bisect.bisect_left(breaks, start), bisect.bisect_left(breaks, stop)
        rows = [[lo + 1]]
        drops = self.seg.drops
        for k in range(lo, hi):
            if breaks[k] in drops:
                rows[-1].append(k + 2)
            else:
                rows.append([k + 2])
        if len(rows) != part.count("\n") + 1:
            return None
        return {n for n, joined in enumerate(rows, 1) if any(row in self.only for row in joined)}

    def word(self, w: tuple[str, bool], part: str) -> set[int]:
        if self.all:
            return set(range(1, part.count("\n") + 2))
        seg = self.seg
        src = getattr(w, "of", w)
        for k, x in enumerate(seg.words):
            if x is src:
                got = self._span(*seg.spans[k], src[0])
                if got is None:
                    break
                skip = src[0].count("\n") - part.count("\n")
                return {n - skip for n in got if n > skip}
        return self.command(part)

    def body(self, k: int) -> set[int]:
        return self.lines(self.seg.body_at[k], self.seg.bodies[k])

    def string(self, k: int) -> set[int]:
        seg = self.seg
        if self.all:
            return set(range(1, seg.strings[k].count("\n") + 2))
        got = self._span(*seg.string_spans[k], seg.strings[k]) if k < len(seg.string_spans) else None
        return self.command(seg.strings[k]) if got is None else got


def _analyze_segment(seg: _Seg, raw: str, args: list[tuple[str, bool]], xargs: bool, ctx: _Ctx, script: bool,
                     depth: int, fresh: _Fresh | None = None):
    name = raw.rsplit("/", 1)[-1]
    if name in ("cd", "pushd"):
        ctx.cwd = _cd_target(ctx, args)
        return None
    if name == "popd":
        ctx.cwd = None
        return None
    if fresh is not None:
        fresh.seg = seg
    _record_writes(seg, name, args, ctx, fresh)
    if name == "ps":
        if _ps_lists_all([t for t, _ in args]):
            return "ps", seg.pos, None
        return None
    if name == "kill":
        sends, everything, computed = _kill_info(args, xargs)
        if not sends:
            return None
        if script:
            return "signal", seg.pos, None
        for kind, test in (("all", everything), ("xargs", xargs), ("computed", computed)):
            if test:
                return kind, seg.pos, None
        return None
    if script and name in ("pkill", "killall"):
        return "signal", seg.pos, None
    if name == "eval":
        code = " ".join(t for t, _ in args)
        if depth >= _MAX_DEPTH or not code.strip():
            return None
        hit = _analyze(code, ctx, script, depth + 1,
                       fresh and (fresh.word(args[0], code) if len(args) == 1 else fresh.command(code)))
        return None if hit is None else (hit[0], seg.pos, hit[2])
    found = None
    if name in ("source", ".") and args:
        found = _run_interp("sh", "bash", args, seg, ctx, depth, fresh)
    elif _family(name):
        found = _run_interp(_family(name), name, args, seg, ctx, depth, fresh)
    elif "/" in raw:
        found = _run_script_word(raw, ctx, depth, fresh is None or fresh.all)
    if found:
        return "nested", seg.pos, found
    return None


def _run_interp(fam: str, name: str, args: list[tuple[str, bool]], seg: _Seg, ctx: _Ctx, depth: int,
                fresh: _Fresh | None = None):
    tgt = _interp_target(fam, name, args)
    if tgt is None or depth >= _MAX_DEPTH:
        return None
    kind, val = tgt
    if kind == "stdin":
        if seg.bodies:
            return _scan_source(fam, seg.bodies[0], "the inline code", ctx, depth, fresh and fresh.body(0))
        if seg.strings:
            return _scan_source(fam, seg.strings[0], "the inline code", ctx, depth, fresh and fresh.string(0))
        path = _resolve_path(seg.ins[0], False, ctx) if seg.ins else None
        return _scan_file(fam, path, ctx, depth, fresh is None or fresh.all) if path else None
    text, comp = val
    if comp:
        return None
    if kind == "code":
        return _scan_source(fam, text, "the inline code", ctx, depth, fresh and fresh.word(val, text))
    path = _resolve_path(text, comp, ctx)
    return _scan_file(fam, path, ctx, depth, fresh is None or fresh.all) if path else None


def _label(path: str, shown: str) -> str:
    return shown if path == shown else f"{shown} (copied from {path})"


def _scan_file(fam: str, path: str, ctx: _Ctx, depth: int, whole: bool = True):
    for src, text, only in _texts(path, ctx, whole):
        found = _scan_source(fam, text, _label(src, path), ctx, depth, only)
        if found:
            return found
    return None


def _run_script_word(raw: str, ctx: _Ctx, depth: int, whole: bool = True):
    if depth >= _MAX_DEPTH:
        return None
    path = _resolve_path(raw, False, ctx)
    if path is None:
        return None
    for src, text, only in _texts(path, ctx, whole):
        fam = _shebang_family(text)
        found = _scan_source(fam, text, _label(src, path), ctx, depth, only) if fam else None
        if found:
            return found
    return None


def _scan_source(fam: str, src: str, label: str, ctx: _Ctx, depth: int, only: set[int] | None = None):
    hit = _scan_text(fam, src, ctx, depth + 1, only)
    if hit is None:
        return None
    kind, lineno, line, final = hit
    if final:
        return final
    what = "lists every process of his" if kind == "ps" else "sends a signal"
    shown = line if len(line) <= _SNIPPET else line[:_SNIPPET - 3] + "..."
    advice = _PS_ADVICE + " Or " + _BOX_ADVICE[0].lower() + _BOX_ADVICE[1:] if kind == "ps" else _BOX_ADVICE
    return f"runs {label}, which {what} (line {lineno}: {shown})", advice


def _scan_text(fam: str, src: str, ctx: _Ctx, depth: int, only: set[int] | None = None):
    ctx.check()
    if fam == "sh":
        saved = ctx.cwd
        try:
            hit = _analyze(src, ctx, True, depth, only)
        finally:
            ctx.cwd = saved
        if hit is None:
            return None
        kind, pos, final = hit
        start = src.rfind("\n", 0, pos) + 1
        stop = src.find("\n", pos)
        line = src[start:len(src) if stop < 0 else stop].strip()
        return kind, src.count("\n", 0, pos) + 1, line, final
    if not _QUICK.search(src):
        return None
    lines = src.split("\n")
    rows = None
    if fam == "py":
        strings, rows = _py_tokens(src, lines, ctx)
    else:
        strings = {}
    for no, line in enumerate(lines, 1):
        ctx.tick()
        s = line.strip()
        if not s or s.startswith(("#", "//", "/*", "*")):
            continue
        if "kill" not in s and "ps" not in s and "signal" not in s:
            continue
        if only is not None and no not in only:
            if rows is None:
                rows = _statements(fam, src, lines, ctx)
            first, last = rows
            if not any(r in only for r in range(max(first[no], no - _STMT_REACH), min(last[no], no + _STMT_REACH) + 1)):
                continue
        lead = len(line) - len(line.lstrip())
        spans = strings.get(no)
        cols = ([a for a, _ in spans], [b for _, b in spans]) if spans else None
        tail = " ".join(_uncomment(fam, x) for x in lines[no:no + 4])
        for off in range(0, len(s), _LINE_STRIDE):
            ctx.tick()
            last = off + _LINE_CAP >= len(s)
            kind = _line_hit(fam, s[off:off + _LINE_CAP], cols, lead + off, None if last else _LINE_STRIDE,
                             tail if last else "")
            if kind:
                return kind, no, s, None
            if last:
                break
    return None


_STMT_REACH = 30
_COMMENT = {"py": re.compile(r"(?:^|\s)#.*$"),
            "js": re.compile(r"(?:^|\s)//.*$|^\s*/?\*.*$"),
            "perl": re.compile(r"(?:^|\s)#(?!\{).*$"),
            "ruby": re.compile(r"(?:^|\s)#(?!\{).*$")}
_JS_BLOCK = re.compile(r"/\*.*?\*/")


def _uncomment(fam: str, line: str) -> str:
    rx = _COMMENT.get(fam)
    if rx is None:
        return line.strip()
    if fam == "js":
        line = _JS_BLOCK.sub(" ", line)
    return rx.sub(" ", line).strip()


def _statements(fam: str, src: str, lines: list[str], ctx: _Ctx) -> tuple[list[int], list[int]]:
    n = len(lines)
    first, last = list(range(n + 2)), list(range(n + 2))
    depth, start = 0, None
    for r, line in enumerate(lines, 1):
        ctx.tick()
        if start is None:
            start = r
        code = _uncomment(fam, line)
        for ch in code:
            if ch in "([":
                depth += 1
            elif ch in ")]" and depth:
                depth -= 1
        if (depth == 0 and not code.endswith("\\")) or r - start >= _STMT_REACH:
            for k in range(start, r + 1):
                first[k], last[k] = start, r
            depth, start = 0, None
    return first, last


def _py_tokens(src: str, lines: list[str], ctx: _Ctx):
    n = len(lines)
    first, last = list(range(n + 2)), list(range(n + 2))
    spans: dict[int, list[tuple[int, int]]] = {}
    middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    skip = (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING)
    start = None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            ctx.tick()
            if tok.type == tokenize.STRING or (middle is not None and tok.type == middle):
                (r1, c1), (r2, c2) = tok.start, tok.end
                for row in range(r1, r2 + 1):
                    spans.setdefault(row, []).append((c1 if row == r1 else 0, c2 if row == r2 else 1 << 30))
            if tok.type == tokenize.NEWLINE:
                if start is not None:
                    for r in range(start, min(tok.start[0], n) + 1):
                        first[r], last[r] = start, tok.start[0]
                start = None
            elif tok.type == tokenize.ENDMARKER:
                break
            elif start is None and tok.type not in skip:
                start = tok.start[0]
    except (tokenize.TokenError, SyntaxError, ValueError):
        pass
    return spans, (first, last)


_QUICK = re.compile(r"kill|\bps\b|pidfd_send_signal")
_SIG_CALL = re.compile(r"\b(os\.killpg|os\.kill|process\.kill|Process\.kill)\s*\(")
_SPAWNERS = (r"run|call|check_output|check_call|Popen|getoutput|getstatusoutput|system|popen|spawn|spawnSync|"
             r"exec|execSync|execFile|execFileSync|execvp|execvpe|execv|Command|capture|sh|shell")
_KP = r"""[rbfuRBFU]{0,2}["'`](?:[^"'`\s]*/)?"""
_KILL_LIST = re.compile(
    r"(?:\[\s*" + _KP + r"(?:pkill|killall)[\"'`]\s*,"
    r"|\[\s*" + _KP + r"kill[\"'`]\s*,(?!\s*[\"'`][A-Za-z_])"
    r"|\b(?:" + _SPAWNERS + r")\s*\(\s*" + _KP + r"(?:kill|pkill|killall)[\"'`])")
_KILL_STR = re.compile(r"""(?P<q>["'`])(?:[^"'`]*?(?:[;&|({]|\\n|\n|\bthen\b|\bdo\b|\belse\b))?\s*"""
                       r"""(?:(?:sudo|exec|command|nohup|env)\s+)*(?:[^\s"'`;&|(){}]*/)?"""
                       r"""(?:(?P<k>kill)\s+(?:-(?![0lL]\b)|[$%{`\d])|(?P<p>pkill|killall)\s+[-\w$"'.])""")
_SELF_ARG = re.compile(r"\s*(?:os\.getpid\(\)|process\.pid|Process\.pid|\$\$|\$BASHPID|\$\{BASHPID\})\s*,")
_XARGS_KILL = re.compile(r"""\bxargs\b[^|;&"'`]*?\s(?:\S*/)?kill\b""")
_PERL_KILL = re.compile(r"""(?<![\w$@%.'"-])kill\b\s*\(?\s*(?:[\d'"$-]|[A-Z]{2,})""")
_PS_WORD = re.compile(r"(?<![\w.$-])ps(?=\s)")
_NOT_A_CALL = re.compile(r"add_parser|add_argument|choices\s*=|getattr|hasattr")


def _arg(rest: str, n: int) -> str | None:
    depth = arg = 0
    cur: list[str] = []
    for ch in rest:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            arg += 1
            if arg > n:
                break
            continue
        if arg == n:
            cur.append(ch)
    return "".join(cur).strip() if arg >= n else None


_PS_NORM = re.compile(r"""[\[\](),"'`]""")
_PS_TOKEN = re.compile(r"(?:^|[^\w.$-])ps$")
_PS_STOP = frozenset(("|", "||", "&&", ";", "&", ">", ">>", "<"))


def _ps_in_line(s: str) -> bool:
    toks = _PS_NORM.sub(" ", s).split()
    seen = 0
    for i, tok in enumerate(toks):
        if not tok.endswith("ps") or (tok != "ps" and not _PS_TOKEN.search(tok)):
            continue
        seen += 1
        if seen > _PS_MATCHES:
            return False
        window: list[str] = []
        for nxt in toks[i + 1:i + 1 + _PS_WINDOW]:
            if nxt in _PS_STOP or nxt.endswith(";"):
                break
            window.append(nxt)
        if _ps_lists_all(window):
            return True
    return False


def _str_kill_sends(s: str) -> bool:
    for m in _KILL_STR.finditer(s):
        if m.group("p"):
            return True
        rest = s[m.end("k"):].split(m.group("q"), 1)[0]
        rest = re.split(r"[;&|)]|\\n|\n", rest, maxsplit=1)[0]
        args = [(t.strip("\"'\\"), False) for t in rest.split()]
        if _kill_info(args, False)[0]:
            return True
    return False


def _line_hit(fam: str, s: str, strings: tuple[list[int], list[int]] | None = None, lead: int = 0,
              limit: int | None = None, tail: str = "") -> str | None:
    for m in _SIG_CALL.finditer(s):
        if limit is not None and m.start() >= limit:
            break
        col = m.start() + lead
        if strings:
            k = bisect.bisect_right(strings[0], col) - 1
            if k >= 0 and col < strings[1][k]:
                continue
        rest = s[m.end():m.end() + 160]
        if tail and len(rest) < 160:
            rest = (_uncomment(fam, rest) + " " + tail)[:160]
        ruby = m.group(1) == "Process.kill"
        if _arg(rest, 0 if ruby else 1) == "0":
            continue
        if not ruby and m.group(1) != "os.killpg" and _SELF_ARG.match(rest):
            continue
        return "signal"
    if "pidfd_send_signal" in s or _XARGS_KILL.search(s) or _str_kill_sends(s):
        return "signal"
    if _KILL_LIST.search(s) and not _NOT_A_CALL.search(s):
        return "signal"
    if fam == "perl" and _PERL_KILL.search(s):
        return "signal"
    return "ps" if _ps_in_line(s) else None



class _Incomplete(BaseException):
    ...


class _LibCtx(_Ctx):

    def __init__(self, first: _Ctx):
        super().__init__(first.cwd)
        self.written, self.written_new, self.touched = first.written, first.written_new, first.touched
        self.copies, self.versions, self.versions_over = first.copies, first.versions, first.versions_over
        self.blind, self.unknown = first.blind, first.unknown
        self.deadline = time.monotonic() + _LIB_BUDGET
        self.entries = 0
        self.walks: dict[tuple[str, bool], list[str]] = {}
        self.listings: dict[str, dict[str, tuple[str, int]] | None] = {}
        self.strict = False
        self._reals: dict[str, list[str]] | None = None

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Incomplete("ran out of time")

    def note_run(self, real: str) -> None:
        pass

    def visit(self, path: str) -> None:
        if not self.strict:
            return
        if path in self.blind or (path in self.touched and path not in self.written and path not in self.copies
                                  and os.path.realpath(path) not in self.reals()):
            raise _Incomplete(f"{path} is changed by a command whose result the guard cannot see")

    def unreadable(self, path: str) -> None:
        if not self.strict or path == "/dev/null":
            return
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as e:
            raise _Incomplete(f"{path} cannot be read ({e.strerror})")
        if not stat.S_ISDIR(st.st_mode):
            raise _Incomplete(f"{path} cannot be read as text")

    def older(self, path: str) -> list[str]:
        if path in self.versions_over:
            raise _Incomplete(f"more than {_MAX_VERSIONS} versions written to {path}")
        return self.versions.get(path, [])[:-1]

    def reals(self) -> dict[str, list[str]]:
        if self._reals is None:
            found: dict[str, list[str]] = {}
            for path, texts in self.versions.items():
                self.tick()
                found.setdefault(os.path.realpath(path), []).extend(texts)
            self._reals = found
        return self._reals

    def read(self, path: str) -> str | None:
        if path in self.cache:
            return self.cache[path]
        try:
            st = os.stat(path)
        except OSError:
            self.cache[path] = None
            return None
        if not stat.S_ISREG(st.st_mode):
            self.cache[path] = None
            return None
        self.check()
        if self.reads >= _MAX_PART_READS:
            raise _Incomplete(f"more than {_MAX_PART_READS} files to read")
        if st.st_size > _MAX_BYTES:
            raise _Incomplete(f"{path} is larger than {_MAX_BYTES} bytes")
        self.reads += 1
        text = _read_script(path)
        self.cache[path] = text
        return text


def _lib_candidates(real_tool: str) -> list[str]:
    name = os.path.basename(real_tool)
    base = os.path.join(os.path.dirname(os.path.dirname(real_tool)), "lib")
    return list(dict.fromkeys((os.path.join(base, name), os.path.join(base, name.replace("-", "_")))))


def _names_lib_tool(cmd: str) -> bool:
    return any(name in cmd for name in _LIB_TOOLS)


def _lib_scope(rel: str, base: bool = False) -> bool:
    parts = rel.split("/")
    if base and len(parts) > 1 and not parts[0].isidentifier():
        return False
    return rel.endswith(".py") and not any(p in ("tests", "__pycache__") or p.startswith(".") for p in parts[:-1])


def _lib_related(lctx: _LibCtx, path: str) -> bool:
    for keys in (lctx.versions, lctx.reals(), lctx.copies, lctx.touched):
        for key in keys:
            if key == path or key.startswith(path + "/") or path.startswith(key + "/"):
                return True
    return False


def _could_land(prefix: str | None, exact: bool, folder: str) -> bool:
    if prefix is None:
        return True
    real = os.path.realpath(prefix) if exact else os.path.join(os.path.realpath(os.path.dirname(prefix)),
                                                              os.path.basename(prefix))
    for p in dict.fromkeys((prefix, real)):
        if exact:
            if p == folder or folder.startswith(p + "/") or p.startswith(folder + "/"):
                return True
        elif (folder + "/").startswith(p) or p.startswith(folder + "/"):
            return True
    return False


def _lib_folders(lctx: _LibCtx, tool: str) -> list[tuple[str, bool]]:
    candidates = _lib_candidates(tool)
    base = os.path.dirname(candidates[0])
    if os.path.islink(base):
        raise _Incomplete(f"{base} is a symbolic link")
    found: list[tuple[str, bool]] = []
    for folder in candidates:
        if os.path.islink(folder):
            raise _Incomplete(f"{folder} is a symbolic link")
        if os.path.isdir(folder) or (not os.path.lexists(folder) and _lib_related(lctx, folder)):
            found.append((folder, False))
    if os.path.basename(tool) in _PACKAGE_TOOLS:
        have = {f for f, _ in found}
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    lctx.entries += 1
                    if lctx.entries > _MAX_LIB_ENTRIES:
                        raise _Incomplete(f"more than {_MAX_LIB_ENTRIES} entries to look at")
                    lctx.tick()
                    if entry.is_symlink():
                        raise _Incomplete(f"{os.path.join(base, entry.name)} is a symbolic link")
                    folder = os.path.join(base, entry.name)
                    if (entry.is_dir(follow_symlinks=False) and entry.name.isidentifier()
                            and entry.name != "__pycache__" and folder not in have):
                        found.append((folder, False))
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as e:
            raise _Incomplete(f"{base} cannot be listed ({e.strerror})")
        if os.path.isdir(base) or _lib_related(lctx, base):
            found.append((base, True))
    return found


def _lib_walk(lctx: _LibCtx, root: str, shallow: bool = False) -> list[str]:
    if (root, shallow) in lctx.walks:
        return lctx.walks[(root, shallow)]
    files: list[str] = []
    todo = [""]
    while todo:
        rel = todo.pop()
        here = os.path.join(root, rel) if rel else root
        lctx.check()
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    lctx.entries += 1
                    if lctx.entries > _MAX_LIB_ENTRIES:
                        raise _Incomplete(f"more than {_MAX_LIB_ENTRIES} entries to look at")
                    lctx.tick()
                    if entry.is_symlink():
                        raise _Incomplete(f"{os.path.join(here, entry.name)} is a symbolic link")
                    name = f"{rel}/{entry.name}" if rel else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if not shallow and not entry.name.startswith("."):
                            todo.append(name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(name)
                    else:
                        raise _Incomplete(f"{os.path.join(here, entry.name)} is not a regular file or a folder")
        except OSError as e:
            raise _Incomplete(f"{here} cannot be listed ({e.strerror})")
    files.sort()
    lctx.walks[(root, shallow)] = files
    return files


def _lib_listing(lctx: _LibCtx, lib: str) -> dict[str, tuple[str, int]] | None:
    if lib in lctx.listings:
        return lctx.listings[lib]
    out = _git_out(lib, ["ls-tree", "-r", "-z", "-l", "HEAD"], lctx)
    lctx.check()
    tree: dict[str, tuple[str, int]] | None = None
    if out is not None:
        tree = {}
        for item in out.split(b"\0"):
            meta, _, name = item.partition(b"\t")
            fields = meta.split()
            if len(fields) == 4 and fields[1] == b"blob":
                try:
                    tree[os.fsdecode(name)] = (fields[2].decode("ascii"), int(fields[3]))
                except ValueError:
                    continue
    lctx.listings[lib] = tree
    return tree


def _lib_tree(lctx: _LibCtx, folder: str, base: bool = False) -> dict[str, tuple[str, int]] | None:
    whole = _lib_listing(lctx, folder if base else os.path.dirname(folder))
    if whole is None:
        return None
    if base:
        return {rel: v for rel, v in whole.items() if "/" not in rel}
    prefix = os.path.basename(folder) + "/"
    return {rel[len(prefix):]: v for rel, v in whole.items() if rel.startswith(prefix)}


def _lib_scan(lctx: _LibCtx, label: str, text: str, only: set[int] | None):
    return _scan_source("py", text, label, lctx, 1, only)


def _judge_folder(lctx: _LibCtx, tool: str, folder: str, base: bool = False):
    here = f"{tool}, which loads lib/" if base else f"{tool}, which loads lib/{os.path.basename(folder)}/"
    present = os.path.isdir(folder)
    tree = _lib_tree(lctx, folder, base) if present else None
    files = [rel for rel in (_lib_walk(lctx, folder, base) if present else []) if _lib_scope(rel)]
    gone: dict[str, int] = {}
    if tree:
        here_now = set(files)
        for rel, (blob, _) in tree.items():
            if _lib_scope(rel) and rel not in here_now:
                gone[blob] = gone.get(blob, 0) + 1
    for rel in files:
        path = os.path.join(folder, rel)
        entry = tree.get(rel) if tree else None
        only = None
        if entry is not None:
            if _matches_blob(path, entry):
                continue
            lctx.head[path] = entry
            left = _head_lines(path, lctx)
            text = lctx.read(path)
            if text is not None and left is not None:
                only = _lines_not_in(text, left)
        else:
            if gone:
                data = _file_bytes(path, _MAX_HASH + 1)
                if data is not None and len(data) <= _MAX_HASH:
                    blob = _blob_id(data, next(iter(gone)))
                    if gone.get(blob, 0) > 0:
                        gone[blob] -= 1
                        continue
            text = lctx.read(path)
        if text is None:
            raise _Incomplete(f"{path} cannot be read as text")
        found = _lib_scan(lctx, here + rel, text, only)
        if found:
            return found
    return _judge_command_changes(lctx, here, folder, base)


def _judge_command_changes(lctx: _LibCtx, here: str, folder: str, base: bool = False):
    inside = folder + "/"
    for (prefix, exact), word in lctx.unknown.items():
        if _could_land(prefix, exact, folder):
            shown = " ".join(word[:60].split())
            raise _Incomplete(f"a write or copy using `{shown}`, which the guard cannot work out, could land in it"
                              if shown else "a write or copy the guard cannot work out could land in it")
    for path in lctx.touched:
        if (path == folder or folder.startswith(path + "/")) and (path in lctx.blind or path not in lctx.copies):
            raise _Incomplete(f"{path} is changed by a command whose result the guard cannot see")
    reals = lctx.reals()
    over = {os.path.realpath(p) for p in lctx.versions_over}
    targets: dict[str, None] = {}

    def add(path: str) -> None:
        if path.startswith(inside) and _lib_scope(path[len(inside):], base):
            targets[path] = None

    def under(root: str) -> dict[str, None]:
        top = root + "/"
        return {key: None for keys in (lctx.versions, reals, lctx.copies, lctx.touched) for key in keys
                if key.startswith(top)}

    def from_root(root: str, dest: str) -> None:
        if os.path.isdir(root):
            if os.path.islink(root):
                raise _Incomplete(f"{root} is a symbolic link")
            for rel in _lib_walk(lctx, root):
                add(f"{dest}/{rel}")
        for key in under(root):
            add(f"{dest}/{key[len(root) + 1:]}")

    for path in reals:
        add(path)
    for key, sources in lctx.copies.items():
        lctx.tick()
        if key.startswith(inside):
            add(key)
            for src in sources:
                from_root(src, key)
        elif key == folder or folder.startswith(key + "/"):
            for src in sources:
                from_root(src + folder[len(key):], folder)
    for path in lctx.touched:
        add(path)
    for path in targets:
        lctx.tick()
        label = here + path[len(inside):]
        for text in reals.get(path, ()):
            found = _lib_scan(lctx, label, text, None)
            if found:
                return found
        if path in over:
            raise _Incomplete(f"more than {_MAX_VERSIONS} versions written to {path}")
        copied = dict.fromkeys(_copy_sources(lctx, path))
        for source in copied:
            lctx.strict = True
            try:
                texts = _texts(source, lctx)
            finally:
                lctx.strict = False
            for src, text, only in texts:
                found = _lib_scan(lctx, _label(src, label), text, only)
                if found:
                    return found
            if lctx.cut:
                raise _Incomplete(f"a chain of copies longer than {_MAX_SOURCES} files")
        if path in lctx.blind or (path not in reals and not copied):
            raise _Incomplete(f"{path} is changed by a command whose result the guard cannot see")
    return None


def _lib_refusal(tool: str | None, folder: str | None, cause: str) -> tuple[str, str]:
    if tool:
        name = os.path.basename(folder) if folder else ""
        where = "lib/" if name == "lib" else (f"lib/{name}/" if name else "lib/ folder")
        why = f"runs {tool}, whose {where} the guard could not read completely ({cause})"
    else:
        why = f"may run a tool that loads code from a lib/ folder, which the guard could not read completely ({cause})"
    return (why,
            _BOX_ADVICE + " Or, when the change under lib/ is finished, commit it: a committed folder with no "
            "edit is not read again.")


def _lib_verdict(ctx: _Ctx) -> tuple[str, str] | None:
    tool = folder = None
    try:
        lctx = _LibCtx(ctx)
        if ctx.lib_over:
            raise _Incomplete(f"more than {_MAX_LIB_RUNS} lib-loading tools in one command")
        done: set[str] = set()
        for tool in ctx.lib_runs:
            folder = None
            lctx.check()
            for folder, is_base in _lib_folders(lctx, tool):
                if folder in done:
                    continue
                done.add(folder)
                found = _judge_folder(lctx, tool, folder, is_base)
                if found:
                    return found
        return None
    except _Incomplete as e:
        cause = str(e)
    except TimeoutError:
        cause = "ran out of time"
    except Exception as e:
        _log(f"guard lib rule error: {e!r}")
        cause = "an error in the guard"
    return _lib_refusal(tool, folder, cause)


def _signal_verdict(cmd: str, cwd: str | None) -> tuple[str, str] | None:
    if not cmd or len(cmd) > _MAX_CMD:
        return None
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None
    ctx = _Ctx(cwd)
    try:
        hit = _analyze(cmd, ctx, False, 0)
    except TimeoutError:
        if ctx.lib_runs or ctx.lib_over or _names_lib_tool(cmd):
            _log(f"guard first pass out of time on a command that names a lib-loading tool, refused: {cmd[:200]!r}")
            return _lib_refusal(None, None, "ran out of time reading the command")
        raise
    if hit is None:
        return _lib_verdict(ctx) if ctx.lib_runs or ctx.lib_over else None
    kind, _, final = hit
    if final:
        return final
    advice = _PS_ADVICE if kind == "ps" else (
        "A pid list worked out by a command can hold your whole session (a probe once ended everything that was open). "
        "Run it from a terminal of your own, since it could end processes outside your own. "
        "To stop a process you started earlier in another command, kill it by its recorded PID from the Bash tool. To stop "
        "whatever holds a port, read the pid first (`ss -ltnp 'sport = :PORT'`), then kill that literal pid. "
        "Killing a PID you recorded yourself when you started the process is not refused.")
    return _CMD_WHY[kind], advice


def guard() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    hit = guard_verdict(cmd, data.get("cwd"))
    if hit:
        why, advice = hit
        _log(f"guard denied: {why}: {cmd[:200]!r}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked: this command {why}. {advice}",
        }}))
    return 0


AGENT_EXES = (
    str(HOME / ".local/share/claude/versions") + "/",
    "/usr/lib/node_modules/@anthropic-ai/claude-code/",
    "/usr/lib/chatgpt/resources/codex",
)
AGENT_COMMS = {"claude", "codex"}


def _stat(pid: int) -> tuple[str, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    comm = raw[raw.index("(") + 1: raw.rindex(")")]
    ppid = int(raw[raw.rindex(")") + 2:].split()[1])
    return comm, ppid


def _is_agent(pid: int) -> bool:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = ""
    if exe.startswith(AGENT_EXES):
        return True
    st = _stat(pid)
    if not st or st[0] not in AGENT_COMMS:
        return False
    return "/chatgpt/ChatGPT" not in exe and not exe.endswith("/claude-desktop")


def agent_ancestor(pid: int) -> int | None:
    st = _stat(pid)
    seen = 0
    while st and st[1] > 1 and seen < 64:
        parent = st[1]
        if _is_agent(parent):
            return parent
        st = _stat(parent)
        seen += 1
    return None


def verdict(pid: int) -> str:
    return "tuck" if pid > 1 and agent_ancestor(pid) else "leave"



def _environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
    except OSError:
        return {}
    out = {}
    for entry in raw.split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            out[key] = value
    return out


def killable(pid: int) -> tuple[bool, str]:
    if pid <= 1 or pid == os.getpid():
        return False, "not a target"
    if agent_ancestor(pid):
        return True, "descends from an agent"
    env = _environ(pid)
    if env.get("DISPLAY") == HIDDEN:
        return True, f"DISPLAY={HIDDEN}"
    if env.get("WAYLAND_DISPLAY", "").startswith("wayland-") or env.get("DISPLAY", "").startswith(":0"):
        return False, "one of his windows"
    return False, "not the agent's"


def kill(args: list[str]) -> int:
    import signal
    import subprocess

    full = "-f" in args
    rest = [a for a in args if a != "-f"]
    if not rest:
        print("usage: claude-agent-screen kill [-f] <name-or-pattern>", file=sys.stderr)
        return 2
    found = subprocess.run(["pgrep", "-f" if full else "-x", rest[0]],
                           capture_output=True, text=True)
    killed, spared = [], []
    for pid in (int(x) for x in found.stdout.split()):
        ok, why = killable(pid)
        comm = (_stat(pid) or ("?", 0))[0]
        if not ok:
            spared.append(f"{pid} {comm}: {why}")
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            spared.append(f"{pid} {comm}: {e}")
            continue
        killed.append(f"{pid} {comm} ({why})")
    for line in killed:
        print("killed  " + line)
    for line in spared:
        print("spared  " + line)
    if spared:
        _log(f"kill {rest[0]}: killed {len(killed)}, spared {len(spared)}")
    return 0



def _log(msg: str) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def watch() -> int:
    import dbus, dbus.service, dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    class AgentScreen(dbus.service.Object):
        @dbus.service.method(BUS_NAME, in_signature="iss", out_signature="s")
        def Verdict(self, pid, wclass, caption):
            v = verdict(int(pid))
            if v == "tuck":
                anc = agent_ancestor(int(pid))
                _log(f"tucked away window {str(caption)[:80]!r} ({wclass}), pid {pid}, agent pid {anc}")
            return v

    bus = dbus.SessionBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)  # noqa: F841 (held)
    AgentScreen(bus, OBJ_PATH)
    _log("watch started")
    GLib.MainLoop().run()
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "session-env":
        return session_env()
    if cmd == "guard":
        return guard()
    if cmd == "watch":
        return watch()
    if cmd == "kill":
        return kill(argv[2:])
    if cmd == "verdict" and len(argv) > 2:
        pid = int(argv[2])
        print(verdict(pid), agent_ancestor(pid) or "")
        return 0
    if cmd == "guard-test" and len(argv) > 2:
        hit = guard_verdict(argv[2], argv[3] if len(argv) > 3 else None)
        print(hit[0] if hit else "allow")
        return 0
    if cmd == "log":
        print(LOG.read_text()[-4000:] if LOG.exists() else "(nothing yet)", end="")
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

_END_OF_GUARD = True
