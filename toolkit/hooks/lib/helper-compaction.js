'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const TAIL_BYTES = 512 * 1024;
const MANUAL_WINDOW_MS = 30 * 60 * 1000;
const SAFE_SESSION = /^[A-Za-z0-9._-]{1,128}$/;
const EVENTS = new Set(['SessionStart', 'PreCompact', 'PostCompact']);

const toMs = (now) => (now instanceof Date ? now.getTime() : Number(now));

function markerDir(env = process.env) {
  const xdg = env.XDG_STATE_HOME;
  const state = typeof xdg === 'string' && xdg ? xdg : path.join(env.HOME || os.homedir(), '.local', 'state');
  return path.join(state, 'claude-hub', 'manual-compact');
}

function markerPath(sessionId, env) {
  if (typeof sessionId !== 'string' || !SAFE_SESSION.test(sessionId) || sessionId === '.' || sessionId === '..') return null;
  return path.join(markerDir(env), sessionId);
}

function writeManualMarker(raw, opts) {
  try {
    const { env = process.env, now = Date.now(), fs: fsx = fs } = opts || {};
    if (!raw || raw.hook_event_name !== 'PreCompact' || raw.trigger !== 'manual') return null;
    const file = markerPath(raw.session_id, env);
    if (!file) return null;
    const dir = path.dirname(file);
    for (const folder of [path.dirname(dir), dir]) {
      fsx.mkdirSync(folder, { recursive: true, mode: 0o700 });
      if (fsx.lstatSync(folder).isSymbolicLink()) return null;
    }
    const temp = `${file}~${process.pid}.${Math.random().toString(36).slice(2, 10)}`;
    let fd = null;
    let created = false;
    try {
      fd = fsx.openSync(temp, 'wx', 0o600);
      created = true;
      const data = Buffer.from(`${new Date(toMs(now)).toISOString()}\n`);
      for (let done = 0; done < data.length;) {
        const n = fsx.writeSync(fd, data, done, data.length - done);
        if (!n) throw new Error('short write');
        done += n;
      }
      const open = fd;
      fd = null; // a close that fails still releases the descriptor: never close it twice
      fsx.closeSync(open);
      fsx.renameSync(temp, file);
      created = false;
    } catch (e) {
      if (fd !== null) { try { fsx.closeSync(fd); } catch { /* ignore */ } }
      if (created) { try { fsx.unlinkSync(temp); } catch { /* ignore */ } }
      throw e;
    }
    return file;
  } catch {
    return null;
  }
}

function recentManualMarker(raw, env, now, fsx) {
  const file = markerPath(raw.session_id, env);
  if (!file) return false;
  let st;
  try {
    st = fsx.statSync(file);
  } catch (e) {
    if (e && (e.code === 'ENOENT' || e.code === 'ENOTDIR')) return false;
    throw e;
  }
  return st.isFile() && toMs(now) - st.mtimeMs < MANUAL_WINDOW_MS;
}

function readTail(file, fsx) {
  let fd = null;
  try {
    fd = fsx.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NONBLOCK || 0));
    const st = fsx.fstatSync(fd);
    const size = st.size;
    if (!st.isFile() || !size) return '';
    const len = Math.min(size, TAIL_BYTES);
    const buf = Buffer.alloc(len);
    let got = 0;
    while (got < len) {
      const n = fsx.readSync(fd, buf, got, len - got, size - len + got);
      if (!n) break;
      got += n;
    }
    return buf.toString('utf8', 0, got);
  } catch {
    return '';
  } finally {
    if (fd !== null) { try { fsx.closeSync(fd); } catch { /* ignore */ } }
  }
}

function mainChatIsIdle(transcriptPath, fsx) {
  if (typeof transcriptPath !== 'string' || !transcriptPath) return false;
  const lines = readTail(transcriptPath, fsx).split('\n');
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const text = lines[i];
    if (!text) continue;
    let entry;
    try { entry = JSON.parse(text); } catch (e) { if (e instanceof SyntaxError) continue; return false; }
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    if (entry.type !== 'user' && entry.type !== 'assistant') continue;
    if (entry.isSidechain === true) continue;
    if (entry.type !== 'assistant') return false;
    const reason = entry.message && entry.message.stop_reason;
    return reason === 'end_turn' || reason === 'tool_use';
  }
  return false;
}

function isHelperCompaction(raw, opts) {
  try {
    const { env = process.env, now = Date.now(), fs: fsx = fs } = opts || {};
    if (!raw || typeof raw !== 'object') return false;
    if (typeof raw.agent_id === 'string' && raw.agent_id) return true;
    const event = raw.hook_event_name;
    if (!EVENTS.has(event)) return false;
    if (event === 'SessionStart' && raw.source !== 'compact') return false;
    if (event === 'SessionStart') {
      if (recentManualMarker(raw, env, now, fsx)) return false;
    } else if (raw.trigger === 'manual') {
      return false;
    }
    return mainChatIsIdle(raw.transcript_path, fsx);
  } catch {
    return false;
  }
}

module.exports = { isHelperCompaction, writeManualMarker, markerDir, markerPath, TAIL_BYTES, MANUAL_WINDOW_MS };
