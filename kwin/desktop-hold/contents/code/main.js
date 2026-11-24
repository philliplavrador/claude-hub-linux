
var DesktopHold = (function () {
    var recent = [];          // the windows he could see when they had the focus, the latest last
    var RECENT = 12;

    function str(v) { return String(v === undefined || v === null ? "" : v); }

    function isShown(w) {
        if (w.onAllDesktops) { return true; }
        var ds = w.desktops || [], cur = workspace.currentDesktop;
        for (var i = 0; i < ds.length; i++) { if (cur && str(ds[i].id) === str(cur.id)) { return true; } }
        return false;
    }

    function lastShown(but) {
        for (var i = recent.length - 1; i >= 0; i--) {
            var r = recent[i];
            if (r !== but && !r.deleted && !r.minimized && isShown(r)) { return r; }
        }
        return null;
    }

    function activated(w) {
        if (!w || w.deleted) { return; }
        if (isShown(w)) {
            var i = recent.indexOf(w);
            if (i !== -1) { recent.splice(i, 1); }
            recent.push(w);
            if (recent.length > RECENT) { recent.shift(); }
            return;
        }
        var back = lastShown(w);
        if (back) { workspace.activeWindow = back; }
        print("claude-desks-hold: " + str(w.caption).slice(0, 80) + " asked for the focus from another desktop; "
              + (back ? "it went back to " + str(back.caption).slice(0, 80) : "no window here to give it to"));
    }

    return { activated: activated, reset: function () { recent = []; } };
})();

workspace.windowActivated.connect(DesktopHold.activated);
if (workspace.activeWindow) { DesktopHold.activated(workspace.activeWindow); }
