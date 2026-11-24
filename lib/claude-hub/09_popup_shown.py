# claude-hub, part 9: Popup's shown / hidden life: the record, apply, show, hide, settle, signals.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class PopupShown:
    def write_record(self) -> None:
        st = {"pid": os.getpid(), "konsole_pid": self.konsole_pid, "kind": "popup",
              "visible": self.visible, "started": self.started}
        if self.watch:
            st["watch"] = self.watch
        write_state(st)

    def apply(self, while_hidden: bool = False) -> bool:
        with self.kwin_lock:
            visible = self.visible
            if while_hidden and visible:
                return False
            if visible:
                self.shown_on = DK.current_desktop()
            self.showing = visible
            try:
                DK.kwin_script(script_show(self.konsole_pid) if visible else script_hide(self.konsole_pid),
                               "hub-show" if visible else "hub-hide")
            finally:
                if visible:
                    self.shown_at = time.monotonic()
                self.showing = False
        return True

    def prepare(self) -> None:
        with self.lock:
            self.mode, self.filter, self.number, self.confirm, self.renaming = "list", "", "", None, None
        try:
            self.look()
        except Exception as e:                           # noqa: BLE001
            with self.lock:
                self.message = f"{REFRESH_FAILED}{e}{OFF}"
        with self.lock:
            self.sel_key = self.current_key() or SESSION_ROW["key"]
        poke()
        self.top = 0
        self.dirty = True

    def show(self) -> None:
        self.visible = True
        self.shown_at = time.monotonic()
        self.write_record()
        self.prepare()
        self.draw()
        self.apply()
        self.focus_due = time.monotonic() + SHOW_CONFIRM
        self.wake.set()

    def confirm_show(self) -> None:
        if self.visible and self.focus_due and time.monotonic() > self.focus_due:
            self.focus_due = 0.0
            keep_out_of_taskbar()
            self.apply()

    def hide(self) -> None:
        self.focus_due = 0.0
        with self.lock:
            self.visible, self.filter, self.confirm, self.renaming = False, "", None, None
            self.mode = "list"
            self.message = ""
        self.write_record()
        self.apply()

    def settle(self) -> None:
        end = time.monotonic() + OPENING_FOR
        time.sleep(0.2)
        keep_out_of_taskbar()
        self.apply()
        time.sleep(1.3)
        keep_out_of_taskbar()
        self.apply()
        while time.monotonic() < end:
            time.sleep(1.0)
            if self.visible or self.touched:
                return
            keep_out_of_taskbar()
            if not self.apply(while_hidden=True):
                return

    def take_signals(self) -> None:
        while self.signals:
            what = self.signals.popleft()
            if what in ("toggle", "hide"):
                self.touched = True
            if what == "toggle":
                if self.visible and not self.elsewhere():
                    self.hide()
                else:
                    self.show()
            elif what == "hide":
                self.hide()
            elif what == "resize":
                self.dirty = True

    def elsewhere(self) -> bool:
        cur = DK.current_desktop()
        return bool(cur and self.shown_on and cur != self.shown_on)

    def hides_on_focus_out(self) -> bool:
        return bool(self.visible and not self.showing and not self.ending
                    and time.monotonic() - self.shown_at >= FOCUS_GRACE)
