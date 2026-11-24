
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "bin/claude-timeout"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


LCT = load(WRAPPER, "claude_timeout")

LOCK = ".oauth_refresh.lock"
OWNER = ".oauth_refresh.lock.owner"
NOTICE = re.compile(r"^claude-timeout: waited \d+ s for a sign-in refresh before stopping claude$", re.M)

FAKE = """#!%(python)s
import json, os, signal, sys, time

LOCK = ".oauth_refresh.lock"
OWNER = ".oauth_refresh.lock.owner"


def log(path, word):
    with open(path, "a") as handle:
        handle.write("%%s %%.4f\\n" %% (word, time.time()))


def take(sd):
    os.mkdir(os.path.join(sd, LOCK))
    tmp = os.path.join(sd, OWNER + ".tmp.abcd1234")
    with open(tmp, "w") as handle:
        json.dump({"pid": os.getpid(), "procStart": "test", "pidDomain": "x"}, handle)
    os.rename(tmp, os.path.join(sd, OWNER))


def release(sd):
    os.unlink(os.path.join(sd, OWNER))
    os.rmdir(os.path.join(sd, LOCK))


def hang():
    while True:
        time.sleep(1)


def start_daemon(pidfile, extra, own_session, named_claude=True):
    import subprocess
    # argv is [<folder>/claude, "daemon", "run", ...]: the script ./daemon in the fake's folder, run by a link named
    # claude to the interpreter, so argv[0] is claude and argv[1] is "daemon", as for the real thing
    exe = os.path.join(os.path.dirname(pidfile), "claude") if named_claude else sys.executable
    subprocess.Popen([exe, "daemon", "run", pidfile + ".daemon"] + extra, cwd=os.path.dirname(pidfile),
                     start_new_session=own_session,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


mode, pidfile = sys.argv[1], sys.argv[2]
with open(pidfile + ".tmp", "w") as handle:
    json.dump({"pid": os.getpid(), "pgid": os.getpgrp(), "t": time.time()}, handle)
os.rename(pidfile + ".tmp", pidfile)

if mode == "cat":
    data = sys.stdin.read()
    sys.stdout.write(data)
    sys.stdout.flush()
    sys.stderr.write("probe=%%s\\n" %% os.environ.get("LCT_PROBE"))
    sys.exit(int(sys.argv[3]))
elif mode == "hang":
    hang()
elif mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    hang()
elif mode in ("spawn_helper", "spawn_helper_setsid"):
    import subprocess
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", pidfile],
                              start_new_session=(mode == "spawn_helper_setsid"),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(pidfile + ".helper.tmp", "w") as handle:
        json.dump({"pid": helper.pid}, handle)
    os.rename(pidfile + ".helper.tmp", pidfile + ".helper")
    hang()
elif mode in ("spawn_daemon", "spawn_daemon_samesession", "spawn_daemon_notclaude"):
    start_daemon(pidfile, sys.argv[3:5], own_session=(mode != "spawn_daemon_samesession"),
                 named_claude=(mode != "spawn_daemon_notclaude"))
    hang()
elif mode in ("hold_exit_helper", "exit_leaving_helper"):
    import subprocess
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", pidfile],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(pidfile + ".helper.tmp", "w") as handle:
        json.dump({"pid": helper.pid}, handle)
    os.rename(pidfile + ".helper.tmp", pidfile + ".helper")
    if mode == "exit_leaving_helper":
        sys.exit(int(sys.argv[3]))                     # ends by itself, its helper keeps running
    sd, logfile = sys.argv[3], sys.argv[4]
    if "daemon" in sys.argv[7:]:
        start_daemon(pidfile, [], own_session=True)
    take(sd)
    log(logfile, "locked")
    time.sleep(float(sys.argv[5]))
    sys.exit(int(sys.argv[6]))              # leaves by itself, lock held, helper (and daemon) left running
elif mode == "fds":
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink("/proc/self/fd/" + name)
        except OSError:
            continue
        if int(name) >= 3 and target.startswith("pipe:"):
            found.append(int(name))
    print(json.dumps(sorted(found)))
elif mode == "take_on_trigger":
    sd, logfile, trigger = sys.argv[3], sys.argv[4], sys.argv[5]
    while not os.path.exists(trigger):
        time.sleep(0.005)
    take(sd)
    log(logfile, "locked")
    time.sleep(float(sys.argv[6]))
    log(logfile, "releasing")
    release(sd)
    hang()
elif mode == "die_term":
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
elif mode in ("hold_release_hang", "hold_exit", "hold_forever"):
    sd, logfile = sys.argv[3], sys.argv[4]
    take(sd)
    log(logfile, "locked")
    if mode == "hold_forever":
        hang()
    time.sleep(float(sys.argv[5]))
    if mode == "hold_release_hang":
        log(logfile, "releasing")                      # before the lock stops counting, so no kill precedes it
        release(sd)
        hang()
    log(logfile, "exiting")
    sys.exit(int(sys.argv[6]))
"""

DAEMON = """import json, os, subprocess, sys, time

# Stands in for `claude daemon run`: runs as ./daemon (so argv[1] is "daemon"), has a child of its own, and with a
# storage folder and a log file takes the refresh lock under its own pid and never lets go.
pidfile = sys.argv[2]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", pidfile])
with open(pidfile + ".tmp", "w") as handle:
    json.dump({"pid": os.getpid(), "sid": os.getsid(0), "child": child.pid}, handle)
os.rename(pidfile + ".tmp", pidfile)
if len(sys.argv) > 4:
    sd, logfile = sys.argv[3], sys.argv[4]
    os.mkdir(os.path.join(sd, ".oauth_refresh.lock"))
    with open(os.path.join(sd, ".oauth_refresh.lock.owner"), "w") as handle:
        json.dump({"pid": os.getpid()}, handle)
    with open(logfile, "a") as handle:
        handle.write("locked %.4f\\n" % time.time())
while True:
    time.sleep(1)
"""

LAUNCH = """import os, signal, sys
ignore = set(sys.argv[1].split(","))
for name in ("TERM", "INT", "HUP"):
    signal.signal(getattr(signal, "SIG" + name), signal.SIG_IGN if name in ignore else signal.SIG_DFL)
os.execv(sys.argv[2], sys.argv[2:])
"""


def wait_for(path, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


def read_log(path):
    out = {}
    if os.path.exists(path):
        for line in Path(path).read_text().splitlines():
            word, stamp = line.split()
            out[word] = float(stamp)
    return out


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lct-test-"))
        self.sd = self.tmp / "storage"
        self.sd.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake = self.tmp / "fake"
        self.fake.write_text(FAKE % {"python": sys.executable})
        self.fake.chmod(0o755)
        self.launch = self.tmp / "launch.py"
        self.launch.write_text(LAUNCH)
        (self.tmp / "daemon").write_text(DAEMON)
        (self.tmp / "claude").symlink_to(sys.executable)
        self.pidfiles = []
        self.procs = []
        self.counter = 0
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE", "CLAUDE_HUB_TIMEOUT"))}
        self.env["HOME"] = str(self.home)
        self.env["CLAUDE_HUB_TIMEOUT_POLL"] = "0.05"

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        for pidfile in self.pidfiles:
            self.kill_pidfile(pidfile)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def kill_pidfile(self, pidfile):
        try:
            info = json.loads(Path(pidfile).read_text())
        except (OSError, ValueError):
            return
        for pid in (info.get("pid"), info.get("child")):
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                if str(self.tmp).encode() in cmdline:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError, TypeError):
                pass

    def new_pidfile(self):
        self.counter += 1
        pidfile = str(self.tmp / f"pid{self.counter}.json")
        self.pidfiles.append(pidfile)
        return pidfile

    def child_pid(self, pidfile):
        self.assertTrue(wait_for(pidfile), "the fake never started")
        return json.loads(Path(pidfile).read_text())["pid"]

    def helper_pid(self, pidfile):
        self.assertTrue(wait_for(pidfile + ".helper"), "the fake never started its helper")
        self.pidfiles.append(pidfile + ".helper")
        return json.loads(Path(pidfile + ".helper").read_text())["pid"]

    def daemon_info(self, pidfile):
        self.assertTrue(wait_for(pidfile + ".daemon"), "the fake never started its daemon")
        self.pidfiles.append(pidfile + ".daemon")
        return json.loads(Path(pidfile + ".daemon").read_text())

    def assertGone(self, pid, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not LCT.pid_alive(pid):
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} is still running")

    def spawn(self, argv, ignore="", env_extra=None, stdin=None):
        env = dict(self.env)
        env.update(env_extra or {})
        proc = subprocess.Popen([sys.executable, str(self.launch), ignore, str(WRAPPER)] + argv, env=env,
                                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def finish(self, proc, seconds=30):
        try:
            out, err = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            for pidfile in self.pidfiles:
                self.kill_pidfile(pidfile)
            proc.communicate()
            self.fail("the wrapper did not end")
        return proc.returncode, out, err

    def run_wrapper(self, argv, env_extra=None, stdin_text=None, seconds=30):
        env = dict(self.env)
        env.update(env_extra or {})
        start = time.monotonic()
        proc = subprocess.Popen([str(WRAPPER)] + argv, env=env, text=True,
                                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        try:
            out, err = proc.communicate(stdin_text, timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            for pidfile in self.pidfiles:
                self.kill_pidfile(pidfile)
            proc.communicate()
            self.fail("the wrapper did not end")
        return proc.returncode, out, err, time.monotonic() - start

    def timeout_run(self, seconds, mode, *extra, storage=True, env_extra=None, stdin_text=None):
        pidfile = self.new_pidfile()
        argv = (["--storage", str(self.sd)] if storage else []) + [str(seconds), "--", str(self.fake), mode, pidfile]
        argv += [str(x) for x in extra]
        result = self.run_wrapper(argv, env_extra=env_extra, stdin_text=stdin_text)
        return result, pidfile

    def wrapped_run(self, seconds, command, env_extra=None, wait=30):
        return self.run_wrapper(["--storage", str(self.sd), str(seconds), "--"] + [str(x) for x in command],
                                env_extra=env_extra, seconds=wait)

    def assertReleasedThenKilled(self, stamps, end):
        self.assertIn("releasing", stamps)
        self.assertFalse((self.sd / LOCK).exists())
        self.assertFalse((self.sd / OWNER).exists())
        self.assertLess(end - stamps["releasing"], 2.0)

    def stop_in_process(self, proc, env=None):
        notices = []
        waker = LCT.Waker()
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_HUB_TIMEOUT_POLL": "0.05", **(env or {})}), \
                    mock.patch.object(LCT, "notice", lambda text: notices.append((text, proc.poll()))):
                status = LCT.safe_stop(proc, str(self.sd), waker)
        finally:
            waker.close()
        return status, notices

    def make_lock(self, owner=None, age=None, raw_owner=None):
        lock = self.sd / LOCK
        lock.mkdir()
        if owner is not None:
            (self.sd / OWNER).write_text(json.dumps({"pid": owner, "procStart": "test"}))
        if raw_owner is not None:
            (self.sd / OWNER).write_text(raw_owner)
        if age is not None:
            stamp = time.time() - age
            os.utime(lock, (stamp, stamp))
        return lock

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid



class PassThroughTests(Case):
    def test_stdin_stdout_stderr_exit_code_and_environment_pass_through(self):
        (rc, out, err, took), pidfile = self.timeout_run(30, "cat", 7, stdin_text="hello\nworld\n",
                                                         env_extra={"LCT_PROBE": "zed"})
        self.assertEqual(rc, 7)
        self.assertEqual(out, "hello\nworld\n")
        self.assertEqual(err, "probe=zed\n")
        self.assertLess(took, 5)

    def test_zero_exit_is_zero(self):
        (rc, out, err, took), _ = self.timeout_run(30, "cat", 0, stdin_text="x")
        self.assertEqual((rc, out), (0, "x"))

    def test_a_signalled_child_gives_128_plus_n(self):
        (rc, out, err, took), _ = self.timeout_run(30, "die_term")
        self.assertEqual(rc, 143)

    def test_the_child_runs_in_the_wrappers_own_process_group(self):
        proc = self.spawn(["--storage", str(self.sd), "30", "--", str(self.fake), "hang", self.new_pidfile()])
        pidfile = self.pidfiles[-1]
        pid = self.child_pid(pidfile)
        info = json.loads(Path(pidfile).read_text())
        self.assertEqual(info["pgid"], os.getpgid(proc.pid))
        self.assertEqual(info["pgid"], os.getpgrp())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.finish(proc)[0], 143)
        self.assertGone(pid)

    def test_command_not_found_is_127_and_not_executable_126(self):
        rc, out, err, _ = self.run_wrapper(["5", "--", str(self.tmp / "nope")])
        self.assertEqual(rc, 127)
        self.assertIn("cannot run", err)
        plain = self.tmp / "plain"
        plain.write_text("x")
        plain.chmod(0o644)
        rc, out, err, _ = self.run_wrapper(["5", "--", str(plain)])
        self.assertEqual(rc, 126)

    def test_the_file_is_executable(self):
        mode = stat.S_IMODE(WRAPPER.stat().st_mode)
        self.assertEqual(mode & 0o555, 0o555)
        self.assertFalse(mode & 0o002, "world-writable")

    def test_the_credentials_file_is_never_named_in_code(self):
        with tokenize.open(WRAPPER) as handle:
            for token in tokenize.generate_tokens(handle.readline):
                self.assertNotIn("credentials.json", token.string)
                self.assertNotIn(".credentials", token.string)



class TimeoutTests(Case):
    def test_a_hang_with_no_lock_exits_124_at_seconds_and_the_child_is_gone(self):
        (rc, out, err, took), pidfile = self.timeout_run(1, "hang")
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 0.95)
        self.assertLess(took, 2.5)
        self.assertGone(self.child_pid(pidfile))
        self.assertNotRegex(err, NOTICE)

    def test_fractional_seconds(self):
        (rc, out, err, took), pidfile = self.timeout_run(0.4, "hang")
        self.assertEqual(rc, 124)
        self.assertLess(took, 2)

    def test_a_child_that_ignores_sigterm_is_gone_all_the_same(self):
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "ignore_term")
        self.assertEqual(rc, 124)
        self.assertGone(self.child_pid(pidfile))

    def test_a_lock_that_names_another_live_pid_does_not_delay(self):
        self.make_lock(owner=os.getpid(), age=0)
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "hang")
        self.assertEqual(rc, 124)
        self.assertLess(took, 2)
        self.assertNotRegex(err, NOTICE)

    def test_a_lock_that_names_a_dead_pid_does_not_delay(self):
        self.make_lock(owner=self.dead_pid(), age=0)
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "hang")
        self.assertEqual(rc, 124)
        self.assertLess(took, 2)
        self.assertNotRegex(err, NOTICE)

    def test_no_owner_and_an_old_lock_does_not_delay(self):
        self.make_lock(age=100)
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "hang")
        self.assertEqual(rc, 124)
        self.assertLess(took, 2)
        self.assertNotRegex(err, NOTICE)

    def test_an_unreadable_owner_record_does_not_delay(self):
        self.make_lock(raw_owner="{not json", age=0)
        (rc, out, err, took), _ = self.timeout_run(0.5, "hang")
        self.assertEqual((rc, took < 2), (124, True))
        self.assertNotRegex(err, NOTICE)

    def test_a_lock_that_is_a_file_does_not_delay(self):
        (self.sd / LOCK).write_text("x")
        (rc, out, err, took), _ = self.timeout_run(0.5, "hang")
        self.assertEqual((rc, took < 2), (124, True))

    def test_no_owner_and_a_fresh_lock_waits_only_until_it_is_no_longer_fresh(self):
        lock = self.make_lock(age=0)
        stamp = os.stat(lock).st_mtime
        (rc, out, err, took), pidfile = self.timeout_run(0.2, "hang", env_extra={"CLAUDE_HUB_TIMEOUT_FRESH": "0.8"})
        end = time.time()
        started = json.loads(Path(pidfile).read_text())["t"]
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(end - stamp, 0.8)
        self.assertLess(end - started, 3)
        self.assertRegex(err, NOTICE)
        self.assertGone(self.child_pid(pidfile))



class OwnLockTests(Case):
    def test_a_child_holding_its_lock_past_seconds_is_not_killed_until_it_releases(self):
        logfile = str(self.tmp / "log")
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "hold_release_hang", self.sd, logfile, 2.0)
        end = time.time()
        stamps = read_log(logfile)
        self.assertEqual(rc, 124)
        self.assertReleasedThenKilled(stamps, end)
        self.assertGreaterEqual(took, 1.8)
        self.assertLess(took, 5)
        self.assertRegex(err, NOTICE)
        self.assertEqual(len(NOTICE.findall(err)), 1)
        self.assertGone(self.child_pid(pidfile))

    def test_a_child_that_exits_by_itself_during_the_wait_gives_its_own_status_not_124(self):
        logfile = str(self.tmp / "log")
        (rc, out, err, took), _ = self.timeout_run(0.5, "hold_exit", self.sd, logfile, 1.5, 0)
        self.assertEqual(rc, 0)
        self.assertIn("exiting", read_log(logfile))
        self.assertGreaterEqual(took, 1.4)
        self.assertRegex(err, NOTICE)

    def test_its_status_is_passed_through_whatever_it_is(self):
        logfile = str(self.tmp / "log")
        (rc, out, err, took), _ = self.timeout_run(0.5, "hold_exit", self.sd, logfile, 1.0, 3)
        self.assertEqual(rc, 3)

    def test_the_wait_is_capped_at_lock_wait(self):
        logfile = str(self.tmp / "log")
        env = {"CLAUDE_HUB_TIMEOUT_LOCK_WAIT": "1.0", "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.sd)}
        (rc, out, err, took), pidfile = self.timeout_run(0.5, "hold_forever", self.sd, logfile, storage=False,
                                                         env_extra=env)
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 1.4)
        self.assertLess(took, 4)
        self.assertNotIn("releasing", read_log(logfile))
        self.assertRegex(err, NOTICE)
        self.assertGone(self.child_pid(pidfile))

    def test_the_storage_folder_comes_from_the_childs_environment(self):
        logfile = str(self.tmp / "log")
        env = {"CLAUDE_CONFIG_DIR": str(self.sd)}
        (rc, out, err, took), _ = self.timeout_run(0.4, "hold_release_hang", self.sd, logfile, 1.5, storage=False,
                                                   env_extra=env)
        self.assertEqual(rc, 124)
        self.assertGreaterEqual(took, 1.4)
        self.assertIn("releasing", read_log(logfile))



class TreeTests(Case):
    def test_a_lock_held_by_a_grandchild_is_waited_for(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        command = ["/bin/sh", "-c", '"$@"; :', "sh", self.fake, "hold_release_hang", pidfile, self.sd, logfile, "1.5"]
        rc, out, err, took = self.wrapped_run(0.5, command)
        end = time.time()
        self.assertEqual(rc, 124)
        self.assertReleasedThenKilled(read_log(logfile), end)
        self.assertGreaterEqual(took, 1.2)
        self.assertRegex(err, NOTICE)
        self.assertGone(self.child_pid(pidfile))

    def test_a_childs_own_helper_is_gone_after_a_timeout(self):
        (rc, out, err, took), pidfile = self.timeout_run(1.5, "spawn_helper")
        self.assertEqual(rc, 124)
        helper = self.helper_pid(pidfile)
        self.assertGone(helper)
        self.assertGone(self.child_pid(pidfile))

    def test_a_member_that_takes_the_lock_between_the_last_check_and_the_freeze_is_waited_for(self):
        logfile = str(self.tmp / "log")
        trigger = str(self.tmp / "go")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "take_on_trigger", pidfile, str(self.sd), logfile, trigger, "1.5"])
        self.procs.append(proc)
        self.child_pid(pidfile)
        real_freeze = LCT.Tree.freeze

        def late_take(tree):
            Path(trigger).write_text("go")
            self.assertTrue(wait_for(logfile), "the fake never took the lock")
            return real_freeze(tree)

        started = time.monotonic()
        with mock.patch.object(LCT.Tree, "freeze", late_take):
            status, notices = self.stop_in_process(proc, {"CLAUDE_HUB_TIMEOUT_LOCK_WAIT": "10"})
        end = time.time()
        self.assertIsNone(status)
        self.assertReleasedThenKilled(read_log(logfile), end)
        self.assertGreaterEqual(time.monotonic() - started, 1.2)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(len(notices), 1)
        self.assertEqual(proc.poll(), -signal.SIGKILL)

    def test_the_notice_is_written_after_the_kill(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "hold_release_hang", pidfile, str(self.sd), logfile, "1.0"])
        self.procs.append(proc)
        self.child_pid(pidfile)
        self.assertTrue(wait_for(logfile))
        status, notices = self.stop_in_process(proc)
        self.assertIsNone(status)
        self.assertEqual(len(notices), 1)
        text, returncode = notices[0]
        self.assertRegex(text, r"^waited \d+ s for a sign-in refresh before stopping claude$")
        self.assertEqual(returncode, -signal.SIGKILL)

    def test_a_child_that_exits_by_itself_during_the_wait_gives_its_own_status_and_one_notice(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "hold_exit", pidfile, str(self.sd), logfile, "0.8", "5"])
        self.procs.append(proc)
        self.child_pid(pidfile)
        self.assertTrue(wait_for(logfile))
        status, notices = self.stop_in_process(proc)
        self.assertEqual(status, 5)
        self.assertEqual(len(notices), 1)

    def test_the_walk_finds_a_grandchild_and_falls_back_to_a_scan_without_children_files(self):
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "spawn_helper", pidfile])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        tree = LCT.Tree(proc.pid)
        self.assertTrue(tree.refresh())
        self.assertTrue(tree.live(helper))
        self.assertFalse(tree.refresh())
        scanned = LCT.Tree(proc.pid)
        with mock.patch.object(LCT, "_children_from_proc", return_value=None):
            scanned.refresh()
        self.assertTrue(scanned.live(helper))
        scanned.kill()
        proc.wait(timeout=5)
        self.assertGone(helper)

    def test_a_pid_with_another_start_time_is_no_member_and_is_never_signalled(self):
        root = subprocess.Popen(["sleep", "60"])
        other = subprocess.Popen(["sleep", "60"])
        self.procs.extend([root, other])
        start = LCT.read_stat(other.pid)[2]
        self.assertFalse(LCT.signal_member(other.pid, start + 1, signal.SIGKILL))
        tree = LCT.Tree(root.pid)
        tree.members[other.pid] = start + 1
        self.assertFalse(tree.live(other.pid))
        self.assertFalse(tree.holds(other.pid))
        tree.members[other.pid] = start + 1
        tree.kill()
        self.assertEqual(root.wait(timeout=5), -signal.SIGKILL)
        time.sleep(0.2)
        self.assertIsNone(other.poll())
        self.assertTrue(LCT.signal_member(other.pid, start, signal.SIGKILL))
        self.assertEqual(other.wait(timeout=5), -signal.SIGKILL)

    def test_in_flight_with_a_tree_counts_a_live_descendant_and_nothing_else(self):
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "spawn_helper", pidfile])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        stranger = subprocess.Popen(["sleep", "60"])
        self.procs.append(stranger)
        tree = LCT.Tree(proc.pid)
        self.make_lock(owner=helper, age=100)
        self.assertTrue(LCT.in_flight(str(self.sd), proc.pid, tree=tree))
        self.assertFalse(LCT.in_flight(str(self.sd), proc.pid))
        for owner in (os.getpid(), stranger.pid, self.dead_pid()):
            (self.sd / OWNER).write_text(json.dumps({"pid": owner}))
            with self.subTest(owner=owner):
                self.assertFalse(LCT.in_flight(str(self.sd), proc.pid, tree=tree))

    def test_a_claude_daemon_and_its_own_child_survive_a_timeout(self):
        (rc, out, err, took), pidfile = self.timeout_run(1.5, "spawn_daemon")
        self.assertEqual(rc, 124)
        daemon = self.daemon_info(pidfile)
        self.assertEqual(daemon["sid"], daemon["pid"])
        self.assertGone(self.child_pid(pidfile))
        time.sleep(0.3)
        self.assertTrue(LCT.pid_alive(daemon["pid"]))
        self.assertTrue(LCT.pid_alive(daemon["child"]))
        self.assertNotRegex(err, NOTICE)

    def test_a_daemon_holding_the_lock_does_not_delay_the_stop(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = self.spawn(["--storage", str(self.sd), "60", "--", str(self.fake), "spawn_daemon", pidfile,
                           str(self.sd), logfile])
        self.assertTrue(wait_for(logfile), "the daemon never took the lock")
        daemon = self.daemon_info(pidfile)
        self.assertTrue((self.sd / LOCK).is_dir())
        self.assertEqual(json.loads((self.sd / OWNER).read_text())["pid"], daemon["pid"])
        self.assertTrue(LCT.pid_alive(daemon["pid"]))
        sent = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc, out, err = self.finish(proc)
        self.assertEqual(rc, 143)
        self.assertLess(time.monotonic() - sent, 10)
        self.assertTrue((self.sd / LOCK).is_dir())
        self.assertNotRegex(err, NOTICE)
        self.assertTrue(LCT.pid_alive(daemon["pid"]))
        self.assertGone(self.child_pid(pidfile))

    def test_a_helper_in_a_session_of_its_own_is_still_killed(self):
        (rc, out, err, took), pidfile = self.timeout_run(1.5, "spawn_helper_setsid")
        self.assertEqual(rc, 124)
        helper = self.helper_pid(pidfile)
        self.assertGone(helper)

    def test_the_walk_leaves_out_a_daemon_with_its_subtree_but_keeps_a_look_alike(self):
        for mode, kept in (("spawn_daemon", False), ("spawn_daemon_samesession", True),
                           ("spawn_daemon_notclaude", True)):
            with self.subTest(mode=mode):
                pidfile = self.new_pidfile()
                proc = subprocess.Popen([str(self.fake), mode, pidfile, str(self.sd), str(self.tmp / ("log-" + mode))])
                self.procs.append(proc)
                self.child_pid(pidfile)
                daemon = self.daemon_info(pidfile)
                self.assertTrue(wait_for(str(self.tmp / ("log-" + mode))))
                tree = LCT.Tree(proc.pid)
                tree.refresh()
                time.sleep(0.2)
                tree.refresh()
                self.assertEqual(tree.live(daemon["pid"]), kept)
                self.assertEqual(tree.live(daemon["child"]), kept)
                self.assertEqual(LCT.in_flight(str(self.sd), proc.pid, tree=tree), kept)
                tree.kill()
                proc.wait(timeout=5)
                time.sleep(0.2)
                self.assertEqual(LCT.pid_alive(daemon["pid"]), not kept)
                if not kept:
                    LCT.signal_member(daemon["pid"], LCT.read_stat(daemon["pid"])[2], signal.SIGKILL)
                    LCT.signal_member(daemon["child"], LCT.read_stat(daemon["child"])[2], signal.SIGKILL)
                for name in (LOCK, OWNER):
                    path = self.sd / name
                    if path.is_dir():
                        path.rmdir()
                    elif path.exists():
                        path.unlink()

    def test_the_child_is_killed_even_when_proc_cannot_be_read(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.procs.append(proc)
        started = time.monotonic()
        with mock.patch.object(LCT, "read_stat", return_value=None):
            status, notices = self.stop_in_process(proc)
        self.assertIsNone(status)
        self.assertEqual(proc.poll(), -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, LCT.REAP_WAIT)

    def test_a_child_that_exits_by_itself_during_a_stop_leaves_none_of_its_tree_running(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "hold_exit_helper", pidfile, str(self.sd), logfile, "1.0", "0"])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        self.assertTrue(wait_for(logfile))
        status, notices = self.stop_in_process(proc)
        self.assertEqual(status, 0)
        self.assertGone(helper)
        self.assertEqual(len(notices), 1)

    def test_a_daemon_left_by_a_child_that_exited_by_itself_is_left_alone(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "hold_exit_helper", pidfile, str(self.sd), logfile, "1.0", "0",
                                 "daemon"])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        daemon = self.daemon_info(pidfile)
        self.assertTrue(wait_for(logfile))
        status, notices = self.stop_in_process(proc)
        self.assertEqual(status, 0)
        self.assertGone(helper)
        time.sleep(0.3)
        self.assertTrue(LCT.pid_alive(daemon["pid"]))
        self.assertTrue(LCT.pid_alive(daemon["child"]))

    def test_a_child_that_ends_by_itself_with_no_stop_requested_leaves_its_children_alone(self):
        (rc, out, err, took), pidfile = self.timeout_run(30, "exit_leaving_helper", 0)
        self.assertEqual(rc, 0)
        helper = self.helper_pid(pidfile)
        time.sleep(0.3)
        self.assertTrue(LCT.pid_alive(helper))
        self.assertEqual(err, "")

    def test_a_freeze_that_did_not_settle_is_followed_by_one_more_walk_before_the_kill(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.procs.append(proc)
        events = []
        real_refresh, real_kill = LCT.Tree.refresh, LCT.Tree.kill

        def freeze(tree):
            events.append("freeze")
            return False

        def refresh(tree):
            events.append("refresh")
            return real_refresh(tree)

        def kill(tree):
            events.append("kill")
            return real_kill(tree)

        with mock.patch.object(LCT.Tree, "freeze", freeze), mock.patch.object(LCT.Tree, "refresh", refresh), \
                mock.patch.object(LCT.Tree, "kill", kill):
            status, notices = self.stop_in_process(proc)
        self.assertIsNone(status)
        self.assertEqual(events[events.index("freeze"):][:3], ["freeze", "refresh", "kill"])
        self.assertEqual(proc.poll(), -signal.SIGKILL)

    def wait_state(self, pid, wanted, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            st = LCT.read_stat(pid)
            if st is not None and wanted(st[0]):
                return st[0]
            time.sleep(0.01)
        self.fail(f"pid {pid} never reached the expected state, last {LCT.read_stat(pid)}")

    def test_a_member_that_turns_out_to_be_a_daemon_is_continued_and_left_out(self):
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "spawn_helper", pidfile])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        tree = LCT.Tree(proc.pid)
        self.assertTrue(tree.freeze())
        start = tree.members[helper]
        self.assertEqual(tree.ours.get(helper), start)
        self.wait_state(helper, lambda state: state in ("T", "t"))
        with mock.patch.object(LCT, "is_claude_daemon", lambda pid, st: pid == helper):
            tree.refresh()
        self.assertNotIn(helper, tree.members)
        self.assertNotIn(helper, tree.ours)
        self.assertIn((helper, start), tree.excluded)
        self.wait_state(helper, lambda state: state not in ("T", "t"))
        tree.refresh()
        self.assertNotIn(helper, tree.members)
        tree.kill()
        proc.wait(timeout=5)
        time.sleep(0.2)
        self.assertTrue(LCT.pid_alive(helper))

    def test_a_process_somebody_else_stopped_stays_stopped_after_a_thaw(self):
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "spawn_helper", pidfile])
        self.procs.append(proc)
        self.child_pid(pidfile)
        helper = self.helper_pid(pidfile)
        os.kill(helper, signal.SIGSTOP)
        self.wait_state(helper, lambda state: state in ("T", "t"))
        tree = LCT.Tree(proc.pid)
        self.assertTrue(tree.freeze())
        self.assertNotIn(helper, tree.ours)
        self.assertIn(proc.pid, tree.ours)
        self.wait_state(proc.pid, lambda state: state in ("T", "t"))
        tree.thaw()
        self.wait_state(proc.pid, lambda state: state not in ("T", "t"))
        time.sleep(0.2)
        self.assertIn(LCT.read_stat(helper)[0], ("T", "t"))
        tree.kill()
        proc.wait(timeout=5)

    def test_a_second_signal_does_not_shorten_the_wait(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = self.spawn(["--storage", str(self.sd), "60", "--", str(self.fake), "hold_release_hang", pidfile,
                           str(self.sd), logfile, "2.0"])
        self.assertTrue(wait_for(logfile))
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.4)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        proc.send_signal(signal.SIGHUP)
        rc, out, err = self.finish(proc)
        end = time.time()
        self.assertEqual(rc, 143)
        self.assertReleasedThenKilled(read_log(logfile), end)
        self.assertRegex(err, NOTICE)
        self.assertGone(self.child_pid(pidfile))

    def test_descriptors_the_caller_handed_down_reach_the_child_and_the_wakeup_pipe_does_not(self):
        pidfile = self.new_pidfile()
        r, w = os.pipe()
        try:
            proc = subprocess.Popen([str(WRAPPER), "--storage", str(self.sd), "30", "--", str(self.fake), "fds", pidfile],
                                    env=self.env, pass_fds=[w], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            self.procs.append(proc)
            out, err = proc.communicate(timeout=30)
        finally:
            os.close(r)
            os.close(w)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(json.loads(out), [w])



class SignalTests(Case):
    def start_hang(self, **kw):
        pidfile = self.new_pidfile()
        proc = self.spawn(["--storage", str(self.sd), "60", "--", str(self.fake), "hang", pidfile], **kw)
        return proc, self.child_pid(pidfile)

    def test_sigterm_runs_the_stop_and_exits_143(self):
        proc, pid = self.start_hang()
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc, out, err = self.finish(proc)
        self.assertEqual(rc, 143)
        self.assertLess(time.monotonic() - start, 3)
        self.assertGone(pid)
        self.assertNotRegex(err, NOTICE)

    def test_sighup_exits_129(self):
        proc, pid = self.start_hang()
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(self.finish(proc)[0], 129)
        self.assertGone(pid)

    def test_sigint_exits_130(self):
        proc, pid = self.start_hang()
        proc.send_signal(signal.SIGINT)
        self.assertEqual(self.finish(proc)[0], 130)
        self.assertGone(pid)

    def test_a_signal_while_the_child_holds_its_lock_waits_for_the_release(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = self.spawn(["--storage", str(self.sd), "60", "--", str(self.fake), "hold_release_hang", pidfile,
                           str(self.sd), logfile, "1.5"])
        self.assertTrue(wait_for(logfile))
        proc.send_signal(signal.SIGTERM)
        rc, out, err = self.finish(proc)
        end = time.time()
        stamps = read_log(logfile)
        self.assertEqual(rc, 143)
        self.assertReleasedThenKilled(stamps, end)
        self.assertRegex(err, NOTICE)
        self.assertGone(self.child_pid(pidfile))

    def test_a_signal_the_wrapper_was_started_with_ignored_stays_ignored(self):
        pidfile = self.new_pidfile()
        proc = self.spawn(["--storage", str(self.sd), "2.5", "--", str(self.fake), "hang", pidfile], ignore="HUP")
        pid = self.child_pid(pidfile)
        started = json.loads(Path(pidfile).read_text())["t"]
        proc.send_signal(signal.SIGHUP)
        rc, out, err = self.finish(proc)
        end = time.time()
        self.assertEqual(rc, 124)
        self.assertGreater(end - started, 1.5)
        self.assertGone(pid)



class WaitRefreshTests(Case):
    def holder(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.procs.append(proc)
        return proc

    def wait_refresh(self, pid, limit="10", storage=None):
        return self.run_wrapper(["--storage", str(storage or self.sd), "--wait-refresh", str(pid), "--limit", limit])

    def test_no_lock_returns_0_at_once(self):
        holder = self.holder()
        rc, out, err, took = self.wait_refresh(holder.pid)
        self.assertEqual(rc, 0)
        self.assertLess(took, 2)

    def test_a_live_holder_returns_1_at_the_limit(self):
        holder = self.holder()
        self.make_lock(owner=holder.pid)
        rc, out, err, took = self.wait_refresh(holder.pid, limit="0.6")
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(took, 0.55)
        self.assertLess(took, 3)

    def test_it_returns_0_as_soon_as_the_holder_releases(self):
        holder = self.holder()
        lock = self.make_lock(owner=holder.pid)

        def release():
            (self.sd / OWNER).unlink()
            lock.rmdir()

        timer = threading.Timer(0.6, release)
        timer.start()
        try:
            rc, out, err, took = self.wait_refresh(holder.pid, limit="20")
        finally:
            timer.join()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(took, 0.5)
        self.assertLess(took, 4)

    def test_a_holder_that_died_with_the_lock_is_not_holding_it(self):
        dead = subprocess.Popen(["true"])
        time.sleep(0.3)
        self.procs.append(dead)
        self.make_lock(owner=dead.pid)
        rc, out, err, took = self.wait_refresh(dead.pid, limit="5")
        self.assertEqual(rc, 0)
        self.assertLess(took, 2)

    def test_somebody_elses_lock_is_not_the_pids(self):
        holder = self.holder()
        self.make_lock(owner=os.getpid())
        rc, out, err, took = self.wait_refresh(holder.pid, limit="5")
        self.assertEqual(rc, 0)

    def test_what_the_pid_started_counts_too(self):
        proc = subprocess.Popen(["sh", "-c", "sleep 60; :"])
        self.procs.append(proc)
        tree = LCT.Tree(proc.pid)
        end = time.monotonic() + 5
        while len(tree.members) < 2 and time.monotonic() < end:
            tree.refresh()
            time.sleep(0.02)
        try:
            self.assertEqual(len(tree.members), 2)
            sleeper = [p for p in tree.members if p != proc.pid][0]
            lock = self.make_lock(owner=sleeper)
            rc, out, err, took = self.wait_refresh(proc.pid, limit="0.6")
            self.assertEqual(rc, 1)
            (self.sd / OWNER).unlink()
            lock.rmdir()
            self.assertEqual(self.wait_refresh(proc.pid, limit="5")[0], 0)
        finally:
            tree.kill()

    def test_a_daemon_under_the_pid_is_left_out_here_too(self):
        logfile = str(self.tmp / "log")
        pidfile = self.new_pidfile()
        proc = subprocess.Popen([str(self.fake), "spawn_daemon", pidfile, str(self.sd), logfile])
        self.procs.append(proc)
        self.daemon_info(pidfile)
        self.assertTrue(wait_for(logfile))
        rc, out, err, took = self.wait_refresh(proc.pid, limit="5")
        self.assertEqual(rc, 0)
        self.assertLess(took, 3)

    def test_the_default_limit_is_45(self):
        self.assertEqual(LCT.parse_args(["--wait-refresh", "5"]).limit, 45.0)
        self.assertEqual(LCT.parse_args(["--wait-refresh", "5", "--limit", "7.5"]).limit, 7.5)



class StorageTests(Case):
    def test_the_order_is_storage_then_secure_storage_then_config_dir_then_home(self):
        env = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(self.tmp / "secure"), "CLAUDE_CONFIG_DIR": str(self.tmp / "conf"),
               "HOME": str(self.home)}
        real = lambda name: os.path.realpath(str(self.tmp / name))
        self.assertEqual(LCT.storage_dir(str(self.tmp / "explicit"), env), real("explicit"))
        self.assertEqual(LCT.storage_dir(None, env), real("secure"))
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = ""
        self.assertEqual(LCT.storage_dir(None, env), os.path.realpath(str(self.home / ".claude")))
        del env["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
        self.assertEqual(LCT.storage_dir(None, env), real("conf"))
        env["CLAUDE_CONFIG_DIR"] = ""
        self.assertEqual(LCT.storage_dir(None, env), os.path.realpath(str(self.home / ".claude")))
        del env["HOME"]
        self.assertEqual(LCT.storage_dir(None, env), os.path.realpath(os.path.expanduser("~/.claude")))

    def test_the_result_is_resolved(self):
        real = self.tmp / "real"
        real.mkdir()
        (self.tmp / "link").symlink_to(real)
        self.assertEqual(LCT.storage_dir(str(self.tmp / "link"), {}), os.path.realpath(real))

    def test_the_folder_the_wrapper_checks_follows_the_order(self):
        holder = subprocess.Popen(["sleep", "60"])
        self.procs.append(holder)
        pid = str(holder.pid)
        dirs = {name: self.tmp / name for name in ("explicit", "secure", "conf")}
        for path in dirs.values():
            path.mkdir()
        (self.home / ".claude").mkdir()

        def held(lock_dir, argv, env):
            lock = lock_dir / LOCK
            lock.mkdir()
            (lock_dir / OWNER).write_text(json.dumps({"pid": holder.pid}))
            try:
                return self.run_wrapper(argv + ["--wait-refresh", pid, "--limit", "0.2"], env_extra=env)[0]
            finally:
                (lock_dir / OWNER).unlink()
                lock.rmdir()

        both = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": str(dirs["secure"]), "CLAUDE_CONFIG_DIR": str(dirs["conf"])}
        self.assertEqual(held(dirs["explicit"], ["--storage", str(dirs["explicit"])], both), 1)
        self.assertEqual(held(dirs["secure"], ["--storage", str(dirs["explicit"])], both), 0)
        self.assertEqual(held(dirs["secure"], [], both), 1)
        self.assertEqual(held(dirs["conf"], [], both), 0)
        self.assertEqual(held(dirs["conf"], [], {"CLAUDE_CONFIG_DIR": str(dirs["conf"])}), 1)
        empty = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "", "CLAUDE_CONFIG_DIR": str(dirs["conf"])}
        self.assertEqual(held(dirs["conf"], [], empty), 0)
        self.assertEqual(held(self.home / ".claude", [], empty), 1)
        self.assertEqual(held(self.home / ".claude", [], {}), 1)



class KnobTests(Case):
    def test_a_knob_may_only_lower_its_default(self):
        name = "CLAUDE_HUB_TIMEOUT_TEST_KNOB"
        for raw, want in (("1", 1.0), ("45", 45.0), ("0.05", 0.05), ("46", 45.0), ("1000", 45.0), ("-1", 45.0),
                          ("nan", 45.0), ("inf", 45.0), ("abc", 45.0), ("", 45.0), ("  ", 45.0)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {name: raw}):
                self.assertEqual(LCT.knob(name, 45.0), want)
        with mock.patch.dict(os.environ, {name: "0.001"}):
            self.assertEqual(LCT.knob(name, 0.25, minimum=0.005), 0.25)
        os.environ.pop(name, None)
        self.assertEqual(LCT.knob(name, 45.0), 45.0)

    def test_a_fresh_knob_above_its_default_does_not_hold_a_stop_back(self):
        self.make_lock(age=5)
        env = {"CLAUDE_HUB_TIMEOUT_FRESH": "100"}
        pidfile = self.new_pidfile()
        argv = ["--storage", str(self.sd), "0.5", "--", str(self.fake), "hang", pidfile]
        rc, out, err, took = self.run_wrapper(argv, env_extra=env, seconds=15)
        self.assertEqual(rc, 124)
        self.assertLess(took, 5)
        self.assertNotRegex(err, NOTICE)

    def test_the_worst_case_stays_under_the_callers_backstop(self):
        self.assertLess(LCT.DEFAULT_LOCK_WAIT + LCT.REAP_WAIT + 1, 55)



class UsageTests(Case):
    def test_usage_errors_exit_125(self):
        cases = [
            [],
            ["abc", "--", "true"],
            ["5", "true"],
            ["5", "--"],
            ["0", "--", "true"],
            ["-5", "--", "true"],
            ["nan", "--", "true"],
            ["inf", "--", "true"],
            ["1_0", "--", "true"],
            ["5", "6", "--", "true"],
            ["--bogus", "5", "--", "true"],
            ["--storage"],
            ["--limit", "5", "3", "--", "true"],
            ["--wait-refresh"],
            ["--wait-refresh", "abc"],
            ["--wait-refresh", "0"],
            ["--wait-refresh", "-3"],
            ["--wait-refresh", "5", "extra"],
            ["--wait-refresh", "5", "--", "true"],
            ["--wait-refresh", "5", "--limit", "x"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err, _ = self.run_wrapper(argv)
                self.assertEqual(rc, 125, err)
                self.assertIn("usage:", err)

    def test_help_exits_0(self):
        rc, out, err, _ = self.run_wrapper(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage:", out)

    def test_the_doc_names_pty_relays_as_unsupported_and_does_not_claim_sudo(self):
        flat = " ".join(LCT.__doc__.split())
        self.assertIn("Not supported: a pty relay in front of claude (`sudo`, `su --pty`, `script`)", flat)
        self.assertNotIn("`sudo` in front of claude", flat)
        self.assertNotIn("sudo in front of claude", flat)

    def test_help_says_a_backstop_must_be_a_group_kill(self):
        rc, out, err, _ = self.run_wrapper(["--help"])
        self.assertIn("SIGKILL", out)
        self.assertIn("process group", out)

    def test_the_equals_forms_work(self):
        args = LCT.parse_args(["--storage=/x", "5", "--", "true"])
        self.assertEqual((args.storage, args.seconds, args.cmd), ("/x", 5.0, ["true"]))
        args = LCT.parse_args(["--wait-refresh=12", "--limit=3"])
        self.assertEqual((args.wait_pid, args.limit), (12, 3.0))

    def test_the_command_may_carry_dashes_and_its_own_double_dash(self):
        args = LCT.parse_args(["5", "--", "claude", "-p", "--", "hi"])
        self.assertEqual(args.cmd, ["claude", "-p", "--", "hi"])



class InFlightTests(Case):
    def test_owner_record_shapes(self):
        me = os.getpid()
        self.assertFalse(LCT.in_flight(str(self.sd), me))
        self.make_lock(owner=me, age=100)
        self.assertTrue(LCT.in_flight(str(self.sd), me))
        self.assertFalse(LCT.in_flight(str(self.sd), me + 1))
        owner = self.sd / OWNER
        for raw in ('{"pid": "%d"}' % me, '{"pid": %d.0}' % me, '{"pid": true}', '{"pid": null}', "{}", "[]", "7",
                    "", "not json", '{"pid": %d' % me, '{"pid": 99999999999999999999}'):
            with self.subTest(raw=raw):
                owner.write_text(raw)
                self.assertFalse(LCT.in_flight(str(self.sd), me))
        owner.write_text(json.dumps({"pid": me}) + " " * (LCT.OWNER_MAX_BYTES + 10))
        self.assertFalse(LCT.in_flight(str(self.sd), me))
        owner.unlink()
        owner.mkdir()
        self.assertFalse(LCT.in_flight(str(self.sd), me))
        owner.rmdir()
        owner.write_text(json.dumps({"pid": me}))
        owner.chmod(0)
        try:
            if os.geteuid() != 0:
                self.assertFalse(LCT.in_flight(str(self.sd), me))
        finally:
            owner.chmod(0o600)

    def test_an_owner_record_that_is_a_link_is_never_followed(self):
        me = os.getpid()
        self.make_lock(age=0)
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps({"pid": me}))
        owner = self.sd / OWNER
        owner.symlink_to(target)
        self.assertFalse(LCT.in_flight(str(self.sd), me))
        owner.unlink()
        owner.symlink_to(self.tmp / "nowhere")
        self.assertFalse(LCT.in_flight(str(self.sd), me))
        owner.unlink()
        os.mkfifo(owner)
        self.assertFalse(LCT.in_flight(str(self.sd), me))

    def test_no_owner_means_a_fresh_directory_only(self):
        lock = self.make_lock(age=0)
        self.assertTrue(LCT.in_flight(str(self.sd), 12345))
        stamp = time.time() - 3
        os.utime(lock, (stamp, stamp))
        self.assertFalse(LCT.in_flight(str(self.sd), 12345))
        self.assertTrue(LCT.in_flight(str(self.sd), 12345, fresh=10))
        stamp = time.time() + 500
        os.utime(lock, (stamp, stamp))
        self.assertFalse(LCT.in_flight(str(self.sd), 12345))

    def test_pid_alive(self):
        self.assertTrue(LCT.pid_alive(os.getpid()))
        self.assertFalse(LCT.pid_alive(0))
        self.assertFalse(LCT.pid_alive(-1))
        self.assertFalse(LCT.pid_alive(True))
        self.assertFalse(LCT.pid_alive(2 ** 70))
        self.assertFalse(LCT.pid_alive(self.dead_pid()))
        zombie = subprocess.Popen(["true"])
        time.sleep(0.3)
        try:
            self.assertFalse(LCT.pid_alive(zombie.pid))
        finally:
            zombie.wait()


if __name__ == "__main__":
    unittest.main()
