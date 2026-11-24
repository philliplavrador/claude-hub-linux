"""The flag that skips Claude Code's permission prompts is off by default and on only with CLAUDE_HUB_SKIP_PERMISSIONS=1.

Two places hand it to the Claudes the hub starts: CLAUDE_DEFAULT_FLAGS in bin/claude-desks and DEFAULT_FLAGS in
bin/claude-window. Each is read the way the real script reads it (the first by loading the module, the second by
running its own two lines in bash), with the switch unset, set to 1 and set to something else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESKS = ROOT / "bin" / "claude-desks"
WINDOW = ROOT / "bin" / "claude-window"
FLAG = "--dangerously-skip-permissions"
SWITCH = "CLAUDE_HUB_SKIP_PERMISSIONS"

LOAD_DESKS = """
import importlib.machinery
import importlib.util
import sys

loader = importlib.machinery.SourceFileLoader("claude_desks_probe", sys.argv[1])
spec = importlib.util.spec_from_loader("claude_desks_probe", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
print("[" + " ".join(mod.CLAUDE_DEFAULT_FLAGS) + "]")
"""


def env_with(tmp_path: Path, value: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != SWITCH}
    env["HOME"] = str(tmp_path)
    if value is not None:
        env[SWITCH] = value
    return env


def desks_flags(tmp_path: Path, value: str | None) -> str:
    done = subprocess.run([sys.executable, "-I", "-c", LOAD_DESKS, str(DESKS)], capture_output=True, text=True,
                          env=env_with(tmp_path, value), cwd=tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


def window_flags(tmp_path: Path, value: str | None) -> str:
    """Runs the lines of claude-window that set DEFAULT_FLAGS, and nothing else of it."""
    lines = [l for l in WINDOW.read_text().splitlines() if l.startswith("DEFAULT_FLAGS=") or
             (SWITCH in l and "DEFAULT_FLAGS=" in l and l.startswith("["))]
    assert len(lines) == 2, lines
    script = "set -uo pipefail\n" + "\n".join(lines) + '\necho "[${DEFAULT_FLAGS[*]}]"\n'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env_with(tmp_path, value), cwd=tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


@pytest.mark.parametrize("reader", [desks_flags, window_flags])
def test_the_flag_is_off_when_the_switch_is_unset(tmp_path, reader):
    assert reader(tmp_path, None) == "[]"


@pytest.mark.parametrize("reader", [desks_flags, window_flags])
def test_the_flag_is_on_when_the_switch_is_1(tmp_path, reader):
    assert reader(tmp_path, "1") == f"[{FLAG}]"


@pytest.mark.parametrize("value", ["", "0", "yes", "true", "11"])
@pytest.mark.parametrize("reader", [desks_flags, window_flags])
def test_any_other_value_leaves_the_flag_off(tmp_path, reader, value):
    assert reader(tmp_path, value) == "[]"


def test_no_other_file_hands_the_flag_to_a_claude():
    """Only the two guarded defaults name the flag, so no other path starts a Claude with prompts skipped."""
    hits = []
    for folder in ("bin", "lib", "toolkit", "agent-bin"):
        for p in sorted((ROOT / folder).rglob("*")):
            if p.is_file() and not p.is_symlink():
                try:
                    text = p.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                hits += [p.relative_to(ROOT).as_posix() for _ in range(text.count(FLAG))]
    assert sorted(hits) == ["bin/claude-desks", "bin/claude-window"], hits


def test_the_readme_says_what_the_switch_does_and_that_it_is_off_by_default():
    text = (ROOT / "README.md").read_text()
    assert SWITCH in text and FLAG in text
    assert "off by default" in text.lower()
