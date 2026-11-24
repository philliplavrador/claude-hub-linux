# claude-hub, part 5: the list's columns and rows, cmd_list, the popup's pty and focus tap.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


STATE_W = 28
TOKENS_W = 4
CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain_text(s) -> str:
    return CONTROLS.sub(lambda m: "\\x%02x" % ord(m.group()), str(s))


class Cols(typing.NamedTuple):
    account: int = 6
    model: int = 0
    tokens: int = 0
    effort: int = 0
    note: int = 0

    @property
    def extra(self) -> int:
        return sum(w + 2 for w in (self.model, self.effort, self.tokens) if w)


def account_w(rows: list) -> int:
    return max([6] + [len(str(r.get("account") or "")) for r in rows])


def claude_state(r: dict) -> tuple[str, str]:
    state, colour = state_words(r)
    if r.get("current"):
        state = f"here · {state}" if state else "here"
    return state, colour


def note_room(rows: list) -> int:
    need = [len(claude_state(r)[0]) + 2 + len(str(r["note"])) + 1 for r in rows if r.get("note")]
    return max([0] + [n - STATE_W for n in need])


def columns(rows: list, width: int | None = None) -> Cols:
    model = max([0] + [len(str(r.get("model") or "")) for r in rows])
    effort = max([0] + [len(str(r.get("effort") or "")) for r in rows])
    counts = [len(kilo(r.get("tokens"))) for r in rows]
    tokens = max([TOKENS_W] + counts) if any(counts) else 0
    cols = Cols(account_w(rows), model, tokens, effort, note_room(rows))
    for gives in ("tokens", "effort", "model"):
        if width is None or not cols.note or width - 12 - cols.account - cols.extra - STATE_W - cols.note >= 16:
            break
        cols = cols._replace(**{gives: 0})
    return cols


def tool(r: dict, icon: str, label: str) -> str:
    num = r.get("num")
    return f"{num:>2}  {icon} {label}" if num else f" {icon}  {label}"


def row_parts(r: dict, width: int, cols: Cols | None = None) -> tuple[str, str, str]:
    cols = cols or Cols()
    acct_w = cols.account
    name_w = max(16, width - 12 - acct_w - cols.extra - STATE_W - cols.note)
    if r.get("type") == "session":
        pick = str(r.get("pick") or "")
        text = f"{SESSION_NUMBER:>2}  Tools"
        return f"{text:<{name_w + acct_w + cols.extra + 6}}", (f"on {pick}" if pick else ""), DIM
    if r.get("type") == "usage":
        return f"{tool(r, '▤', 'Usages'):<{name_w + acct_w + cols.extra + 6}}", "week · Fable · Codex", DIM
    if r.get("type") == "skills":
        n = int(r.get("count") or 0)
        said = f"{n} reusable prompt{'' if n == 1 else 's'}" if n else "none saved"
        return f"{tool(r, '✦', 'Skills'):<{name_w + acct_w + cols.extra + 6}}", said, DIM
    if r.get("type") == "skill":
        room = name_w + acct_w + cols.extra + 2
        said = f"{model_label(str(r.get('model') or ''))} · {effort_label(str(r.get('effort') or ''))}"
        return f"{r.get('num') or '':>2}  {clip(str(r.get('name') or ''), room):<{room}}", clip(said, STATE_W), DIM
    if r.get("type") == "new":
        pick = str(r.get("pick") or "")
        if not pick:
            return tool(r, "+", "New Claude Code"), "", ""
        return f"{tool(r, '+', 'New Claude Code'):<{name_w + acct_w + cols.extra + 6}}", f"on {pick} · → choose", DIM
    if r.get("type") == "desktop" and r.get("key") == DESKTOP_ROW["key"]:
        label_w = name_w + acct_w + cols.extra + 6
        said = ANSI.sub("", str(r.get("said") or ""))
        label = tool(r, "+", "New Desktop")
        take = min(max(0, len(said) - STATE_W), max(0, label_w - len(label) - 1))
        return f"{label:<{label_w - take}}", clip(said, STATE_W + take), RED if said else ""
    if r.get("type") in ("model", "effort"):
        room = name_w + acct_w + cols.extra + 2
        return f"{r.get('n') or '':>2}  {clip(str(r.get('label') or ''), room):<{room}}", "", ""
    if r.get("type") == "screen":
        on, room = bool(r.get("sticky")), name_w + acct_w + cols.extra + 2
        return (f"{r.get('num') or '':>2}  {clip(str(r.get('word') or r.get('name') or ''), room):<{room}}",
                "sticky" if on else "switches", CYAN if on else DIM)
    num = str(list_number(r["number"])) if r.get("number") else "·"
    if r.get("type") == "panel":
        room = name_w + acct_w + cols.extra + 2
        return f"{num:>2}  {clip(str(r.get('name') or DK.PANEL_NAME), room):<{room}}", "here" if r.get("current") else "", DIM
    if r.get("type") == "aux":
        room = name_w + acct_w + cols.extra + 2
        return f"{num:>2}  {clip(str(r.get('name') or AUX_NAME), room):<{room}}", "here" if r.get("current") else "", DIM
    if r.get("yours"):
        name = main_name(r)
        state, colour = "here" if r.get("current") else "", DIM
        if r.get("sticky"):
            said = "sticky: " + ", ".join(map(str, r["sticky"]))
            state = f"{state} · {said}" if state else said
        closing, closing_colour = close_words(r.get("closing"))
        if closing:
            state, colour = closing, closing_colour
        room = name_w + acct_w + cols.extra + 2
        take = min(max(0, len(state) - STATE_W), room - len(name))
        return f"{num:>2}  {name:<{room - take}}", clip(state, STATE_W + take), colour
    name = display_name(r)
    if not r.get("desktop_id"):
        name = f"(no desktop) {name}"
    elif r.get("desktop_gone"):
        name = f"(desktop gone) {name}"
    state, colour = claude_state(r)
    meta = ""
    if cols.model:
        meta += f"  {str(r.get('model') or ''):<{cols.model}}"
    if cols.effort:
        meta += f"  {str(r.get('effort') or ''):<{cols.effort}}"
    if cols.tokens:
        meta += f"  {kilo(r.get('tokens')):>{cols.tokens}}"
    account = f"{str(r.get('account') or ''):<{acct_w}}"
    if r.get("paused") and account.strip():
        account = pad_ansi(f"{STRIKE}{account.rstrip()}{NOSTRIKE}", acct_w)
    shown = clip(name, name_w)
    if r.get("by_him") and shown.strip():
        lead = len(shown) - len(shown.lstrip())
        core = shown.strip()
        shown = f"{shown[:lead]}{HAND_NAMED}{core}{NOHAND_NAMED}"
        shown += " " * max(0, name_w - len(ANSI.sub("", shown)))
    else:
        shown = f"{shown:<{name_w}}"
    return f"{num:>2}  {shown}  {account}{meta}", state, colour


def format_row(r: dict, width: int, selected: bool, cols: Cols | None = None) -> str:
    text, state, colour = row_parts(r, width, cols)
    note = str(r.get("note") or "")
    if selected:
        tail = f"  {note}" if note else ""
        return f"{REV}{BOLD}{pad_ansi(clip_ansi(f' > {text}  {state}{tail}', width - 1), width - 1)}{OFF}"
    weight = BOLD if r.get("current") else ""
    if (r.get("type") in ("desktop", "claude") and not r.get("yours")
            and r.get("key") != DESKTOP_ROW["key"]):
        lead = len(text) - len(text.lstrip())
        digits = len(text[lead:].split(" ", 1)[0])
        hue = colour.replace(BOLD, "")
        text = f"{text[:lead]}{hue}{text[lead:lead + digits]}{OFF}{weight}{text[lead + digits:]}"
    def build(t: str) -> str:
        return f" {weight}  {t}  {colour}{state}{OFF}"

    line = build(text)
    over = len(ANSI.sub("", line)) - (width - 1)
    if over > 0:
        pad = len(text) - len(text.rstrip(" "))
        line = clip_ansi(build(text[:len(text) - min(over, pad)]), width - 1)
    if note:
        room = width - len(ANSI.sub("", line)) - 2
        if room > 0:
            line += f"  {DIM}{clip(note, room)}{OFF}"
    return line


def haystack(r: dict) -> str:
    if r.get("type") == "session":
        return f"{SESSION_NUMBER} tools new session claude code desktop usages skills"
    if r.get("type") == "usage":
        return "usages usage accounts codex"
    if r.get("type") == "skills":
        return ("skills reusable prompts " + " ".join(str(n) for n in r.get("names") or [])).lower()
    if r.get("type") == "skill":
        return f"{r.get('num') or ''} {r.get('name') or ''}".lower()
    if r.get("type") == "new":
        return "new claude code"
    if r.get("type") == "desktop" and r.get("key") == DESKTOP_ROW["key"]:
        return "new desktop"
    if r.get("type") in ("model", "effort"):
        return f"{r.get('n') or ''} {r.get('label') or ''}".lower()
    if r.get("type") == "panel":
        return f"{list_number(r.get('number')) or ''} the panel{' here' if r.get('current') else ''}".lower()
    if r.get("type") == "aux":
        return f"{list_number(r.get('number')) or ''} aux desktop{' here' if r.get('current') else ''}".lower()
    words, _ = state_words(r)
    name = main_name(r) if r.get("yours") else display_name(r)
    here = " here" if r.get("current") else ""
    if r.get("yours"):
        words = ""
    return f"{list_number(r.get('number')) or ''} {r.get('account') or ''} {name} {words}{here}".lower()


def numbered(mode: str, rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, 1):
        if mode == "list":
            num = (SESSION_NUMBER if r.get("type") == "session"
                   else list_number(r.get("number")) if r.get("desktop_id") else None)
        elif mode == "session":
            num = TOOL_NUMBERS.get(str(r.get("type")))
        elif mode in ("model", "effort"):
            num = r.get("n")
        else:
            num = i
        out.append({**r, "num": num} if num else r)
    return out


def matches(pattern: str, text: str) -> bool:
    at = 0
    for ch in pattern.lower():
        at = text.find(ch, at)
        if at < 0:
            return False
        at += 1
    return True


def cmd_list(as_json: bool) -> int:
    snap = DK.gather(shared=True)
    try:
        DK.panel_refresh(snap.desktops)
    except Exception:                                    # noqa: BLE001, the numbers below come from the list itself
        pass
    panel = DK.panel_of(snap.desktops)
    rows = DK.rows(snap, panel)
    add_notes(rows)
    add_live(rows)
    add_paused(rows)
    if as_json:
        print(json.dumps({"current": snap.current, "kwin": snap.desktops is not None, "unread": sorted(snap.unread),
                          "rows": rows},
                         indent=1, ensure_ascii=False))
        return 0
    if panel:
        add_panel(rows, panel["number"], snap.current, panel["id"])
    aux_id, aux_number = aux_pair(service_rows())
    add_aux(rows, aux_number, snap.current, aux_id)
    width = shutil.get_terminal_size((140, 40)).columns
    cols = columns(rows, width)
    for r in [{**SESSION_ROW, "pick": autoswap_pick()}] + rows:
        print(ANSI.sub("", format_row(r, width, False, cols)).rstrip())
    for a in sorted(snap.unread):
        print(f"  ({a}: its session list did not answer)")
    if snap.desktops is None:
        print("  (KWin is not answering: no desktop numbers)")
    return 0



FOCUS_REPORTS = {b"\x1b[I": "focus-in", b"\x1b[O": "focus-out"}
ESC_SPLIT = 0.05


class FocusTap:

    def __init__(self) -> None:
        self.held = b""
        self.since = 0.0

    def feed(self, data: bytes, now: float) -> tuple[bytes, list[str]]:
        buf, self.held = self.held + data, b""
        out, reports = bytearray(), []
        while buf:
            i = buf.find(b"\x1b")
            if i < 0:
                out += buf
                break
            out += buf[:i]
            buf = buf[i:]
            hit = next((seq for seq in FOCUS_REPORTS if buf.startswith(seq)), None)
            if hit:
                reports.append(FOCUS_REPORTS[hit])
                buf = buf[len(hit):]
            elif any(seq.startswith(buf) for seq in FOCUS_REPORTS):
                self.held, self.since = buf, now
                break
            else:
                out += buf[:1]
                buf = buf[1:]
        return bytes(out), reports

    def due(self, now: float) -> bytes:
        if self.held and now - self.since >= ESC_SPLIT:
            held, self.held = self.held, b""
            return held
        return b""


def quiet_pty(fd: int) -> None:
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (termios.error, OSError):
        pass


def match_size(fd: int, like: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, fcntl.ioctl(like, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def drain(fd: int, out: int, quiet: float = 0.05, most: float = 0.3) -> None:
    end = time.monotonic() + most
    while time.monotonic() < end:
        try:
            r, _, _ = select.select([fd], [], [], quiet)
        except OSError:
            return
        if not r:
            return
        try:
            data = os.read(fd, 65536)
        except OSError:
            return
        if not data:
            return
        os.write(out, data)
