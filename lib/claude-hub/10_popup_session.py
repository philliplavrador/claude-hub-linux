# claude-hub, part 10: Popup's Tools (session) mode, New Claude's model and effort, the usages.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class PopupSession:
    def enter_session(self, select: str = "") -> None:
        with self.lock:
            self.mode, self.filter, self.message, self.confirm = "session", "", "", None
        self.sel_key = select or NEW_ROW["key"]
        self.top, self.dirty = 0, True

    def leave_session(self) -> None:
        with self.lock:
            self.mode, self.filter, self.message = "list", "", ""
        self.sel_key = SESSION_ROW["key"]
        self.top, self.dirty = 0, True

    def on_session_key(self, k: str) -> None:
        if k == "esc":
            if self.filter:
                self.set_filter("")
            else:
                self.leave_session()
        elif k == "backspace":
            self.set_filter(self.filter[:-1])
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.move(k)
        elif k in ("enter", "right"):
            row = self.selected()
            if row and k == "right" and row.get("type") == "new":
                self.new_claude()
            elif row:
                self.act(row)
        elif len(k) == 1 and k.isprintable():
            self.set_filter(self.filter + k)

    def enter_model(self, account: str, auto: bool = False) -> None:
        with self.lock:
            self.mode, self.filter, self.message, self.confirm = "model", "", "", None
            self.new_account, self.new_model = account, ""
            self.new_auto = auto
            self.new_auto_pick = account if self.new_auto else ""
        self.sel_key = f"model:{DEFAULT_MODEL}"
        self.top, self.dirty = 0, True

    def leave_model(self) -> None:
        self.enter_session()

    def pick_model(self, r: dict) -> None:
        model = str(r["model"])
        if getattr(self, "new_auto", False):
            if is_fable_model(model):
                pick = autoswap_pick(model)
                if not pick:
                    self.message = f"{RED}no account has Fable room: pick another model{OFF}"
                    self.dirty = True
                    return
                self.new_account = pick
            else:
                self.new_account = self.new_auto_pick or self.new_account
        with self.lock:
            self.mode, self.filter, self.message = "effort", "", ""
            self.new_model = model
        self.sel_key = f"effort:{DEFAULT_EFFORT}"
        self.top, self.dirty = 0, True

    def leave_effort(self) -> None:
        model = self.new_model
        with self.lock:
            self.mode, self.filter, self.message = "model", "", ""
            self.new_model = ""
        self.sel_key = f"model:{model}"
        self.top, self.dirty = 0, True

    def pick_effort(self, r: dict) -> None:
        self.go(["new", self.new_account, "--model", self.new_model, "--effort", str(r["effort"])])

    def enter_skills(self) -> None:
        with self.lock:
            self.mode, self.filter, self.message, self.confirm = "skills", "", "", None
        rows = self.shown_rows()
        self.sel_key = rows[0].get("key") if rows else None
        self.top, self.dirty = 0, True

    def leave_skills(self) -> None:
        with self.lock:
            self.mode, self.filter, self.message = "session", "", ""
        self.sel_key = SKILLS_ROW["key"]
        self.top, self.dirty = 0, True

    def on_skills_key(self, k: str) -> None:
        if k == "esc":
            if self.filter:
                self.set_filter("")
            else:
                self.leave_skills()
        elif k == "backspace":
            self.set_filter(self.filter[:-1])
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.move(k)
        elif k in ("enter", "right"):
            row = self.skill_in_front()
            if row and k == "enter":
                self.start_skill(row)
            elif row:
                self.enter_skill_info(row)
        elif len(k) == 1 and k.isprintable():
            self.set_filter(self.filter + k)

    def skill_in_front(self) -> dict | None:
        if self.pinned is not None:
            return self.pinned
        key = self.sel_key
        rows = self.shown_rows()
        row = next((r for r in rows if r.get("key") == key), None)
        if row is None and key:
            self.message = f"{RED}this skill is no longer saved{OFF}"
        return row

    def start_skill(self, row: dict) -> None:
        skill = next((s for s in hub_skills()[0] if s["key"] == row.get("key")), None)
        if skill is None:
            self.message = f"{RED}this skill is no longer saved{OFF}"
            return
        pick = autoswap_pick(skill["model"])
        if not pick:
            if is_fable_model(skill["model"]):
                self.message = f"{RED}nothing started: the auto-swap names no account with room for Fable{OFF}"
            else:
                self.message = f"{RED}no account to start on: the auto-swap has no recommendation{OFF}"
            return
        self.go(["new", pick, "--model", skill["model"], "--effort", skill["effort"],
                 "--title", f"{skill['name']} · {pick}", "--message", skill["prompt"]])

    def enter_skill_info(self, row: dict) -> None:
        with self.lock:
            self.mode, self.filter, self.message, self.confirm = "skill-info", "", "", None
            self.skill = dict(row)
        self.info_top, self.dirty = 0, True

    def leave_skill_info(self) -> None:
        key = (self.skill or {}).get("key")
        with self.lock:
            self.mode, self.filter, self.message = "skills", "", ""
        self.sel_key = key
        self.top, self.info_top, self.dirty = 0, 0, True

    def refresh_skill(self) -> None:
        key = (self.skill or {}).get("key")
        now = next((s for s in hub_skills()[0] if s["key"] == key), None)
        if now:
            self.skill = now

    def skill_info_lines(self, cols: int) -> list[str]:
        self.refresh_skill()
        skill = self.skill or {}
        lines = [f"   {DIM}Opens a desktop of its own · {model_label(str(skill.get('model') or ''))} · "
                 f"{effort_label(str(skill.get('effort') or ''))} effort{OFF}", ""]
        for item in skill.get("summary") or []:
            for i, part in enumerate(textwrap.wrap(str(item), width=max(10, cols - 6), break_on_hyphens=False) or [""]):
                lines.append(f"   • {part}" if i == 0 else f"     {part}")
        return [clip_ansi(ln, cols - 1) for ln in lines]

    def skill_info_room(self, rows_n: int) -> int:
        return max(1, rows_n - 2 - 2 - (1 if self.message else 0))

    def scroll_skill_info(self, how: str) -> None:
        rows_n, cols = self.size()
        last = max(0, len(self.skill_info_lines(cols)) - self.skill_info_room(rows_n))
        at = self.info_top
        at = {"up": at - 1, "down": at + 1, "home": 0, "end": last, "pgup": at - PAGE, "pgdn": at + PAGE}[how]
        self.info_top = max(0, min(at, last))

    def on_skill_info_key(self, k: str) -> None:
        if k == "enter":
            self.start_skill(self.skill or {})
        elif k in ("esc", "backspace", "left"):
            self.leave_skill_info()
        elif k in ("up", "down", "home", "end", "pgup", "pgdn"):
            self.scroll_skill_info(k)

    def show_usages(self) -> None:
        self.new_claude(title="Usages · Enter starts a Claude on that account")
