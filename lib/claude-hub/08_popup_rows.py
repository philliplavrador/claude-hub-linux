# claude-hub, part 8: Popup's rows: sweeps, the shown rows, the selection and the filter.
# Runs in the entry's namespace (bin/claude-hub) after the parts listed before it
# in PARTS; not importable on its own. A header comment, never a docstring: a leading string would replace the
# entry's __doc__, which main() prints as the usage.
from __future__ import annotations


class PopupRows:
    def ask_sweep(self) -> None:
        self.want_sweep = True
        self.wake.set()

    def sweeper(self) -> None:
        try:
            result = DK.sweep_agents(shared=True)
        except Exception:                                # noqa: BLE001
            result = None
        with self.lock:
            if result is not None:
                self.sweep, self.fresh = result, True
            self.sweeping = False
        self.wake.set()

    def look(self) -> None:
        data = service_rows()
        if data is not None:
            rows = data["rows"]
            cur = DK.current_desktop()
            current = cur if cur is not None else data.get("current")
            mark_current(rows, current)
            kwin, unread = bool(data.get("kwin", True)), set(data.get("unread") or [])
            panel_id = data.get("panel_id")
            panel_number = data.get("panel_at")
            if not panel_id or not panel_number:
                panel_id, panel_number = None, 0
            aux_id, aux_number = aux_pair(data)
        else:
            t0 = time.monotonic()
            with self.lock:
                due = not self.sweeping and (self.want_sweep or t0 - self.swept >= SWEEP)
                if due:
                    self.want_sweep, self.sweeping, self.swept = False, True, t0
                fresh, self.fresh, sweep = self.fresh, False, self.sweep
            if due:
                threading.Thread(target=self.sweeper, daemon=True).start()
            snap = DK.gather(sweep=sweep if sweep is not None else {})
            notes = DK.sync(snap) if fresh and snap.desktops else []
            if notes:
                snap.registry, snap.desktops = DK.read_registry(), DK.live_desktops()
            try:
                DK.panel_refresh(snap.desktops)
            except Exception:                            # noqa: BLE001, the numbers below come from the list itself
                pass
            panel = DK.panel_of(snap.desktops)
            aux = DK.aux_of(snap.desktops)
            rows = DK.rows(snap, panel, aux)
            panel_id = panel["id"] if panel else None
            panel_number = panel["number"] if panel else 0
            aux_id = aux["id"] if aux else None
            aux_number = aux["number"] if aux else 0
            current = snap.current
            kwin, unread = snap.desktops is not None, set(snap.unread)
        add_notes(rows)
        add_live(rows)
        add_paused(rows)
        pick = autoswap_pick()
        add_panel(rows, panel_number, current, panel_id)
        add_aux(rows, aux_number, current, aux_id)
        with self.lock:
            add_sticky(rows, DK.sticky_screens(data) if DK.screens_known(data) else DK.marked_screens())
            self.service = data
            self.rows, self.kwin, self.unread, self.dirty = rows, kwin, unread, True
            self.pick = pick
            if self.message.startswith(REFRESH_FAILED):
                self.message = ""

    def refresher(self) -> None:
        while True:
            self.wake.wait(REFRESH)
            self.wake.clear()
            if not self.visible:
                continue
            try:
                self.look()
            except Exception as e:                       # noqa: BLE001 — the list must keep refreshing
                with self.lock:
                    self.message, self.dirty = f"{REFRESH_FAILED}{e}{OFF}", True

    def screen_rows(self) -> list[dict]:
        with self.lock:
            data = self.service
        return [{**s, "type": "screen", "key": f"screen:{s['name']}"} for s in DK.sticky_screens(data)]

    def shown_rows(self) -> list[dict]:
        with self.lock:
            mode, pattern = self.mode, self.filter
        if mode == "skill-info":
            return []
        if mode == "screens":
            rows = self.screen_rows()
        elif mode == "model":
            rows = list(MODEL_ROWS)
        elif mode == "effort":
            rows = list(EFFORT_ROWS)
        elif mode == "skills":
            rows = hub_skills()[0]
        elif mode == "session":
            skills = hub_skills()[0]
            with self.lock:
                rows = ([{**NEW_ROW, "pick": self.pick}]
                        + [{**DESKTOP_ROW, "said": self.new_desktop_said}]
                        + [dict(USAGE_ROW),
                           {**SKILLS_ROW, "count": len(skills), "names": [s["name"] for s in skills]}])
        else:
            with self.lock:
                rows = [{**SESSION_ROW, "pick": self.pick}] + self.with_closes(self.live_rows())
        rows = numbered(mode, rows)
        return [r for r in rows if matches(pattern, haystack(r))] if pattern else rows

    def selected(self) -> dict | None:
        if self.pinned is not None:
            return self.pinned
        rows = self.shown_rows()
        self.keep_a_session_row([r.get("key") for r in rows])
        return next((r for r in rows if r.get("key") == self.sel_key), None)

    def keep_a_session_row(self, keys: list) -> None:
        if (self.mode in ("session", "screens", "model", "effort", "skills")
                and keys and self.sel_key not in keys and not self.number):
            self.sel_key = keys[0]

    def live_rows(self) -> list[dict]:
        ending = {k for k, v in self.busy.items() if v.startswith("ending")}
        return [r for r in self.rows if not (r.get("desktop_id") and r.get("desktop_id") in ending)]

    def with_closes(self, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            did = r.get("desktop_id")
            if r.get("yours") and did and main_number(r) >= 2:
                c = close_state(str(did), self.closes.get(str(did)))
                if c:
                    r = {**r, "closing": c}
            out.append(r)
        return out

    def current_key(self) -> str | None:
        rows = self.live_rows()
        return next((r.get("key") for r in rows if r.get("current")), None) or first_main_key(rows)

    def select_current(self) -> None:
        with self.lock:
            self.sel_key = self.current_key()

    def select_current_if_listing(self, only_if_gone: bool = False) -> None:
        with self.lock:
            if self.mode != "list":
                return
            if only_if_gone and any(r.get("key") == self.sel_key for r in self.live_rows()):
                return
            self.sel_key = self.current_key()

    def set_filter(self, text: str) -> None:
        self.filter = text
        if not text and self.mode == "list":
            self.select_current()
            return
        rows = self.shown_rows()
        typed = text.strip().lower()
        self.sel_key = rows[0].get("key") if rows else None

    def move(self, how: str) -> None:
        keys = [r.get("key") for r in self.shown_rows()]
        if not keys:
            return
        if self.sel_key not in keys:
            self.sel_key = keys[-1] if how == "end" else keys[0]
            return
        i = keys.index(self.sel_key)
        i = {"up": i - 1, "down": i + 1, "home": 0, "end": len(keys) - 1,
             "pgup": i - PAGE, "pgdn": i + PAGE}[how]
        self.sel_key = keys[max(0, min(i, len(keys) - 1))]
