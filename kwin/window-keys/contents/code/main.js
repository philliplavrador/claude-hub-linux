
function closeActive() {
    const w = workspace.activeWindow;
    if (!w || w.deleted) return;
    if (w.specialWindow || w.desktopWindow || w.dock) return;
    if (!w.closeable) return;
    w.closeWindow();
}

registerShortcut("ClaudeHubCloseWindow", "Claude hub: Close Window", "Meta+Ctrl+Shift+Q", closeActive);
