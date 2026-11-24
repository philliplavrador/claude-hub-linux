
function nameFocused() {
    const w = workspace.activeWindow;
    if (!w || w.deleted || !(w.pid > 0)) return;
    callDBus("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
             "org.freedesktop.systemd1.Manager", "StartUnit",
             "claude-console-name@" + w.pid + ".service", "replace");
}

registerShortcut("ClaudeHubNameConsole", "Claude hub: Type A Name For This Claude Window", "Meta+Shift+N", nameFocused);
