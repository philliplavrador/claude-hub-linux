"""agent-bin/kwin_wayland refuses a KWin that would share your settings or your session bus.

Run: python3 -m pytest tests/test_refuse_shared_kwin.py

A second kwin_wayland on the real session bus with the real ~/.config can register your global shortcuts with the
compositor's shortcut server and leave them dead when it exits. The shim is run through its `kwin_wayland` symlink
with a crafted environment; a refusal never reaches the real binary, and the pass-through cases only use flags that
print and exit.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
import unittest
from pathlib import Path

AGENT_BIN = Path(__file__).resolve().parents[1] / "agent-bin"
SHIM = AGENT_BIN / "kwin_wayland"
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
REAL_CONFIG = f"{REAL_HOME}/.config"
REAL_BUS = f"unix:path=/run/user/{os.getuid()}/bus"


class Shim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.private_config = str(Path(self.tmp.name) / "config")
        self.private_bus = f"unix:path={self.tmp.name}/bus"

    def run_shim(self, *args, config=None, bus=None, runtime=None, home=None):
        env = {"PATH": "/usr/bin:/bin", "XDG_STATE_HOME": str(self.state),
               "HOME": home or REAL_HOME}
        if config is not None:
            env["XDG_CONFIG_HOME"] = config
        if bus is not None:
            env["DBUS_SESSION_BUS_ADDRESS"] = bus
        if runtime is not None:
            env["XDG_RUNTIME_DIR"] = runtime
        return subprocess.run([str(SHIM), *args], env=env, capture_output=True, text=True, timeout=30)

    def log_text(self):
        f = self.state / "claude-hub/agent-screen.log"
        return f.read_text() if f.exists() else ""

    def assert_refused(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("claude-hub:", r.stderr)
        self.assertIn("global shortcuts", r.stderr)
        self.assertIn("kwin_wayland", self.log_text())
        self.assertIn("refused", self.log_text())

    def test_the_shim_is_wired(self):
        self.assertTrue(os.access(AGENT_BIN / "refuse-shared-kwin", os.X_OK))
        self.assertEqual(os.readlink(AGENT_BIN / "kwin_wayland"), "refuse-shared-kwin")
        self.assertEqual(os.readlink(AGENT_BIN / "kwin_wayland_wrapper"), "refuse-shared-kwin")

    def test_a_real_config_with_a_private_bus_is_refused(self):
        r = self.run_shim("--virtual", config=REAL_CONFIG, bus=self.private_bus)
        self.assert_refused(r)
        self.assertIn("settings folder", r.stderr)

    def test_a_real_config_with_a_trailing_slash_is_refused(self):
        r = self.run_shim("--virtual", config=REAL_CONFIG + "/", bus=self.private_bus)
        self.assert_refused(r)

    def test_an_unset_config_under_his_home_is_refused(self):
        r = self.run_shim("--virtual", bus=self.private_bus)
        self.assert_refused(r)

    def test_a_changed_home_does_not_hide_his_config(self):
        r = self.run_shim("--virtual", config=REAL_CONFIG, bus=self.private_bus, home=self.tmp.name)
        self.assert_refused(r)

    def test_a_private_config_with_his_real_bus_is_refused(self):
        r = self.run_shim("--virtual", config=self.private_config, bus=REAL_BUS)
        self.assert_refused(r)
        self.assertIn("D-Bus", r.stderr)

    def test_a_private_config_with_no_bus_set_falls_back_to_his_runtime_bus(self):
        r = self.run_shim("--virtual", config=self.private_config)
        self.assert_refused(r)

    def test_the_wrapper_name_is_refused_too(self):
        env = {"PATH": "/usr/bin:/bin", "XDG_STATE_HOME": str(self.state), "HOME": REAL_HOME,
               "XDG_CONFIG_HOME": REAL_CONFIG}
        r = subprocess.run([str(AGENT_BIN / "kwin_wayland_wrapper"), "--virtual"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("kwin_wayland_wrapper", self.log_text())

    def test_private_everything_runs_the_real_binary(self):
        r = self.run_shim("--version", config=self.private_config, bus=self.private_bus)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kwin", r.stdout.lower())
        self.assertEqual(self.log_text(), "")

    def test_help_passes_even_with_everything_real(self):
        r = self.run_shim("--help", config=REAL_CONFIG, bus=REAL_BUS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("claude-hub:", r.stderr)
        self.assertEqual(self.log_text(), "")

    def test_a_private_bus_address_with_a_guid_and_his_runtime_dir_goes_through_the_checks_and_passes(self):
        r = self.run_shim("--virtual", "--version", config=self.private_config,
                          bus="unix:path=/tmp/dbus-XXXX,guid=0123", runtime=f"/run/user/{os.getuid()}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kwin", r.stdout.lower())
        self.assertEqual(self.log_text(), "")

    def test_a_backslash_in_an_argument_is_printed_as_typed(self):
        r = self.run_shim("--virtual", "a\\nb", config=REAL_CONFIG, bus=self.private_bus)
        self.assert_refused(r)
        self.assertIn("a\\nb", r.stderr)
        self.assertIn("a\\nb", self.log_text())

    def test_without_a_passwd_entry_home_stands_in(self):
        env = {"PATH": "/nonexistent-only", "XDG_STATE_HOME": str(self.state), "HOME": self.tmp.name}
        env["DBUS_SESSION_BUS_ADDRESS"] = self.private_bus
        r = subprocess.run(["/bin/sh", str(SHIM), "--virtual"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("settings folder", r.stderr)

    def test_a_harmless_flag_beside_a_real_one_is_not_a_pass(self):
        r = self.run_shim("--version", "--virtual", config=REAL_CONFIG, bus=REAL_BUS)
        self.assert_refused(r)


if __name__ == "__main__":
    unittest.main()
