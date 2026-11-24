# claude-hub, part 2: the auto-swap watcher's note, wait words, the rows (claude-desks's contract), clipping.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations



AUTOSWAP_STATE = Path(os.environ.get("CLAUDE_HUB_AUTOSWAP_STATE") or DK.STATE_HOME / "claude-autoswap.json")
AUTOSWAP_FRESH_FOR = 600
AUTOSWAP_FROM_SHOWN = 2 * 3600


def autoswap_state() -> dict:
    try:
        data = json.loads(AUTOSWAP_STATE.read_text())
    except Exception:                                # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        at = float(data.get("at") or 0)
    except (TypeError, ValueError, OverflowError, OSError):
        return {}
    return data if time.time() - at <= AUTOSWAP_FRESH_FOR else {}


def is_fable_model(model) -> bool:
    return "fable" in str(model or "").lower()


def autoswap_pick(model: str = "") -> str:
    pick = autoswap_state().get("recommended_fable" if is_fable_model(model) else "recommended")
    return pick if isinstance(pick, str) and pick in DK.accounts() and not DK.paused(pick) else ""


def autoswap_notes() -> dict[str, str]:
    data = autoswap_state()
    out = {}
    for sid, c in (data.get("claudes") or {}).items():
        if not isinstance(c, dict):
            continue
        fresh_move = False
        if c.get("moved_at") and c.get("from"):
            try:
                fresh_move = time.time() - float(c["moved_at"]) <= AUTOSWAP_FROM_SHOWN
            except (TypeError, ValueError, OverflowError, OSError):
                fresh_move = False
        if fresh_move:
            out[sid] = f"from {c['from']}"
        elif c.get("waits_until"):
            try:
                when = time.strftime("%-I:%M%p", time.localtime(float(c["waits_until"]))).lower()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            out[sid] = f"waits till {when}"
    return out


def add_notes(rows: list[dict]) -> None:
    notes = autoswap_notes()
    for r in rows:
        sid = r.get("session_id") or ""
        r["note"] = row_note(sid, notes.get(sid, ""))


CONSOLE_CACHE = Path(os.environ.get("CLAUDE_HUB_CONSOLE_CACHE") or Path.home() / ".cache" / "claude-console")
MOD_FILE_FRESH = 180
HHMM = re.compile(r"\d{2}:\d{2}")
UNTIL = re.compile(r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?\d{2}:\d{2}")


def finite_number(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def mod_file(session_id, kind: str) -> dict:
    sid = re.sub(r"[^a-zA-Z0-9-]", "", str(session_id or ""))
    if not sid:
        return {}
    try:
        data = json.loads((CONSOLE_CACHE / f"{sid}.{kind}.json").read_text())
    except Exception:                                # noqa: BLE001
        return {}
    if not isinstance(data, dict) or data.get("v") != 1:
        return {}
    seen = data.get("seen")
    if not finite_number(seen) or time.time() * 1000 - seen > MOD_FILE_FRESH * 1000:
        return {}
    return data


def wait_words(w: dict) -> str:
    kind = w.get("kind")
    if not kind:
        return ""
    until = w.get("until") if isinstance(w.get("until"), str) and UNTIL.fullmatch(w["until"]) else ""
    count = w.get("count") if isinstance(w.get("count"), int) and not isinstance(w.get("count"), bool) else 0
    if kind == "question":
        return "your answer"
    if kind == "usage":
        return f"usage resets {until}" if until else "usage at its limit"
    if kind == "helpers":
        return f"{count} helper{'' if count == 1 else 's'}" if count > 0 else ""
    return "".join(c for c in str(w.get("detail") or "") if c.isprintable())[:40]


def eta_words(e: dict) -> str:
    done, due = e.get("itemDone"), e.get("itemDoneMs")
    if e.get("closed") or not isinstance(done, str) or not HHMM.fullmatch(done):
        return ""
    if not finite_number(due):
        return ""
    late = int((time.time() * 1000 - due) // 60000)
    if late < 1:
        return f"done ~{done}"
    h, m = divmod(late, 60)
    return f"done ~{done}, {f'{h}h {m}m' if h else f'{m}m'} late"


def row_note(session_id, swap: str = "") -> str:
    try:
        w = mod_file(session_id, "waiting")
        words = "" if w.get("kind") == "usage" and swap.startswith("waits till") else wait_words(w)
    except Exception:                                # noqa: BLE001
        words = ""
    try:
        eta = eta_words(mod_file(session_id, "eta"))
    except Exception:                                # noqa: BLE001
        eta = ""
    parts = (f"waits: {words}" if words else "", swap, eta)
    return " · ".join(p for p in parts if p)


def live_of(session_id) -> dict:
    sid = re.sub(r"[^a-zA-Z0-9-]", "", str(session_id or ""))
    if not sid:
        return {}
    try:
        data = json.loads((CONSOLE_CACHE / f"{sid}.live.json").read_text())
    except Exception:                                # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def short_model(name) -> str:
    s = str(name or "").strip()
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?(?:\[\w+\])?", s)
    if m:
        s = f"{m[1].title()} {m[2]}" + (f".{m[3]}" if m[3] else "")
    return re.sub(r"\s*\([^)]*\)$", "", s)


EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "XHigh", "max": "Max", "fast": "Fast"}


def effort_label(level) -> str:
    s = level.strip() if isinstance(level, str) else ""
    return EFFORT_LABELS.get(s.lower(), s[:1].upper() + s[1:])


def kilo(tokens) -> str:
    if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or not tokens > 0:
        return ""
    return f"{round(tokens / 1000)}k"


def add_live(rows: list[dict]) -> None:
    for r in rows:
        live = live_of(r.get("session_id"))
        tokens = live.get("tokens")
        r["model"] = short_model(live.get("model"))
        r["effort"] = effort_label(live.get("effort"))
        r["tokens"] = int(tokens) if kilo(tokens) else None


def add_paused(rows: list[dict]) -> None:
    seen: dict[str, bool] = {}
    for r in rows:
        account = str(r.get("account") or "")
        if account:
            if account not in seen:
                seen[account] = DK.paused(account)
            r["paused"] = seen[account]


def strike_names(text: str, names: list) -> str:
    for name in names:
        text = re.sub(rf"(?<![\w-]){re.escape(str(name))}(?![\w-])", lambda hit: STRIKE + hit.group() + NOSTRIKE, text)
    return text


def pad_ansi(s: str, n: int) -> str:
    return s + " " * max(0, n - len(ANSI.sub("", s)))


def main_number(r: dict) -> int:
    try:
        return max(1, int(r.get("main") or 1))
    except (TypeError, ValueError):
        return 1


def main_name(r: dict) -> str:
    n = main_number(r)
    return "Main Desktop" if n == 1 else f"Main Desktop {n}"


def first_main_key(rows: list[dict]) -> str | None:
    mine = [r for r in rows if r.get("yours")]
    return min(mine, key=main_number).get("key") if mine else None


def sticky_home_row(rows: list[dict]) -> dict | None:
    mine = [r for r in rows if r.get("yours")]
    if not mine:
        return None
    here = next((r for r in mine if r.get("current")), None)
    if here is None:
        try:
            home = str((DK.main_state() or {}).get("home") or "")
        except Exception:                                # noqa: BLE001 — a file that cannot be read: desktop 1
            home = ""
        here = next((r for r in mine if home and r.get("desktop_id") == home), None)
    return here or min(mine, key=main_number)


def add_sticky(rows: list[dict], screens: list[dict]) -> None:
    words = [str(s.get("word") or s.get("name") or "") for s in screens if s.get("sticky")]
    for r in rows:
        if r.get("yours"):
            r.pop("sticky", None)
    row = sticky_home_row(rows)
    if row is not None:
        row["sticky"] = words



CLOSE_FRESH_FOR = 600
CLOSE_LIVE = ("closing", "asking")
CLOSE_END = ("closed", "kept", "stopped")


def close_dir() -> Path:
    stood = os.environ.get("CLAUDE_HUB_MAIN_CLOSE_DIR")
    return Path(stood) if stood else Path(getattr(DK, "MAIN_CLOSE_DIR", None) or DK.STATE_HOME / "main-desktops")


def read_close(did: str) -> dict | None:
    try:
        data = json.loads((close_dir() / f"close-{did}.json").read_text())
    except Exception:                                    # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("state") not in CLOSE_LIVE + CLOSE_END:
        return None
    windows = data.get("windows")
    try:
        at = float(data.get("at") or 0)
    except (TypeError, ValueError, OverflowError):
        at = 0.0
    return {"state": data["state"], "at": at,
            "windows": [str(w) for w in windows] if isinstance(windows, list) else []}


def close_lock_held(did: str) -> bool:
    try:
        with open(close_dir() / f"close-{did}.lock", "rb") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def close_state(did: str, mine: dict | None) -> dict | None:
    data = read_close(did)
    start = mine.get("start") if mine is not None else None
    proc = mine.get("proc") if mine is not None else None
    running = proc is not None and proc.poll() is None

    def alive() -> bool:
        return running or close_lock_held(did)

    if data is not None and start is not None and data["at"] < start - 1 \
            and not (data["state"] in CLOSE_LIVE and close_lock_held(did)):
        data = None

    if mine is not None and mine.get("failed") and (data is None or (data["state"] in CLOSE_LIVE and not alive())):
        ended = mine.get("ended")
        if ended is not None and time.time() - ended > CLOSE_FRESH_FOR:
            return None
        return {"state": "failed", "why": str(mine["failed"]), "windows": []}
    if data is None:
        return {"state": "closing", "windows": []} if mine is not None and alive() else None
    if data["state"] in CLOSE_LIVE:
        return data if alive() else None
    if time.time() - data["at"] <= CLOSE_FRESH_FOR:
        return data
    return None


def close_words(c: dict | None) -> tuple[str, str]:
    if not c:
        return "", ""
    state = c.get("state")
    if state == "closing":
        return "closing", YELLOW
    if state == "asking":
        return "answer the question there", YELLOW
    if state == "closed":
        return "closed", DIM
    if state == "stopped":
        return "stopped: kept open", DIM
    if state == "kept":
        windows = c.get("windows") or []
        n = len(windows)
        if not n:
            return "kept: windows still open", YELLOW
        return f"kept: {n} window{'' if n == 1 else 's'} open: {', '.join(windows)}", YELLOW
    return str(c.get("why") or "close failed"), RED



def service_rows() -> dict | None:
    try:
        data = json.loads(DK.HUB_ROWS.read_text())
        at = float(data.get("at") or 0)
        rows = data.get("rows")
    except Exception:                                # noqa: BLE001
        return None
    if not isinstance(rows, list) or time.time() - at > FRESH_FOR:
        return None
    data["rows"] = [r for r in rows if isinstance(r, dict)]
    return data


def add_special(rows: list[dict], kind: str, name: str, number, current: str | None, did: str | None) -> None:
    if not did or not isinstance(number, int) or isinstance(number, bool) or number < 2:
        return
    at = max((i for i, r in enumerate(rows) if r.get("yours")), default=None)
    if at is None or any(r.get("type") == kind for r in rows):
        return
    if any(r.get("number") == number or (r.get("desktop_id") and r.get("desktop_id") == did) for r in rows):
        return
    while (at + 1 < len(rows) and rows[at + 1].get("type") in ("desktop", "panel", "aux")
           and not rows[at + 1].get("desktop_gone") and 0 < (rows[at + 1].get("number") or 0) < number):
        at += 1
    rows.insert(at + 1, {"key": f"d:{did}", "type": kind, "number": number, "desktop_id": did,
                         "desktop_gone": False, "yours": False, "current": bool(current) and did == current,
                         "name": name, "account": "", "state": kind, "needs": None, "session_id": "",
                         "by_him": False, "id": "", "kind": "", "konsole_pid": 0, "started": 0, "cwd": ""})


def add_panel(rows: list[dict], number, current: str | None, fid: str | None = None) -> None:
    add_special(rows, "panel", DK.PANEL_NAME, number, current, fid or DK.panel_id())


AUX_NAME = "Aux Desktop"


def aux_pair(data: dict | None) -> tuple[str | None, int]:
    pid, number = (data or {}).get("aux_id"), (data or {}).get("aux_at")
    if not isinstance(pid, str) or not pid or not isinstance(number, int) or isinstance(number, bool) or number < 2:
        return None, 0
    return pid, number


def add_aux(rows: list[dict], number, current: str | None, pid: str | None) -> None:
    add_special(rows, "aux", AUX_NAME, number, current, pid)


def mark_current(rows: list[dict], current: str | None) -> None:
    for r in rows:
        r["current"] = bool(current) and r.get("desktop_id") == current and not r.get("desktop_gone")


STATE_WORDS = {"yours": "Main Desktop", "working": "working", "parked": "waiting on a task",
               "waiting": "waiting for you", "idle": "idle",
               "stopped": "stopped", "no-window": "no window", "starting": "starting…", "looking": "looking…",
               "no-claude": "no Claude", "background": "background", "plain": "plain"}
STATE_COLOUR = {"working": GREEN, "waiting": YELLOW, "idle": BLUE, "parked": BLUE}


def state_words(r: dict) -> tuple[str, str]:
    s = str(r.get("state") or "")
    if s == "needs-you":
        needs = str(r.get("needs") or "")
        return "NEEDS YOU" + (f" · {needs}" if needs else ""), BOLD + YELLOW
    return STATE_WORDS.get(s, s), STATE_COLOUR.get(s, DIM)


def display_name(r: dict) -> str:
    name, account = str(r.get("name") or ""), str(r.get("account") or "")
    if account and name.endswith(f" · {account}"):
        name = name[: -len(f" · {account}")]
    if not name:
        name = "(no title yet)" if r.get("kind") == "background" else "claude"
    return name


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def clip_ansi(s: str, n: int) -> str:
    if len(ANSI.sub("", s)) <= n:
        return s
    kept, seen, i = [], 0, 0
    while i < len(s) and seen < max(0, n - 1):
        code = ANSI.match(s, i)
        if code:
            kept.append(code.group())
            i = code.end()
        else:
            kept.append(s[i])
            seen += 1
            i += 1
    return "".join(kept) + "…" + OFF
