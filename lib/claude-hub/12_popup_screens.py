# claude-hub, part 12: Popup's Screens mode.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class PopupScreens:
    def enter_screens(self) -> None:
        with self.lock:
            self.mode, self.filter, self.message, self.confirm = "screens", "", "", None
        rows = self.shown_rows()
        self.sel_key = rows[0].get("key") if rows else None
        self.top, self.dirty = 0, True

    def leave_screens(self) -> None:
        with self.lock:
            self.mode, self.filter, self.message = "list", "", ""
            home = sticky_home_row(self.rows)
        self.sel_key = (home or {}).get("key") or SESSION_ROW["key"]
        self.top, self.dirty = 0, True

    def on_screens_key(self, k: str) -> None:
        if k == "esc":
            self.leave_screens()
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.message = ""
            self.move(k)
        elif k in ("enter", "right"):
            self.flip_screen()

    def flip_screen(self) -> None:
        row = self.selected()
        if not row:
            return
        with self.lock:
            data = self.service or {}
        try:
            screens = DK.set_sticky(row["name"], not row["sticky"], data)
        except DK.Refused as e:
            self.message = f"{RED}{clip(str(e), 200)}{OFF}"
            return
        except Exception as e:                            # noqa: BLE001 — a change that failed says so; it never closes the popup
            self.message = f"{RED}could not change it: {clip(str(e), 200)}{OFF}"
            return
        with self.lock:
            self.message = ""
            add_sticky(self.rows, screens)
