#!/usr/bin/env node
/**
 * bg.js: start long-lived commands for a Claude Code session without opening a terminal window, keep a record of
 * each one, and clean them up.
 *
 * Why:
 *   - A detached child with no console can end up with a brand-new visible terminal window, and closing that window
 *     kills the server inside it. Sessions that run several of these pile windows up.
 *   - Here a detached supervisor starts the command with no window, so the whole tree shares a console that has no
 *     window. There is nothing to see and nothing to close.
 *   - Each session's own processes may sit in a kill-on-close job that allows silent breakaway. Anything started
 *     through here outlives the session unless it is stopped, which is why every start is recorded and swept.
 *
 * Records live in ~/.claude/bg/ (one set of small JSON files per process), logs in ~/.claude/bg/logs/. Run
 * `node <toolkit-store>/hooks/bg/bg.js help` for usage.
 *
 * Lives in the Toolkit store; ~/.claude/tools/bg.js is a forwarding shim, so both paths work and both read the same
 * records.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const HOME = process.env.BG_HOME || path.join(os.homedir(), '.claude', 'bg');
const LOG_DIR = path.join(HOME, 'logs');
const WIN = process.platform === 'win32';
const SELF = __filename;
const CONTROL_C_EXIT = 3221225786;
const KEEP_ENDED_DAYS = 7;

const REASONS = {
  'window-closed': 'its window or tab was closed (the console sent a close event) — the user closed it',
  'ctrl-c': 'Ctrl+C or Ctrl+Break was pressed in its window — the user did it',
  'window-gone': 'its window is gone and nothing else recorded why — almost certainly the user closed it',
  'console-event': 'a console signal reached its hidden console — not the user, he cannot see that console',
  stopped: 'stopped through bg.js stop',
  exited: 'the command exited by itself',
  'failed-to-start': 'the command could not be started',
  'killed-outside': 'killed from outside with no record (Task Manager, taskkill, a reboot or a log-off)',
};

const HELP = `bg.js — windowless background processes for Claude Code sessions

  start --name <name> [--keep] [--visible] [--cwd <dir>] [--log <file>] [--env K=V]... -- <command...>
        Starts the command with NO window and returns at once. After --, either one
        quoted command line ("npx vite --port 6810") or the program and its arguments.
        --keep     outlive this Claude session (default: stopped when the session ends)
        --visible  open a real terminal window instead (only when a human must see or
                   type into it; the user closes these windows whenever they pile up)
  list [--all] [--mine]     running processes, plus ones that ended in the last 24h
  status <name|id>          one process in detail, with the last 20 log lines
  logs <name|id> [--lines N]
  wait <name|id> [--timeout <seconds>]   block until it ends, then print why
  stop <name|id>... | --mine [--include-keep]
  sweep                     stop session-lifetime processes whose Claude session is gone
  hook                      SessionStart / SessionEnd hook entry point (reads stdin)

Why a process ended ("reason"):
${Object.entries(REASONS).map(([k, v]) => `  ${k.padEnd(16)} ${v}`).join('\n')}
`;


function die(msg, code = 1) {
  process.stderr.write(`bg.js: ${msg}\n`);
  process.exit(code);
}

const stamp = () => new Date().toISOString();
const rec = (id, kind) => path.join(HOME, `${id}.${kind}.json`);

function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}

function writeJson(p, obj) {
  const tmp = `${p}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
  fs.renameSync(tmp, p);
}

function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function slug(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'proc';
}

function commandLine(parts) {
  if (parts.length === 1) return parts[0];
  return parts.map((a) => (/^[\w\-.:\\/=@,+%]+$/.test(a) ? a : `"${a.replace(/"/g, '""')}"`)).join(' ');
}

function processTable() {
  const table = new Map();
  if (WIN) {
    const r = spawnSync('tasklist', ['/FO', 'CSV', '/NH'], { encoding: 'utf8', windowsHide: true });
    for (const line of String(r.stdout || '').split(/\r?\n/)) {
      const m = /^"([^"]+)","(\d+)"/.exec(line);
      if (m) table.set(Number(m[2]), m[1].toLowerCase());
    }
  } else {
    const r = spawnSync('ps', ['-eo', 'pid=,comm='], { encoding: 'utf8' });
    for (const line of String(r.stdout || '').split('\n')) {
      const m = /^\s*(\d+)\s+(.+)$/.exec(line);
      if (m) table.set(Number(m[1]), path.basename(m[2]).toLowerCase());
    }
  }
  return table;
}

const isNode = (procs, pid) => Boolean(pid) && /^node(\.exe)?$/.test(procs.get(pid) || '');
const isClaude = (procs, pid) => Boolean(pid) && /^(claude|node)(\.exe)?$/.test(procs.get(pid) || '');

function killTree(pid) {
  if (!pid) return;
  if (WIN) {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    return;
  }
  try { process.kill(-pid, 'SIGTERM'); } catch { try { process.kill(pid, 'SIGTERM'); } catch { /* gone */ } }
}

function owner() {
  return {
    claudePid: Number(process.env.CLAUDE_PID) || null,
    sessionId: process.env.CLAUDE_CODE_SESSION_ID || null,
    cwd: process.cwd(),
  };
}


function allEntries() {
  let names = [];
  try { names = fs.readdirSync(HOME); } catch { return []; }
  return names
    .filter((n) => n.endsWith('.entry.json'))
    .map((n) => readJson(path.join(HOME, n)))
    .filter(Boolean)
    .sort((a, b) => Date.parse(a.startedAt) - Date.parse(b.startedAt));
}

function describe(entry, procs) {
  const { id } = entry;
  const pids = readJson(rec(id, 'pids'));
  const exit = readJson(rec(id, 'exit'));
  const base = { entry, pids };
  if (exit) return { ...base, state: 'ended', exit };
  if (pids && isNode(procs, pids.supervisorPid)) return { ...base, state: 'running' };
  if (!pids && Date.now() - Date.parse(entry.startedAt) < 15000) return { ...base, state: 'starting' };
  const stop = readJson(rec(id, 'stop'));
  const inferred = {
    reason: stop ? 'stopped' : entry.visible ? 'window-gone' : 'killed-outside',
    ...(stop ? { by: stop.by } : {}),
    inferred: true,
    at: stamp(),
  };
  writeJson(rec(id, 'exit'), inferred);
  return { ...base, state: 'ended', exit: inferred };
}

function find(key, procs) {
  const entries = allEntries();
  const exact = entries.filter((e) => e.id === key);
  const named = entries.filter((e) => e.name === key);
  const pool = exact.length ? exact : named;
  if (!pool.length) die(`no process named or with id "${key}" — see: node ${SELF} list --all`);
  const described = pool.map((e) => describe(e, procs));
  return described.find((d) => d.state !== 'ended') || described[described.length - 1];
}

function reasonText(exit) {
  if (!exit) return '';
  const code = exit.code != null ? `, exit code ${exit.code}` : '';
  const by = exit.by ? ` (by: ${exit.by})` : '';
  return `${exit.reason}${code}${by} — ${REASONS[exit.reason] || ''}`;
}

function mine(entry) {
  const me = owner();
  return (me.sessionId && entry.owner.sessionId === me.sessionId) || (me.claudePid && entry.owner.claudePid === me.claudePid);
}

function stopOne(d, by) {
  if (d.state === 'ended') return false;
  const { id } = d.entry;
  writeJson(rec(id, 'stop'), { by, at: stamp() });
  let pids = d.pids;
  for (let i = 0; !pids && i < 50; i++) { sleep(100); pids = readJson(rec(id, 'pids')); }
  if (pids) killTree(pids.supervisorPid);
  if (!readJson(rec(id, 'exit'))) writeJson(rec(id, 'exit'), { reason: 'stopped', by, at: stamp() });
  return true;
}

function removeRecords(entry) {
  for (const kind of ['entry', 'pids', 'exit', 'stop']) {
    try { fs.unlinkSync(rec(entry.id, kind)); } catch { /* not there */ }
  }
  if (entry.log && path.dirname(entry.log) === LOG_DIR) {
    try { fs.unlinkSync(entry.log); } catch { /* not there */ }
  }
}

function tail(file, lines) {
  try {
    const text = fs.readFileSync(file, 'utf8');
    return text.split(/\r?\n/).slice(-lines - 1).join('\n');
  } catch {
    return '(no log)';
  }
}


function parseStart(argv) {
  const dash = argv.indexOf('--');
  if (dash === -1) die('start needs the command after --, e.g.  bg.js start --name api -- npm run dev');
  const o = { env: {}, lifetime: 'session', visible: false, command: argv.slice(dash + 1) };
  if (!o.command.length) die('nothing to run after --');
  const flags = argv.slice(0, dash);
  for (let i = 0; i < flags.length; i++) {
    const f = flags[i];
    const val = () => {
      const v = flags[++i];
      if (v === undefined) die(`${f} needs a value`);
      return v;
    };
    if (f === '--name') o.name = val();
    else if (f === '--cwd') o.cwd = path.resolve(val());
    else if (f === '--log') o.log = path.resolve(val());
    else if (f === '--env') {
      const kv = val();
      const eq = kv.indexOf('=');
      if (eq < 1) die(`--env wants KEY=VALUE, got "${kv}"`);
      o.env[kv.slice(0, eq)] = kv.slice(eq + 1);
    } else if (f === '--keep') o.lifetime = 'keep';
    else if (f === '--visible') o.visible = true;
    else die(`unknown flag ${f} (see: node ${SELF} help)`);
  }
  if (!o.name) die('start needs --name <short-name>');
  return o;
}

function cmdStart(argv) {
  const o = parseStart(argv);
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const clash = allEntries()
    .filter((e) => e.name === o.name)
    .map((e) => describe(e, processTable()))
    .find((d) => d.state !== 'ended');
  if (clash) die(`"${o.name}" is already ${clash.state} (id ${clash.entry.id}). Stop it first:  node ${SELF} stop ${o.name}`);

  const me = owner();
  const id = `${slug(o.name)}-${Date.now().toString(36)}`;
  const entry = {
    id,
    name: o.name,
    command: o.command,
    cwd: o.cwd || process.cwd(),
    log: o.visible ? null : o.log || path.join(LOG_DIR, `${id}.log`),
    envKeys: Object.keys(o.env),
    lifetime: me.claudePid || me.sessionId ? o.lifetime : 'keep',
    visible: o.visible,
    owner: me,
    startedAt: stamp(),
  };
  writeJson(rec(id, 'entry'), entry);

  const env = { ...process.env, ...o.env, BG_ID: id };
  if (o.visible && WIN) {
    const line = `start "bg ${o.name.replace(/"/g, '')}" "${process.execPath}" "${SELF}" _supervise ${id}`;
    spawn('cmd.exe', ['/d', '/c', line], {
      cwd: entry.cwd, env, detached: true, stdio: 'ignore', windowsHide: true, windowsVerbatimArguments: true,
    }).unref();
  } else {
    spawn(process.execPath, [SELF, '_supervise', id], {
      cwd: entry.cwd, env, detached: true, stdio: 'ignore', windowsHide: true,
    }).unref();
  }

  for (let i = 0; i < 50; i++) {
    const pids = readJson(rec(id, 'pids'));
    if ((pids && pids.childPid) || readJson(rec(id, 'exit'))) break;
    sleep(100);
  }
  const d = describe(entry, processTable());
  if (d.state === 'ended') {
    process.stdout.write(`"${o.name}" ended at once: ${reasonText(d.exit)}\n`);
    if (entry.log) process.stdout.write(`${tail(entry.log, 20)}\n`);
    process.exit(1);
  }
  const how = o.visible ? 'in a VISIBLE window (the user may close it at any time)' : 'with no window';
  process.stdout.write([
    `started "${o.name}" ${how} — id ${id}, lifetime ${entry.lifetime}`,
    d.pids ? `  supervisor pid ${d.pids.supervisorPid}, command pid ${d.pids.childPid}` : '  (still starting)',
    entry.log ? `  log:  ${entry.log}` : '  output: in its window only',
    `  stop: node ${SELF} stop ${o.name}`,
    '',
  ].join('\n'));
}

function cmdSupervise(id) {
  const entry = readJson(rec(id, 'entry'));
  if (!entry) process.exit(1);
  let logFd = null;
  if (entry.log) {
    fs.mkdirSync(path.dirname(entry.log), { recursive: true });
    logFd = fs.openSync(entry.log, 'a');
  }
  const note = (msg) => {
    const line = `[bg ${stamp()}] ${msg}\n`;
    if (logFd !== null) fs.writeSync(logFd, line);
    else process.stdout.write(line);
  };
  let done = false;
  const finish = (exit) => {
    if (done) return;
    done = true;
    writeJson(rec(id, 'exit'), { ...exit, at: stamp() });
    note(`ended: ${reasonText(exit)}`);
  };

  const line = commandLine(entry.command);
  note(`starting in ${entry.cwd}: ${line}`);
  let child;
  try {
    child = spawn(line, [], {
      cwd: entry.cwd,
      env: process.env,
      shell: true,
      windowsHide: !entry.visible,
      stdio: entry.visible ? 'inherit' : ['ignore', logFd, logFd],
    });
  } catch (e) {
    finish({ reason: 'failed-to-start', detail: e.message });
    process.exit(1);
  }
  writeJson(rec(id, 'pids'), { supervisorPid: process.pid, childPid: child.pid, at: stamp() });

  let consoleReason = null;
  const human = (reason) => (entry.visible ? reason : 'console-event');
  process.on('SIGINT', () => { consoleReason = human('ctrl-c'); });
  process.on('SIGBREAK', () => { consoleReason = human('ctrl-c'); });
  process.on('SIGHUP', () => {
    finish({ reason: human('window-closed'), detail: 'the supervisor received SIGHUP (console close event)' });
    killTree(child.pid);
    process.exit(0);
  });

  child.on('error', (e) => {
    finish({ reason: 'failed-to-start', detail: e.message });
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    const stop = readJson(rec(id, 'stop'));
    if (stop) finish({ reason: 'stopped', by: stop.by, code });
    else if (consoleReason) finish({ reason: consoleReason, code });
    else if (code === CONTROL_C_EXIT) {
      setTimeout(() => {
        finish({ reason: consoleReason || human('window-gone'), code });
        process.exit(0);
      }, 1500);
      return;
    } else finish({ reason: 'exited', code, signal });
    process.exit(0);
  });
}

function printRows(rows) {
  if (!rows.length) {
    process.stdout.write('no background processes to show\n');
    return;
  }
  const procs = processTable();
  const me = owner();
  for (const d of rows) {
    const e = d.entry;
    const who = e.owner.sessionId ? e.owner.sessionId.slice(0, 8) : 'no session';
    const ownerState = e.owner.claudePid ? (isClaude(procs, e.owner.claudePid) ? 'live' : 'gone') : '-';
    const self = mine(e) && (me.sessionId || me.claudePid) ? ', this session' : '';
    const head = `${e.name}  [${d.state}]  ${e.lifetime}${e.visible ? ', visible window' : ''}  owner ${who} (${ownerState}${self})`;
    const when = d.state === 'ended'
      ? `  ended ${ago(d.exit.at)} ago: ${reasonText(d.exit)}`
      : `  up ${ago(e.startedAt)}, supervisor pid ${d.pids ? d.pids.supervisorPid : '?'}`;
    process.stdout.write(`${head}\n${when}\n  cmd: ${commandLine(e.command)}  (in ${e.cwd})\n${e.log ? `  log: ${e.log}\n` : ''}`);
  }
}

function cmdList(argv) {
  const procs = processTable();
  const all = argv.includes('--all');
  const onlyMine = argv.includes('--mine');
  const rows = allEntries()
    .filter((e) => !onlyMine || mine(e))
    .map((e) => describe(e, procs))
    .filter((d) => all || d.state !== 'ended' || Date.now() - Date.parse(d.exit.at) < 86400000);
  printRows(rows);
}

function cmdStatus(argv) {
  if (!argv[0]) die('status <name|id>');
  const d = find(argv[0], processTable());
  printRows([d]);
  if (d.entry.log) process.stdout.write(`--- last 20 log lines ---\n${tail(d.entry.log, 20)}\n`);
}

function cmdLogs(argv) {
  if (!argv[0]) die('logs <name|id> [--lines N]');
  const i = argv.indexOf('--lines');
  const n = i === -1 ? 50 : Number(argv[i + 1]) || 50;
  const d = find(argv[0], processTable());
  if (!d.entry.log) die(`"${d.entry.name}" runs in a visible window; its output is only there`);
  process.stdout.write(`${tail(d.entry.log, n)}\n`);
}

function cmdWait(argv) {
  if (!argv[0]) die('wait <name|id> [--timeout seconds]');
  const i = argv.indexOf('--timeout');
  const deadline = i === -1 ? Infinity : Date.now() + (Number(argv[i + 1]) || 0) * 1000;
  for (;;) {
    const d = find(argv[0], processTable());
    if (d.state === 'ended') {
      process.stdout.write(`"${d.entry.name}" ended: ${reasonText(d.exit)}\n`);
      return;
    }
    if (Date.now() > deadline) {
      process.stdout.write(`"${d.entry.name}" is still ${d.state}\n`);
      process.exit(2);
    }
    sleep(2000);
  }
}

function cmdStop(argv) {
  const procs = processTable();
  const includeKeep = argv.includes('--include-keep');
  let targets;
  if (argv.includes('--mine')) {
    targets = allEntries()
      .filter((e) => mine(e) && (includeKeep || e.lifetime === 'session'))
      .map((e) => describe(e, procs));
  } else {
    const keys = argv.filter((a) => !a.startsWith('--'));
    if (!keys.length) die('stop <name|id>... | --mine [--include-keep]');
    targets = keys.map((k) => find(k, procs));
  }
  const me = owner();
  const by = me.sessionId ? `session ${me.sessionId.slice(0, 8)}` : 'by hand';
  let n = 0;
  for (const d of targets) {
    if (stopOne(d, by)) {
      n++;
      process.stdout.write(`stopped "${d.entry.name}"\n`);
    } else if (!argv.includes('--mine')) {
      process.stdout.write(`"${d.entry.name}" had already ended: ${reasonText(d.exit)}\n`);
    }
  }
  if (argv.includes('--mine')) process.stdout.write(`stopped ${n} of this session's processes\n`);
}

function sweep() {
  const procs = processTable();
  const stopped = [];
  for (const entry of allEntries()) {
    const d = describe(entry, procs);
    if (d.state === 'running' && entry.lifetime === 'session' && entry.owner.claudePid && !isClaude(procs, entry.owner.claudePid)) {
      stopOne(d, 'sweep: its Claude session is gone');
      stopped.push(entry.name);
    } else if (d.state === 'ended' && Date.now() - Date.parse(d.exit.at) > KEEP_ENDED_DAYS * 86400000) {
      removeRecords(entry);
    }
  }
  return stopped;
}

function readStdin() {
  try { return fs.readFileSync(0, 'utf8'); } catch { return ''; }
}

function cmdHook() {
  let input = {};
  try { input = JSON.parse(readStdin() || '{}'); } catch { /* not JSON: treat as a bare sweep */ }
  if (input.hook_event_name === 'SessionEnd' && input.session_id) {
    const procs = processTable();
    for (const entry of allEntries()) {
      if (entry.lifetime !== 'session' || entry.owner.sessionId !== input.session_id) continue;
      stopOne(describe(entry, procs), `session ended (${input.reason || 'exit'})`);
    }
    return;
  }
  const stopped = sweep();
  if (stopped.length) {
    process.stdout.write(`bg.js stopped ${stopped.length} background process(es) left behind by Claude sessions that have ended: ${stopped.join(', ')}\n`);
  }
}


const [cmd, ...rest] = process.argv.slice(2);
if (cmd === '_supervise') cmdSupervise(rest[0]);
else if (cmd === 'start') cmdStart(rest);
else if (cmd === 'list') cmdList(rest);
else if (cmd === 'status') cmdStatus(rest);
else if (cmd === 'logs') cmdLogs(rest);
else if (cmd === 'wait') cmdWait(rest);
else if (cmd === 'stop') cmdStop(rest);
else if (cmd === 'sweep') {
  const s = sweep();
  process.stdout.write(s.length ? `stopped: ${s.join(', ')}\n` : 'nothing to sweep\n');
} else if (cmd === 'hook') cmdHook();
else process.stdout.write(HELP);
