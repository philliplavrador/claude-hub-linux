'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { norm } = require('./paths');

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function configError(file, detail) {
  const err = new Error(`Plugin configuration ${norm(file)}: ${detail}`);
  err.code = 'PLUGIN_CONFIG';
  return err;
}

function expandRoot(value, root) {
  if (typeof value === 'string') return value.split('${CLAUDE_PLUGIN_ROOT}').join(root);
  if (Array.isArray(value)) return value.map((x) => expandRoot(x, root));
  if (isObject(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expandRoot(v, root)]));
  return value;
}

function loadPluginSurfaces(installPath) {
  const root = path.resolve(installPath);
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) throw configError(root, 'plugin directory is missing');
  const realRoot = fs.realpathSync(root);
  const out = { skills: [], commands: [], agents: [], mcp: [], hooks: {}, modules: [], unsupported: [], warnings: [] };
  const within = (base, target) => {
    const rel = path.relative(base, target);
    return rel === '' || (!path.isAbsolute(rel) && rel !== '..' && !rel.startsWith(`..${path.sep}`));
  };
  const resolve = (value, required = true) => {
    if (typeof value !== 'string' || !value.trim()) throw configError(root, 'expected a nonempty plugin-relative path');
    const target = path.resolve(root, expandRoot(value, norm(root)).replace(/[\\/]/g, path.sep));
    if (!within(root, target)) throw configError(root, 'declared path escapes the plugin directory');
    if (!fs.existsSync(target)) {
      if (required) throw configError(target, 'declared path is missing');
      return null;
    }
    if (!within(realRoot, fs.realpathSync(target))) throw configError(target, 'resolved path escapes the plugin directory');
    return target;
  };
  const json = (file) => {
    let value;
    try { value = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); }
    catch { throw configError(file, 'could not read valid JSON'); }
    if (!isObject(value)) throw configError(file, 'expected a JSON object');
    return value;
  };
  const manifestFile = resolve('.claude-plugin/plugin.json', false);
  const manifest = manifestFile ? json(manifestFile) : {};
  const paths = (value, label) => {
    if (value === undefined) return [];
    const values = Array.isArray(value) ? value : [value];
    if (values.some((v) => typeof v !== 'string')) throw configError(manifestFile || root, `${label} must contain paths`);
    return values.map((v) => resolve(v));
  };

  for (const kind of ['skills', 'commands', 'agents']) {
    const seenPaths = new Set();
    const seenDirs = new Set();
    const names = new Set();
    const add = (name, target) => {
      const canonical = fs.realpathSync(target).toLowerCase();
      if (seenPaths.has(canonical)) return;
      seenPaths.add(canonical);
      if (names.has(name.toLowerCase())) {
        out.warnings.push(`Duplicate ${kind} name '${name}' ignored`);
        return;
      }
      names.add(name.toLowerCase());
      out[kind].push({ name, path: norm(target) });
    };
    const visit = (file, prefix = '') => {
      const target = resolve(file);
      const info = fs.statSync(target);
      if (info.isFile()) {
        if (kind === 'skills') {
          if (path.basename(target) !== 'SKILL.md') throw configError(target, 'skill file must be named SKILL.md');
          add(path.basename(path.dirname(target)), path.dirname(target));
        } else if (/\.md$/i.test(target)) {
          add(prefix + path.basename(target).replace(/\.md$/i, ''), target);
        } else throw configError(target, `${kind} file must be Markdown`);
        return;
      }
      const canonical = fs.realpathSync(target).toLowerCase();
      if (seenDirs.has(canonical)) return;
      seenDirs.add(canonical);
      if (kind === 'skills' && resolve(path.join(target, 'SKILL.md'), false)) {
        add(path.basename(target), target);
        return;
      }
      for (const entry of fs.readdirSync(target, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const child = resolve(path.join(target, entry.name));
        if (fs.statSync(child).isDirectory()) visit(child, kind === 'skills' ? '' : `${prefix}${entry.name}--`);
        else if (kind !== 'skills' && /\.md$/i.test(entry.name)) visit(child, prefix);
      }
    };
    const standard = resolve(kind, false);
    if (standard) visit(standard);
    for (const declared of paths(manifest[kind], kind)) visit(declared);
  }

  const servers = new Map();
  const addMcp = (value, source) => {
    if (!isObject(value)) throw configError(source, 'MCP servers must be an object');
    const map = Object.hasOwn(value, 'mcpServers') ? value.mcpServers : value;
    if (!isObject(map)) throw configError(source, 'MCP servers must be an object');
    for (const [name, definition] of Object.entries(map)) {
      if (!isObject(definition) || !((typeof definition.command === 'string' && definition.command.trim()) || (typeof definition.url === 'string' && definition.url.trim()))) {
        throw configError(source, `MCP server '${name}' needs a command or URL`);
      }
      if (definition.args !== undefined && (!Array.isArray(definition.args) || definition.args.some((v) => typeof v !== 'string'))) throw configError(source, `MCP server '${name}' arguments must be strings`);
      const server = expandRoot(definition, norm(root));
      if (servers.has(name) && JSON.stringify(servers.get(name)) !== JSON.stringify(server)) out.warnings.push(`MCP server '${name}' overrides an earlier plugin definition`);
      servers.set(name, server);
    }
  };
  const readConfigEntries = (value, add) => {
    for (const entry of Array.isArray(value) ? value : [value]) {
      if (typeof entry === 'string') {
        const file = resolve(entry);
        add(json(file), file);
      } else add(entry, manifestFile || root);
    }
  };
  const mcpFile = resolve('.mcp.json', false);
  if (mcpFile) addMcp(json(mcpFile), mcpFile);
  if (manifest.mcpServers !== undefined) readConfigEntries(manifest.mcpServers, addMcp);
  out.mcp = [...servers].map(([name, server]) => ({ name, server }));

  const hookEntries = new Set();
  const addModules = (list, source) => {
    if (!Array.isArray(list) || list.some((m) => typeof m !== 'string' || !m.trim())) throw configError(source, 'hook modules must be a list of paths');
    for (const m of list) out.modules.push(norm(resolve(path.relative(root, path.resolve(path.dirname(source), m)))));
  };
  const addHooks = (value, source) => {
    if (isObject(value) && Object.hasOwn(value, 'modules')) {
      addModules(value.modules, source);
      if (!Object.hasOwn(value, 'hooks')) return;
    }
    const hooks = isObject(value) && Object.hasOwn(value, 'hooks') ? value.hooks : value;
    if (!isObject(hooks)) throw configError(source, 'hooks must be an event map');
    for (const [event, entries] of Object.entries(hooks)) {
      if (!Array.isArray(entries)) throw configError(source, `hook event '${event}' must contain an array`);
      for (const entry of entries) {
        if (!isObject(entry)) throw configError(source, `hook event '${event}' contains an invalid entry`);
        const expanded = expandRoot(entry, norm(root));
        const signature = JSON.stringify([event, expanded]);
        if (hookEntries.has(signature)) continue;
        hookEntries.add(signature);
        if (!Object.hasOwn(out.hooks, event)) Object.defineProperty(out.hooks, event, { value: [], enumerable: true });
        out.hooks[event].push(expanded);
      }
    }
  };
  const hookFile = resolve('hooks/hooks.json', false);
  if (hookFile) addHooks(json(hookFile), hookFile);
  if (manifest.hooks !== undefined) readConfigEntries(manifest.hooks, addHooks);
  if (Object.keys(out.hooks).length) out.unsupported.push('Plugin hooks require a host-specific adapter');
  if (out.modules.length) out.unsupported.push('Plugin function hooks (modules) run only in Claude Code');
  if (manifest.lspServers !== undefined || resolve('.lsp.json', false)) out.unsupported.push('Plugin LSP servers require host-specific support');
  return out;
}

module.exports = { loadPluginSurfaces };
