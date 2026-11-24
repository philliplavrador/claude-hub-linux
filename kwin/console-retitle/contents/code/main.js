
function retitleFocused() {
    const w = workspace.activeWindow;
    if (!w || w.deleted || !(w.pid > 0)) return;
    callDBus("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
             "org.freedesktop.systemd1.Manager", "StartUnit",
             "claude-console-retitle@" + w.pid + ".service", "replace");
}

registerShortcut("ClaudeHubRetitleConsole", "Claude hub: Rename This Claude Window", "Meta+N", retitleFocused);
