"""Tests for bin/claude-agents-ceiling: stop the one runaway when agents.slice stalls.

Run: python3 -m pytest tests/test_agents_ceiling.py

The /proc and the lane's cgroup files are a tree the test writes (Config pointed at it), and the signaller is a fake
that records what it would send. Nothing here signals a process: the real signaller refuses to while unittest is
loaded, and no pid a fixture names can exist (all are above the kernel's largest pid_max, 4194304).
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/claude-agents-ceiling"
UNIT = ROOT / "systemd/claude-agents-ceiling.service"


def load():
    loader = importlib.machinery.SourceFileLoader("claude_agents_ceiling", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


M = load()
UID = 4242
GB = 1 << 30
BASE = 5_000_000


class World:

    def __init__(self, tmp: str):
        self.proc = os.path.join(tmp, "proc")
        self.cfg = M.Config(proc=self.proc, cgroup=os.path.join(tmp, "cgroup"), uid=UID)
        os.makedirs(self.proc)
        os.makedirs(os.path.dirname(self.cfg.lane_file("x")))
        self.write_lane(full=0.0)

    def write_lane(self, full: float | None, current: int = 30 * GB, high: str = str(44 * GB),
                   swap: int = 2 * GB, swap_max: str = str(16 * GB)) -> None:
        files = {"memory.current": str(current), "memory.high": high, "memory.swap.current": str(swap),
                 "memory.swap.max": swap_max}
        if full is not None:
            files["memory.pressure"] = (f"some avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n"
                                        f"full avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n")
        for name, text in files.items():
            Path(self.cfg.lane_file(name)).write_text(text + "\n")

    def list_in(self, pid: int, group: str) -> None:
        folder = Path(self.cfg.cgroup, group.lstrip("/"))
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "cgroup.procs", "a") as f:
            f.write(f"{pid}\n")

    def add(self, pid: int, gb: float, *, swap_gb: float = 0, comm: str = "node", claude: bool = False,
            cgroup: str | None = None, uid: int = UID, state: str = "S", start: int = 100,
            kernel: bool = False, anon_gb: float | bool | None = None, cwd: str | bytes = "/home/x/jobs/job1",
            listed: bool = True) -> int:
        d = Path(self.proc, str(pid))
        d.mkdir()
        group = cgroup if cgroup is not None else self.cfg.lane + "/agent-1-ab.scope"
        (d / "cgroup").write_text(f"0::{group}\n")
        if listed:
            self.list_in(pid, group)
        status = f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        if not kernel:
            status += f"VmRSS:\t{int(gb * GB / 1024)} kB\n"
            if anon_gb is not False:
                status += f"RssAnon:\t{int((gb if anon_gb is None else anon_gb) * GB / 1024)} kB\n"
            status += f"VmSwap:\t{int(swap_gb * GB / 1024)} kB\n"
        (d / "status").write_text(status)
        (d / "stat").write_text(f"{pid} ({comm}) {state} 1 " + " ".join(["0"] * 17) + f" {start} 0 0\n")
        exe = os.path.join(M.CLAUDE_VERSIONS, "2.1.311") if claude else "/usr/bin/node"
        os.symlink(exe, d / "exe")
        os.symlink(cwd, d / "cwd")
        return pid


class FakeSignaller:

    def __init__(self, ends_on=signal.SIGTERM, gone: bool = False, clock=None, refuse: bool = False, world=None):
        self.ends_on, self.gone, self.refuse, self.world = ends_on, gone, refuse, world
        self.alive, self.sent, self.clock, self.at = {}, [], clock, []
        self.waits = []
        self.raise_on_open = set()
        self.raise_on_wait_all = False

    def ends(self, pid):
        return self.ends_on.get(pid, signal.SIGTERM) if isinstance(self.ends_on, dict) else self.ends_on

    def open(self, proc):
        if proc.pid in self.raise_on_open:
            raise RuntimeError("open broke")
        if self.gone:
            return None
        self.alive[proc.pid] = True
        return proc

    def send(self, handle, sig):
        if not self.alive.get(handle.pid):
            return "gone"
        if self.refuse:
            return "refused"
        self.sent.append((handle.pid, sig))
        self.at.append(self.clock() if self.clock else None)
        if sig == self.ends(handle.pid):
            self.alive[handle.pid] = False
            if self.world:
                shutil.rmtree(os.path.join(self.world.proc, str(handle.pid)), ignore_errors=True)
        return "sent"

    def wait(self, handle, timeout):
        self.waits.append(timeout)
        return not self.alive.get(handle.pid)

    def wait_all(self, handles, timeout):
        if self.raise_on_wait_all:
            raise RuntimeError("wait broke")
        self.waits.append(timeout)
        return [h for h in handles if not self.alive.get(h.pid)]

    def close(self, handle):
        pass


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.world = World(tmp.name)
        self.lines, self.notices = [], []
        self.clock = Clock()

    @staticmethod
    def looks(n: int) -> int:
        return int((n - 1) * M.TICK / M.SIZE_TICK) + 1

    def run_watch(self, rounds: int, signaller=None, dry_run: bool = False, between=None):
        signaller = signaller or FakeSignaller(clock=self.clock, world=self.world)
        looks = iter(range(rounds))

        def sleep(seconds):
            self.clock.sleep(seconds)
            if between:
                between(next(looks))
        M.watch(self.world.cfg, dry_run, signaller=signaller, tell=lambda t, b: self.notices.append((t, b)),
                out=self.lines.append, clock=self.clock, sleep=sleep, rounds=rounds)
        return signaller


class Stalls(Base):

    def test_no_stall_stops_nothing(self):
        self.world.add(BASE + 1, 9, swap_gb=3)
        self.world.write_lane(full=M.STALL_PERCENT - 0.1)
        s = self.run_watch(20 * self.looks(2))
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])

    def test_a_stall_shorter_than_the_strikes_stops_nothing(self):
        self.world.add(BASE + 1, 12, swap_gb=3)
        self.world.write_lane(full=80)
        s = self.run_watch(self.looks(M.STRIKES - 1))
        self.assertEqual(s.sent, [])

    def test_the_looks_are_tick_apart_while_the_rounds_are_size_tick_apart(self):
        self.assertLess(M.SIZE_TICK, M.TICK)
        self.world.add(BASE + 1, 12, swap_gb=3)
        self.world.write_lane(full=80)
        s = self.run_watch(int((M.STRIKES - 1) * M.TICK / M.SIZE_TICK))
        self.assertEqual(s.sent, [])
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(len(s.sent), 1)

    def test_the_strikes_must_be_in_a_row(self):
        self.world.add(BASE + 1, 12, swap_gb=3)
        pressures = [80] * (M.STRIKES - 1) + [10] + [80] * (M.STRIKES - 1)
        rounds = self.looks(len(pressures))

        def at(n):
            return pressures[int(n * M.SIZE_TICK // M.TICK)]
        self.world.write_lane(full=at(0))
        s = self.run_watch(rounds, between=lambda n: n + 1 < rounds and self.world.write_lane(full=at(n + 1)))
        self.assertEqual(s.sent, [])
        self.world.write_lane(full=80)
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(len(s.sent), 1)

    def test_a_stall_stops_the_biggest_process_that_is_no_claude(self):
        claude = self.world.add(BASE + 1, 50, claude=True, comm="2.1.311")
        hog = self.world.add(BASE + 2, 12, swap_gb=3)
        self.world.add(BASE + 3, 5, comm="vite")
        self.world.write_lane(full=78.6, current=48 * GB, swap=16 * GB)
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(s.sent, [(hog, signal.SIGTERM)])
        self.assertNotIn(claude, [pid for pid, _ in s.sent])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("15 GB", self.notices[0][1])
        line = self.lines[-1]
        self.assertIn(f"pid {hog} node (15.0 GB) in agent-1-ab.scope", line)
        self.assertIn("swap 16.0 GB of 16 GB, stalled 79%", line)
        self.assertFalse(line.startswith("over "))

    def test_a_mid_size_claude_is_never_the_victim_of_a_stall(self):
        claude = self.world.add(BASE + 1, 10, claude=True, comm="2.1.311")
        node = self.world.add(BASE + 2, 5)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(s.sent, [(node, signal.SIGTERM)])
        self.assertNotIn(claude, [pid for pid, _ in s.sent])

    def test_a_mid_size_claude_alone_under_a_stall_is_left_and_said(self):
        self.world.add(BASE + 1, 10, claude=True, comm="2.1.311")
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(sum("nothing stopped" in l for l in self.lines), 1)

    def test_swap_counts_toward_the_size(self):
        self.world.add(BASE + 1, 6)
        small_rss = self.world.add(BASE + 2, 2, swap_gb=10)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(s.sent, [(small_rss, signal.SIGTERM)])

    def test_sigkill_after_the_grace_when_sigterm_is_not_enough(self):
        hog = self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES), FakeSignaller(ends_on=signal.SIGKILL))
        self.assertEqual(s.sent, [(hog, signal.SIGTERM), (hog, signal.SIGKILL)])
        self.assertEqual(s.waits, [M.RUNAWAY_GRACE, M.RUNAWAY_GRACE])
        self.assertTrue(self.lines[-1].startswith("killed (SIGKILL after SIGTERM):"))

    def test_one_that_outlives_sigkill_is_said(self):
        self.world.add(BASE + 1, 12, state="D")
        self.world.write_lane(full=90)
        self.run_watch(self.looks(M.STRIKES), FakeSignaller(ends_on=None))
        self.assertTrue(self.lines[-1].startswith("SIGKILL sent, still not ended:"))

    def test_one_gone_before_the_signal_is_not_notified(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES), FakeSignaller(gone=True))
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])
        self.assertTrue(self.lines[-1].startswith("ended by itself before a signal:"))

    def test_cooldown_then_the_next_runaway(self):
        first = self.world.add(BASE + 1, 14)
        second = self.world.add(BASE + 2, 12)
        self.world.write_lane(full=90)
        first_dir = os.path.join(self.world.proc, str(first))

        def between(n):
            if os.path.exists(first_dir) and s_box[0].sent:
                os.rename(first_dir, os.path.join(self.world.proc, "gone"))
        s_box = [FakeSignaller(clock=self.clock)]
        rounds = int((M.STRIKES * M.TICK + M.COOLDOWN + M.STRIKES * M.TICK) / M.SIZE_TICK) + 1
        s = self.run_watch(rounds, s_box[0], between=between)
        self.assertEqual(s.sent, [(first, signal.SIGTERM), (second, signal.SIGTERM)])
        self.assertGreaterEqual(s.at[1] - s.at[0], M.COOLDOWN + (M.STRIKES - 1) * M.TICK)

    def test_dry_run_stops_nothing(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES * 3), dry_run=True)
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])
        self.assertTrue(any(l.startswith("would stop pid") for l in self.lines))


class SizeRule(Base):

    def test_the_numbers(self):
        self.assertEqual(M.RUNAWAY_BYTES, 16 * GB)
        self.assertEqual(M.SIZE_TICK, 1.0)
        self.assertEqual(M.RUNAWAY_GRACE, 2.0)

    def test_a_17_gb_program_is_stopped_in_the_first_round_without_a_stall(self):
        hog = self.world.add(BASE + 1, 17, comm="node")
        self.world.write_lane(full=0)
        s = self.run_watch(1)
        self.assertEqual(s.sent, [(hog, signal.SIGTERM)])
        self.assertEqual(len(self.notices), 1)
        title, body = self.notices[0]
        self.assertIn("node (pid", body)
        self.assertIn("17 GB", body)
        self.assertIn("at most 16 GB", body)
        self.assertIn("Claude sessions keep running", body)
        line = self.lines[-1]
        self.assertTrue(line.startswith("over 16 GB for one program: stopped (SIGTERM): "), line)
        self.assertIn(f"pid {hog} node (17.0 GB) in agent-1-ab.scope", line)
        self.assertIn("stalled 0%", line)

    def test_the_journal_line_has_no_command_line(self):
        hog = self.world.add(BASE + 1, 17)
        Path(self.world.proc, str(hog), "cmdline").write_bytes(b"node\0--secret-prompt\0")
        self.run_watch(1)
        self.assertNotIn("secret", " ".join(self.lines + [b for _, b in self.notices]))

    def test_a_claude_is_never_stopped_by_either_rule(self):
        self.world.add(BASE + 1, 40, claude=True)
        self.world.write_lane(full=95)
        s = self.run_watch(self.looks(M.STRIKES * 3))
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])

    def test_just_under_the_limit_is_left_to_the_stall_rule(self):
        self.world.add(BASE + 1, 15.9)
        s = self.run_watch(20)
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])

    def test_swap_counts_toward_the_limit(self):
        hog = self.world.add(BASE + 1, 6, swap_gb=10.5)
        self.assertEqual(self.run_watch(1).sent, [(hog, signal.SIGTERM)])

    def test_exactly_the_limit_is_stopped(self):
        hog = self.world.add(BASE + 1, 16)
        self.assertEqual(self.run_watch(1).sent, [(hog, signal.SIGTERM)])

    def test_two_oversized_are_both_stopped_biggest_first(self):
        small = self.world.add(BASE + 1, 20)
        big = self.world.add(BASE + 2, 30)
        s = self.run_watch(1)
        self.assertEqual(s.sent, [(big, signal.SIGTERM), (small, signal.SIGTERM)])
        self.assertEqual(len(self.notices), 2)

    def test_it_works_with_no_lane_reading(self):
        hog = self.world.add(BASE + 1, 20)
        os.remove(self.world.cfg.lane_file("memory.pressure"))
        s = self.run_watch(1)
        self.assertEqual(s.sent, [(hog, signal.SIGTERM)])
        self.assertNotIn("lane memory", self.lines[-1])

    def test_it_waits_runaway_grace_before_sigkill_and_kill_wait_after(self):
        hog = self.world.add(BASE + 1, 20)
        s = self.run_watch(1, FakeSignaller(ends_on=signal.SIGKILL))
        self.assertEqual(s.sent, [(hog, signal.SIGTERM), (hog, signal.SIGKILL)])
        self.assertEqual(s.waits, [M.RUNAWAY_GRACE, M.KILL_WAIT])
        self.assertTrue(self.lines[-1].startswith("over 16 GB for one program: killed (SIGKILL after SIGTERM):"))

    def test_a_program_that_is_gone_is_not_notified(self):
        self.world.add(BASE + 1, 20)
        s = self.run_watch(1, FakeSignaller(gone=True))
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])
        self.assertTrue(self.lines[-1].startswith("over 16 GB for one program: ended by itself before a signal:"))

    def test_a_stuck_one_is_said_once_not_every_round(self):
        self.world.add(BASE + 1, 20, state="D")
        s = self.run_watch(30, FakeSignaller(ends_on=None))
        self.assertEqual(s.sent, [(BASE + 1, signal.SIGTERM), (BASE + 1, signal.SIGKILL)])
        self.assertEqual(sum("SIGKILL sent, still not ended" in l for l in self.lines), 1)
        self.assertEqual(len(self.notices), 1)

    def test_a_new_process_on_the_same_pid_is_stopped_again(self):
        pid = self.world.add(BASE + 1, 20, state="D", start=100)
        signaller = FakeSignaller(ends_on=None, world=self.world)

        def between(n):
            if n == 2:
                os.rename(os.path.join(self.world.proc, str(pid)), os.path.join(self.world.proc, "old"))
            if n == 3:
                self.world.add(pid, 20, start=555)
                signaller.ends_on = signal.SIGTERM
        self.run_watch(6, signaller, between=between)
        self.assertEqual(sum(l.startswith("over 16 GB for one program: SIGKILL sent") for l in self.lines), 1)
        self.assertEqual(sum(l.startswith("over 16 GB for one program: stopped (SIGTERM)") for l in self.lines), 1)

    def test_a_stuck_one_that_is_gone_is_forgotten_so_the_same_pid_and_start_is_acted_on_again(self):
        pid = self.world.add(BASE + 1, 20, state="D")
        signaller = FakeSignaller(ends_on=None)

        def between(n):
            d = os.path.join(self.world.proc, str(pid))
            if n == 1:
                os.rename(d, os.path.join(self.world.proc, "away"))
            if n == 2:
                os.rename(os.path.join(self.world.proc, "away"), d)
        self.run_watch(5, signaller, between=between)
        self.assertEqual(sum("SIGKILL sent, still not ended" in l for l in self.lines), 2)

    def test_dry_run_says_would_stop_once_per_process_and_stops_nothing(self):
        self.world.add(BASE + 1, 20)
        s = self.run_watch(30, dry_run=True)
        self.assertEqual(s.sent, [])
        self.assertEqual(self.notices, [])
        said = [l for l in self.lines if "would stop" in l]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("over 16 GB for one program: would stop pid"))
        self.world.add(BASE + 2, 22)
        self.run_watch(10, dry_run=True)
        self.assertEqual(sum("would stop" in l for l in self.lines), 1 + 2)

    def test_the_stall_rule_stays_quiet_for_the_cooldown_after_a_size_stop(self):
        self.world.add(BASE + 1, 20)
        innocent = self.world.add(BASE + 2, 6)
        self.world.write_lane(full=90)
        s = FakeSignaller(clock=self.clock, world=self.world)
        self.run_watch(int(M.COOLDOWN / M.SIZE_TICK), s)
        self.assertEqual(s.sent, [(BASE + 1, signal.SIGTERM)])
        self.assertNotIn(innocent, [pid for pid, _ in s.sent])

    def test_the_stall_rule_counts_again_after_the_cooldown_with_a_genuine_cause(self):
        self.world.add(BASE + 1, 20)
        cause = self.world.add(BASE + 2, 12)
        self.world.write_lane(full=90)
        s = FakeSignaller(clock=self.clock, world=self.world)
        self.run_watch(int((M.COOLDOWN + M.STRIKES * M.TICK) / M.SIZE_TICK) + 1, s)
        self.assertEqual(s.sent, [(BASE + 1, signal.SIGTERM), (cause, signal.SIGTERM)])
        self.assertGreaterEqual(s.at[1] - s.at[0], M.COOLDOWN + (M.STRIKES - 1) * M.TICK)

    def test_a_size_stop_during_a_stall_cooldown_still_acts(self):
        first = self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = FakeSignaller(clock=self.clock, world=self.world)
        late = {}

        def between(n):
            if n == self.looks(M.STRIKES) + 2 and not late:
                late["pid"] = self.world.add(BASE + 2, 25)
        self.run_watch(self.looks(M.STRIKES) + 5, s, between=between)
        self.assertEqual(s.sent[0], (first, signal.SIGTERM))
        self.assertIn((late["pid"], signal.SIGTERM), s.sent)
        self.assertLess(s.at[1] - s.at[0], M.COOLDOWN)

    def test_a_gone_result_does_not_start_the_cooldown(self):
        self.world.add(BASE + 1, 20)
        self.world.add(BASE + 2, 6)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES), FakeSignaller(gone=True))
        self.assertEqual(s.sent, [])
        self.assertTrue(any(l.startswith("ended by itself") for l in self.lines))

    def test_a_gone_result_is_not_remembered_so_a_failed_open_is_retried_next_round(self):
        hog = self.world.add(BASE + 1, 20)
        s = FakeSignaller(gone=True, world=self.world)

        def between(n):
            s.gone = False
        self.run_watch(3, s, between=between)
        self.assertEqual(sum("ended by itself" in l for l in self.lines), 1)
        self.assertEqual(s.sent, [(hog, signal.SIGTERM)])


class Hardening(Base):

    def test_a_refused_signal_is_said_once_and_not_retried_every_round(self):
        self.world.add(BASE + 1, 20)
        s = self.run_watch(30, FakeSignaller(refuse=True))
        self.assertEqual(sum("the kernel refused a signal:" in l for l in self.lines), 1)
        self.assertTrue(self.lines[-1].startswith("over 16 GB for one program: the kernel refused a signal:"))
        self.assertEqual(len(self.notices), 1)
        self.assertIn("could not be stopped", self.notices[0][1])
        self.assertEqual(s.sent, [])

    def test_a_refused_signal_in_the_stall_rule(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        self.run_watch(self.looks(M.STRIKES), FakeSignaller(refuse=True))
        self.assertTrue(self.lines[-1].startswith("the kernel refused a signal: pid"))

    def test_the_real_signaller_reports_refused_and_gone(self):
        sig = M.Signaller(self.world.cfg)
        self.assertEqual(sig.send(-1, signal.SIGTERM), "refused")
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        fd = os.pidfd_open(child.pid)
        try:
            child.wait()
            self.assertEqual(sig.send(fd, signal.SIGTERM), "gone")
        finally:
            os.close(fd)

    def test_an_error_with_one_victim_does_not_stop_the_others(self):
        bad = self.world.add(BASE + 1, 30)
        good = self.world.add(BASE + 2, 20)
        s = FakeSignaller(world=self.world)
        s.raise_on_open.add(bad)
        self.run_watch(3, s)
        self.assertEqual(s.sent, [(good, signal.SIGTERM)])
        said = [l for l in self.lines if f"pid {bad} " in l]
        self.assertEqual(len(said), 1)
        self.assertIn("refused a signal", said[0])
        self.assertIn("RuntimeError: open broke", said[0])

    def test_an_error_in_a_round_is_one_line_and_the_loop_goes_on(self):
        hog = self.world.add(BASE + 1, 20)
        calls = []
        real = M.lane_procs

        def flaky(cfg):
            calls.append(1)
            if len(calls) <= 3:
                raise ValueError("boom\nforged line")
            return real(cfg)
        with mock.patch.object(M, "lane_procs", flaky):
            s = self.run_watch(6)
        errors = [l for l in self.lines if "failed" in l]
        self.assertEqual(len(errors), 1)
        self.assertIn("size rule failed: ValueError: boom", errors[0])
        self.assertNotIn("\n", errors[0])
        self.assertEqual(s.sent, [(hog, signal.SIGTERM)])

    def test_an_error_while_waiting_is_said_once_and_the_victims_are_not_retried(self):
        self.world.add(BASE + 1, 20)
        s = FakeSignaller(ends_on=None, world=self.world)
        s.raise_on_wait_all = True
        self.run_watch(8, s)
        self.assertEqual(sum("size rule failed: RuntimeError: wait broke" in l for l in self.lines), 1)
        self.assertEqual(s.sent, [(BASE + 1, signal.SIGTERM)])

    def test_the_stall_rule_still_runs_when_the_size_rule_fails(self):
        cause = self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        with mock.patch.object(M, "pick_over", side_effect=OSError("no")):
            s = self.run_watch(self.looks(M.STRIKES))
        self.assertEqual(s.sent, [(cause, signal.SIGTERM)])

    def test_a_folder_name_that_is_no_utf8_does_not_crash_or_forge_a_line(self):
        hog = self.world.add(BASE + 1, 20, cwd=b"/home/x/\xff\xfe\nover 16 GB for one program: forged\x1b[2J")
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        with contextlib.redirect_stdout(buf):
            M.watch(self.world.cfg, signaller=FakeSignaller(world=self.world),
                    tell=lambda t, b: self.notices.append((t, b)),
                    clock=self.clock, sleep=self.clock.sleep, rounds=2)
        buf.flush()
        text = buf.buffer.getvalue().decode("utf-8")
        self.assertFalse(any(l.startswith("over 16 GB for one program: forged") for l in text.split("\n")))
        self.assertIn("\\udcff\\udcfe\\n", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(sum("pid " + str(hog) in l for l in text.split("\n")), 1)
        self.assertEqual(len(self.notices), 1)

    def test_describe_escapes_bytes_and_control_characters_in_comm_and_cwd(self):
        self.world.add(BASE + 1, 5, comm="no\nde", cwd=b"/a\xffb\tc")
        (proc,) = M.lane_procs(self.world.cfg)
        line = M.describe(self.world.cfg, proc)
        line.encode("utf-8")
        self.assertNotIn("\n", line)
        self.assertNotIn("\t", line)
        self.assertIn("no\\nde", line)
        self.assertIn("/a\\udcffb\\tc", line)

    def test_say_survives_an_stdout_that_is_no_utf8(self):
        buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
        with contextlib.redirect_stdout(buf):
            M.say("café \udcff")
        buf.flush()
        self.assertEqual(buf.buffer.getvalue().decode("ascii").strip(), "caf\\xe9 \\udcff")

    def test_every_victim_gets_sigterm_before_any_sigkill_and_the_waits_are_shared(self):
        stubborn = self.world.add(BASE + 1, 30)
        polite = self.world.add(BASE + 2, 20)
        s = self.run_watch(1, FakeSignaller(ends_on={stubborn: signal.SIGKILL}, world=self.world))
        self.assertEqual(s.sent, [(stubborn, signal.SIGTERM), (polite, signal.SIGTERM), (stubborn, signal.SIGKILL)])
        self.assertEqual(s.waits, [M.RUNAWAY_GRACE, M.KILL_WAIT])
        results = [l.split(": ", 1)[1] for l in self.lines if l.startswith("over 16 GB")]
        self.assertTrue(results[0].startswith("killed (SIGKILL after SIGTERM):"))
        self.assertTrue(results[1].startswith("stopped (SIGTERM):"))

    def test_the_sigkill_wait_is_kill_wait_not_runaway_grace(self):
        self.world.add(BASE + 1, 20, state="D")
        s = self.run_watch(1, FakeSignaller(ends_on=None))
        self.assertEqual(s.waits, [M.RUNAWAY_GRACE, M.KILL_WAIT])
        self.assertGreater(M.KILL_WAIT, M.RUNAWAY_GRACE)
        self.assertTrue(self.lines[-1].startswith("over 16 GB for one program: SIGKILL sent, still not ended:"))

    def test_the_stall_rule_waits_no_longer_than_runaway_grace_per_wait(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES), FakeSignaller(ends_on=None))
        self.assertEqual(s.waits, [M.RUNAWAY_GRACE, M.RUNAWAY_GRACE])
        self.assertFalse(hasattr(M, "GRACE"))

    def test_the_real_wait_all_waits_for_all_together_and_returns_the_ones_that_ended(self):
        sig = M.Signaller(self.world.cfg)
        kids = [subprocess.Popen(["sleep", "30"]) for _ in range(2)]
        fds = [os.pidfd_open(k.pid) for k in kids]
        try:
            self.assertEqual(sig.wait_all(fds, 0.0), [])
            self.assertEqual(sig.send(fds[0], signal.SIGKILL), "sent")
            started = time.monotonic()
            self.assertEqual(sig.wait_all(fds, 0.4), [fds[0]])
            self.assertTrue(0.3 <= time.monotonic() - started < 2.0)
            self.assertTrue(sig.wait(fds[0], 0.0))
            self.assertFalse(sig.wait(fds[1], 0.05))
        finally:
            for kid in kids:
                kid.kill()
                kid.wait()
            for fd in fds:
                os.close(fd)

    def test_the_notice_is_not_waited_for_and_is_reaped(self):
        script = Path(self.world.proc).parent / "slow-notify"
        script.write_text("#!/bin/sh\nexec sleep 1\n")
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_HUB_AGENTS_CEILING_NOTIFY": str(script)}):
            started = time.monotonic()
            self.assertTrue(M.notify("t", "b"))
            self.assertLess(time.monotonic() - started, 0.5)
        try:
            self.assertEqual(len(M._NOTICES), 1)
            M._NOTICES[0][0].wait(5)
            M.reap_notices()
            self.assertEqual(M._NOTICES, [])
        finally:
            for proc, _ in M._NOTICES:
                proc.kill()
                proc.wait()
            M._NOTICES.clear()

    def test_a_notice_that_never_ends_is_killed_after_its_life(self):
        script = Path(self.world.proc).parent / "stuck-notify"
        script.write_text("#!/bin/sh\nexec sleep 30\n")
        script.chmod(0o755)
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_HUB_AGENTS_CEILING_NOTIFY": str(script)}):
                self.assertTrue(M.notify("t", "b"))
            with mock.patch.object(M, "NOTICE_LIFE", 0.0):
                M.reap_notices()
            self.assertEqual(M._NOTICES, [])
        finally:
            for proc, _ in M._NOTICES:
                proc.kill()
                proc.wait()
            M._NOTICES.clear()

    def test_the_stall_rule_does_not_move_on_to_the_next_biggest_when_the_biggest_is_stuck(self):
        stuck = self.world.add(BASE + 1, 20, state="D")
        self.world.add(BASE + 2, 8)
        self.world.write_lane(full=90)
        s = self.run_watch(int((M.COOLDOWN + 6 * M.TICK) / M.SIZE_TICK), FakeSignaller(ends_on=None))
        self.assertEqual(s.sent, [(stuck, signal.SIGTERM), (stuck, signal.SIGKILL)])
        self.assertEqual(sum("one that could not be stopped" in l for l in self.lines), 1)

    def test_the_stall_rule_does_not_move_on_when_the_biggest_was_refused(self):
        self.world.add(BASE + 1, 20)
        self.world.add(BASE + 2, 8)
        self.world.write_lane(full=90)
        s = self.run_watch(int((M.COOLDOWN + 6 * M.TICK) / M.SIZE_TICK), FakeSignaller(refuse=True))
        self.assertEqual(s.sent, [])
        self.assertEqual(sum("one that could not be stopped" in l for l in self.lines), 1)

    def test_a_stall_stop_that_is_stuck_is_blocked_told_as_not_stopped_and_not_repeated(self):
        stuck = self.world.add(BASE + 1, 12, state="D")
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES) + int(4 * (M.COOLDOWN + 3 * M.TICK) / M.SIZE_TICK),
                           FakeSignaller(ends_on=None))
        self.assertEqual(s.sent, [(stuck, signal.SIGTERM), (stuck, signal.SIGKILL)])
        self.assertEqual([t for t, _ in self.notices], ["Could not stop a runaway agent process"])
        self.assertIn("could not be stopped", self.notices[0][1])
        self.assertTrue(any(l.startswith("SIGKILL sent, still not ended:") for l in self.lines))

    def test_a_stall_stop_that_is_refused_is_blocked_told_as_not_stopped_and_not_repeated(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        s = self.run_watch(self.looks(M.STRIKES) + int(4 * (M.COOLDOWN + 3 * M.TICK) / M.SIZE_TICK),
                           FakeSignaller(refuse=True))
        self.assertEqual(s.sent, [])
        self.assertEqual([t for t, _ in self.notices], ["Could not stop a runaway agent process"])
        self.assertEqual(sum(l.startswith("the kernel refused a signal:") for l in self.lines), 1)

    def test_a_stall_stop_that_works_still_says_stopped(self):
        self.world.add(BASE + 1, 12)
        self.world.write_lane(full=90)
        self.run_watch(self.looks(M.STRIKES))
        self.assertEqual([t for t, _ in self.notices], ["Stopped a runaway agent process"])

    def test_page_cache_and_shared_memory_do_not_trip_the_size_rule(self):
        self.world.add(BASE + 1, 20, anon_gb=1)
        s = self.run_watch(10)
        self.assertEqual(s.sent, [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_status(self.world.cfg, True)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["over_limit"], [])
        self.assertEqual(data["biggest"]["bytes"], 20 * GB)
        self.assertEqual(data["biggest"]["anon_bytes"], GB)

    def test_a_process_without_an_rssanon_line_counts_as_zero_anon(self):
        self.world.add(BASE + 1, 20, anon_gb=False)
        self.assertEqual(self.run_watch(3).sent, [])
        (proc,) = M.lane_procs(self.world.cfg)
        self.assertEqual(proc.anon, 0)

    def test_anon_plus_swap_is_what_counts(self):
        hog = self.world.add(BASE + 1, 14, anon_gb=10, swap_gb=7)
        small = self.world.add(BASE + 2, 14, anon_gb=10, swap_gb=5)
        self.assertEqual(self.run_watch(1).sent, [(hog, signal.SIGTERM)])
        self.assertIn(small, [p.pid for p in M.lane_procs(self.world.cfg)])

    def test_the_stall_rule_still_counts_rss_not_anon(self):
        cause = self.world.add(BASE + 1, 12, anon_gb=1)
        self.world.write_lane(full=90)
        self.assertEqual(self.run_watch(self.looks(M.STRIKES)).sent, [(cause, signal.SIGTERM)])


class WhoIsNever(Base):
    def stalled_watch(self):
        self.world.write_lane(full=95)
        return self.run_watch(self.looks(M.STRIKES * 4))

    def test_nothing_big_enough_is_said_once_and_stops_nothing(self):
        for i in range(10):
            self.world.add(BASE + i, 0.5, claude=True)
        self.world.add(BASE + 20, M.MIN_BYTES / GB - 0.01)
        s = self.stalled_watch()
        self.assertEqual(s.sent, [])
        self.assertEqual(sum("nothing stopped" in l for l in self.lines), 1)

    def test_outside_the_lane(self):
        uid = UID
        self.world.add(BASE + 1, 40, cgroup=f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/firefox.scope")
        self.world.add(BASE + 2, 40, cgroup=f"/user.slice/user-{uid}.slice/user@{uid}.service/agents.slice-other/x")
        self.assertEqual(self.stalled_watch().sent, [])

    def test_someone_elses(self):
        self.world.add(BASE + 1, 40, uid=0)
        self.assertEqual(self.stalled_watch().sent, [])

    def test_a_kernel_thread_or_a_zombie(self):
        self.world.add(BASE + 1, 40, kernel=True)
        self.world.add(BASE + 2, 40, state="Z")
        self.assertEqual(self.stalled_watch().sent, [])

    def test_a_claude_however_big(self):
        self.world.add(BASE + 1, 40, claude=True)
        self.assertEqual(self.stalled_watch().sent, [])

    def test_no_pressure_file(self):
        self.world.add(BASE + 1, 12)
        os.remove(self.world.cfg.lane_file("memory.pressure"))
        self.assertEqual(self.run_watch(self.looks(M.STRIKES * 4)).sent, [])

    def test_no_lane_at_all(self):
        cfg = M.Config(proc=self.world.proc, cgroup="/nonexistent-claude-test", uid=UID)
        self.world.add(BASE + 1, 12)
        s = FakeSignaller()
        M.watch(cfg, signaller=s, tell=lambda t, b: None, out=self.lines.append, clock=self.clock,
                sleep=self.clock.sleep, rounds=self.looks(M.STRIKES * 4))
        self.assertEqual(s.sent, [])


class Pieces(Base):
    def test_full_avg10(self):
        self.assertEqual(M.full_avg10("some avg10=91.60 avg60=1 avg300=2 total=3\nfull avg10=78.62 avg60=76.16"), 78.62)
        self.assertIsNone(M.full_avg10("some avg10=91.60 avg60=1 avg300=2 total=3\n"))
        self.assertIsNone(M.full_avg10("full avg10=x\n"))

    def test_max_reads_as_no_ceiling(self):
        self.world.write_lane(full=1, high="max", swap_max="max")
        lane = M.read_lane(self.world.cfg)
        self.assertIsNone(lane.high)
        self.assertIn("of no ceiling", lane.describe())
        self.assertIn("of no cap", lane.describe())

    def test_comm_with_parentheses_and_spaces(self):
        pid = self.world.add(BASE + 1, 5, comm="a) b (c")
        (proc,) = M.lane_procs(self.world.cfg)
        self.assertEqual((proc.pid, proc.comm, proc.start), (pid, "a) b (c", 100))

    def test_still_same_follows_the_start_time(self):
        pid = self.world.add(BASE + 1, 5, start=100)
        (proc,) = M.lane_procs(self.world.cfg)
        self.assertTrue(M.still_same(self.world.cfg, proc))
        Path(self.world.proc, str(pid), "stat").write_text(f"{pid} (node) S 1 " + " ".join(["0"] * 17) + " 999 0\n")
        self.assertFalse(M.still_same(self.world.cfg, proc))
        os.remove(Path(self.world.proc, str(pid), "stat"))
        self.assertFalse(M.still_same(self.world.cfg, proc))

    def test_the_real_signaller_refuses_under_test(self):
        self.world.add(os.getpid(), 5)
        (proc,) = M.lane_procs(self.world.cfg)
        self.assertTrue(M.still_same(self.world.cfg, proc))
        self.assertIsNone(M.Signaller(self.world.cfg).open(proc))

    def test_the_real_notice_is_never_sent_under_test(self):
        os.environ.pop("CLAUDE_HUB_AGENTS_CEILING_NOTIFY", None)
        self.assertIsNone(M.notify("t", "b"))

    def test_status_json(self):
        hog = self.world.add(BASE + 1, 38, swap_gb=11)
        self.world.add(BASE + 2, 1, claude=True)
        self.world.write_lane(full=78.6)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(M.cmd_status(self.world.cfg, True), 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["stalled"])
        self.assertEqual((data["processes"], data["claudes"]), (2, 1))
        self.assertEqual(data["would_stop"]["pid"], hog)
        self.assertEqual([p["pid"] for p in data["over_limit"]], [hog])
        self.assertEqual(data["limit_bytes"], 16 * GB)

    def test_status_over_limit_lists_every_one_biggest_first_and_no_claude(self):
        small = self.world.add(BASE + 1, 20)
        big = self.world.add(BASE + 2, 30)
        self.world.add(BASE + 3, 40, claude=True)
        self.world.add(BASE + 4, 15.9)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_status(self.world.cfg, True)
        self.assertEqual([p["pid"] for p in json.loads(buf.getvalue())["over_limit"]], [big, small])

    def test_status_text_names_the_size_rule(self):
        hog = self.world.add(BASE + 1, 20)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_status(self.world.cfg, False)
        self.assertIn("over 16 GB of its own memory for one program "
                      f"(checked every second; SIGTERM, SIGKILL 2 s later): pid {hog} node (20.0 GB)", buf.getvalue())
        self.assertNotIn("within a second", buf.getvalue())
        os.rename(os.path.join(self.world.proc, str(hog)), os.path.join(self.world.proc, "away"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_status(self.world.cfg, False)
        self.assertIn("none over 16 GB for one program", buf.getvalue())
        self.assertEqual(json.loads(self._json())["over_limit"], [])

    def _json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M.cmd_status(self.world.cfg, True)
        return buf.getvalue()

    def test_the_startup_line_states_both_rules(self):
        self.run_watch(1)
        first = self.lines[0]
        self.assertIn("16 GB", first)
        self.assertIn(f"{M.SIZE_TICK:g} s", first)
        self.assertIn(f"{M.STRIKES} looks {M.TICK:g} s apart", first)

    def test_usage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(M.main([]), 2)
            self.assertEqual(M.main(["watch", "--json"]), 2)
            self.assertEqual(M.main(["kill"]), 2)


def keyed(procs, links: bool = True) -> dict:
    return {p.pid: (p.start, p.comm, p.size, p.anon, p.cgroup) + ((p.cwd, p.claude) if links else ()) for p in procs}


class Equivalence(Base):

    def setUp(self):
        super().setUp()
        self.lane = self.world.cfg.lane
        self.outside = f"/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/firefox.scope"

    def rich(self) -> dict:
        w = self.world
        deep = self.lane + "/a.slice/b.slice/c.slice/d.slice/e.slice/f.scope"
        roles = {
            "claude_big": w.add(BASE + 1, 20, claude=True, comm="2.1.311"),
            "runaway": w.add(BASE + 2, 18, swap_gb=1),
            "nested": w.add(BASE + 3, 17, cgroup=deep, comm="deepnode"),
            "at_root": w.add(BASE + 4, 6, cgroup=self.lane),
            "mid": w.add(BASE + 5, 5, comm="vite"),
            "small": w.add(BASE + 6, 1),
            "small_claude": w.add(BASE + 7, 0.5, claude=True),
            "outside": w.add(BASE + 8, 40, cgroup=self.outside),
            "lookalike": w.add(BASE + 9, 40, cgroup=self.lane + "-other/x"),
            "other_uid": w.add(BASE + 10, 40, uid=0),
            "zombie": w.add(BASE + 11, 40, state="Z", listed=False),
            "kernel": w.add(BASE + 12, 40, kernel=True, listed=False),
            "no_anon_line": w.add(BASE + 13, 30, anon_gb=False),
        }
        w.list_in(BASE + 14, self.lane + "/agent-1-ab.scope")
        w.list_in(roles["runaway"], self.lane + "/agent-1-ab.scope")
        w.list_in(roles["runaway"], deep)
        roles["gone"] = BASE + 14
        return roles

    def assert_same(self, new_full, old, new_lazy=None):
        self.assertEqual(keyed(new_full), keyed(old))
        if new_lazy is not None:
            self.assertEqual(keyed(new_lazy, links=False), keyed(old, links=False))
            self.assertEqual(len(new_lazy), len(old))
            old_by_pid = {p.pid: p for p in old}
            for p in new_lazy:
                if M.candidate(p.size, p.anon):
                    self.assertEqual((p.cwd, p.claude), (old_by_pid[p.pid].cwd, old_by_pid[p.pid].claude))
            self.assertEqual([p.pid for p in M.pick_over(new_lazy)], [p.pid for p in M.pick_over(old)])
            a, b = M.pick(new_lazy), M.pick(old)
            self.assertEqual(a.pid if a else None, b.pid if b else None)

    def test_the_lane_walk_gives_the_old_listing_on_a_tree_with_everything(self):
        roles = self.rich()
        old = M.lane_procs_old(self.world.cfg)
        new = M.lane_procs(self.world.cfg, full=True)
        self.assert_same(new, old, M.lane_procs(self.world.cfg))
        got = {p.pid for p in new}
        for role in ("claude_big", "runaway", "nested", "at_root", "mid", "small", "small_claude", "no_anon_line"):
            self.assertIn(roles[role], got, role)
        for role in ("outside", "lookalike", "other_uid", "zombie", "kernel", "gone"):
            self.assertNotIn(roles[role], got, role)
        self.assertEqual(len([p for p in new if p.pid == roles["runaway"]]), 1)
        self.assertEqual([p.pid for p in M.pick_over(new)], [roles["runaway"], roles["nested"]])

    def test_a_claude_over_the_line_is_never_a_victim_in_the_lazy_listing(self):
        roles = self.rich()
        lazy = M.lane_procs(self.world.cfg)
        self.assertNotIn(roles["claude_big"], [p.pid for p in M.pick_over(lazy)])
        self.assertTrue(next(p for p in lazy if p.pid == roles["claude_big"]).claude)

    def test_the_lazy_listing_leaves_the_links_of_small_ones_unread_and_status_reads_them_all(self):
        roles = self.rich()
        lazy = {p.pid: p for p in M.lane_procs(self.world.cfg)}
        self.assertEqual((lazy[roles["small"]].cwd, lazy[roles["small"]].claude), ("", False))
        self.assertEqual(lazy[roles["mid"]].cwd, "/home/x/jobs/job1")
        full = {p.pid: p for p in M.lane_procs(self.world.cfg, full=True)}
        self.assertEqual(full[roles["small"]].cwd, "/home/x/jobs/job1")
        self.assertTrue(full[roles["small_claude"]].claude)

    def test_the_candidate_line_is_what_the_two_rules_read(self):
        self.assertTrue(M.candidate(M.MIN_BYTES, 0))
        self.assertFalse(M.candidate(M.MIN_BYTES - 1, M.RUNAWAY_BYTES - 1))
        self.assertTrue(M.candidate(0, M.RUNAWAY_BYTES))

    def test_a_round_reads_no_cgroup_file_of_a_pid_outside_the_lane(self):
        roles = self.rich()
        opened, linked = [], []
        real_text, real_link = M.read_text, M.read_link

        def text(path):
            opened.append(path)
            return real_text(path)

        def link(path):
            linked.append(path)
            return real_link(path)
        with mock.patch.object(M, "read_text", text), mock.patch.object(M, "read_link", link):
            M.lane_procs_old(self.world.cfg)
            old_opened, old_linked = list(opened), list(linked)
            opened.clear()
            linked.clear()
            M.lane_procs(self.world.cfg)
        for role in ("outside", "lookalike"):
            folder = os.path.join(self.world.proc, str(roles[role])) + os.sep
            self.assertTrue(any(p.startswith(folder) for p in old_opened), "the old listing reads it")
            self.assertFalse(any(p.startswith(folder) for p in opened), role)
        small = os.path.join(self.world.proc, str(roles["small"])) + os.sep
        self.assertTrue(any(p.startswith(small) for p in old_linked))
        self.assertFalse(any(p.startswith(small) for p in linked))
        proc_opened = [p for p in opened if p.startswith(self.world.proc + os.sep)]
        self.assertLess(len(proc_opened), len([p for p in old_opened if p.startswith(self.world.proc + os.sep)]))

    def test_a_round_of_the_loop_uses_the_lazy_listing(self):
        roles = self.rich()
        seen = []
        real = M.lane_procs

        def spy(cfg, *args, **kwargs):
            seen.append((args, kwargs))
            return real(cfg, *args, **kwargs)
        with mock.patch.object(M, "lane_procs", spy), mock.patch.object(M, "lane_procs_old") as old:
            self.run_watch(2, FakeSignaller(clock=self.clock, world=self.world, gone=True), dry_run=True)
        self.assertEqual(seen, [((), {})] * 2)
        old.assert_not_called()
        self.assertTrue(any(f"pid {roles['runaway']} " in l for l in self.lines))

    def raising(self, match, exc):
        real = M.read_text

        def read(path):
            if match(path):
                raise exc
            return real(path)
        return mock.patch.object(M, "read_text", read)

    def test_a_pid_that_ends_between_the_listing_and_the_reads_is_skipped_as_by_the_old_listing(self):
        roles = self.rich()
        for name in ("cgroup", "status", "stat"):
            victim = roles["runaway"]
            with self.raising(lambda p, n=name: p.endswith(f"/{victim}/{n}"), FileNotFoundError("gone")):
                old, new = M.lane_procs_old(self.world.cfg), M.lane_procs(self.world.cfg, full=True)
            self.assertNotIn(victim, [p.pid for p in new], name)
            self.assertEqual(keyed(new), keyed(old), name)

    def test_a_pid_whose_links_vanish_ends_in_the_old_answer(self):
        roles = self.rich()
        real = os.readlink

        def readlink(path, *a, **k):
            if f"/{roles['runaway']}/" in str(path) or f"/{roles['claude_big']}/" in str(path):
                raise FileNotFoundError("gone")
            return real(path, *a, **k)
        with mock.patch.object(M.os, "readlink", readlink):
            old, new = M.lane_procs_old(self.world.cfg), M.lane_procs(self.world.cfg, full=True)
            lazy = M.lane_procs(self.world.cfg)
        self.assert_same(new, old, lazy)

    def test_a_cgroup_procs_that_vanishes_mid_walk_loses_only_its_pids(self):
        roles = self.rich()
        deep = self.lane + "/a.slice/b.slice/c.slice/d.slice/e.slice/f.scope"
        gone_file = os.path.join(self.world.cfg.cgroup, deep.lstrip("/"), "cgroup.procs")
        with self.raising(lambda p: p == gone_file, FileNotFoundError("scope ended")):
            new = M.lane_procs(self.world.cfg, full=True)
        shutil.rmtree(os.path.join(self.world.proc, str(roles["nested"])))
        old = M.lane_procs_old(self.world.cfg)
        self.assertNotIn(roles["nested"], [p.pid for p in new])
        self.assertIn(roles["runaway"], [p.pid for p in new])
        self.assertEqual(keyed(new), keyed(old))

    def test_a_cgroup_procs_that_cannot_be_read_skips_its_pids_and_the_walk_goes_on(self):
        roles = self.rich()
        deep = self.lane + "/a.slice/b.slice/c.slice/d.slice/e.slice/f.scope"
        bad = os.path.join(self.world.cfg.cgroup, deep.lstrip("/"), "cgroup.procs")
        with self.raising(lambda p: p == bad, PermissionError("no")):
            new = M.lane_procs(self.world.cfg, full=True)
        old = M.lane_procs_old(self.world.cfg)
        self.assertNotIn(roles["nested"], [p.pid for p in new])
        self.assertTrue({p.pid for p in new} <= {p.pid for p in old})
        self.assertEqual({p.pid for p in old} - {p.pid for p in new}, {roles["nested"]})
        self.assertEqual(keyed(new), {k: v for k, v in keyed(old).items() if k != roles["nested"]})

    def test_a_folder_that_cannot_be_listed_is_skipped_and_the_walk_goes_on(self):
        roles = self.rich()
        deep_top = os.path.join(self.world.cfg.cgroup, (self.lane + "/a.slice").lstrip("/"))
        real = os.scandir

        def scandir(path):
            if os.fspath(path) == deep_top:
                raise PermissionError("no")
            return real(path)
        with mock.patch.object(M.os, "scandir", scandir):
            new = M.lane_procs(self.world.cfg, full=True)
        self.assertIn(roles["runaway"], [p.pid for p in new])
        self.assertIn(roles["at_root"], [p.pid for p in new])

    def test_an_empty_lane_and_a_missing_lane_give_nothing_like_the_old_listing(self):
        Path(self.world.cfg.lane_file("cgroup.procs")).write_text("")
        self.assertEqual(M.lane_procs(self.world.cfg, full=True), [])
        self.assertEqual(M.lane_procs_old(self.world.cfg), [])
        cfg = M.Config(proc=self.world.proc, cgroup="/nonexistent-claude-test", uid=UID)
        self.assertEqual(M.lane_procs(cfg), [])
        cfg = M.Config(proc="/nonexistent-claude-test", cgroup=self.world.cfg.cgroup, uid=UID)
        self.assertEqual(M.lane_procs(cfg), [])

    def test_garbage_in_cgroup_procs_is_ignored(self):
        pid = self.world.add(BASE + 1, 5)
        Path(self.world.cfg.lane_file("cgroup.procs")).write_text(f"\nabc\n-3\n{pid}\n 7 x\n")
        self.assertEqual([p.pid for p in M.lane_procs(self.world.cfg)], [pid])

    def test_a_process_listed_but_moved_out_of_the_lane_is_dropped(self):
        pid = self.world.add(BASE + 1, 20)
        Path(self.world.proc, str(pid), "cgroup").write_text(f"0::{self.outside}\n")
        self.assertEqual(M.lane_procs(self.world.cfg), [])
        self.assertEqual(M.lane_procs_old(self.world.cfg), [])

    def test_the_loop_goes_on_through_all_of_it(self):
        roles = self.rich()
        deep = self.lane + "/a.slice/b.slice/c.slice/d.slice/e.slice/f.scope"
        bad = os.path.join(self.world.cfg.cgroup, deep.lstrip("/"), "cgroup.procs")
        mid = roles["mid"]
        with self.raising(lambda p: p == bad or p.endswith(f"/{mid}/stat"), OSError("changing")):
            s = self.run_watch(3, FakeSignaller(clock=self.clock, world=self.world))
        self.assertEqual([l for l in self.lines if "failed" in l], [])
        stopped = {pid for pid, _ in s.sent}
        self.assertEqual(stopped, {roles["runaway"]})
        self.assertNotIn(roles["claude_big"], stopped)

    def test_the_same_victims_as_the_old_listing_in_a_whole_round(self):
        roles = self.rich()
        expected = {p.pid for p in M.pick_over(M.lane_procs_old(self.world.cfg))}
        s = self.run_watch(1, FakeSignaller(clock=self.clock, world=self.world))
        self.assertEqual({pid for pid, _ in s.sent}, expected)
        self.assertEqual(expected, {roles["runaway"], roles["nested"]})


class Unit(unittest.TestCase):
    def test_the_service(self):
        text = UNIT.read_text()
        self.assertIn("ExecStart=%h/.local/bin/claude-agents-ceiling watch\n", text)
        self.assertIn("Restart=always\n", text)
        self.assertIn("Slice=app.slice\n", text)
        self.assertNotIn("agents.slice\n", [l.split("=", 1)[-1] + "\n" for l in text.splitlines()
                                            if l.startswith("Slice=")])
        self.assertIn("WantedBy=default.target\n", text)

    def test_the_tool_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
