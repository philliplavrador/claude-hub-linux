'use strict';
const fs = require('node:fs');
const { readJson, writeJson } = require('./fsx');
const { norm, same } = require('./paths');

const EMPTY_DEFAULTS = () => ({ skills: [], rules: [], agents: [], outputStyle: null, hooks: [], mcp: [], plugins: [], mods: [] });

const MOD_MARKETPLACE = 'claude-mods';

function loadRegistry(P) {
  if (!fs.existsSync(P.registryFile)) throw new Error(`Registry not found: ${P.registryFile} (toolkit.json)`);
  const raw = readJson(P.registryFile, null);
  const reg = {
    version: raw.version || 1,
    projectsRoot: norm(raw.projectsRoot || `${require('node:os').homedir()}/Projects`),
    projects: {},
    defaults: { ...EMPTY_DEFAULTS(), ...(raw.defaults || {}) },
    externalHooks: raw.externalHooks || {},
    pluginHints: raw.pluginHints || {},
  };
  for (const [name, p] of Object.entries(raw.projects || {})) {
    reg.projects[name] = { ...p, path: norm(p.path) };
  }
  for (const k of ['skills', 'rules', 'agents', 'hooks', 'mcp', 'plugins', 'mods']) {
    if (!Array.isArray(reg.defaults[k])) reg.defaults[k] = [];
  }
  return reg;
}

function saveRegistry(P, reg) {
  writeJson(P.registryFile, reg);
}

function installedPlugins(P) {
  const file = `${P.claudeHome}/plugins/installed_plugins.json`;
  const data = readJson(file, { plugins: {} });
  return Object.keys(data.plugins || {}).map((key) => {
    const at = key.indexOf('@');
    return { key, name: key.slice(0, at), marketplace: key.slice(at + 1) };
  });
}

function pluginHints(reg) {
  return reg.pluginHints || {};
}

function pluginInstallPath(P, key) {
  const file = `${P.claudeHome}/plugins/installed_plugins.json`;
  const data = readJson(file, { plugins: {} });
  const records = (data.plugins || {})[key];
  if (!Array.isArray(records) || !records.length) return null;
  const installPath = records[0].installPath;
  if (!installPath) return null;
  const p = norm(installPath);
  return fs.existsSync(p) ? p : null;
}

function pluginVersion(P, key) {
  const file = `${P.claudeHome}/plugins/installed_plugins.json`;
  const data = readJson(file, { plugins: {} });
  const records = (data.plugins || {})[key];
  if (!Array.isArray(records) || !records.length) return null;
  return records[0].version || null;
}

function resolvePluginKey(P, name) {
  const all = installedPlugins(P);
  if (name.includes('@')) {
    if (all.some((p) => p.key === name)) return name;
    const err = new Error(`Plugin not installed: ${name}`); err.code = 'PLUGIN_UNKNOWN'; throw err;
  }
  const hits = all.filter((p) => p.name === name);
  if (hits.length === 1) return hits[0].key;
  if (hits.length === 0) { const err = new Error(`Plugin not installed: ${name}`); err.code = 'PLUGIN_UNKNOWN'; throw err; }
  const err = new Error(`Plugin name is ambiguous: ${hits.map((h) => h.key).join(', ')}`); err.code = 'PLUGIN_AMBIGUOUS'; throw err;
}

function findProject(reg, dirOrName) {
  if (reg.projects[dirOrName]) return { name: dirOrName, path: reg.projects[dirOrName].path };
  const dir = norm(dirOrName).toLowerCase();
  let best = null;
  for (const [name, p] of Object.entries(reg.projects)) {
    const base = norm(p.path).toLowerCase();
    if (dir === base || dir.startsWith(base + '/')) {
      if (!best || base.length > best.path.length) best = { name, path: p.path };
    }
  }
  return best;
}

module.exports = { MOD_MARKETPLACE, loadRegistry, saveRegistry, installedPlugins, pluginHints, pluginInstallPath, pluginVersion, resolvePluginKey, findProject, same };
