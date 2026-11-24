
var WindowHome = (function () {
    var SERVICE = "org.claudehub.Desktops", PATH = "/Desktops";
    var claims = [];          // the service's list: {id, desktop, pid, cls, marker, until (ms), grace (ms)}
    var version = -1;         // the list's version; -1 = none yet, and the service answers at once
    var mains = [];           // the ids of his desktops the service names (desktop 1 and the extra Main Desktops); [] = none sent
    var asking = 0;           // when the question in flight was asked (ms); 0 = none in flight
    var used = {};            // claim id → true: taken here; never taken twice, whatever the list says
    var watching = {};        // internalId → {w, claim, from, until}: taken by a marker claim, not yet confirmed
    var markers = {};         // desktop id → {marker: until (ms)}: every marker claimed for it lately
    var MARKERS_MS = 60000;   // how long a marker is remembered past its claim
    var recent = [];          // the windows that had the focus, the latest last (at most RECENT)
    var RECENT = 12;
    var now = function () { return Date.now(); };
    var STALE_MS = 30000;     // a question unanswered this long is lost (the service restarted): ask again
    var pending = {};         // internalId → {w, from (desktop ids it opened on), born (ms), asked, moved}: Owner asked, no answer yet
    var OWNER_WAIT_MS = 250;  // an owner's answer later than this after the window opened is ignored: he may be using it
    var missed = {};          // internalId → job: its named answer came too late and it was left where it opened (journaled);
    var hushed = {};          // internalId → {w, opacity, until (ms)}: sent home, invisible until its open animation is over
    var HUSH_MS = 600;        // three times KWin's open animation (200 ms at the default speed)
    var wake = function () {};   // set by the timer below: something is waiting for check()

    function str(v) { return String(v === undefined || v === null ? "" : v); }

    function desktopById(id) {
        var ds = workspace.desktops || [];
        for (var i = 0; i < ds.length; i++) { if (str(ds[i].id) === str(id)) { return ds[i]; } }
        return null;
    }

    function onlyOn(w, d) {
        var ds = w.desktops || [];
        return !w.onAllDesktops && ds.length === 1 && str(ds[0].id) === str(d.id);
    }

    function classOf(w) {
        return [str(w.resourceClass), str(w.resourceName), str(w.desktopFileName)].map(function (s) {
            return s.toLowerCase();
        });
    }

    function answers(claim, w) {
        if (used[claim.id] || !(claim.until > now())) { return false; }
        if (claim.pid > 0) { return w.pid === claim.pid; }
        return !!claim.cls && !!claim.marker && classOf(w).indexOf(str(claim.cls).toLowerCase()) !== -1;
    }

    function isHis(d) {
        var first = (workspace.desktops || [])[0], id = str(d.id);
        if (first && str(first.id) === id) { return true; }
        return mains.indexOf(id) !== -1;
    }

    function take(claim, w) {
        used[claim.id] = true;
        callDBus(SERVICE, PATH, SERVICE, "Taken", str(claim.id), str(w.pid));
    }

    function added(w) {
        if (!w || !w.normalWindow || w.specialWindow || w.onAllDesktops) { return false; }
        for (var i = 0; i < claims.length; i++) {
            var c = claims[i];
            if (!answers(c, w)) { continue; }
            var d = desktopById(c.desktop);
            if (!d || isHis(d)) { continue; }     // never the user's desktop: the first, or one of the Main Desktops `mains` names
            var from = (w.desktops || []).slice();
            take(c, w);
            if (!onlyOn(w, d)) { w.desktops = [d]; }
            if (c.marker && !shows(w, c.desktop)) {
                var job = { w: w, claim: c, from: from, until: now() + (c.grace || 10000), ok: false };
                watching[str(w.internalId)] = job;
                if (w.captionChanged && w.captionChanged.connect) {
                    w.captionChanged.connect(function () { if (shows(w, c.desktop)) { job.ok = true; } });
                }
            }
            if (workspace.activeWindow === w) {
                var back = lastShown(w);
                if (back) { workspace.activeWindow = back; }
            }
            return true;
        }
        return false;
    }

    function ids(list) {
        var out = [], ds = list || [];
        for (var i = 0; i < ds.length; i++) { out.push(str(ds[i].id)); }
        return out;
    }

    function sameIds(a, b) {
        return a.length === b.length && a.slice().sort().join("\n") === b.slice().sort().join("\n");
    }

    function consider(w) {
        try {
            if (!w || w.deleted || w.specialWindow || w.onAllDesktops || w.transient) { return false; }
            if (!w.normalWindow && !w.dialog) { return false; }
            if (!(w.pid > 1)) { return false; }
            var cur = workspace.currentDesktop;
            if (!cur || !onlyOn(w, cur)) { return false; }
            var t = now();
            for (var k in pending) {
                if (pending[k].w.deleted || t - pending[k].born > 4 * OWNER_WAIT_MS) { delete pending[k]; }
            }
            for (var m in missed) {
                if (missed[m].w.deleted || t - missed[m].born > 4 * OWNER_WAIT_MS) { delete missed[m]; }
            }
            var job = pending[str(w.internalId)] = { w: w, from: ids(w.desktops), born: t, asked: false, moved: false };
            if (w.desktopsChanged && w.desktopsChanged.connect) {
                w.desktopsChanged.connect(function () { job.moved = true; });
            }
            owner(job);
            return true;
        } catch (e) { return false; }
    }

    function owner(job) {
        var w = job.w;
        if (job.asked || w.deleted || now() - job.born > OWNER_WAIT_MS) { return; }
        job.asked = true;
        callDBus(SERVICE, PATH, SERVICE, "Owner", w.pid, str(w.resourceClass), str(w.caption), function (answer) {
            homeward(job, answer);
        });
    }

    function waiting(o, job, late) {
        var key = str(o.internalId), p = pending[key], s = p || (late ? missed[key] : null);
        if (!s || s.moved || !sameIds(ids(o.desktops), s.from)) { return false; }
        if (!late) { return !!p && now() - p.born <= OWNER_WAIT_MS; }
        return s.born >= job.born - OWNER_WAIT_MS;
    }

    function apart(job, d, late) {
        var w = job.w;
        var all = workspace.windowList ? workspace.windowList() : [];
        for (var i = 0; i < all.length; i++) {
            var o = all[i];
            if (str(o.internalId) === str(w.internalId) || o.deleted || o.pid !== w.pid || o.onAllDesktops) { continue; }
            if (!o.normalWindow && !o.dialog) { continue; }
            if (onlyOn(o, d)) { continue; }
            if (waiting(o, job, late)) { continue; }
            return true;
        }
        return false;
    }

    function hush(w) {
        var key = str(w.internalId);
        if (hushed[key]) { return; }
        hushed[key] = { w: w, opacity: w.opacity > 0 ? w.opacity : 1, until: now() + HUSH_MS };
        w.opacity = 0;
        wake();
    }

    function recover() {
        var all = [], n = 0;
        try { all = workspace.windowList ? workspace.windowList() : []; } catch (e) { return 0; }
        for (var i = 0; i < all.length; i++) {
            try {
                var w = all[i];
                if (!w || w.deleted || (!w.normalWindow && !w.dialog) || hushed[str(w.internalId)]) { continue; }
                if (w.opacity === 0) { w.opacity = 1; n++; }
            } catch (e) { }
        }
        return n;
    }

    function homeward(job, answer) {
        var w = job.w;
        delete pending[str(w.internalId)];
        try {
            var a;
            try { a = JSON.parse(str(answer)); } catch (e) { return false; }
            if (!a || typeof a.desktop !== "string" || !a.desktop) { return false; }
            if (w.deleted || w.onAllDesktops) { return false; }
            var d = desktopById(a.desktop);
            if (!d || isHis(d)) { return false; }     // never the user's desktop: the first, or one of the Main Desktops `mains` names
            if (job.moved || !sameIds(ids(w.desktops), job.from) || onlyOn(w, d)) { return false; }
            var took = now() - job.born, late = took > OWNER_WAIT_MS;
            if (apart(job, d, late)) { return false; }
            if (late) {
                missed[str(w.internalId)] = job;
                callDBus(SERVICE, PATH, SERVICE, "Sent", JSON.stringify({
                    left: "late", pid: w.pid, cls: str(w.resourceClass), caption: str(w.caption).slice(0, 60),
                    desktop: str(d.id), session: str(a.session), how: str(a.how), ms: took }));
                return false;
            }
            hush(w);
            w.desktops = [d];
            if (workspace.activeWindow === w) {
                var back = lastShown(w);
                if (back) { workspace.activeWindow = back; }
            }
            callDBus(SERVICE, PATH, SERVICE, "Sent", JSON.stringify({
                pid: w.pid, cls: str(w.resourceClass), caption: str(w.caption).slice(0, 60), desktop: str(d.id),
                session: str(a.session), how: str(a.how), ms: now() - job.born }));
            return true;
        } catch (e) { return false; }
    }

    function isShown(w) {
        if (w.onAllDesktops) { return true; }
        var ds = w.desktops || [], cur = workspace.currentDesktop;
        for (var i = 0; i < ds.length; i++) { if (cur && str(ds[i].id) === str(cur.id)) { return true; } }
        return false;
    }

    function shows(w, desktopId) {
        var m = markers[str(desktopId)] || {}, cap = str(w.caption), t = now();
        for (var k in m) { if (m[k] > t && cap.indexOf(k) !== -1) { return true; } }
        return false;
    }

    function check() {
        var left = 0;
        for (var h in hushed) {
            try {
                var hushJob = hushed[h], hw = hushJob.w;
                if (hw.deleted) { delete hushed[h]; continue; }
                if (now() >= hushJob.until) {
                    if (hw.opacity === 0) { hw.opacity = hushJob.opacity; }     // unless something else set it since
                    delete hushed[h];
                    continue;
                }
                left++;
            } catch (e) { delete hushed[h]; }
        }
        for (var k in watching) {
            try {
                var job = watching[k], w = job.w;
                if (w.deleted) { delete watching[k]; continue; }
                if (job.ok || shows(w, job.claim.desktop)) { delete watching[k]; continue; }
                if (now() >= job.until) {
                    var d = desktopById(job.claim.desktop);
                    if (d && onlyOn(w, d) && job.from.length) {
                        w.desktops = job.from;
                        callDBus(SERVICE, PATH, SERVICE, "Report", "window-home-returned",
                                 JSON.stringify({ claim: str(job.claim.id), pid: w.pid, caption: str(w.caption) }));
                    }
                    delete watching[k];
                    continue;
                }
                left++;
            } catch (e) { delete watching[k]; }
        }
        return left;
    }

    function activated(w) {
        if (!w) { return; }
        var i = recent.indexOf(w);
        if (i !== -1) { recent.splice(i, 1); }
        recent.push(w);
        if (recent.length > RECENT) { recent.shift(); }
    }

    function lastShown(but) {
        for (var i = recent.length - 1; i >= 0; i--) {
            var r = recent[i];
            if (r !== but && !r.deleted && !r.minimized && r.normalWindow && isShown(r)) { return r; }
        }
        return null;
    }

    function received(answer) {
        asking = 0;
        var a;
        try { a = JSON.parse(str(answer)); } catch (e) { return false; }
        if (!a || typeof a.version !== "number" || !Array.isArray(a.claims)) { return false; }
        version = a.version;
        claims = a.claims;
        mains = [];
        if (Array.isArray(a.mains)) {
            for (var j = 0; j < a.mains.length; j++) {
                if (typeof a.mains[j] === "string" && a.mains[j]) { mains.push(a.mains[j]); }
            }
        }
        var live = {}, t = now();
        for (var i = 0; i < claims.length; i++) {
            var c = claims[i];
            live[c.id] = true;
            if (c.marker) {
                var m = markers[str(c.desktop)] = markers[str(c.desktop)] || {};
                m[str(c.marker)] = Math.max(m[str(c.marker)] || 0, (c.until || t) + MARKERS_MS);
            }
        }
        for (var dk in markers) { for (var mk in markers[dk]) { if (markers[dk][mk] <= t) { delete markers[dk][mk]; } } }
        for (var id in used) { if (!live[id]) { delete used[id]; } }
        return true;
    }

    function ask() {
        if (asking && now() - asking < STALE_MS) { return; }
        asking = now();
        var mine = asking;
        callDBus(SERVICE, PATH, SERVICE, "Claims", String(version), function (answer) {
            if (asking !== mine) { return; }      // an older, abandoned question
            received(answer);
            ask();
        });
    }

    return {
        added: added, consider: consider, check: check, activated: activated, received: received, ask: ask,
        recover: recover,
        watching: function () { return watching; },
        pending: function () { return pending; },
        missed: function () { return missed; },
        hushed: function () { return hushed; },
        claims: function () { return claims; },
        version: function () { return version; },
        mains: function () { return mains; },
        clock: function (f) { now = f; },
        setWake: function (f) { wake = f; },
        reset: function () {
            for (var h in hushed) {
                try { if (hushed[h].w.opacity === 0) { hushed[h].w.opacity = hushed[h].opacity; } } catch (e) { }
            }
            claims = []; mains = []; version = -1; asking = 0; used = {}; watching = {}; recent = []; markers = {}; pending = {};
            missed = {}; hushed = {};
        }
    };
})();

(function () {
    var checks = new QTimer();
    checks.singleShot = false;
    checks.interval = 250;
    checks.timeout.connect(function () { if (!WindowHome.check()) { checks.stop(); } });
    WindowHome.setWake(function () { if (!checks.active) { checks.start(); } });

    var keepalive = new QTimer();
    keepalive.singleShot = false;
    keepalive.interval = 5000;
    keepalive.timeout.connect(WindowHome.ask);
    keepalive.start();

    workspace.windowActivated.connect(WindowHome.activated);
    workspace.windowAdded.connect(function (w) {
        if (WindowHome.added(w)) {
            if (Object.keys(WindowHome.watching()).length) { checks.start(); }
        } else {
            WindowHome.consider(w);     // no claim covered it: ask whose it is
        }
    });
    if (workspace.activeWindow) { WindowHome.activated(workspace.activeWindow); }
    try {
        var shown = WindowHome.recover();
        if (shown > 0 && typeof print === "function") {
            print("claude-window-home: gave opacity back to " + shown + " window(s) a previous copy left invisible");
        }
    } catch (e) { }
    WindowHome.ask();
})();
