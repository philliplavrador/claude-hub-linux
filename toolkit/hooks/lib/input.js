'use strict';
const fs = require('node:fs');

function pick(obj, names) {
  for (const n of names) if (obj && obj[n] !== undefined && obj[n] !== null) return obj[n];
  return null;
}

const STDIN_DEADLINE_MS = 2000;
const STDIN_POLL_MS = 5;

function readStdin({ fd = 0, deadlineMs = STDIN_DEADLINE_MS, pollMs = STDIN_POLL_MS } = {}) {
  const chunks = [];
  const buf = Buffer.allocUnsafe(65536);
  const stop = Date.now() + deadlineMs;
  let nap = null;
  for (;;) {
    let n;
    try {
      n = fs.readSync(fd, buf, 0, buf.length, null);
    } catch (e) {
      if (e && e.code === 'EAGAIN' && Date.now() < stop) {
        if (nap === null) nap = new Int32Array(new SharedArrayBuffer(4));
        Atomics.wait(nap, 0, 0, pollMs);
        continue;
      }
      break; // EOF on some platforms, a closed stdin, the deadline: what has arrived is all there is
    }
    if (n === 0) break;
    chunks.push(Buffer.from(buf.subarray(0, n)));
  }
  return Buffer.concat(chunks).toString('utf8');
}

function readHookInput(text) {
  if (text === undefined) text = readStdin();
  let raw = null;
  try { raw = text && text.trim() ? JSON.parse(text) : null; } catch { raw = null; }
  const cwd = pick(raw, ['cwd', 'workingDirectory', 'working_directory', 'project_dir']) || process.cwd();
  return {
    event: pick(raw, ['hook_event_name', 'event', 'hookEventName', 'event_name']),
    tool: pick(raw, ['tool_name', 'toolName', 'tool']),
    input: pick(raw, ['tool_input', 'toolInput', 'input']) || {},
    cwd: String(cwd).replace(/\\/g, '/'),
    sessionId: pick(raw, ['session_id', 'sessionId', 'thread_id']),
    raw,
  };
}

module.exports = { readHookInput, readStdin };
