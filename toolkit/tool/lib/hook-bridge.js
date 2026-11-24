'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { norm, same } = require('./paths');
const { commandFor } = require('./codex');

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const supported = new Set(['PreToolUse', 'PostToolUse']);

function configError(label) {
  const error = new Error(`Cannot plan Codex hooks: ${label} is not valid hook configuration.`);
  error.code = 'HOOK_BRIDGE_CONFIG';
  return error;
}

function readJson(file, label, hashes) {
  if (!fs.existsSync(file)) return null;
  let text, data;
  try {
    text = fs.readFileSync(file, 'utf8');
    data = JSON.parse(text.replace(/^\uFEFF/, ''));
  } catch { throw configError(label); }
  if (!object(data)) throw configError(label);
  hashes[norm(file)] = createHash('sha256').update(text).digest('hex');
  return data;
}

function parseNodeCommand(command, base, projectRoot) {
  if (typeof command !== 'string') return null;
  if (projectRoot) command = command.replace(/\$\{CLAUDE_PROJECT_DIR\}|\$CLAUDE_PROJECT_DIR\b|%CLAUDE_PROJECT_DIR%/g, projectRoot);
  if (/[\0\r\n`$%;&|<>]/.test(command)) return null;
  const tokens = [];
  let cursor = 0;
  while (cursor < command.length) {
    while (/\s/.test(command[cursor] || '') && cursor < command.length) cursor++;
    if (cursor === command.length) break;
    const quote = ['"', "'"].includes(command[cursor]) ? command[cursor++] : null;
    let value = '';
    if (quote) {
      while (cursor < command.length && command[cursor] !== quote) value += command[cursor++];
      if (command[cursor++] !== quote || (cursor < command.length && !/\s/.test(command[cursor]))) return null;
    } else {
      while (cursor < command.length && !/\s/.test(command[cursor])) value += command[cursor++];
      if (/["'()]/.test(value)) return null;
    }
    if (/["']/.test(value)) return null;
    tokens.push(value);
  }
  if (tokens.length < 2 || !/^node(?:\.exe)?$/i.test(path.basename(tokens[0]))) return null;
  if (tokens[1].startsWith('-') || !/\.(?:[cm]?js)$/i.test(tokens[1])) return null;
  if (!projectRoot && !path.isAbsolute(tokens[1])) return null;
  const script = norm(path.resolve(base, tokens[1]));
  try { if (!fs.statSync(script).isFile()) return null; } catch { return null; }
  return { script, args: tokens.slice(2) };
}

function codexMatcher(matcher) {
  if (matcher === undefined || matcher === null || matcher === '' || matcher === '*') return matcher || undefined;
  if (typeof matcher !== 'string' || /\(\?[=!<]|\\[1-9]/.test(matcher)) throw new Error('unsupported matcher');
  const regex = new RegExp(matcher);
  const extra = [];
  if ((regex.test('Write') || regex.test('Edit')) && !regex.test('apply_patch')) extra.push('^apply_patch$');
  if (regex.test('PowerShell') && !regex.test('Bash')) extra.push('^Bash$');
  return extra.length ? `(?:${matcher})|${extra.join('|')}` : matcher;
}

function quote(value) {
  if (/[\0\r\n"`$%]/.test(value)) throw new Error('unsafe command argument');
  return `"${value}"`;
}

function isShelfLauncher(P, command, hashes) {
  if (typeof command !== 'string') return false;
  const fold = process.platform === 'win32' ? (text) => text.toLowerCase() : (text) => text;
  const store = norm(P.store);
  const prefix = `/bin/sh "${store}/hooks/lib/guard-launch.sh" "${store}/hooks/`;
  if (!fold(command).startsWith(fold(prefix))) return false;
  const named = /^([A-Za-z0-9][A-Za-z0-9._-]*)\//.exec(command.slice(prefix.length));
  if (!named || named[1] === 'lib') return false;
  const name = named[1];
  let meta;
  try { meta = readJson(`${store}/hooks/${name}/hook.json`, 'hook metadata', hashes); } catch { return false; }
  if (!meta) return false;
  const args = Array.isArray(meta.args) ? meta.args.map(String) : [];
  const expected = commandFor({ name, script: `${store}/hooks/${name}/${name}.js`, meta: { ...meta, args, runtime: 'bun' } }, { claude: true });
  return fold(command) === fold(expected);
}

function isExcluded(P, script, hashes) {
  const candidates = [path.join(path.dirname(script), 'hook.json')];
  if (same(path.dirname(script), `${P.claudeHome}/tools`)) {
    candidates.push(`${P.store}/hooks/${path.basename(script).replace(/\.[^.]+$/, '')}/hook.json`);
  }
  for (const candidate of candidates) {
    const meta = readJson(candidate, 'hook metadata', hashes);
    if (meta?.codex === false || meta?.meta?.codex === false) return true;
  }
  return false;
}

function augmentCodexHooks(P, built, { root } = {}) {
  if (!object(built) || !Array.isArray(built.actions)) throw configError('action plan');
  root = root ? norm(path.resolve(root)) : null;
  const base = root || P.claudeHome;
  const target = root ? `${root}/.codex/hooks.json` : `${P.codexHome}/hooks.json`;
  const wrapper = norm(path.resolve(P.store, 'hooks/codex-bridge/codex-bridge.js'));
  const hashes = {};
  const warnings = [];
  const addWarning = message => { if (!warnings.includes(message)) warnings.push(message); };
  const files = root
    ? [[`${root}/.claude/settings.json`, 'project settings'], [`${root}/.claude/settings.local.json`, 'project local settings']]
    : [[`${P.claudeHome}/settings.json`, 'user settings']];
  const existing = built.actions.find(action => same(action.dest || action.file, target));
  let doc = { description: 'Generated by Toolkit from portable Claude hooks. Edit the source hooks, not this file.', hooks: {} };
  if (existing) {
    try { doc = existing.type === 'file' ? JSON.parse(existing.content) : structuredClone(existing.spec); }
    catch { throw configError('generated Codex hooks'); }
    if (!object(doc) || (doc.hooks !== undefined && !object(doc.hooks))) throw configError('generated Codex hooks');
    doc.hooks ??= {};
  }
  let added = 0;
  for (const [file, label] of files) {
    const data = readJson(file, label, hashes);
    if (!data || data.hooks === undefined) continue;
    if (!object(data.hooks)) throw configError(label);
    for (const [event, groups] of Object.entries(data.hooks)) {
      if (!Array.isArray(groups)) throw configError(label);
      if (!supported.has(event)) {
        if (groups.length) addWarning(`Codex hook bridge skipped lifecycle or permission hooks in ${label}; these need an explicit host-specific adapter.`);
        continue;
      }
      for (const group of groups) {
        if (!object(group) || !Array.isArray(group.hooks)) throw configError(label);
        if (group.codex === false || group.meta?.codex === false) continue;
        let matcher;
        try { matcher = codexMatcher(group.matcher); }
        catch { addWarning(`Codex hook bridge skipped an unsupported ${event} matcher in ${label}.`); continue; }
        for (const hook of group.hooks) {
          if (!object(hook)) throw configError(label);
          if (hook.codex === false || hook.meta?.codex === false) continue;
          if (hook.type !== 'command' || hook.async === true) {
            addWarning(`Codex hook bridge skipped a non-command or asynchronous ${event} hook in ${label}.`);
            continue;
          }
          if (isShelfLauncher(P, hook.command, hashes)) continue;
          const parsed = parseNodeCommand(hook.command, base, root);
          if (!parsed) {
            addWarning(`Codex hook bridge skipped an unsupported command or missing Node script for ${event} in ${label}.`);
            continue;
          }
          if (norm(parsed.script).toLowerCase().startsWith(`${norm(P.store).toLowerCase()}/hooks/`)) continue;
          if (isExcluded(P, parsed.script, hashes)) continue;
          if (hook.timeout !== undefined && (!Number.isFinite(hook.timeout) || hook.timeout <= 0)) {
            addWarning(`Codex hook bridge skipped an invalid ${event} timeout in ${label}.`);
            continue;
          }
          const duplicateSource = (doc.hooks[event] || []).some(entry => (entry.hooks || []).some(other => {
            const command = parseNodeCommand(other.command, base, root);
            return command && same(command.script, parsed.script) && JSON.stringify(command.args) === JSON.stringify(parsed.args);
          }));
          if (duplicateSource) continue;
          let command;
          try { command = 'node ' + [wrapper, parsed.script, ...parsed.args].map(quote).join(' '); }
          catch { addWarning(`Codex hook bridge skipped unsafe quoting for ${event} in ${label}.`); continue; }
          const child = { type: 'command', command };
          if (hook.timeout !== undefined) child.timeout = hook.timeout;
          const entry = { ...(matcher === undefined ? {} : { matcher }), hooks: [child] };
          const entries = doc.hooks[event] || [];
          if (entries.some(other => JSON.stringify(other) === JSON.stringify(entry))) continue;
          doc.hooks[event] = [...entries, entry];
          added++;
        }
      }
    }
  }
  for (const plugin of built.pluginHooks || []) {
    if (object(plugin.hooks) && Object.keys(plugin.hooks).length) {
      addWarning('Codex hook bridge leaves plugin hooks to native plugin import; plugin lifecycle integrations require separate compatibility review.');
    }
  }
  if (added) {
    try { if (!fs.statSync(wrapper).isFile()) throw new Error(); }
    catch { throw configError('hook bridge script'); }
    if (existing) {
      if (existing.type === 'file') existing.content = JSON.stringify(doc, null, 2) + '\n';
      else existing.spec = doc;
    } else if (root) {
      built.actions.push({ type: 'file', dest: target, content: JSON.stringify(doc, null, 2) + '\n' });
    } else {
      built.actions.push({ type: 'json', file: target, spec: { hooks: doc.hooks } });
    }
    if (root) built.gitignoreLines = [...new Set([...(built.gitignoreLines || []), '.codex/hooks.json'])].sort();
  }
  built.inputs ??= {};
  built.inputs.portableHookWarnings = warnings;
  built.warnings = [...new Set([...(built.warnings || []), ...warnings])];
  return built;
}

module.exports = { augmentCodexHooks, parseNodeCommand, codexMatcher };
