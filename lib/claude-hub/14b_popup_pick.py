# claude-hub, part 14b: Popup's key handling for a list that is picked from: New Claude Code's model and effort.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class PopupPick:
    def on_pick_key(self, k: str, open_row, back) -> None:
        if k == "esc":
            if self.filter:
                self.set_filter("")
            else:
                back()
        elif k == "backspace":
            self.set_filter(self.filter[:-1])
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.move(k)
        elif k in ("enter", "right"):
            row = self.selected()
            if row:
                open_row(row)
        elif len(k) == 1 and k.isprintable():
            self.set_filter(self.filter + k)
