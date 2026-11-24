const SERVICE = "org.claudehub.AgentScreen";

function isShown(w) {
    if (w.onAllDesktops) return true;
    const cur = workspace.currentDesktop;
    if (!cur) return true;
    const ds = w.desktops || [];
    for (let i = 0; i < ds.length; i++) {
        if (String(ds[i].id) === String(cur.id)) return true;
    }
    return false;
}

function consider(w) {
    if (!w || !(w.normalWindow || w.dialog) || w.specialWindow) return;
    const pid = w.pid;
    if (!pid || pid <= 1) return;
    const before = workspace.activeWindow;
    callDBus(SERVICE, "/AgentScreen", SERVICE, "Verdict", pid,
             String(w.resourceClass || ""), String(w.caption || ""),
             function (v) {
                 if (v !== "tuck" || w.deleted) return;
                 if (!isShown(w)) return;
                 const hadFocus = workspace.activeWindow === w;
                 w.minimized = true;
                 if (hadFocus && before && before !== w && !before.deleted && !before.minimized && isShown(before))
                     workspace.activeWindow = before;
             });
}

workspace.windowAdded.connect(consider);
