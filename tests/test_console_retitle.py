"""Tests for the rename-the-focused-Claude-window shortcut.

Run: python3 -m pytest tests/test_console_retitle.py

  bin/claude-console-retitle    focused window -> its Claude -> a new name
  bin/claude-console-strip        `retitle`, and the birth-name rule
  bin/lib/claude-tab.js           the inherited-tab guard
  bin/claude-console-summarize    --whole --print, and your spelling

Konsole is a fake `qdbus6` over a JSON file of tabs, the model a fake `curl`; the "konsole" and
"claude" processes are real processes under those names, because the helper reads /proc.
Nothing here touches your screen, your Konsole windows or the real model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
RETITLE = BIN / "claude-console-retitle"
STRIP = BIN / "claude-console-strip"
SUMMARIZE = BIN / "claude-console-summarize"
SID = "11111111-2222-4333-8444-555555555555"

FAKE_QDBUS = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ["FAKE_KONSOLE"]
k = json.load(open(state))
a = sys.argv[1:]
with open(os.environ["FAKE_KONSOLE"] + ".log", "a") as f:
    f.write(" ".join(a) + "\n")
def svc(s):
    return k.get("names", {}).get(s, s)
if len(a) == 1:
    if a[0] not in k.get("objects", {}): sys.exit(1)
    print("\n".join(k["objects"][a[0]])); sys.exit(0)
if a[:3] == ["org.freedesktop.DBus", "/", "org.freedesktop.DBus.GetNameOwner"]:
    if a[3] not in k.get("names", {}): sys.exit(1)
    print(k["names"][a[3]]); sys.exit(0)
if len(a) >= 3 and a[2] == "org.kde.konsole.Session.title":
    t = k.get("titles", {}).get(svc(a[0]) + " " + a[1])
    if t is None: sys.exit(1)
    print(t); sys.exit(0)
if len(a) >= 3 and a[2] == "org.kde.konsole.Session.setTitle":
    key = svc(a[0]) + " " + a[1]
    if key not in k.get("titles", {}): sys.exit(1)
    k["titles"][key] = " ".join(a[4:])
    json.dump(k, open(state, "w")); sys.exit(0)
ans = k.get("answers", {}).get(" ".join(a))
if ans is None:
    sys.exit(0 if "QAction" in " ".join(a) or "activateAction" in " ".join(a) else 1)
print(ans)
'''

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
body = json.loads(sys.stdin.read())
open(os.environ["FAKE_PROMPT"], "w").write(body["prompt"])
print(json.dumps({"response": os.environ.get("FAKE_ANSWER", "")}))
'''

LOGGER = '''#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
{tail}
'''


def user(text: str, meta: bool = False) -> str:
    rec = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    if meta:
        rec["isMeta"] = True
    return json.dumps(rec)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="retitle-test-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.accounts = self.tmp / "accounts"
        self.records = self.tmp / "records"
        self.records.mkdir()
        self.konsole_state = self.tmp / "konsole.json"
        self.write_konsole({})
        for name, body in (("qdbus6", FAKE_QDBUS), ("curl", FAKE_CURL)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_konsole(self, k: dict):
        self.konsole_state.write_text(json.dumps(k))

    def konsole(self) -> dict:
        return json.loads(self.konsole_state.read_text())

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "KONSOLE_"))}
        env.update(PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home),
                   FAKE_KONSOLE=str(self.konsole_state), FAKE_PROMPT=str(self.tmp / "prompt.txt"),
                   CLAUDE_HUB_WINDOWS_DIR=str(self.records), CLAUDE_HUB_ACCOUNTS=str(self.accounts))
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def stand_in(self, name: str) -> Path:
        exe = self.tmp / "procs" / name
        exe.parent.mkdir(exist_ok=True)
        exe.write_text('#!/bin/sh\nsleep "${1:-60}"\n')
        exe.chmod(0o755)
        return exe

    def process_named(self, name: str, **env) -> subprocess.Popen:
        p = subprocess.Popen([str(self.stand_in(name))], env=self.env(**env), start_new_session=True)
        self.procs.append(p)
        return p

    def state(self, sid=SID) -> dict:
        f = self.home / ".cache/claude-console" / f"{sid}.state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def set_state(self, value: dict, sid=SID):
        d = self.home / ".cache/claude-console"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.state.json").write_text(json.dumps(value))


class RetitleHelper(Base):

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "calls.log"
        self.namer = self.logger("namer", 'case "$1" in\n'
                                          '  get) echo "${FAKE_CURRENT-Old name}"; exit 0 ;;\n'
                                          '  set) shift 3; echo "$*"; exit ${FAKE_SET_RC:-0} ;;\n'
                                          'esac\n'
                                          'echo "${FAKE_TITLE-Fix the login page bug}"\n'
                                          '[ -n "${FAKE_TITLE-x}" ] || exit 1\n'
                                          'exit ${FAKE_NAMER_RC:-0}')
        self.ask = self.logger("ask", '[ "${FAKE_ASK_RC:-0}" = 0 ] || exit $FAKE_ASK_RC\n'
                                      'echo "${FAKE_TYPED-Photo approval notes}"')
        self.summarize = self.logger("summarize", "echo Should never be asked")
        self.strip = self.logger("strip", "exit ${FAKE_STRIP_RC:-0}")
        self.notify = self.logger("notify", "")
        conv = self.accounts / "kappa/projects/-home-user-workspace"
        conv.mkdir(parents=True)
        self.transcript = conv / f"{SID}.jsonl"
        self.transcript.write_text(user("look into my login page bug") + "\n")

    def logger(self, name: str, tail: str) -> Path:
        p = self.bin / f"fake-{name}"
        p.write_text(LOGGER.format(log=f"{self.tmp}/{name}.log", tail=tail))
        p.chmod(0o755)
        return p

    def lines(self, name: str) -> list[str]:
        f = self.tmp / f"{name}.log"
        return f.read_text().splitlines() if f.exists() else []

    def run_helper(self, pid, typed=False, **env) -> subprocess.CompletedProcess:
        cmd = ["node", str(RETITLE), *(["--type"] if typed else []), str(pid)]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              env=self.env(CLAUDE_HUB_RETITLE_SUMMARIZE=self.summarize,
                                           CLAUDE_HUB_RETITLE_NAMER=self.namer,
                                           CLAUDE_HUB_RETITLE_ASK=self.ask,
                                           CLAUDE_HUB_RETITLE_STRIP=self.strip,
                                           CLAUDE_HUB_RETITLE_NOTIFY=self.notify, **env))

    def one_tab_konsole(self, pid: int, shell_pid: int = 1, title="claude · 18:44"):
        svc = f"org.kde.konsole-{pid}"
        self.write_konsole({
            "objects": {svc: ["/", "/Sessions", "/Sessions/1", "/Windows", "/Windows/1", "/konsole/MainWindow_1"]},
            "names": {svc: ":1.5347"},
            "titles": {":1.5347 /Sessions/1": title},
            "answers": {f"{svc} /Windows/1 org.kde.konsole.Window.currentSession": "1",
                        f"{svc} /Sessions/1 org.kde.konsole.Session.processId": str(shell_pid)},
        })

    def test_a_background_window_is_found_by_its_record_and_renamed(self):
        k = self.process_named("konsole")
        self.one_tab_konsole(k.pid)
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "id": "11111111", "session_id": SID,
            "konsole_service": ":1.5347", "konsole_session": "/Sessions/1", "attached_at": 1}))
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("namer"), [f"rename kappa {SID}"])
        self.assertEqual(self.lines("summarize"), [], "one namer: the hub's")
        self.assertEqual(self.lines("strip"), [f"retitle {SID} :1.5347 /Sessions/1 Fix the login page bug"])

    def test_a_claude_started_in_the_tab_is_found_by_its_process(self):
        k = self.process_named("konsole")
        cfg = self.accounts / "kappa"
        shell = subprocess.Popen(["bash", "-c", f"{self.stand_in('claude')} 60; :"],
                                 env=self.env(CLAUDE_CONFIG_DIR=cfg), start_new_session=True)
        self.procs.append(shell)
        child = None
        for _ in range(100):
            kids = subprocess.run(["pgrep", "-P", str(shell.pid)], capture_output=True, text=True).stdout.split()
            if kids:
                child = kids[0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(child, "the stand-in claude never started")
        (cfg / "sessions").mkdir(parents=True)
        (cfg / "sessions" / f"{child}.json").write_text(json.dumps({"pid": int(child), "sessionId": SID}))
        self.one_tab_konsole(k.pid, shell_pid=shell.pid)
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [f"retitle {SID} :1.5347 /Sessions/1 Fix the login page bug"])

    def test_a_window_that_is_not_konsole_gets_a_notice_and_nothing_else(self):
        other = self.process_named("firefox")
        r = self.run_helper(other.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [])
        self.assertEqual(self.lines("namer"), [])
        self.assertTrue(any("Not a Claude window" in l for l in self.lines("notify")), self.lines("notify"))

    def test_a_konsole_with_no_claude_in_it_is_left_alone(self):
        k = self.process_named("konsole")
        shell = self.process_named("bash-stand-in")
        self.one_tab_konsole(k.pid, shell_pid=shell.pid)
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("Not a Claude window" in l for l in self.lines("notify")))

    def test_no_name_from_the_model_says_so_and_changes_nothing(self):
        k = self.process_named("konsole")
        self.one_tab_konsole(k.pid)
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.5347", "konsole_session": "/Sessions/1"}))
        r = self.run_helper(k.pid, FAKE_TITLE="")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("Could not come up with a name" in l for l in self.lines("notify")))

    def test_what_a_namer_that_failed_printed_is_not_taken_for_a_name(self):
        k = self.process_named("konsole")
        self.one_tab_konsole(k.pid)
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.5347", "konsole_session": "/Sessions/1"}))
        r = self.run_helper(k.pid, FAKE_TITLE="Traceback (most recent call last):", FAKE_NAMER_RC=1)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines("namer"), [f"rename kappa {SID}"], "the harness did ask")
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("Could not come up with a name" in l for l in self.lines("notify")))


    def a_claude_window(self) -> int:
        k = self.process_named("konsole")
        self.one_tab_konsole(k.pid)
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.5347", "konsole_session": "/Sessions/1"}))
        return k.pid

    def test_what_he_types_names_the_row_and_the_bar(self):
        r = self.run_helper(self.a_claude_window(), typed=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        asked, = self.lines("ask")
        self.assertTrue(asked.startswith("ask --title Name this Claude --value Old name --hint "), asked)
        self.assertEqual(self.lines("namer"), [f"get {SID}", f"set kappa {SID} Photo approval notes"])
        self.assertEqual(self.lines("strip"), [f"retitle {SID} :1.5347 /Sessions/1 Photo approval notes"])
        self.assertEqual(self.lines("notify"), [], "it worked: nothing to say")

    def test_the_box_opens_on_the_name_it_has_and_on_nothing_when_it_has_none(self):
        self.run_helper(self.a_claude_window(), typed=True, FAKE_CURRENT="")
        asked, = self.lines("ask")
        self.assertTrue(asked.startswith("ask --title Name this Claude --value  --hint "), asked)

    def test_leaving_the_box_changes_nothing_and_says_nothing(self):
        r = self.run_helper(self.a_claude_window(), typed=True, FAKE_ASK_RC=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.lines("ask")), 1, "the harness did open the box")
        self.assertEqual(self.lines("namer"), [f"get {SID}"])
        self.assertEqual((self.lines("strip"), self.lines("notify")), ([], []))

    def test_a_box_that_cannot_open_says_so(self):
        r = self.run_helper(self.a_claude_window(), typed=True, FAKE_ASK_RC=3)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("Could not open the name box" in l for l in self.lines("notify")), self.lines("notify"))

    def test_a_name_that_cannot_be_used_says_so_and_changes_nothing(self):
        r = self.run_helper(self.a_claude_window(), typed=True, FAKE_SET_RC=1)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("cannot be used" in l for l in self.lines("notify")), self.lines("notify"))

    def test_a_claude_with_no_saved_chat_can_be_named_by_hand(self):
        pid = self.a_claude_window()
        self.transcript.unlink()
        r = self.run_helper(pid)
        self.assertTrue(any("nothing to name yet" in l for l in self.lines("notify")), "the control: Meta+N")
        r = self.run_helper(pid, typed=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [f"retitle {SID} :1.5347 /Sessions/1 Photo approval notes"])

    def test_a_window_that_is_not_a_claude_opens_no_box(self):
        other = self.process_named("firefox")
        r = self.run_helper(other.pid, typed=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.lines("ask"), self.lines("namer"), self.lines("strip")), ([], [], []))
        self.assertTrue(any("Not a Claude window" in l for l in self.lines("notify")), self.lines("notify"))

    def test_a_name_that_did_not_change_is_said_out_loud(self):
        k = self.process_named("konsole")
        self.one_tab_konsole(k.pid)
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.5347", "konsole_session": "/Sessions/1"}))
        self.run_helper(k.pid)
        self.assertTrue(any("Still the best name" in l for l in self.lines("notify")), self.lines("notify"))

    def test_the_tab_on_screen_is_picked_from_a_konsole_with_two_windows(self):
        k = self.process_named("konsole")
        svc = f"org.kde.konsole-{k.pid}"
        self.write_konsole({
            "objects": {svc: ["/Sessions/1", "/Sessions/2", "/Windows/1", "/Windows/2",
                              "/konsole/MainWindow_1", "/konsole/MainWindow_2"]},
            "names": {svc: ":1.9"},
            "titles": {":1.9 /Sessions/1": "first", ":1.9 /Sessions/2": "second"},
            "answers": {f"{svc} /Windows/1 org.kde.konsole.Window.currentSession": "1",
                        f"{svc} /Windows/2 org.kde.konsole.Window.currentSession": "2",
                        f"{svc} /konsole/MainWindow_1 org.qtproject.Qt.QWidget.isActiveWindow": "false",
                        f"{svc} /konsole/MainWindow_2 org.qtproject.Qt.QWidget.isActiveWindow": "true",
                        f"{svc} /konsole/MainWindow_2 org.qtproject.Qt.QWidget.windowTitle": "second"},
        })
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.9", "konsole_session": "/Sessions/2"}))
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [f"retitle {SID} :1.9 /Sessions/2 Fix the login page bug"])

    def two_windows_same_title(self, pid: int, active: int | None):
        svc = f"org.kde.konsole-{pid}"
        answers = {f"{svc} /Windows/1 org.kde.konsole.Window.currentSession": "1",
                   f"{svc} /Windows/2 org.kde.konsole.Window.currentSession": "2"}
        for n in (1, 2):
            answers[f"{svc} /konsole/MainWindow_{n} org.qtproject.Qt.QWidget.isActiveWindow"] = str(n == active).lower()
            answers[f"{svc} /konsole/MainWindow_{n} org.qtproject.Qt.QWidget.windowTitle"] = "claude · 18:44"
        self.write_konsole({
            "objects": {svc: ["/Sessions/1", "/Sessions/2", "/Windows/1", "/Windows/2",
                              "/konsole/MainWindow_1", "/konsole/MainWindow_2"]},
            "names": {svc: ":1.9"},
            "titles": {":1.9 /Sessions/1": "claude · 18:44", ":1.9 /Sessions/2": "claude · 18:44"},
            "answers": answers,
        })
        for n in (1, 2):
            sid = SID[:-1] + str(n)
            (self.records / f"{sid}.json").write_text(json.dumps({
                "account": "kappa", "session_id": sid, "konsole_service": ":1.9", "konsole_session": f"/Sessions/{n}"}))
            (self.transcript.parent / f"{sid}.jsonl").write_text(user("anything at all") + "\n")

    def test_two_windows_with_the_same_title_are_told_apart_by_the_active_one(self):
        k = self.process_named("konsole")
        self.two_windows_same_title(k.pid, active=2)
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.lines("strip"), [f"retitle {SID[:-1]}2 :1.9 /Sessions/2 Fix the login page bug"])

    def test_with_no_active_window_to_go_on_it_renames_nothing(self):
        k = self.process_named("konsole")
        self.two_windows_same_title(k.pid, active=None)
        r = self.run_helper(k.pid)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines("strip"), [])
        self.assertTrue(any("Could not tell which" in l for l in self.lines("notify")), self.lines("notify"))


class NamerBudget(unittest.TestCase):

    WRAPPER_WORST_PAST = 51
    STRIP = 15
    D_BUS = 20

    def title_constants(self):
        text = (BIN / "claude-title").read_text()
        return tuple(map(int, re.search(r"^CLAUDE_WAIT, LOCAL_WAIT, STRIP_WAIT = (\d+), (\d+), (\d+)",
                                        text, re.M).groups()))

    def namer_seconds(self):
        text = RETITLE.read_text()
        call = re.search(r"spawnSync\(NAMER, \['rename'[^\n]*\n\s*\{[^}]*timeout: (\w+) \}", text)
        self.assertTrue(call, "the namer call has a timeout")
        value = call.group(1)
        if not value.isdigit():
            value = re.search(rf"^const {value} = (\d+);", text, re.M).group(1)
        return int(value) / 1000

    def unit_seconds(self):
        unit = ROOT / "systemd/claude-console-retitle@.service"
        return int(re.search(r"^TimeoutStartSec=(\d+)", unit.read_text(), re.M).group(1))

    def test_the_namer_has_room_for_the_wrappers_worst_case(self):
        claude, local, strip = self.title_constants()
        self.assertGreaterEqual(self.namer_seconds(), claude + self.WRAPPER_WORST_PAST + local + strip)

    def test_the_helper_passes_the_namer_budget_to_the_namer_call(self):
        text = RETITLE.read_text()
        self.assertRegex(text, r"spawnSync\(NAMER, \['rename'[^\n]*\n\s*\{ encoding: 'utf8', timeout: NAMER_MS \}\)")

    def test_the_unit_covers_the_namer_the_strip_and_the_d_bus_reads(self):
        self.assertGreaterEqual(self.unit_seconds(), self.namer_seconds() + self.STRIP + self.D_BUS)


class StripRetitle(Base):

    def setUp(self):
        super().setUp()
        self.write_konsole({"titles": {":1.5 /Sessions/1": "claude · 18:44", ":1.old /Sessions/1": "Get printer setup"}})

    def strip(self, *args, stdin: dict | None = None, wrap_konsole=False, **env) -> subprocess.CompletedProcess:
        cmd = ["node", str(STRIP), *args]
        if wrap_konsole:
            tab = self.tmp / "tab" / "konsole"
            tab.parent.mkdir(exist_ok=True)
            tab.write_text('#!/bin/sh\n"$@"\n')
            tab.chmod(0o755)
            cmd = [str(tab), *cmd]
        return subprocess.run(cmd, input=json.dumps(stdin or {}), capture_output=True, text=True, timeout=30,
                              env=self.env(**env))

    def title(self, key=":1.5 /Sessions/1") -> str:
        return self.konsole()["titles"][key]

    def write_name(self, name, sid=SID, by="haiku"):
        d = self.home / ".cache/claude-console"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.title.json").write_text(json.dumps({"title": name, "by": by}))

    def test_a_title_this_wrote_without_a_dot_is_still_its_own(self):
        self.shown_in_its_tab()
        r = self.strip("summary", SID, "Photo approval notes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.title(), "Photo approval notes", "no state, no dot: the case")
        r = self.strip("retitle", SID, ":1.5", "/Sessions/1", "Photo approval notes, batch 2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.title(), "Photo approval notes, batch 2")
        r = self.strip("summary", SID, "Photo approval notes, batch 3")
        self.assertEqual(self.title(), "Photo approval notes, batch 3")
        self.assertNotIn("pinned", self.state())

    def test_a_name_he_typed_himself_is_drawn_letter_for_letter(self):
        self.shown_in_its_tab()
        for mine in ("iPad layout", "the one where I cannot get the printer to work at all, again, on the spare PC"):
            self.set_state({"summary": "finish the aurora wipe"})
            self.write_name(mine, by="him")
            self.strip("state", "done", stdin={"session_id": SID})
            self.assertIn(f"\U0001F7E2  {mine}", self.title())

    def shown_in_its_tab(self):
        (self.records / f"{SID}.json").write_text(json.dumps({"konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))

    def test_the_title_bar_says_what_the_hub_row_says(self):
        self.shown_in_its_tab()
        self.set_state({"summary": "tidy the sample notes and also list the two files i am editing"})
        self.write_name("Page items to the calendar")
        self.strip("state", "done", stdin={"session_id": SID})
        self.assertIn("\U0001F7E2  Page items to the calendar", self.title())
        self.assertNotIn("meta + A", self.title())

    def test_a_late_answer_from_the_strips_own_summarizer_does_not_take_the_name_back(self):
        self.shown_in_its_tab()
        self.write_name("Page items to the calendar")
        self.set_state({"summary": "add the page to the launcher", "state": "running"})
        r = self.strip("summary", SID, "Calendar sync integration")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Page items to the calendar", self.title())
        self.assertNotIn("Calendar sync", self.title())

    def test_a_written_name_that_is_not_one_is_not_drawn(self):
        self.shown_in_its_tab()
        for bad in ("Calendar \x1b]0;pwned\x07 meetings", "Calendar \u202emeetings to calendar", "Calendar\x9b31m meetings",
                    "Page items\nto calendar", 7, "", None, "word " * 40):
            k = self.konsole()
            k["titles"][":1.5 /Sessions/1"] = "claude · 18:44"
            self.write_konsole(k)
            self.set_state({"summary": "finish the aurora wipe"})
            self.write_name(bad)
            r = self.strip("state", "done", stdin={"session_id": SID}, CLAUDE_CONSOLE_DEBUG="1")
            self.assertEqual(r.stderr, "", repr(bad))
            self.assertIn("Finish the aurora wipe", self.title(), repr(bad))
            self.assertNotIn("pwned", self.title())

    def test_a_name_he_typed_by_hand_still_wins_over_the_hubs(self):
        self.shown_in_its_tab()
        k = self.konsole()
        k["titles"][":1.5 /Sessions/1"] = "my own name"
        self.write_konsole(k)
        self.set_state({"summary": "finish the aurora wipe", "written": "\U0001F7E0  x"})
        self.write_name("Page items to the calendar")
        self.strip("state", "done", stdin={"session_id": SID})
        self.assertEqual(self.title(), "my own name")

    def test_retitle_writes_the_new_name_to_the_tab_it_was_given(self):
        r = self.strip("retitle", SID, ":1.5", "/Sessions/1", "Fix the login page bug")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Fix the login page bug", self.title())
        self.assertEqual(self.state()["summary"], "Fix the login page bug")

    def test_retitle_overrides_a_name_he_typed(self):
        self.set_state({"summary": "old", "pinned": True, "written": "\U0001F7E0  old"})
        k = self.konsole()
        k["titles"][":1.5 /Sessions/1"] = "my own name"
        self.write_konsole(k)
        r = self.strip("retitle", SID, ":1.5", "/Sessions/1", "Printer setup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Printer setup", self.title())
        self.assertNotIn("pinned", self.state())

    def test_retitle_fails_loudly_when_the_tab_is_gone(self):
        r = self.strip("retitle", SID, ":1.gone", "/Sessions/1", "Printer setup")
        self.assertEqual(r.returncode, 1)

    def test_a_window_still_wearing_its_birth_name_is_not_a_hand_rename(self):
        (self.records / f"{SID}.json").write_text(json.dumps({"konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        self.set_state({"summary": "finish the aurora wipe", "written": "\U0001F7E0  Opus  —  Kappa"})
        self.strip("state", "done", stdin={"session_id": SID})
        self.assertIn("Finish the aurora wipe", self.title())
        self.assertNotIn("pinned", self.state())

    def test_a_session_frozen_on_its_birth_name_thaws_on_its_next_hook(self):
        (self.records / f"{SID}.json").write_text(json.dumps({"konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        self.set_state({"summary": "finish the aurora wipe", "written": "x", "pinned": True})
        self.strip("state", "done", stdin={"session_id": SID})
        self.assertIn("Finish the aurora wipe", self.title())

    def test_a_real_hand_rename_still_wins(self):
        (self.records / f"{SID}.json").write_text(json.dumps({"konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        k = self.konsole()
        k["titles"][":1.5 /Sessions/1"] = "my own name"
        self.write_konsole(k)
        self.set_state({"summary": "finish the aurora wipe", "written": "\U0001F7E0  x"})
        self.strip("state", "done", stdin={"session_id": SID})
        self.assertEqual(self.title(), "my own name")
        self.assertTrue(self.state().get("pinned"))

    def test_a_background_session_without_its_record_leaves_the_inherited_tab_alone(self):
        self.strip("state", "done", stdin={"session_id": SID},
                   KONSOLE_DBUS_SERVICE=":1.old", KONSOLE_DBUS_SESSION="/Sessions/1")
        self.assertEqual(self.title(":1.old /Sessions/1"), "Get printer setup")
        self.assertNotIn("written", self.state())

    def test_a_window_record_filed_under_an_earlier_session_id_still_names_the_tab_to_title(self):
        (self.records / "9f9f9f9f-0000-4000-8000-000000000000.json").write_text(json.dumps(
            {"session_id": SID, "konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        self.set_state({"summary": "get printer setup"})
        self.strip("state", "done", stdin={"session_id": SID},
                   KONSOLE_DBUS_SERVICE=":1.old", KONSOLE_DBUS_SESSION="/Sessions/1")
        self.assertIn("Get printer setup", self.title(":1.5 /Sessions/1"))
        self.assertEqual(self.title(":1.old /Sessions/1"), "Get printer setup", "the inherited tab was titled")

    def test_a_file_named_by_the_session_id_that_holds_another_session_never_hides_the_record_that_has_it(self):
        (self.records / f"{SID}.json").write_text(json.dumps(
            {"session_id": "9f9f9f9f-0000-4000-8000-000000000000", "konsole_service": ":1.77", "konsole_session": "/Sessions/7"}))
        (self.records / "zz-live.json").write_text(json.dumps(
            {"session_id": SID, "konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        self.set_state({"summary": "get printer setup"})
        self.strip("state", "done", stdin={"session_id": SID},
                   KONSOLE_DBUS_SERVICE=":1.old", KONSOLE_DBUS_SESSION="/Sessions/1")
        self.assertIn("Get printer setup", self.title(":1.5 /Sessions/1"))
        self.assertNotIn("Get printer setup", self.konsole()["titles"].get(":1.77 /Sessions/7", ""),
                         "the dead file's tab was titled")

    def test_a_claude_in_its_own_tab_still_titles_it_without_a_record(self):
        self.set_state({"summary": "get printer setup"})
        self.strip("state", "done", stdin={"session_id": SID}, wrap_konsole=True,
                   KONSOLE_DBUS_SERVICE=":1.old", KONSOLE_DBUS_SESSION="/Sessions/1")
        self.assertIn("Get printer setup", self.title(":1.old /Sessions/1"))
        self.assertIn("\U0001F7E2", self.title(":1.old /Sessions/1"))


class StripTokens(Base):

    def setUp(self):
        super().setUp()
        self.write_konsole({"titles": {":1.5 /Sessions/1": "claude · 18:44"}})
        (self.records / f"{SID}.json").write_text(json.dumps({"konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))

    def statusline(self, context_window) -> str:
        data = {"session_id": SID, "model": {"display_name": "Opus 5.5"}, "effort": {"level": "xhigh"}}
        if context_window is not None:
            data["context_window"] = context_window
        r = subprocess.run(["node", str(STRIP), "statusline"], input=json.dumps(data), capture_output=True,
                           text=True, timeout=30, env=self.env(CLAUDE_CONSOLE_DEBUG="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return self.konsole()["titles"][":1.5 /Sessions/1"]

    def live(self) -> dict:
        return json.loads((self.home / ".cache/claude-console" / f"{SID}.live.json").read_text())

    def test_the_title_counts_the_context_in_thousands_of_tokens(self):
        title = self.statusline({"total_input_tokens": 142345, "context_window_size": 1000000,
                                 "used_percentage": 14, "remaining_percentage": 86,
                                 "current_usage": {"input_tokens": 12000, "output_tokens": 900,
                                                   "cache_creation_input_tokens": 30000,
                                                   "cache_read_input_tokens": 100345}})
        self.assertIn("Opus 5.5 · XHigh · 142k tokens", title)
        self.assertNotIn("%", title)
        self.assertEqual(self.live()["tokens"], 142345)

    def test_output_tokens_are_not_in_the_count(self):
        self.statusline({"context_window_size": 200000, "used_percentage": 5,
                         "current_usage": {"input_tokens": 3, "output_tokens": 50000,
                                           "cache_creation_input_tokens": 997, "cache_read_input_tokens": 9000}})
        self.assertEqual(self.live()["tokens"], 10000)

    def test_without_the_usage_it_counts_from_the_percentage_and_the_window(self):
        title = self.statusline({"context_window_size": 200000, "used_percentage": 60})
        self.assertIn("120k tokens", title)
        self.assertNotIn("%", title)

    def test_before_the_first_answer_it_shows_no_count(self):
        title = self.statusline({"total_input_tokens": 0, "context_window_size": 200000,
                                 "current_usage": None, "used_percentage": None})
        self.assertIn("Opus 5.5 · XHigh", title)
        self.assertNotIn("tokens", title)
        self.assertNotIn("%", title)
        self.assertNotIn("tokens", self.live())


class SummarizeWhole(Base):

    def setUp(self):
        super().setUp()
        self.transcript = self.tmp / "t.jsonl"

    def summarize(self, answer: str, *lines: str, whole=True) -> subprocess.CompletedProcess:
        self.transcript.write_text("\n".join(lines) + "\n")
        args = ["--whole", "--print"] if whole else ["--print"]
        return subprocess.run(["node", str(SUMMARIZE), *args, "kappa", SID, str(self.transcript)],
                              capture_output=True, text=True, timeout=30, env=self.env(FAKE_ANSWER=answer))

    def prompt(self) -> str:
        return (self.tmp / "prompt.txt").read_text()

    def test_every_ask_is_in_the_prompt_and_harness_text_is_not(self):
        r = self.summarize("Aurora wipe", user("please finish the aurora wipe"),
                           user("Base directory for this skill: /x", meta=True),
                           user("<task-notification>done</task-notification>"),
                           user("also check the trim log after"))
        self.assertEqual(r.stdout.strip(), "Aurora wipe")
        p = self.prompt()
        self.assertIn("please finish the aurora wipe", p)
        self.assertIn("also check the trim log after", p)
        self.assertNotIn("Base directory", p)
        self.assertNotIn("task-notification", p)

    def test_acronyms_and_names_come_back_the_way_he_spells_them(self):
        r = self.summarize("Login Page Bug Status For Alpha",
                           user("please look into the status of my login page bug at ALPHA"))
        self.assertEqual(r.stdout.strip(), "Login page bug status for ALPHA")

    def test_a_copied_example_is_refused(self):
        r = self.summarize("Invoice PDF export", user("resume the thing from before please"))
        self.assertEqual(r.stdout.strip(), "")

    def test_a_session_with_nothing_asked_yet_is_not_sent_to_the_model(self):
        r = self.summarize("Anything", user("<command-name>/model</command-name>"))
        self.assertEqual(r.stdout.strip(), "")
        self.assertFalse((self.tmp / "prompt.txt").exists())


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": os.environ["FAKE_NAME"]}))
'''


class HubNameOnTheTitleBar(Base):

    def setUp(self):
        super().setUp()
        p = self.bin / "claude"
        p.write_text(FAKE_CLAUDE)
        p.chmod(0o755)
        conv = self.accounts / "kappa/projects/-home-user-workspace"
        conv.mkdir(parents=True)
        typed = {"type": "user", "message": {"role": "user", "content": "put my two items from the page on my calendar"},
                 "origin": {"kind": "human"}, "promptSource": "typed", "turnOrigin": "human"}
        (conv / f"{SID}.jsonl").write_text(json.dumps(typed, separators=(",", ":")) + "\n")
        (self.records / f"{SID}.json").write_text(json.dumps({
            "account": "kappa", "session_id": SID, "konsole_service": ":1.5", "konsole_session": "/Sessions/1"}))
        self.write_konsole({"titles": {":1.5 /Sessions/1": "claude · 18:44", ":1.mine /Sessions/1": "my own terminal"}})

    def namer(self, *args, name="Page items to a calendar", in_a_tab=False) -> subprocess.CompletedProcess:
        cmd = [str(BIN / "claude-title"), *args]
        if in_a_tab:
            tab = self.tmp / "tab" / "konsole"
            tab.parent.mkdir(exist_ok=True)
            tab.write_text('#!/bin/sh\n"$@"\n')
            tab.chmod(0o755)
            cmd = [str(tab), *cmd]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              env=self.env(CLAUDE_HUB_TITLE_OLLAMA="off", FAKE_NAME=name,
                                           KONSOLE_DBUS_SERVICE=":1.mine", KONSOLE_DBUS_SESSION="/Sessions/1"))

    def titles(self) -> dict:
        return self.konsole()["titles"]

    def test_a_name_lands_on_the_window_that_shows_that_claude(self):
        r = self.namer("refresh", "kappa", SID)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "named: Page items to a calendar (haiku)"), r.stderr)
        self.assertIn("Page items to a calendar", self.titles()[":1.5 /Sessions/1"])
        self.assertEqual(self.titles()[":1.mine /Sessions/1"], "my own terminal")

    def test_the_tab_the_namer_was_run_from_is_never_the_one_renamed(self):
        (self.records / f"{SID}.json").unlink()
        control = subprocess.run(["node", str(STRIP), "summary", SID, "Page items to a calendar"],
                                 capture_output=True, text=True, timeout=30,
                                 env=self.env(KONSOLE_DBUS_SERVICE=":1.mine", KONSOLE_DBUS_SESSION="/Sessions/1"))
        self.assertEqual(control.returncode, 0, control.stderr)
        r = self.namer("refresh", "kappa", SID, in_a_tab=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "named: Page items to a calendar (haiku)"), r.stderr)
        self.assertEqual(self.titles()[":1.mine /Sessions/1"], "my own terminal")

    def test_meta_n_names_the_row_and_the_bar_alike(self):
        self.set_state({"summary": "put my two items from the page on my", "state": "asking"})
        r = self.namer("rename", "kappa", SID, name="Two meetings onto the calendar")
        self.assertEqual((r.returncode, r.stdout), (0, "Two meetings onto the calendar\n"), r.stderr)
        self.assertIn("Two meetings onto the calendar", self.titles()[":1.5 /Sessions/1"])
        hub = subprocess.run([str(BIN / "claude-title"), "get", SID], capture_output=True, text=True,
                             timeout=30, env=self.env())
        self.assertEqual(hub.stdout.strip(), "Two meetings onto the calendar")

    def test_a_name_he_typed_on_the_tab_is_left_alone(self):
        k = self.konsole()
        k["titles"][":1.5 /Sessions/1"] = "my own name"
        self.write_konsole(k)
        self.set_state({"summary": "put my two items", "written": "\U0001F7E0  x"})
        r = self.namer("refresh", "kappa", SID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.titles()[":1.5 /Sessions/1"], "my own name")


if __name__ == "__main__":
    unittest.main()
