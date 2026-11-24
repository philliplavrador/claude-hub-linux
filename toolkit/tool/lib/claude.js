'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { norm } = require('./paths');
const { readText, readJson, fileHash } = require('./fsx');
const codex = require('./codex');
const { tomlProjectTables, TOML_BEGIN, TOML_END } = require('./managed');
const { resolvePluginKey } = require('./registry');
const { loadPluginSurfaces } = require('./plugin-surfaces');
const { parseFrontmatter } = require('./store');
const { augmentCodexHooks } = require('./hook-bridge');
const modsLib = require('./mods');

const CLAUDE_EVENTS = ['PreToolUse', 'PostToolUse', 'Stop', 'SessionStart', 'SessionEnd', 'UserPromptSubmit', 'SubagentStop', 'Notification', 'PreCompact', 'PermissionRequest'];

function readProbeVerdict(P) {
  return 'junction';
}

function byName(list, names) {
  return names.map((n) => list.find((i) => i.name === n)).filter(Boolean);
}

function listFilesRecursive(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = `${dir}/${e.name}`;
    if (e.isDirectory()) out.push(...listFilesRecursive(p)); else out.push(p);
  }
  return out.sort();
}

function codexSkillActions(dir, name, agentsDir, codexSkills, sources) {
  if (codexSkills !== 'copy') return [{ type: 'junction', link: `${agentsDir}/${name}`, target: dir }];
  const actions = [];
  for (const f of listFilesRecursive(dir)) {
    const rel = f.slice(dir.length + 1);
    actions.push({ type: 'copy', src: f, dest: `${agentsDir}/${name}/${rel}` });
    sources[f] = fileHash(f);
  }
  return actions;
}

function skillActions(P, skill, claudeDir, agentsDir, codexSkills, sources) {
  return [
    { type: 'junction', link: `${claudeDir}/${skill.name}`, target: skill.path },
    ...codexSkillActions(skill.path, skill.name, agentsDir, codexSkills, sources),
  ];
}

function hookSpec(hooks, entryFn) {
  const spec = {};
  for (const h of hooks) {
    const entry = entryFn(h);
    if (!entry) continue;
    for (const ev of codex.eventsOf(h)) (spec[ev] = spec[ev] || []).push(entry);
  }
  if (Object.keys(spec).length === 0) {
    for (const ev of CLAUDE_EVENTS) spec[ev] = [];
  }
  return spec;
}

function pluginSpec(P, wanted, prevManaged) {
  const spec = {};
  for (const k of prevManaged || []) spec[k] = null;
  for (const [k, v] of Object.entries(wanted)) spec[k] = v;
  return spec;
}

function pluginSelection(P, wanted, prevManaged, settingsFiles) {
  const live = Object.assign({}, ...settingsFiles.map((file) => readJson(file, {}).enabledPlugins || {}));
  const spec = pluginSpec(P, wanted, prevManaged);
  for (const key of Object.keys(spec)) if (live[key] === false) spec[key] = false;
  const active = new Set(Object.keys(wanted).filter((key) => wanted[key] && live[key] !== false));
  for (const [key, value] of Object.entries(live)) {
    if (value === true && wanted[key] !== false && !(prevManaged || []).includes(key)) active.add(key);
  }
  const disabled = new Set(Object.keys(wanted).filter((key) => wanted[key] === false));
  for (const [key, value] of Object.entries(live)) if (value === false) disabled.add(key);
  return { spec, active: [...active].sort(), disabled: [...disabled] };
}

function pluginRecord(P, key, root) {
  const records = readJson(`${P.claudeHome}/plugins/installed_plugins.json`, { plugins: {} }).plugins[key] || [];
  const available = records.filter((r) => r.installPath && fs.existsSync(r.installPath));
  const matching = root && available.filter((r) => ['project', 'local'].includes(r.scope) && norm(r.projectPath || '').toLowerCase() === norm(root).toLowerCase());
  return (matching && (matching.find((r) => r.scope === 'local') || matching[0])) || available.find((r) => r.scope === 'user') || available.find((r) => !r.scope) || null;
}

function unmanagedMcpNames(text) {
  const names = new Set();
  let managed = false;
  for (const line of (text || '').split(/\r?\n/)) {
    if (line === TOML_BEGIN) { managed = true; continue; }
    if (line === TOML_END) { managed = false; continue; }
    if (managed) continue;
    const m = /^\s*\[mcp_servers\.(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))(?:\.[^\]]*)?\]\s*(?:#.*)?$/.exec(line);
    if (m) names.add(m[1] === undefined ? (m[2] === undefined ? m[3] : m[2]) : JSON.parse(`"${m[1]}"`));
  }
  return names;
}

function readPluginJson(file, fallback) {
  try { return readJson(file, fallback); }
  catch {
    const error = new Error(`Plugin configuration ${norm(file)}: could not read valid JSON`);
    error.code = 'PLUGIN_CONFIG';
    throw error;
  }
}

function nativePlugins(P, configs) {
  const flags = new Map();
  for (const text of configs) {
    let plugin = null;
    for (const line of (text || '').split(/\r?\n/)) {
      const table = /^\s*\[plugins\.(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\]\s*(?:#.*)?$/.exec(line);
      if (table) plugin = table[1] === undefined ? table[2] : JSON.parse(`"${table[1]}"`);
      else if (/^\s*\[/.test(line)) plugin = null;
      const enabled = /^\s*enabled\s*=\s*(true|false)\s*(?:#.*)?$/.exec(line);
      if (plugin && enabled) flags.set(plugin, enabled[1] === 'true');
    }
  }
  const found = [];
  for (const [key, enabled] of flags) {
    const split = key.lastIndexOf('@');
    const name = key.slice(0, split), market = key.slice(split + 1);
    if (split < 1 || ![name, market].every((v) => /^[A-Za-z0-9_-][A-Za-z0-9._-]*$/.test(v))) continue;
    const cache = `${P.codexHome}/plugins/cache/${market}/${name}`;
    if (!fs.existsSync(cache)) continue;
    const versions = fs.readdirSync(cache).sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
    for (const version of versions) {
      const root = `${cache}/${version}`;
      const manifest = readPluginJson(`${root}/.codex-plugin/plugin.json`, null) || readPluginJson(`${root}/.claude-plugin/plugin.json`, null);
      if (!manifest) continue;
      found.push({ key, name, root, manifest, enabled });
      break;
    }
  }
  return found;
}

const repository = (manifest) => String(typeof manifest.repository === 'object' ? manifest.repository?.url || '' : manifest.repository || '').replace(/\.git\/?$/, '').replace(/\/$/, '').toLowerCase();
function nativeMatches(native, key, manifest) {
  return native.key === key || (native.name === key.slice(0, key.indexOf('@')) && repository(manifest) && repository(native.manifest) === repository(manifest));
}

function nativeSurfaces(native) {
  const result = { skills: new Set(), commands: new Set(), agents: new Set(), mcp: new Set(), hooks: false };
  const resolve = (relative) => {
    if (typeof relative !== 'string') return null;
    const file = norm(path.resolve(native.root, relative));
    const base = norm(path.resolve(native.root));
    if (file !== base && !file.toLowerCase().startsWith(base.toLowerCase() + '/')) return null;
    if (!fs.existsSync(file)) return null;
    const real = norm(fs.realpathSync(file)), realBase = norm(fs.realpathSync(base));
    return real === realBase || real.toLowerCase().startsWith(realBase.toLowerCase() + '/') ? file : null;
  };
  for (const kind of ['skills', 'commands', 'agents']) {
    const seen = new Set();
    const scan = (file, prefix = '') => {
      file = resolve(file);
      if (!file) return;
      const identity = norm(fs.realpathSync(file)).toLowerCase();
      if (seen.has(identity)) return;
      seen.add(identity);
      if (fs.statSync(file).isDirectory()) {
        if (kind === 'skills' && resolve(`${file}/SKILL.md`)) { result.skills.add(path.basename(file)); return; }
        for (const child of fs.readdirSync(file)) scan(`${file}/${child}`, kind === 'skills' ? '' : prefix + (fs.statSync(`${file}/${child}`).isDirectory() ? `${child}--` : ''));
      } else if (kind === 'skills' && path.basename(file) === 'SKILL.md') result.skills.add(path.basename(path.dirname(file)));
      else if (kind !== 'skills' && /\.md$/i.test(file)) result[kind].add(prefix + path.basename(file).replace(/\.md$/i, ''));
    };
    const declaration = native.manifest[kind] === undefined && kind === 'skills' ? './skills' : native.manifest[kind];
    for (const entry of Array.isArray(declaration) ? declaration : [declaration]) { const file = resolve(entry); if (file) scan(file); }
  }
  const readDeclared = (value) => {
    if (typeof value !== 'string') return value;
    const file = resolve(value);
    return file ? readPluginJson(file, null) : null;
  };
  const mcp = native.manifest.mcpServers === undefined ? './.mcp.json' : native.manifest.mcpServers;
  for (const entry of Array.isArray(mcp) ? mcp : [mcp]) {
    const document = readDeclared(entry);
    if (document && typeof document === 'object') for (const name of Object.keys(document.mcpServers || document)) result.mcp.add(name);
  }
  result.hooks = !!readDeclared(native.manifest.hooks === undefined ? './hooks/hooks.json' : native.manifest.hooks);
  return result;
}

function pluginActions(P, keys, { root, agentsDir, codexDir, codexSkills, sources, claimedSkills, claimedAgents, warnings, natives = [] }) {
  const actions = [], mcp = [], pluginHooks = [];
  const pluginVersions = {};
  for (const key of keys) {
    const record = pluginRecord(P, key, root);
    if (!record) continue;
    const installPath = norm(record.installPath);
    const surfaces = loadPluginSurfaces(installPath);
    const manifest = readPluginJson(`${installPath}/.claude-plugin/plugin.json`, {}) || {};
    const native = natives.filter((n) => n.enabled && nativeMatches(n, key, manifest)).map(nativeSurfaces);
    const provided = (kind, name) => native.some((n) => n[kind].has(name));
    if (record.version != null) pluginVersions[key] = record.version;
    warnings.push(...surfaces.warnings.map((w) => `plugin ${key}: ${w}`));
    warnings.push(...surfaces.unsupported.filter((w) => !/hooks/i.test(w)).map((w) => `plugin ${key}: ${w}`));
    if (Object.keys(surfaces.hooks).length && !native.some((n) => n.hooks)) pluginHooks.push({ key, installPath, hooks: surfaces.hooks });
    mcp.push(...surfaces.mcp.filter((m) => !provided('mcp', m.name)));
    const pluginName = key.slice(0, key.indexOf('@'));
    if (!/^[A-Za-z0-9._-]+$/.test(pluginName)) {
      warnings.push(`plugin ${key}: its name is not a plain slug, so its skills are not mirrored for Codex`);
      continue;
    }
    const claim = (name, claimed, kind) => {
      if (!/^[A-Za-z0-9._-]+$/.test(name)) { warnings.push(`plugin ${key}: ${kind} name is not a plain slug`); return null; }
      const mirror = `${pluginName}--${name}`;
      if (claimed.has(mirror.toLowerCase())) { warnings.push(`plugin ${key}: ${mirror} is already claimed; not mirrored`); return null; }
      claimed.add(mirror.toLowerCase());
      return mirror;
    };
    for (const skill of surfaces.skills) {
      if (provided('skills', skill.name)) continue;
      const mirror = claim(skill.name, claimedSkills, 'skill');
      if (mirror) actions.push(...codexSkillActions(skill.path, mirror, agentsDir, codexSkills, sources));
    }
    for (const command of surfaces.commands) {
      if (provided('commands', command.name)) continue;
      const mirror = claim(command.name, claimedSkills, 'command');
      if (!mirror) continue;
      sources[command.path] = fileHash(command.path);
      const { data } = parseFrontmatter(readText(command.path));
      const description = data.description && !['>', '|'].includes(data.description) ? data.description : `Run the ${command.name} command from ${pluginName}`;
      actions.push({ type: 'file', dest: `${agentsDir}/${mirror}/SKILL.md`, content: `---\nname: ${mirror}\ndescription: ${JSON.stringify(description)}\n---\n\nRead and follow the original command at [${command.name}](${command.path}).\nResolve its relative resource paths against ${norm(path.dirname(command.path))}.\nThe plugin root is ${installPath}; use that absolute path for any CLAUDE_PLUGIN_ROOT reference.\nUse the current host's equivalent tools when the command names Claude tools.\n` });
    }
    for (const agent of surfaces.agents) {
      if (provided('agents', agent.name)) continue;
      const mirror = claim(agent.name, claimedAgents, 'agent');
      if (!mirror) continue;
      sources[agent.path] = fileHash(agent.path);
      const original = readText(agent.path);
      const { body } = parseFrontmatter(original);
      const context = `\n\nOriginal agent: ${agent.path}\nResolve relative resources against ${norm(path.dirname(agent.path))}.\nPlugin root: ${installPath}.\n`;
      actions.push({ type: 'file', dest: `${codexDir}/agents/${mirror}.toml`, content: codex.agentMdToToml(mirror, original.slice(0, original.length - body.length) + body + context, agent.path) });
    }
  }
  return { actions, mcp, pluginHooks, pluginVersions };
}

function mergeCodexMcp(store, plugins, unmanaged, warnings) {
  const names = new Set();
  const result = [];
  for (const m of [...store, ...plugins]) {
    if (unmanaged.has(m.name)) { warnings.push(`MCP server '${m.name}' has an unmanaged native Codex entry; preserved it`); continue; }
    if (names.has(m.name)) { warnings.push(`plugin MCP server '${m.name}' clashes with a store or earlier plugin server; the earlier server wins`); continue; }
    names.add(m.name); result.push(m);
  }
  return result;
}

function projectActions(P, reg, items, { root, ptr, eff, prevState, codexSkills }) {
  root = norm(root);
  const actions = [];
  const warnings = [];
  const sources = {};
  const rel = (p) => norm(p).slice(root.length + 1);
  const gitignore = new Set(['.claude/settings.local.json', '.agents/toolkit-state.json', '.agents/trail.md', '.agents/fit.json']);

  for (const skill of byName(items.skills, eff.skills)) {
    const acts = skillActions(P, skill, `${root}/.claude/skills`, `${root}/.agents/skills`, codexSkills, sources);
    actions.push(...acts);
    for (const a of acts) gitignore.add(a.type === 'junction' ? `${rel(a.link)}/` : rel(a.dest));
  }
  const packs = byName(items.rules, eff.rules);
  for (const pack of packs) {
    actions.push({ type: 'junction', link: `${root}/.claude/rules/${pack.name}`, target: pack.path });
    gitignore.add(`.claude/rules/${pack.name}/`);
    for (const f of pack.files) sources[f] = fileHash(f);
  }
  for (const agent of byName(items.agents, eff.agents)) {
    actions.push({ type: 'copy', src: agent.path, dest: `${root}/.claude/agents/${agent.name}.md` });
    gitignore.add(`.claude/agents/${agent.name}.md`);
    sources[agent.path] = fileHash(agent.path);
    actions.push({ type: 'file', dest: `${root}/.codex/agents/${agent.name}.toml`, content: codex.agentMdToToml(agent.name, readText(agent.path), `agents/${agent.name}.md`) });
    gitignore.add(`.codex/agents/${agent.name}.toml`);
  }
  const style = eff.outputStyle ? items['output-styles'].find((s) => s.name === eff.outputStyle) : null;
  if (style) {
    actions.push({ type: 'copy', src: style.path, dest: `${root}/.claude/output-styles/${style.name}.md` });
    gitignore.add(`.claude/output-styles/${style.name}.md`);
    sources[style.path] = fileHash(style.path);
  }
  const hooks = byName(items.hooks, eff.hooks);
  const mcp = byName(items.mcp, eff.mcp);
  for (const h of hooks) {
    sources[`${h.path}/hook.json`] = fileHash(`${h.path}/hook.json`);
    sources[h.script] = fileHash(h.script);
  }
  for (const m of mcp) sources[m.path] = fileHash(m.path);

  const configFile = `${root}/.codex/config.toml`;
  const existingConfig = readText(configFile) || '';
  const ownConfig = /^# Generated by Toolkit/m.test(existingConfig) && !existingConfig.includes(TOML_BEGIN);
  const natives = nativePlugins(P, [readText(`${P.codexHome}/config.toml`), existingConfig]);
  const selected = pluginSelection(P, eff.plugins, prevState.managedPlugins, [`${root}/.claude/settings.json`, `${root}/.claude/settings.local.json`]);
  const mirrored = pluginActions(P, selected.active, { root, agentsDir: `${root}/.agents/skills`, codexDir: `${root}/.codex`, codexSkills, sources, claimedSkills: new Set(eff.skills.map((n) => n.toLowerCase())), claimedAgents: new Set(eff.agents.map((n) => n.toLowerCase())), warnings, natives });
  actions.push(...mirrored.actions);
  for (const a of mirrored.actions) gitignore.add(a.type === 'junction' ? `${rel(a.link)}/` : rel(a.dest));

  actions.push({
    type: 'json',
    file: `${root}/.claude/settings.local.json`,
    spec: {
      outputStyle: style ? style.name : null,
      enabledPlugins: selected.spec,
      hooks: hookSpec(hooks, codex.hookEntryClaude),
    },
  });
  const servers = {};
  for (const m of mcp) servers[m.name] = m.server;
  actions.push({ type: 'mcpjson', file: `${root}/.mcp.json`, servers, managedNames: (prevState.mcpNames || []).filter((n) => !servers[n]) });

  if (hooks.length) {
    actions.push({ type: 'file', dest: `${root}/.codex/hooks.json`, content: JSON.stringify(codex.generateHooksJson(hooks, hooks.map((h) => `hooks/${h.name}`)), null, 2) + '\n' });
    gitignore.add('.codex/hooks.json');
  }
  const projectMcp = mcp.map((m) => /\$\{[^}]+\}/.test(JSON.stringify({ command: m.server.command, args: m.server.args, env: m.server.env })) ? { ...m, server: { command: 'node', args: [`${P.store}/hooks/mcp-bridge/mcp-bridge.js`, '--project', root, '--server', m.name] } } : m);
  const codexMcp = mergeCodexMcp(projectMcp, mirrored.mcp, ownConfig ? new Set() : unmanagedMcpNames(existingConfig), warnings);
  const nativeOff = new Set();
  for (const key of selected.disabled) {
    const record = pluginRecord(P, key, root);
    const manifest = record ? readPluginJson(`${norm(record.installPath)}/.claude-plugin/plugin.json`, {}) : {};
    for (const native of natives) if (nativeMatches(native, key, manifest)) nativeOff.add(native.key);
  }
  if (codexMcp.length || nativeOff.size || existingConfig.includes(TOML_BEGIN) || ownConfig || Object.hasOwn(prevState.tomlRegions || {}, configFile)) {
    const content = (codexMcp.length ? codex.generateCodexConfig({ mcp: codexMcp, sources: codexMcp.map((m) => `mcp/${m.name}`) }) : '') + [...nativeOff].map((key) => `\n[plugins.${JSON.stringify(key)}]\nenabled = false\n`).join('');
    actions.push({ type: 'tomlRegion', file: configFile, body: content, ...(ownConfig ? { legacyBody: existingConfig } : {}) });
    gitignore.add('.codex/config.toml');
  }
  const gitignoreLines = [...gitignore].sort();
  const built = augmentCodexHooks(P, { actions, gitignoreLines, pluginHooks: mirrored.pluginHooks, inputs: { eff, ptr, sources, gitignore: gitignoreLines, pluginVersions: mirrored.pluginVersions, pluginHooks: mirrored.pluginHooks }, warnings }, { root });
  built.inputs.gitignore = built.gitignoreLines;
  return built;
}

function globalActions(P, reg, items, { prevState, existingCodexConfig, codexSkills }) {
  const d = reg.defaults;
  const actions = [];
  const warnings = [];
  const sources = {};
  const codexSkillNames = new Set();
  const agentsSkillsDir = `${P.agentsHome}/skills`;
  for (const skill of byName(items.skills, d.skills)) {
    actions.push(...skillActions(P, skill, `${P.claudeHome}/skills`, agentsSkillsDir, codexSkills, sources));
    codexSkillNames.add(skill.name.toLowerCase());
  }
  const plugins = {};
  for (const n of d.plugins) {
    try { plugins[resolvePluginKey(P, n)] = true; } catch { warnings.push(`default plugin unknown: ${n}`); }
  }
  const mods = modsLib.wantedMods(P, reg, warnings);
  for (const m of mods) plugins[m.key] = true;
  const selected = pluginSelection(P, plugins, prevState.managedPlugins, [`${P.claudeHome}/settings.json`]);
  const mirrored = pluginActions(P, selected.active, { root: null, agentsDir: agentsSkillsDir, codexDir: P.codexHome, codexSkills, sources, claimedSkills: codexSkillNames, claimedAgents: new Set(d.agents.map((n) => n.toLowerCase())), warnings, natives: nativePlugins(P, [existingCodexConfig]) });
  actions.push(...mirrored.actions);
  const { pluginVersions, pluginHooks } = mirrored;
  const packs = byName(items.rules, d.rules);
  for (const pack of packs) {
    for (const f of pack.files) {
      actions.push({ type: 'copy', src: f, dest: `${P.claudeHome}/rules/toolkit/${pack.name}/${path.basename(f)}` });
      sources[f] = fileHash(f);
    }
  }
  for (const agent of byName(items.agents, d.agents)) {
    actions.push({ type: 'copy', src: agent.path, dest: `${P.claudeHome}/agents/${agent.name}.md` });
    sources[agent.path] = fileHash(agent.path);
    actions.push({ type: 'file', dest: `${P.codexHome}/agents/${agent.name}.toml`, content: codex.agentMdToToml(agent.name, readText(agent.path), `agents/${agent.name}.md`) });
  }
  const style = d.outputStyle ? items['output-styles'].find((s) => s.name === d.outputStyle) : null;
  if (style) {
    actions.push({ type: 'copy', src: style.path, dest: `${P.claudeHome}/output-styles/${style.name}.md` });
    sources[style.path] = fileHash(style.path);
  }
  const hooks = byName(items.hooks, d.hooks);
  const mcp = byName(items.mcp, d.mcp);
  for (const h of hooks) {
    sources[`${h.path}/hook.json`] = fileHash(`${h.path}/hook.json`);
    sources[h.script] = fileHash(h.script);
  }
  for (const m of mcp) sources[m.path] = fileHash(m.path);
  const liveEnabled = readJson(`${P.claudeHome}/settings.json`, {}).enabledPlugins || {};
  actions.push(...modsLib.modActions(P, mods, prevState.managedMods, liveEnabled, modsLib.listedModKeys(reg)));
  actions.push({ type: 'json', file: `${P.claudeHome}/settings.json`, spec: { outputStyle: style ? style.name : null, enabledPlugins: selected.spec, hooks: hookSpec(hooks, codex.hookEntryClaude) } });
  actions.push({ type: 'json', file: `${P.codexHome}/hooks.json`, spec: { hooks: hookSpec(hooks, codex.hookEntryCodex) } });
  for (const m of mcp) actions.push({ type: 'claudeMcp', name: m.name, server: m.server });
  for (const n of prevState.mcpNames || []) {
    if (!mcp.some((m) => m.name === n)) actions.push({ type: 'claudeMcp', name: n, server: null });
  }
  const existingTables = new Set(tomlProjectTables(existingCodexConfig || '').map((p) => norm(p).toLowerCase()));
  const projects = Object.values(reg.projects).map((p) => p.path).filter((p) => !existingTables.has(norm(p).toLowerCase()));
  const codexMcp = mergeCodexMcp(mcp, mirrored.mcp, unmanagedMcpNames(existingCodexConfig), warnings);
  actions.push({ type: 'tomlRegion', file: `${P.codexHome}/config.toml`, body: codex.generateCodexRegion({ projectDocMaxBytes: 131072, projectDocFallbackFilenames: ['CLAUDE.md'], projects, mcp: codexMcp }) });
  const codexOwn = `${P.store}/codex/AGENTS.md`;
  const codexOwnText = readText(codexOwn);
  if (codexOwnText != null) sources[codexOwn] = fileHash(codexOwn);
  const agentsSources = [...(codexOwnText != null ? ['codex/AGENTS.md'] : []), ...packs.map((p) => `rules/${p.name}`), ...(style ? [`output-styles/${style.name}`] : [])];
  actions.push({
    type: 'file',
    dest: `${P.codexHome}/AGENTS.md`,
    content: codex.generateAgentsMd({
      sources: agentsSources.length ? agentsSources : ['(no default rules or style yet)'],
      claudeMd: codexOwnText,
      packs: packs.map((p) => ({ name: p.name, files: p.files.map((f) => ({ name: path.basename(f), text: readText(f) })) })),
      style: style ? { name: style.name, text: readText(style.path) } : null,
      trail: true,
      store: P.store,
    }),
  });
  const modInputs = mods.length || (prevState.managedMods || []).length
    ? { installedMods: modsLib.installedModKeys(P), modMarketplace: modsLib.marketplaceSource(P, modsLib.MOD_MARKETPLACE, warnings) }
    : {};
  return augmentCodexHooks(P, { actions, pluginHooks, inputs: { defaults: d, projects: reg.projects, sources, codexProjectTables: [...existingTables].sort(), pluginVersions, pluginHooks, ...modInputs }, warnings });
}

function withPluginRoot(server, installPath) {
  const sub = (v) => (typeof v === 'string' ? v.split('${CLAUDE_PLUGIN_ROOT}').join(installPath) : v);
  const out = { ...server };
  if (typeof out.command === 'string') out.command = sub(out.command);
  if (Array.isArray(out.args)) out.args = out.args.map(sub);
  if (out.env && typeof out.env === 'object') out.env = Object.fromEntries(Object.entries(out.env).map(([k, v]) => [k, sub(v)]));
  if (typeof out.cwd === 'string') out.cwd = sub(out.cwd);
  return out;
}

module.exports = { CLAUDE_EVENTS, readProbeVerdict, projectActions, globalActions, withPluginRoot };
