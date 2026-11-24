# claude-hub, part 3b: the list's numbers: Tools is 1, a row's number, the typed number and its filter modes.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


SESSION_NUMBER = 1
TOOL_NUMBERS = {"new": 1, "desktop": 2, "usage": 3, "skills": 4}
FILTER_MODES = ("list", "session", "model", "effort", "skills")
NUMBER_DIGITS = 3


def list_number(n) -> int | None:
    if not n:
        return None
    return int(n) + SESSION_NUMBER


SAID_NUMBER = re.compile(r"^((?:claude-desks: )?(?:ended desktop|KWin did not remove desktop|KWin did not say "
                         r"whether desktop|KWin did not switch to desktop|desktop) )(\d+)\b")


def in_list_numbers(said: str) -> str:
    return SAID_NUMBER.sub(lambda m: f"{m.group(1)}{list_number(m.group(2))}", said, count=1)


def adopt_said(res) -> str | None:
    out = ANSI.sub("", str(res.stdout or ""))
    if res.returncode != 0:
        why = (ANSI.sub("", str(res.stderr or "")).strip() or out.strip()).replace("\n", " ")
        return f"{RED}{clip(in_list_numbers(why), 200) or 'claude-desks adopt failed'}{OFF}"
    lines = [line.strip() for line in out.splitlines()]
    left = [line for line in lines if line.startswith("not adopted")]
    if left:
        return f"{RED}{clip(left[-1], 200)}{OFF}"
    none = [line for line in lines if line.startswith("nothing to adopt")]
    return f"{DIM}{clip(none[-1], 200)}{OFF}" if none else None
