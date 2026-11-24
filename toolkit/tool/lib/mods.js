'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { performance } = require('node:perf_hooks');
const { spawnSync } = require('node:child_process');
const fsx = require('./fsx');
const { norm } = require('./paths');
const { MOD_MARKETPLACE } = require('./registry');

const SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;

function modsDir(P) {
  return `${P.store}/mods`;
}

function readMarketplace(P) {
  try { return JSON.parse(fs.readFileSync(`${modsDir(P)}/.claude-plugin/marketplace.json`, 'utf8')); } catch { return null; }
}

function listModFolders(P) {
  let entries;
  try { entries = fs.readdirSync(modsDir(P), { withFileTypes: true }); } catch { return []; }
  return entries
    .filter((e) => e.isDirectory() && !/^[_.]/.test(e.name) && fs.existsSync(`${modsDir(P)}/${e.name}/.claude-plugin/plugin.json`))
    .map((e) => e.name)
    .sort();
}

const CLI_TIMEOUT_MS = 8000;

function accountName(P) {
  try { return path.basename(fs.realpathSync(P.claudeHome)); } catch { return path.basename(P.claudeHome); }
}

function wantedMods(P, reg, warnings = []) {
  const market = readMarketplace(P);
  const listed = new Set(((market && market.plugins) || []).map((p) => p && p.name).filter(Boolean));
  const account = accountName(P);
  const out = [];
  for (const entry of (reg.defaults && reg.defaults.mods) || []) {
    const name = typeof entry === 'string' ? entry : entry && entry.name;
    if (typeof name !== 'string' || !SLUG.test(name)) { warnings.push(`default mod is not a mod name: ${JSON.stringify(entry)}`); continue; }
    if (!listed.has(name)) { warnings.push(`default mod not in the shelf marketplace: ${name}`); continue; }
    let accounts = null;
    if (entry && typeof entry === 'object' && entry.accounts !== undefined) {
      if (!Array.isArray(entry.accounts)) { warnings.push(`default mod ${name}: accounts must be a list of account names, not ${JSON.stringify(entry.accounts)}; it is loaded nowhere`); continue; }
      accounts = entry.accounts;
      for (const a of accounts) {
        const folder = `${P.home}/.claude-accounts/${typeof a === 'string' ? a : JSON.stringify(a)}`;
        if (typeof a !== 'string' || !a || a.includes('/') || !fs.existsSync(folder)) warnings.push(`default mod ${name}: no account folder ${folder}`);
      }
    }
    if (accounts && !accounts.includes(account)) continue;
    out.push({ name, key: `${name}@${MOD_MARKETPLACE}` });
  }
  return out;
}

function listedModKeys(reg) {
  const out = new Set();
  for (const entry of (reg.defaults && reg.defaults.mods) || []) {
    const name = typeof entry === 'string' ? entry : entry && entry.name;
    if (typeof name !== 'string' || !SLUG.test(name)) continue;
    if (entry && typeof entry === 'object' && entry.accounts !== undefined && !(Array.isArray(entry.accounts) && entry.accounts.length)) continue;
    out.add(`${name}@${MOD_MARKETPLACE}`);
  }
  return out;
}

function listedModNames(P) {
  let reg;
  try { reg = fsx.readJson(P.registryFile, null); } catch { return null; }
  if (!reg || typeof reg !== 'object') return null;
  return new Set([...listedModKeys(reg)].map((k) => k.slice(0, k.indexOf('@'))));
}

function installedRecord(P, key) {
  const data = fsx.readJson(`${P.claudeHome}/plugins/installed_plugins.json`, { plugins: {} });
  const records = (data.plugins || {})[key];
  return Array.isArray(records) ? records.find((r) => r && r.scope === 'user') || null : null;
}

function installedModKeys(P) {
  const data = fsx.readJson(`${P.claudeHome}/plugins/installed_plugins.json`, { plugins: {} });
  return Object.keys(data.plugins || {}).filter((k) => k.endsWith(`@${MOD_MARKETPLACE}`)).sort();
}

function marketplaceSource(P, name, warnings = []) {
  let data;
  try { data = fsx.readJson(`${P.claudeHome}/plugins/known_marketplaces.json`, {}); } catch (e) {
    warnings.push(`known_marketplaces.json could not be read (${(e && e.message) || e}); the ${name} marketplace is left alone`);
    return '';
  }
  const entry = data && data[name];
  if (!entry) return null;
  const source = entry.source || {};
  const where = source.path || source.url || source.repo || entry.installLocation || '';
  return where ? norm(where) : '';
}

function sameSource(a, b) {
  if (a === b) return true;
  if (!path.isAbsolute(a) || !path.isAbsolute(b)) return false;
  try { return fs.realpathSync(a) === fs.realpathSync(b); } catch { return false; }
}

function pluginPlan(P, a) {
  if (a.op === 'marketplace') {
    const have = marketplaceSource(P, a.name);
    if (have !== null && sameSource(have, norm(a.source))) return 'noop';
    if (have !== null) throw new Error(`marketplace ${a.name} already reads from ${have || 'an unknown source'}, not ${a.source}; left alone`);
    return 'change';
  }
  if (a.op === 'install') return installedRecord(P, a.key) ? 'noop' : 'change';
  if (a.op === 'uninstall') return installedRecord(P, a.key) ? 'change' : 'noop';
  throw new Error(`unknown mod operation: ${a.op}`);
}

function defaultRunClaudePlugin(P, spawn = spawnSync) {
  const env = P.claudeConfigDirExplicit ? { ...process.env, CLAUDE_CONFIG_DIR: P.claudeHome } : process.env;
  const cli = (args) => {
    const r = spawn('claude', args, { encoding: 'utf8', windowsHide: true, shell: false, timeout: CLI_TIMEOUT_MS, env, stdio: ['ignore', 'pipe', 'pipe'] });
    if (!r || r.status !== 0) {
      const why = String((r && (r.stderr || r.stdout)) || (r && r.error && r.error.message) || 'no exit status').trim().split('\n').slice(-3).join(' ');
      throw new Error(`claude ${args.slice(0, 3).join(' ')} failed: ${why}`);
    }
  };
  return (a) => {
    if (pluginPlan(P, a) === 'noop') return false;
    if (a.op === 'marketplace') cli(['plugin', 'marketplace', 'add', a.source, '--scope', 'user']);
    else cli(['plugin', a.op, a.key, '--scope', 'user']);
    return true;
  };
}

function modActions(P, mods, prevManaged, liveEnabled, listed = new Set()) {
  const actions = [];
  if (mods.length) actions.push({ type: 'claudePlugin', op: 'marketplace', name: MOD_MARKETPLACE, source: modsDir(P) });
  for (const m of mods) actions.push(liveEnabled[m.key] === false ? { type: 'claudePlugin', op: 'install', key: m.key, off: true } : { type: 'claudePlugin', op: 'install', key: m.key });
  for (const key of prevManaged || []) if (!mods.some((m) => m.key === key) && !listed.has(key)) actions.push({ type: 'claudePlugin', op: 'uninstall', key });
  return actions;
}


function stateDir(P, env = process.env) {
  const base = !env.TOOLKIT_HOME && env.XDG_STATE_HOME ? env.XDG_STATE_HOME : path.join((P && P.home) || os.homedir(), '.local', 'state');
  return path.join(base, 'claude-hub', 'mods');
}

function claudeVersion(P, { spawn = true, run = spawnSync } = {}) {
  try {
    const target = fs.readlinkSync(path.join(P.home, '.local', 'bin', 'claude'));
    const m = /(\d+\.\d+\.\d+)$/.exec(target);
    if (m) return m[1];
  } catch { /* not a link: fall through */ }
  if (!spawn) return null;
  const r = run('claude', ['--version'], { encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'pipe'] });
  const m = r && r.status === 0 ? /^(\d+\.\d+\.\d+)/.exec(String(r.stdout || '').trim()) : null;
  return m ? m[1] : null;
}

function modsStamp(P) {
  const rows = [];
  const walk = (dir, rel) => {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (rel === '' && e.name === '_types') continue;
      const p = path.join(dir, e.name);
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) walk(p, r);
      else { try { const s = fs.statSync(p); rows.push(`${r}:${s.size}:${Math.floor(s.mtimeMs)}`); } catch { /* vanished mid-walk */ } }
    }
  };
  walk(modsDir(P), '');
  return crypto.createHash('sha256').update(rows.sort().join('\n')).digest('hex').slice(0, 16);
}

const CLI_CALL_MS = 60000;
const TEST_CALL_MS = 120000;
const SESSION_CALL_MS = 4000;
const SESSION_DEADLINE_MS = 8000;
const SESSION_HOOK_BUDGET_MS = 15000;
const UNVERIFIED_HOLD_MS = 6 * 60 * 60 * 1000;

function runCommand(file, args, run, { timeout, deadline = null, now = Date.now } = {}) {
  let limit = timeout;
  const options = { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] };
  if (deadline !== null) {
    const left = deadline - now();
    if (left <= 0) return { status: null, stdout: '', stderr: '', notRun: 'timed out' };
    limit = Math.max(1, Math.floor(Math.min(timeout, left)));
    options.killSignal = 'SIGKILL';
  }
  const r = run(file, args, { ...options, timeout: limit });
  let notRun = null;
  if (!r) notRun = 'could not run';
  else if (r.error) notRun = r.error.code === 'ETIMEDOUT' ? 'timed out' : 'could not run';
  else if (r.signal || typeof r.status !== 'number') notRun = 'could not run';
  return { status: r && typeof r.status === 'number' ? r.status : null, stdout: String((r && r.stdout) || ''), stderr: String((r && r.stderr) || (r && r.error && r.error.message) || ''), notRun };
}
const runCli = (args, run, options) => runCommand('claude', args, run, options);

function isTs5097(message) {
  const codes = new Set(String(message || '').match(/\bTS\d{4,5}\b/g) || []);
  return codes.size === 1 && codes.has('TS5097');
}

function strayFindings(report, problems, notes, warnings = problems) {
  const text = (e) => (typeof e === 'string' ? e : (e && e.message) || JSON.stringify(e));
  const line = (kind, where, e) => `${kind} (${where}): ${e && e.path ? `${e.path}: ` : ''}${text(e)}`;
  const list = (value) => (Array.isArray(value) ? value : value ? [value] : []);
  const walk = (node, where, inAdvice) => {
    if (Array.isArray(node)) { node.forEach((v, i) => walk(v, `${where}[${i}]`, inAdvice)); return; }
    if (!node || typeof node !== 'object') return;
    if (inAdvice && node.severity === 'error') problems.push(line('error', where, node));
    for (const [key, value] of Object.entries(node)) {
      if (where === 'report' && (key === 'manifest' || key === 'contents')) continue;
      if (where === 'report.directory' && key === 'findings') continue;
      if (key === 'errors') for (const e of list(value)) problems.push(line('error', where, e));
      else if (key === 'warnings') for (const w of list(value)) (isTs5097(text(w)) ? notes : warnings).push(line('warning', where, w));
      else walk(value, `${where}.${key}`, inAdvice || (where === 'report' && key === 'advice'));
    }
  };
  walk(report, 'report', false);
}

const TEST_FILE = /\.test\.tsx?$/;
function partFile(dir, part, isManifest) {
  const file = part && typeof part.file === 'string' && part.file ? part.file : null;
  if (!file) return { rel: null, test: false };
  const under = (root) => { const rel = path.relative(root, file); return rel && rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel) ? rel.split(path.sep).join('/') : null; };
  let rel = under(dir);
  if (rel === null) { try { rel = under(fs.realpathSync(dir)); } catch { /* the folder is not on disk: the path as given decides */ } }
  if (rel === null) return { rel: file, test: false };
  return { rel, test: !isManifest && (rel.startsWith('tests/') || TEST_FILE.test(rel)) };
}

function validate(dir, run = spawnSync, { timeout = CLI_CALL_MS, deadline = null, now = Date.now } = {}) {
  const r = runCli(['plugin', 'validate', dir, '--strict', '--json'], run, { timeout, deadline, now });
  if (r.notRun) return { ok: false, unverified: true, problems: [`claude plugin validate ${r.notRun}`] };
  let report;
  try { report = JSON.parse(r.stdout); } catch {
    return { ok: false, problems: [`validate gave no JSON report (exit ${r.status}): ${(r.stderr || r.stdout).trim().split('\n').slice(-2).join(' ')}`] };
  }
  const problems = [];
  const codeWarnings = [];
  const testWarnings = [];
  const notes = [];
  const parts = [report.manifest, ...(report.contents || [])];
  parts.forEach((part, index) => {
    if (!part) return;
    const { rel, test } = partFile(dir, part, index === 0);
    for (const e of part.errors || []) problems.push(`error: ${e.path ? `${e.path}: ` : ''}${e.message}`);
    for (const w of part.warnings || []) {
      const line = `warning: ${rel ? `${rel}: ` : ''}${w.path ? `${w.path}: ` : ''}${w.message}`;
      (isTs5097(w.message) ? notes : test ? testWarnings : codeWarnings).push(line);
    }
    for (const g of part.gatingHooks || []) if (!g.hasCatch) problems.push(`gating hook without .catch: ${g.hook}`);
  });
  if (r.status === 1) strayFindings(report, problems, notes, codeWarnings);
  const warnings = [...codeWarnings, ...testWarnings];
  const manifestIsObject = !!report.manifest && typeof report.manifest === 'object' && !Array.isArray(report.manifest);
  if (manifestIsObject && (r.status === 0 || r.status === 1) && !problems.length && warnings.length) {
    return { ok: false, warned: true, ...(codeWarnings.length ? { warnedInCode: true } : {}), problems: warnings };
  }
  problems.push(...warnings);
  const onlyNotes = r.status === 1 && manifestIsObject && !problems.length && notes.length > 0;
  if (r.status !== 0 && !problems.length && !onlyNotes) problems.push(`validate exited ${r.status}`);
  const ok = (r.status === 0 || onlyNotes) && problems.length === 0;
  return { ok, problems, ...(ok && notes.length ? { notes } : {}) };
}

function runTests(dir, run = spawnSync, { timeout = TEST_CALL_MS, deadline = null, now = Date.now } = {}) {
  const r = runCli(['plugin', 'test', dir], run, { timeout, deadline, now });
  if (r.notRun) return { ok: false, unverified: true, problems: [`claude plugin test ${r.notRun}`] };
  const tail = (r.stdout + r.stderr).trim().split('\n').slice(-6).join(' | ');
  return { ok: r.status === 0, problems: r.status === 0 ? [] : [`claude plugin test exited ${r.status}: ${tail}`] };
}

function typeCheck(tsc, dir, run, { timeout = TEST_CALL_MS, deadline = null, now = Date.now } = {}) {
  const tsconfig = `${dir}/tsconfig.json`;
  if (!fs.existsSync(tsconfig)) return { ok: false, problems: [`tsc found no tsconfig.json in ${path.basename(dir)}`] };
  const r = runCommand(process.execPath, [tsc, '-p', tsconfig], run, { timeout, deadline, now });
  if (r.notRun) return { ok: false, unverified: true, problems: [`tsc ${r.notRun}`] };
  if (r.status === 0) return { ok: true, problems: [] };
  const lines = (r.stdout + r.stderr).trim().split('\n').map((l) => l.trim()).filter(Boolean);
  return { ok: false, problems: [`tsc found type errors: ${lines.slice(0, 3).join(' | ') || `exit ${r.status}`}`] };
}

function checkMods(P, { tests = false, run = spawnSync, timeout, deadline, now = Date.now, tmpRoot } = {}) {
  const limits = { deadline: deadline === undefined ? null : deadline, now };
  const validateLimits = { ...limits, ...(timeout ? { timeout } : {}) };
  const testLimits = { ...limits, ...(timeout ? { timeout } : {}) };
  const notes = [];
  const version = claudeVersion(P, { run });
  let tsc = null;
  if (tests) {
    const file = `${P.store}/node_modules/typescript/bin/tsc`;
    if (!fs.existsSync(file)) notes.push(`tsc not found at ${file}: type check skipped`);
    else {
      const types = ensureTypes(P, { version, tmpRoot });
      if (!types.ok) notes.push(`${types.problem}; type check skipped`);
      else { tsc = file; if (types.problem) notes.push(types.problem); }
    }
  }
  const noteTs5097 = (label, v) => { if (v.notes && v.notes.length) notes.push(`${label}: ${v.notes.length} TS5097 warning(s) (a ".ts" import path) noted, not refused`); };
  const market = validate(modsDir(P), run, validateLimits);
  noteTs5097('marketplace', market);
  const mods = listModFolders(P).map((name) => {
    const dir = `${modsDir(P)}/${name}`;
    const v = validate(dir, run, validateLimits);
    noteTs5097(name, v);
    const t = tests ? runTests(dir, run, testLimits) : { ok: true, problems: [] };
    const c = tests && tsc ? typeCheck(tsc, dir, run, testLimits) : { ok: true, problems: [] };
    const refused = !v.ok && !v.unverified && !v.warned;
    const testsFailed = (!t.ok && !t.unverified) || (!c.ok && !c.unverified);
    const unverified = !!(v.unverified || t.unverified || c.unverified);
    return {
      name, ok: v.ok && t.ok && c.ok, ...(unverified ? { unverified: true } : {}), refused, testsFailed,
      ...(v.warned ? { warned: true, ...(v.warnedInCode ? { warnedInCode: true } : {}), warnings: v.problems } : {}),
      problems: [...(v.warned ? [] : v.problems), ...t.problems, ...c.problems],
    };
  });
  const typesVersion = (fsx.readText(`${modsDir(P)}/_types/VERSION`) || '').trim() || null;
  const firstUnverified = market.unverified ? market : mods.find((m) => m.unverified);
  return {
    claude: version,
    typesVersion,
    stamp: modsStamp(P),
    at: new Date(now()).toISOString(),
    tests,
    marketplaceOk: market.ok,
    ...(market.unverified ? { marketplaceUnverified: true } : {}),
    ...(market.warned ? { marketplaceWarned: true } : {}),
    ...(market.warnedInCode ? { marketplaceWarnedInCode: true } : {}),
    marketplaceProblems: market.problems,
    mods,
    notes,
    ...(firstUnverified ? { unverified: true, unverifiedWhy: firstUnverified.problems.find((p) => /timed out|could not run/.test(p)) || firstUnverified.problems[0] } : {}),
    ok: market.ok && mods.every((m) => m.ok),
  };
}

function readCheckRecord(dir) {
  try { return JSON.parse(fs.readFileSync(path.join(dir, 'check.json'), 'utf8')); } catch { return null; }
}

function writeCheckRecord(result, dir) {
  const file = path.join(dir, 'check.json');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(result, null, 2) + '\n');
  fs.renameSync(tmp, file);
}

const TS_CODE = /\bTS\d{4,5}\b/;
function failLine(result, listed = null) {
  const wanted = (name) => !listed || listed.has(name);
  const listedMarket = !listed || listed.size > 0;
  const marketBad = !result.marketplaceOk && !result.marketplaceUnverified && !result.marketplaceWarned && listedMarket;
  const bad = marketBad ? [{ name: 'marketplace', problems: result.marketplaceProblems }] : [];
  const failing = (result.mods || []).filter((m) => !m.ok && !(m.unverified && !m.refused && !m.testsFailed) && !(m.warned && !m.testsFailed) && wanted(m.name));
  const refused = [...bad, ...failing.filter((m) => m.refused !== false)];
  const failed = failing.filter((m) => m.refused === false);
  const inCode = [
    ...(result.marketplaceWarned && result.marketplaceWarnedInCode && listedMarket ? [{ name: 'marketplace', warnings: result.marketplaceProblems }] : []),
    ...(result.mods || []).filter((m) => m.warned && m.warnedInCode && !m.testsFailed && wanted(m.name)),
  ];
  const list = (ms) => ms.map((m) => `${m.name} (${(m.problems || [])[0] || 'failed'})`).join('; ');
  const firstWarning = (m) => String((m.warnings || [])[0] || 'warning').replace(/^warning: /, '');
  const v = result.claude || 'unknown';
  const parts = [];
  if (refused.length) parts.push(`mods refused on Claude Code ${v}: ${refused.map((m) => `${m.name} (${(m.problems || [])[0] || 'refused'})`).join('; ')}`);
  if (failed.length) parts.push(`mod tests failed on Claude Code ${v}: ${list(failed)}`);
  if (inCode.length) {
    const kind = inCode.every((m) => TS_CODE.test(firstWarning(m))) ? 'type errors' : 'validator warnings';
    parts.push(`${kind} in mod code on Claude Code ${v} (the mods still load; check they work): ${inCode.map((m) => `${m.name} (${firstWarning(m)})`).join('; ')}`);
  }
  return parts.length ? `Toolkit: ${parts.map((t, i) => (i ? t[0].toUpperCase() + t.slice(1) : t)).join('. ')}` : null;
}

function unverifiedWhy(result, listed = null) {
  const why = (problems) => (problems || []).find((p) => /timed out|could not run/.test(p)) || (problems || [])[0] || 'claude plugin validate could not run';
  if (result.marketplaceUnverified) return why(result.marketplaceProblems);
  const mod = (result.mods || []).find((m) => m.unverified && (!listed || listed.has(m.name)));
  return mod ? why(mod.problems) : null;
}

function afterUpdateLine(P, { run = spawnSync, now = Date.now, runCheck, dir = stateDir(P), start } = {}) {
  const hookStart = start !== undefined ? start : now === Date.now ? performance.timeOrigin : now();
  if (!listModFolders(P).length) return null;
  const version = claudeVersion(P, { spawn: false });
  if (!version) return null;
  const listed = listedModNames(P);
  const record = readCheckRecord(dir);
  if (record && record.claude === version && record.stamp === modsStamp(P)) {
    const held = record.unverified && now() - Date.parse(record.at) < UNVERIFIED_HOLD_MS;
    if (!record.unverified || held) return failLine(record, listed);
  }
  const check = runCheck || ((p) => checkMods(p, { tests: false, run, now, timeout: SESSION_CALL_MS, deadline: Math.min(now() + SESSION_DEADLINE_MS, hookStart + SESSION_HOOK_BUDGET_MS) }));
  const result = check(P);
  writeCheckRecord(result, dir);
  const refusal = failLine(result, listed);
  if (refusal) return refusal;
  const notChecked = result.unverified ? unverifiedWhy(result, listed) : null;
  if (notChecked) return `Toolkit: could not re-check mods on Claude Code ${version}: ${notChecked}`;
  if (!result.ok) return null;
  return record && (record.claude !== version || record.unverified) ? `Toolkit: all ${result.mods.length} mod(s) still valid on Claude Code ${version}` : null;
}

const versionParts = (v) => String(v).split('.').map((n) => Number.parseInt(n, 10) || 0);
function compareVersions(a, b) {
  const x = versionParts(a), y = versionParts(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  return 0;
}

function bundledTypes(version, tmpRoot = path.join(os.tmpdir(), `claude-${process.getuid ? process.getuid() : 'user'}`)) {
  const root = path.join(tmpRoot, 'bundled-skills');
  let versions;
  try { versions = fs.readdirSync(root); } catch { return null; }
  const order = [...(version && versions.includes(version) ? [version] : []), ...versions.filter((v) => v !== version).sort(compareVersions).reverse()];
  for (const v of order) {
    let hashes;
    try { hashes = fs.readdirSync(path.join(root, v)); } catch { continue; }
    for (const h of hashes) {
      const file = path.join(root, v, h, 'plugin-authoring', 'types', 'claude-code.d.ts');
      if (fs.existsSync(file)) return { file, version: v };
    }
  }
  return null;
}

function ensureTypes(P, { version = claudeVersion(P), tmpRoot } = {}) {
  const dir = `${modsDir(P)}/_types`;
  const have = (fsx.readText(`${dir}/VERSION`) || '').trim();
  const present = fs.existsSync(`${dir}/claude-code.d.ts`);
  if (version && have === version && present) return { ok: true, version, copied: false };
  const src = bundledTypes(version, tmpRoot);
  const stale = (v) => `mod types are from Claude Code ${v}, not ${version || 'unknown'}; a session started on ${version || 'the new version'} unpacks the exact ones`;
  if (!src) return { ok: present, version: have || null, copied: false, problem: `no engine types for Claude Code ${version || 'unknown'} under the bundled-skills folder; a running session of that version unpacks them` };
  if (src.version !== version && present && have && compareVersions(have, src.version) >= 0) return { ok: true, version: have, copied: false, problem: stale(have) };
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(src.file, `${dir}/claude-code.d.ts`);
  fs.writeFileSync(`${dir}/VERSION`, `${src.version}\n`);
  return src.version === version ? { ok: true, version, copied: true } : { ok: true, version: src.version, copied: true, problem: stale(src.version) };
}

function command(P, rest, flags, out, { run = spawnSync, dir = stateDir(P) } = {}) {
  const sub = rest[0];
  if (sub === 'check') {
    let result = null;
    if (flags.reuse) {
      const record = readCheckRecord(dir);
      if (record && !record.unverified && (!flags.tests || record.tests) && record.claude === claudeVersion(P, { spawn: false }) && record.stamp === modsStamp(P)) result = { ...record, reused: true };
    }
    if (!result) {
      result = checkMods(P, { tests: !!flags.tests, run });
      if (flags.record || flags.reuse) writeCheckRecord(result, dir);
    }
    if (flags.json) { out.write(JSON.stringify(result, null, 2) + '\n'); return result.ok ? 0 : 1; }
    const warnText = (ws, what, inCode) => `${ws.every((w) => TS_CODE.test(w)) ? 'type warnings' : 'validator warnings'} ${inCode ? `in its code (${what} still loads; check it works)` : `in its tests (${what} still loads)`}: ${ws.length} warning(s): ${ws.slice(0, 3).map((w) => w.replace(/^warning: /, '')).join('; ')}${ws.length > 3 ? `; and ${ws.length - 3} more` : ''}`;
    const marketText = result.marketplaceWarned ? warnText(result.marketplaceProblems, 'the marketplace', !!result.marketplaceWarnedInCode) : `${result.marketplaceUnverified ? 'not checked: ' : ''}${result.marketplaceProblems.join('; ')}`;
    out.write(`marketplace ${MOD_MARKETPLACE}: ${result.marketplaceOk ? 'ok' : marketText}\n`);
    for (const m of result.mods) {
      const parts = [];
      if (m.warned) parts.push(warnText(m.warnings || [], 'the mod', !!m.warnedInCode));
      if (m.problems.length) parts.push(`${m.unverified && !m.refused && !m.testsFailed ? 'not checked: ' : ''}${m.problems.join('; ')}`);
      out.write(`${m.name.padEnd(24)} ${m.ok ? 'ok' : parts.join('; ') || 'not ok'}\n`);
    }
    for (const n of result.notes || []) out.write(`note: ${n}\n`);
    out.write(`checked on Claude Code ${result.claude || 'unknown'}${result.tests ? ' (validate and tests)' : ' (validate)'}${result.reused ? `, recorded ${result.at}` : ''}\n`);
    return result.ok ? 0 : 1;
  }
  if (sub === 'types') {
    const r = ensureTypes(P);
    out.write(r.ok ? `mod types for Claude Code ${r.version}${r.copied ? ' copied' : ' already in place'}: ${modsDir(P)}/_types\n` : `${r.problem}\n`);
    if (r.ok && r.problem) out.write(`note: ${r.problem}\n`);
    return r.ok ? 0 : 1;
  }
  throw new Error('Usage: toolkit mods check [--json] [--tests] [--record] [--reuse] | types');
}

module.exports = {
  SLUG, MOD_MARKETPLACE, modsDir, accountName, readMarketplace, listModFolders, wantedMods, listedModKeys, listedModNames,
  installedRecord, installedModKeys, marketplaceSource, pluginPlan, defaultRunClaudePlugin, modActions,
  stateDir, claudeVersion, modsStamp, validate, runTests, typeCheck, checkMods, readCheckRecord, writeCheckRecord,
  failLine, afterUpdateLine, compareVersions, bundledTypes, ensureTypes, command,
};
