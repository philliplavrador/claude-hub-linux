#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { readHookInput } = require('../lib/input');

const ACCOUNTS = process.env.CLAUDE_HUB_ACCOUNTS || require('path').join(require('os').homedir(), '.claude-accounts');
const ID = String.raw`[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?`;
const RESUME = new RegExp(String.raw`(?:^|[\s;|&(/'"])claude\b[^\n;|&]*?(?:--resume(?:=|\s+)|-r\s*)['"]?(${ID})\b([^\n;|&]*)`, 'gi');

function procStart(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    return stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19] || null;
  } catch { return null; }
}

function liveSessions(root = ACCOUNTS) {
  const out = [];
  let names = [];
  try { names = fs.readdirSync(root); } catch { return out; }
  for (const account of names) {
    const dir = path.join(root, account, 'sessions');
    let files = [];
    try { files = fs.readdirSync(dir).filter((f) => f.endsWith('.json')); } catch { continue; }
    for (const f of files) {
      let s;
      try { s = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); } catch { continue; }
      const pid = Number(s && s.pid);
      if (!Number.isInteger(pid) || pid <= 0 || typeof s.sessionId !== 'string') continue;
      const started = procStart(pid);
      if (started === null || (s.procStart && String(s.procStart) !== started)) continue;
      out.push({ account, pid, sessionId: s.sessionId.toLowerCase() });
    }
    let workers = {};
    try { workers = JSON.parse(fs.readFileSync(path.join(root, account, 'daemon', 'roster.json'), 'utf8')).workers || {}; } catch { continue; }
    for (const w of Object.values(workers)) {
      const launch = w && w.dispatch && w.dispatch.launch;
      if (!launch || launch.mode !== 'resume' || typeof launch.sessionId !== 'string' || typeof w.sessionId !== 'string') continue;
      const runs = out.find((x) => x.account === account && x.sessionId === w.sessionId.toLowerCase());
      if (runs) out.push({ account, pid: runs.pid, sessionId: launch.sessionId.toLowerCase() });
    }
  }
  return out;
}

function verdict(command, live = null) {
  const text = String(command || '').replace(/\\\r?\n/g, ' ');   // a line continued with \ is one command
  RESUME.lastIndex = 0;
  let m;
  while ((m = RESUME.exec(text)) !== null) {
    if (/--fork-session\b/.test(m[0])) continue;
    const id = m[1].toLowerCase();
    const sessions = live || liveSessions();
    const hit = sessions.find((s) => s.sessionId === id || (id.length === 8 && s.sessionId.startsWith(id)));
    if (hit) {
      return `Refused: conversation ${hit.sessionId.slice(0, 8)} already runs on the ${hit.account} account (pid ${hit.pid}). `
        + 'Resuming it again makes a second live copy of one Claude. '
        + 'Reach the running one where it is instead (ListAgents, then SendMessage to it), and to move it to another '
        + 'account use `claude-move <session id> <account>`.';
    }
  }
  return null;
}

function main() {
  try {
    const input = readHookInput();
    const command = input.input && (input.input.command || input.input.cmd);
    const reason = command ? verdict(Array.isArray(command) ? command.join(' ') : command) : null;
    if (reason) {
      process.stdout.write(JSON.stringify({
        hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason },
      }));
    }
  } catch { /* fail open */ }
  process.exit(0);
}

if (require.main === module) main();
module.exports = { verdict, liveSessions };
