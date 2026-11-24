# claude-hub, part 3: the New Claude menus, the Desktop and Usage rows, Skills and the helpers' lookups.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


NEW_ROW = {"type": "new", "key": "new"}
MODEL_ROWS = tuple({"type": "model", "key": f"model:{m}", "n": n, "model": m, "label": label}
                   for n, (m, label) in enumerate((("fable", "Fable"), ("opus", "Opus"),
                                                   ("sonnet", "Sonnet"), ("haiku", "Haiku")), 1))
EFFORT_ROWS = tuple({"type": "effort", "key": f"effort:{e}", "n": n, "effort": e, "label": label}
                    for n, (e, label) in enumerate((("low", "Low"), ("medium", "Medium"), ("high", "High"),
                                                    ("xhigh", "XHigh"), ("max", "Max")), 1))
DEFAULT_MODEL, DEFAULT_EFFORT = "opus", "xhigh"


def model_label(model: str) -> str:
    return next((r["label"] for r in MODEL_ROWS if r["model"] == model), model)


DESKTOP_ROW = {"type": "desktop", "key": "new-desktop"}
NOT_FOR_CLAUDES = "refused: started from a Claude's shell"


def main_new_said(res) -> str | None:
    if res.returncode == 0:
        return None
    if res.returncode == 7:
        return f"{RED}{NOT_FOR_CLAUDES}{OFF}"
    why = last_line(res.stderr) or last_line(res.stdout)
    return f"{RED}{clip(why, 200) or 'claude-desks main-desktop new failed'}{OFF}"


def last_line(text) -> str:
    lines = [l.strip() for l in ANSI.sub("", str(text or "")).splitlines() if l.strip()]
    return lines[-1] if lines else ""
SESSION_ROW = {"type": "session", "key": "session"}
USAGE_ROW = {"type": "usage", "key": "usage"}
SKILLS_ROW = {"type": "skills", "key": "skills"}
SKILL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
SKILL_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,59}")
SKILL_KEY = re.compile(r"([a-z][a-z0-9_-]*):\s*(.*)")
SKILL_KEYS = ("name", "model", "effort", "summary")
SKILL_SPACE = re.compile(r"\s+")
SKILL_MAX_BYTES = 64 * 1024
_SKILLS_CACHE: dict = {}


def drawable_name(name: str) -> str:
    return plain_text(os.fsencode(name).decode("utf-8", "replace"))


def skills_dir() -> Path:
    named = os.environ.get("CLAUDE_HUB_HUB_SKILLS_DIR")
    return Path(named) if named else Path(__file__).resolve().parents[1] / "hub-skills"


def parse_skill(text: str, stem: str) -> dict | str:
    lines = text.split("\n")
    cut = next((i for i, ln in enumerate(lines) if ln.rstrip("\r") == "---"), None)
    if cut is None:
        return "no --- line"
    name = model = effort = None
    summary: list[str] = []
    in_summary = False
    for raw in lines[:cut]:
        s = raw.strip()
        if not s:
            continue
        if s == "-" or s.startswith("- "):
            if not in_summary:
                return "a - line outside the summary"
            summary.append(s[1:].strip())
            continue
        m = SKILL_KEY.fullmatch(s)
        if m and (m.group(1) in SKILL_KEYS or not (in_summary and summary)):
            key, value = m.group(1), m.group(2).strip()
            if key not in SKILL_KEYS:
                return f"unknown key {plain_text(key)[:30]}"
            in_summary = key == "summary"
            if key == "name":
                name = SKILL_SPACE.sub(" ", value)
            elif key == "model":
                model = value
            elif key == "effort":
                effort = value
            elif value:
                summary.append(value)
        elif in_summary and summary:
            summary[-1] = f"{summary[-1]} {s}".strip()
        elif in_summary:
            summary.append(s)
        else:
            return f"unknown line: {plain_text(s)[:30]}"
    if not name:
        return "no name"
    if len(name) > 60:
        return "name longer than 60 characters"
    if not model or not SKILL_MODEL.fullmatch(model):
        return "bad model"
    if effort not in SKILL_EFFORTS:
        return "bad effort"
    summary = [ln for ln in (SKILL_SPACE.sub(" ", ln).strip() for ln in summary) if ln]
    if not summary:
        return "no summary"
    body = lines[cut + 1:]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return "no prompt"
    return {"type": "skill", "key": f"skill:{stem}", "name": plain_text(name), "model": model, "effort": effort,
            "summary": [plain_text(ln) for ln in summary], "prompt": "\n".join(body)}


def hub_skills() -> tuple[list[dict], list[str]]:
    folder = skills_dir()
    try:
        names = sorted(p.name for p in folder.iterdir()
                       if p.name.endswith(".md") and p.name != "README.md" and not p.name.startswith("."))
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    except OSError as e:
        return [], [f"{drawable_name(folder.name)}: {e.strerror or 'could not be read'}"]
    skills: list[dict] = []
    unreadable: list[str] = []
    for gone in set(_SKILLS_CACHE) - {str(folder / n) for n in names}:
        _SKILLS_CACHE.pop(gone, None)
    for name in names:
        path = folder / name
        shown = drawable_name(name)
        try:
            st = path.stat()
            sig = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            cached = _SKILLS_CACHE.get(str(path))
            if cached and cached[0] == sig:
                result = cached[1]
            elif not stat.S_ISREG(st.st_mode):
                result = "not a file"
            elif st.st_size > SKILL_MAX_BYTES:
                result = "too long"
            else:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as fh:
                    regular = stat.S_ISREG(os.fstat(fh.fileno()).st_mode)
                    data = fh.read(SKILL_MAX_BYTES + 1) if regular else b""
                if not regular:
                    result = "not a file"
                elif len(data) > SKILL_MAX_BYTES:
                    result = "too long"
                else:
                    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
                    result = parse_skill(text, path.stem)
            _SKILLS_CACHE[str(path)] = (sig, result)
        except OSError as e:
            unreadable.append(f"{shown}: {e.strerror or 'could not be read'}")
            continue
        except ValueError:
            unreadable.append(f"{shown}: not text")
            continue
        if isinstance(result, dict):
            skills.append({**result, "summary": list(result["summary"])})
        else:
            unreadable.append(f"{shown}: {result}")
    return skills, unreadable


def title_cmd() -> str:
    return os.environ.get("CLAUDE_HUB_TITLE_CMD") or str(Path(__file__).resolve().parent / "claude-title")


def run_quiet(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except Exception as e:                                # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", str(e))
