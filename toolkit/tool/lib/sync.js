'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { norm, same } = require('./paths');
const fsx = require('./fsx');
const { loadRegistry, findProject } = require('./registry');
const { listItems } = require('./store');
const pointer = require('./pointer');
const claude = require('./claude');
const managed = require('./managed');
const { assertNoSecret } = require('./secrets');
const state = require('./state');
const { appendLog } = require('./log');
const { withSyncLock } = require('./sync-lock');
const mods = require('./mods');

function inputsHash(inputs) {
  return fsx.sha256(JSON.stringify(inputs, Object.keys(flatten(inputs)).sort()));
}

function planHash(built) {
  return inputsHash({ inputs: built.inputs, actions: built.actions });
}
function flatten(obj, prefix = '', out = {}) {
  if (obj && typeof obj === 'object') { for (const k of Object.keys(obj)) { out[k] = true; flatten(obj[k], k, out); } }
  return out;
}

function loadContext(P) {
  return { reg: loadRegistry(P), items: listItems(P), codexSkills: claude.readProbeVerdict(P) };
}

function cmdQuote(s) {
  let out = '"';
  let slashes = 0;
  for (const ch of String(s)) {
    if (ch === '\\') { slashes++; continue; }
    if (ch === '"') { out += '\\'.repeat(slashes * 2 + 1) + '"'; slashes = 0; continue; }
    out += '\\'.repeat(slashes) + ch;
    slashes = 0;
  }
  return `${out}${'\\'.repeat(slashes * 2)}"`;
}

function comspecCommand(argv) {
  return {
    file: process.env.ComSpec || 'cmd.exe',
    args: ['/d', '/s', '/c', `"${argv.map(cmdQuote).join(' ')}"`],
    options: { windowsHide: true, encoding: 'utf8', shell: false, windowsVerbatimArguments: true },
  };
}

const CLI_PKG_REL = 'node_modules/@anthropic-ai/claude-code';
const CLI_ENTRIES = ['cli.js', 'bin/claude.exe', 'bin/claude'];

let cliEntryCache; // undefined = not looked up yet; a string or null afterwards

function entryUnder(root) {
  for (const rel of CLI_ENTRIES) {
    const p = norm(`${root}/${rel}`);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function claudeCliEntry(opts = {}) {
  const injected = opts.env !== undefined || opts.spawn !== undefined || opts.fresh;
  if (!injected && cliEntryCache !== undefined) return cliEntryCache;
  const env = opts.env || process.env;
  const spawn = opts.spawn || spawnSync;
  let found = null;
  if (env.APPDATA) found = entryUnder(`${norm(env.APPDATA)}/npm/${CLI_PKG_REL}`);
  if (!found) {
    try {
      const r = spawn('npm', ['root', '-g'], { encoding: 'utf8', windowsHide: true, shell: process.platform === 'win32' });
      const root = r && r.status === 0 ? String(r.stdout || '').trim() : '';
      if (root) found = entryUnder(`${norm(root)}/@anthropic-ai/claude-code`);
    } catch { /* npm missing or not runnable: fall back */ }
  }
  if (!injected) cliEntryCache = found;
  return found;
}

const COMSPEC_UNSAFE = /[&|<>^%"]/;

function findUnsafeForComSpec(value, label) {
  if (typeof value === 'string') {
    const m = COMSPEC_UNSAFE.exec(value);
    return m ? { field: label, char: m[0] } : null;
  }
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      const hit = findUnsafeForComSpec(value[i], `${label}[${i}]`);
      if (hit) return hit;
    }
    return null;
  }
  if (value && typeof value === 'object') {
    for (const k of Object.keys(value)) {
      const hit = findUnsafeForComSpec(value[k], `${label}.${k}`);
      if (hit) return hit;
    }
    return null;
  }
  return null;
}

function claudeCommand(argv, platform, cliEntry) {
  if (platform === 'win32') {
    if (cliEntry) {
      const isJs = /\.(c|m)?js$/i.test(cliEntry);
      return {
        file: isJs ? process.execPath : cliEntry,
        args: isJs ? [cliEntry, ...argv] : argv,
        options: { windowsHide: true, encoding: 'utf8', shell: false },
      };
    }
    return comspecCommand(['claude', ...argv]);
  }
  return { file: 'claude', args: argv, options: { windowsHide: true, encoding: 'utf8', shell: false } };
}

function claudeMcpCommand(name, server, platform = process.platform, opts = {}) {
  const cliEntry = platform === 'win32' ? (opts.resolveCli || claudeCliEntry)() : null;
  if (platform === 'win32' && !cliEntry) {
    const hit = findUnsafeForComSpec(server, 'server') || findUnsafeForComSpec(name, 'name');
    if (hit) {
      throw new Error(`Refusing to register MCP server "${name}": ${hit.field} contains "${hit.char}", which cmd.exe would treat as syntax, and the claude CLI's own entry point could not be found to spawn it directly. Install @anthropic-ai/claude-code as an npm global, or take the character out of the definition.`);
    }
  }
  return claudeCommand(['mcp', 'add-json', name, JSON.stringify(server), '--scope', 'user'], platform, cliEntry);
}

function claudeMcpRemoveCommand(name, platform = process.platform, opts = {}) {
  const cliEntry = platform === 'win32' ? (opts.resolveCli || claudeCliEntry)() : null;
  if (platform === 'win32' && !cliEntry) {
    const hit = findUnsafeForComSpec(name, 'name');
    if (hit) throw new Error(`Refusing to unregister MCP server "${name}": ${hit.field} contains "${hit.char}", which cmd.exe would treat as syntax.`);
  }
  return claudeCommand(['mcp', 'remove', '--scope', 'user', name], platform, cliEntry);
}

function canonical(v) {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === 'object') {
    const out = {};
    for (const k of Object.keys(v).sort()) out[k] = canonical(v[k]);
    return out;
  }
  return v;
}

function defaultRunClaudeMcp(P, spawn = spawnSync, cmdOpts = {}) {
  const platform = cmdOpts.platform || process.platform;
  return (name, server) => {
    const cfg = fsx.readJson(P.claudeConfigFile || `${P.home}/.claude.json`, {});
    const existing = (cfg.mcpServers || {})[name];
    const removing = server === null;
    if (removing) {
      if (existing === undefined) return false;
    } else if (existing !== undefined && JSON.stringify(canonical(existing)) === JSON.stringify(canonical(server))) {
      return false;
    }
    const { file, args, options } = removing ? claudeMcpRemoveCommand(name, platform, cmdOpts) : claudeMcpCommand(name, server, platform, cmdOpts);
    const spawnOptions = P.claudeConfigDirExplicit
      ? { ...options, env: { ...process.env, CLAUDE_CONFIG_DIR: P.claudeHome } }
      : options;
    const r = spawn(file, args, spawnOptions);
    if (!r || r.status !== 0) {
      const why = ((r && (r.stderr || r.stdout)) || (r && r.error && r.error.message) || '').trim();
      throw new Error(`claude mcp ${removing ? 'remove' : 'add-json'} ${name} failed: ${why}`);
    }
    return true;
  };
}

function pushBack(P, st, out) {
  for (const [dest, info] of Object.entries(st.copies || {})) {
    if (!info || info.src == null) continue;
    try {
      const cur = fsx.fileHash(dest);
      if (cur == null || cur === info.deliveredHash) continue;
      const srcNow = fsx.fileHash(info.src);
      if (srcNow === cur) continue;
      if (srcNow === info.storeHash) {
        try {
          assertNoSecret(dest);
        } catch (e) {
          out.warnings.push(e.message);
          out.conflicts.push({ dest, src: info.src, reason: 'secret' });
          continue;
        }
        if (!out.dryRun) {
          fsx.backupFile(P, info.src);
          fs.copyFileSync(dest, info.src);
          out.changes.push(`pushed back ${dest} → ${info.src}`);
        } else {
          out.changes.push(`would push back ${dest} → ${info.src}`);
        }
      } else {
        out.conflicts.push({ dest, src: info.src, reason: 'both changed' });
        out.warnings.push(`conflict: ${dest} and ${info.src} both changed; left alone`);
      }
    } catch (e) {
      out.warnings.push(`failed push-back ${dest}: ${e.message}`);
    }
  }
}

function actionPath(a) {
  if (a.type === 'junction') return a.link;
  if (a.type === 'copy' || a.type === 'file') return a.dest;
  if (a.type === 'json' || a.type === 'mcpjson' || a.type === 'tomlRegion') return a.file;
  if (a.type === 'claudeMcp') return a.name;
  if (a.type === 'claudePlugin') return a.key || a.name;
  return '';
}

function linksIntact(st, builtActions) {
  const allExist = (st.created || []).every((p) => fs.existsSync(p) || fsx.isLink(p));
  if (!allExist) return false;
  return (builtActions || []).every((a) => a.type !== 'junction' || (fsx.isLink(a.link) && same(fsx.linkTarget(a.link), a.target)));
}

function applyActions(P, opts) {
  const { actions, state: st, root, gitignoreFile, gitignoreLines, dryRun } = opts;
  const runClaudeMcp = opts.runClaudeMcp || defaultRunClaudeMcp(P);
  const runClaudePlugin = opts.runClaudePlugin || mods.defaultRunClaudePlugin(P);
  const out = { changes: [], warnings: [], conflicts: [], created: [], copies: {}, managedPlugins: [], mcpNames: [], managedMods: [], tomlRegions: { ...(st.tomlRegions || {}) }, skipped: [], dryRun: !!dryRun };
  const skip = (p) => { const n = norm(p); if (n && !out.skipped.includes(n)) out.skipped.push(n); };
  const prevCreated = new Set((st.created || []).map((p) => norm(p).toLowerCase()));
  const wasCreated = (p) => prevCreated.has(norm(p).toLowerCase());
  const conflicted = (p) => out.conflicts.some((c) => same(c.dest, p));
  let modMarketplaceFailed = false;

  if (!opts.readonlySources) pushBack(P, st, out);
  else for (const [dest, info] of Object.entries(st.copies || {})) {
    if (info?.src && fsx.fileHash(dest) != null && fsx.fileHash(dest) !== info.deliveredHash) {
      out.conflicts.push({ dest, src: info.src, reason: 'source writes disabled during Codex-only sync' });
      out.warnings.push(`changed Codex copy left in place during Codex-only sync: ${dest}`);
      skip(dest);
    }
  }

  for (const a of actions) {
    try {
      if (a.type === 'junction') {
        if (fs.existsSync(a.link) && !fsx.isLink(a.link)) { out.warnings.push(`exists and is not a link: ${a.link}`); skip(a.link); continue; }
        if (!dryRun) {
          const r = fsx.makeJunction(P, a.target, a.link);
          if (r !== 'unchanged') out.changes.push(`${r} link ${a.link} → ${a.target}`);
        } else if (!fsx.isLink(a.link)) out.changes.push(`would link ${a.link} → ${a.target}`);
        out.created.push(norm(a.link));
      } else if (a.type === 'copy' || a.type === 'file') {
        const dest = norm(a.dest);
        const content = a.type === 'copy' ? fs.readFileSync(a.src) : Buffer.from(a.content);
        const newHash = fsx.sha256(content);
        const curHash = fsx.fileHash(dest);
        if (conflicted(dest)) { out.created.push(dest); out.copies[dest] = st.copies[dest]; continue; }
        if (curHash != null && curHash !== newHash && !wasCreated(dest)) { out.warnings.push(`exists and is not managed: ${dest}`); skip(dest); continue; }
        if (curHash !== newHash) {
          if (!dryRun) { fsx.ensureDir(path.dirname(dest)); fsx.writeFileAtomic(dest, content); }
          out.changes.push(`${dryRun ? 'would write' : 'wrote'} ${dest}`);
        }
        out.created.push(dest);
        out.copies[dest] = { src: a.type === 'copy' ? norm(a.src) : null, storeHash: a.type === 'copy' ? newHash : null, deliveredHash: newHash };
      } else if (a.type === 'json') {
        const existing = fsx.readJson(a.file, {});
        const { obj, changed } = managed.applyManagedJson(existing, a.spec, P.store);
        if (changed) {
          if (!dryRun) fsx.writeFileBackup(P, a.file, JSON.stringify(obj, null, 2) + '\n');
          out.changes.push(`${dryRun ? 'would update' : 'updated'} ${a.file}`);
        }
        for (const [k, v] of Object.entries(a.spec.enabledPlugins || {})) if (v !== null) out.managedPlugins.push(k);
      } else if (a.type === 'mcpjson') {
        const existing = fsx.readJson(a.file, { mcpServers: {} });
        existing.mcpServers = existing.mcpServers || {};
        const before = JSON.stringify(existing);
        for (const n of a.managedNames) delete existing.mcpServers[n];
        for (const [n, s] of Object.entries(a.servers)) existing.mcpServers[n] = s;
        if (JSON.stringify(existing) !== before) {
          if (!dryRun) fsx.writeFileBackup(P, a.file, JSON.stringify(existing, null, 2) + '\n');
          out.changes.push(`${dryRun ? 'would update' : 'updated'} ${a.file}`);
        }
        out.mcpNames.push(...Object.keys(a.servers));
      } else if (a.type === 'tomlRegion') {
        const text = fsx.readText(a.file) || '';
        const input = a.legacyBody && !text.includes(managed.TOML_BEGIN) ? `${managed.TOML_BEGIN}\n${text}\n${managed.TOML_END}\n` : text;
        const next = managed.preserveTomlRegion(input, a.body, { previousBody: st.tomlRegions?.[norm(a.file)], legacyMcpNames: st.mcpNames || [], warnings: out.warnings });
        if (next !== text) {
          if (!dryRun) fsx.writeFileBackup(P, a.file, next);
          out.changes.push(`${dryRun ? 'would update' : 'updated'} ${a.file}`);
        }
        out.tomlRegions[norm(a.file)] = a.body;
      } else if (a.type === 'claudeMcp') {
        const removing = a.server === null;
        if (dryRun) {
          out.changes.push(`${removing ? 'would unregister' : 'would register'} MCP server ${a.name}`);
          if (!removing) out.mcpNames.push(a.name);
        } else {
          try {
            const ran = runClaudeMcp(a.name, a.server);
            if (removing) { if (ran) out.changes.push(`unregister MCP server ${a.name}`); }
            else {
              if (ran) out.changes.push(`registered user MCP server ${a.name}`);
              out.mcpNames.push(a.name);
            }
          } catch (e) {
            out.warnings.push(e.message);
            skip(a.name);
            if (removing) out.mcpNames.push(a.name);
          }
        }
      } else if (a.type === 'claudePlugin') {
        const owned = (st.managedMods || []).includes(a.key);
        if (a.op === 'install' && a.off) { if (owned) out.managedMods.push(a.key); continue; }
        if (dryRun) {
          if (a.op === 'install' && modMarketplaceFailed) {
            out.warnings.push(`mod ${a.key} not installed: the ${mods.MOD_MARKETPLACE} marketplace is not in place`);
            skip(a.key);
            continue;
          }
          try {
            if (mods.pluginPlan(P, a) === 'change') out.changes.push(a.op === 'marketplace' ? `would add marketplace ${a.name}` : `would ${a.op} mod ${a.key}`);
          } catch (e) {
            out.warnings.push(e.message);
            skip(a.key || a.name);
            if (a.op === 'marketplace') modMarketplaceFailed = true;
          }
          continue;
        }
        if (a.op === 'install' && modMarketplaceFailed) {
          out.warnings.push(`mod ${a.key} not installed: the ${mods.MOD_MARKETPLACE} marketplace is not in place`);
          skip(a.key);
          if (owned) out.managedMods.push(a.key);
          continue;
        }
        try {
          const ran = runClaudePlugin(a);
          if (a.op === 'marketplace') { if (ran) out.changes.push(`added marketplace ${a.name} (${a.source})`); }
          else if (a.op === 'install') { if (ran) out.changes.push(`installed mod ${a.key}`); out.managedMods.push(a.key); }
          else if (a.op === 'uninstall') { if (ran) out.changes.push(`uninstalled mod ${a.key}`); }
        } catch (e) {
          out.warnings.push(e.message);
          skip(a.key || a.name);
          if (a.op === 'marketplace') modMarketplaceFailed = true;
          if (a.op === 'uninstall' || (a.op === 'install' && owned)) out.managedMods.push(a.key);
        }
      }
    } catch (e) {
      if (e.code === 'EOWNERSHIP') {
        out.warnings.push(e.message);
        if (a.type === 'copy' || a.type === 'file') {
          const dest = norm(a.dest);
          out.created.push(dest);
          if (st.copies?.[dest]) out.copies[dest] = st.copies[dest];
        }
      } else out.warnings.push(`failed ${a.type} ${actionPath(a)}: ${e.message}`);
      skip(actionPath(a) || a.type);
    }
  }

  const nowCreated = new Set(out.created.map((p) => p.toLowerCase()));
  for (const p of st.created || []) {
    if (actions.some((a) => a.type === 'tomlRegion' && same(a.file, p))) continue;
    if (nowCreated.has(norm(p).toLowerCase())) continue;
    if (fsx.isLink(p)) { if (!dryRun) fsx.removeLink(p); out.changes.push(`${dryRun ? 'would remove' : 'removed'} link ${p}`); continue; }
    if (!fs.existsSync(p)) continue;
    const info = (st.copies || {})[p];
    if (info && fsx.fileHash(p) === info.deliveredHash) { if (!dryRun) fs.unlinkSync(p); out.changes.push(`${dryRun ? 'would remove' : 'removed'} ${p}`); }
    else { out.warnings.push(`changed since delivery, left in place: ${p}`); skip(p); }
  }

  if (gitignoreFile) {
    const text = fsx.readText(gitignoreFile) || '';
    const next = managed.setGitignoreBlock(text, gitignoreLines || []);
    if (next !== text) {
      if (!dryRun) fsx.writeFileBackup(P, gitignoreFile, next);
      out.changes.push(`${dryRun ? 'would update' : 'updated'} ${gitignoreFile}`);
    }
  }
  return out;
}

function needsPushBack(st) {
  return Object.entries(st.copies || {}).some(([dest, info]) => { const h = fsx.fileHash(dest); return h != null && h !== info.deliveredHash; });
}

function finish(P, st, hash, out, scope, label) {
  const result = { skipped: false, changes: out.changes, warnings: out.warnings, conflicts: out.conflicts };
  if (out.dryRun) return result;
  st.inputsHash = hash;
  st.created = out.created;
  st.copies = out.copies;
  st.conflicts = out.conflicts;
  st.warnings = out.warnings;
  st.managedPlugins = [...new Set(out.managedPlugins)];
  st.mcpNames = [...new Set(out.mcpNames)];
  st.managedMods = [...new Set(out.managedMods || [])];
  st.tomlRegions = out.tomlRegions;
  st.skipped = out.skipped;
  st.incomplete = out.skipped.length > 0;
  st.lastSync = new Date().toISOString();
  appendLog(P, { scope, project: label, changes: out.changes.length, warnings: out.warnings.length, conflicts: out.conflicts.length, detail: out.changes.slice(0, 50) });
  return result;
}

function isCodexPath(P, root, filename) {
  const value = norm(filename || '').toLowerCase();
  const bases = root ? [`${norm(root)}/.codex/`, `${norm(root)}/.agents/skills/`] : [`${P.codexHome}/`, `${P.agentsHome}/skills/`];
  return bases.some((base) => value.startsWith(base.toLowerCase()));
}

function applyScope(P, built, st, root, opts, extra = {}) {
  if (!opts.codexOnly) return applyActions(P, { actions: built.actions, state: st, root, ...extra, dryRun: opts.dryRun, runClaudeMcp: opts.runClaudeMcp, runClaudePlugin: opts.runClaudePlugin });
  const selected = (p) => isCodexPath(P, root, p);
  const subset = { ...st, created: (st.created || []).filter(selected), copies: Object.fromEntries(Object.entries(st.copies || {}).filter(([p]) => selected(p))) };
  const out = applyActions(P, { actions: built.actions.filter((a) => selected(actionPath(a))), state: subset, root, dryRun: opts.dryRun, readonlySources: true });
  out.created.push(...(st.created || []).filter((p) => !selected(p)));
  Object.assign(out.copies, Object.fromEntries(Object.entries(st.copies || {}).filter(([p]) => !selected(p))));
  out.managedPlugins = st.managedPlugins || [];
  out.mcpNames = st.mcpNames || [];
  out.managedMods = st.managedMods || [];
  return out;
}

function syncProject(P, ctx, opts = {}) {
  return withSyncLock(P, () => syncProjectLocked(P, ctx, opts));
}

function syncProjectLocked(P, ctx, opts) {
  const { reg, items, codexSkills } = ctx;
  let root = opts.root ? norm(path.resolve(opts.root)) : null;
  let name = opts.projectName || null;
  if (!root) {
    const found = pointer.findRoot(opts.cwd || process.cwd());
    if (!found) return { skipped: true, reason: 'no project here', changes: [], warnings: [], conflicts: [] };
    root = found.root;
  }
  const registered = findProject(reg, root);
  if (!name) name = registered ? registered.name : path.basename(root);
  let ptr = pointer.loadPointer(root);
  if (!ptr) {
    if (!(registered || opts.init)) return { skipped: true, reason: 'no pointer', root, name, changes: [], warnings: [], conflicts: [] };
    if (!opts.dryRun && !opts.codexOnly) ptr = pointer.createPointer(root, name); else ptr = { version: 1, name, add: {}, remove: {}, options: {}, outputStyle: null };
    ptr = pointer.loadPointer(root) || ptr;
  }
  const st = state.loadProjectState(root);
  const eff = pointer.effectiveSet(P, reg, ptr, items);
  const built = claude.projectActions(P, reg, items, { root, ptr, eff, prevState: st, codexSkills });
  const hash = planHash(built);
  if (st.inputsHash === hash && !st.incomplete && linksIntact(st, built.actions) && !needsPushBack(st) && !opts.force) {
    return { skipped: true, reason: 'unchanged', root, name, changes: [], warnings: st.warnings || [], conflicts: st.conflicts || [] };
  }
  const gitignoreFile = fs.existsSync(`${root}/.git`) ? `${root}/.gitignore` : null;
  const out = applyScope(P, built, st, root, opts, { gitignoreFile, gitignoreLines: built.gitignoreLines });
  out.warnings.push(...built.warnings, ...eff.unknown.map((u) => `unknown ${u.kind} in pointer: ${u.name}`));
  const result = finish(P, st, opts.codexOnly ? null : hash, out, 'project', name);
  if (!opts.dryRun) state.saveProjectState(root, st);
  return { ...result, root, name };
}

function syncGlobal(P, ctx, opts = {}) {
  return withSyncLock(P, () => syncGlobalLocked(P, ctx, opts));
}

function syncGlobalLocked(P, ctx, opts) {
  const { reg, items, codexSkills } = ctx;
  const st = state.loadGlobalState(P);
  const built = claude.globalActions(P, reg, items, { prevState: st, existingCodexConfig: fsx.readText(`${P.codexHome}/config.toml`) || '', codexSkills });
  const hash = planHash(built);
  if (st.inputsHash === hash && !st.incomplete && linksIntact(st, built.actions) && !needsPushBack(st) && !opts.force) {
    return { skipped: true, reason: 'unchanged', changes: [], warnings: st.warnings || [], conflicts: st.conflicts || [] };
  }
  const out = applyScope(P, built, st, null, opts);
  out.warnings.push(...built.warnings);
  const result = finish(P, st, opts.codexOnly ? null : hash, out, 'global', null);
  if (!opts.dryRun) state.saveGlobalState(P, st);
  return result;
}

module.exports = {
  inputsHash, planHash, loadContext, applyActions, syncProject, syncGlobal, defaultRunClaudeMcp,
  cmdQuote, canonical, claudeMcpCommand, claudeMcpRemoveCommand, claudeCliEntry, findUnsafeForComSpec,
};
