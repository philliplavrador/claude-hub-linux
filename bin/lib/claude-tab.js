const fs = require('fs');
const os = require('os');
const path = require('path');

function recordsDir(env = process.env) {
  return env.CLAUDE_HUB_WINDOWS_DIR ||
    path.join(env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'claude-hub', 'claude-windows');
}

function insideKonsole(pid = process.pid) {
  for (let p = pid, hops = 0; p > 1 && hops < 64; hops++) {
    let stat;
    try { stat = fs.readFileSync(`/proc/${p}/stat`, 'utf8'); } catch { return false; }
    const close = stat.lastIndexOf(')');
    if (stat.slice(stat.indexOf('(') + 1, close) === 'konsole') return true;
    p = parseInt(stat.slice(close + 2).split(' ')[1], 10);
  }
  return false;
}

function recordOf(dir, id) {
  try {
    for (const name of fs.readdirSync(dir).sort()) {
      if (!name.endsWith('.json')) continue;
      try {
        const rec = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'));
        if (rec && rec.session_id === id) return rec;
      } catch { /* not a record */ }
    }
  } catch { /* no records folder */ }
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, `${id}.json`), 'utf8'));
  } catch { /* no file of that name either */ }
  return null;
}

function konsoleTab(sessionId, env = process.env) {
  const id = String(sessionId || '').replace(/[^a-zA-Z0-9-]/g, '');
  if (id) {
    const rec = recordOf(recordsDir(env), id);
    if (rec && rec.konsole_service && rec.konsole_session) {
      return { svc: rec.konsole_service, ses: rec.konsole_session };
    }
  }
  const svc = env.KONSOLE_DBUS_SERVICE;
  const ses = env.KONSOLE_DBUS_SESSION;
  return svc && ses && insideKonsole() ? { svc, ses } : null;
}

module.exports = { konsoleTab, recordsDir, insideKonsole };
