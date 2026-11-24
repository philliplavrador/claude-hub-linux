'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { readJson, readText } = require('./fsx');
const { norm } = require('./paths');

const KINDS = ['skills', 'rules', 'agents', 'output-styles', 'hooks', 'mcp'];

function parseFrontmatter(text) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  if (!m) return { data: {}, body: text };
  const data = {};
  for (const line of m[1].split(/\r?\n/)) {
    const kv = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (kv) data[kv[1]] = kv[2].trim();
  }
  return { data, body: text.slice(m[0].length) };
}

function dirs(p) {
  if (!fs.existsSync(p)) return [];
  return fs.readdirSync(p, { withFileTypes: true }).filter((d) => d.isDirectory() || d.isSymbolicLink()).map((d) => d.name).sort();
}
function files(p, ext) {
  if (!fs.existsSync(p)) return [];
  return fs.readdirSync(p, { withFileTypes: true }).filter((d) => d.isFile() && d.name.endsWith(ext)).map((d) => d.name).sort();
}

function firstHeading(text) {
  const m = /^#\s+(.+)$/m.exec(text || '');
  return m ? m[1].trim() : null;
}
function firstLine(text) {
  const l = (text || '').split(/\r?\n/).find((x) => x.trim());
  return l ? l.trim() : null;
}

function listItems(P) {
  const S = P.store;
  const out = { skills: [], rules: [], agents: [], 'output-styles': [], hooks: [], mcp: [] };

  for (const name of dirs(`${S}/skills`)) {
    const file = `${S}/skills/${name}/SKILL.md`;
    const text = readText(file);
    if (text == null) continue;
    const { data } = parseFrontmatter(text);
    out.skills.push({ kind: 'skills', name, path: `${S}/skills/${name}`, description: data.description || name, userOnly: data['disable-model-invocation'] === 'true', fits: data.fits || null });
  }
  for (const name of dirs(`${S}/rules`)) {
    const dir = `${S}/rules/${name}`;
    const mds = files(dir, '.md').filter((f) => f.toLowerCase() !== 'readme.md').map((f) => `${dir}/${f}`);
    const readme = readText(`${dir}/README.md`);
    const description = firstLine(readme) || (mds.length ? firstHeading(readText(mds[0])) : null) || name;
    const fitsMatch = /^fits:\s*(.+)$/m.exec(readme || '');
    out.rules.push({ kind: 'rules', name, path: dir, description, files: mds, fits: fitsMatch ? fitsMatch[1].trim() : null });
  }
  for (const f of files(`${S}/agents`, '.md')) {
    const { data } = parseFrontmatter(readText(`${S}/agents/${f}`));
    const name = f.slice(0, -3);
    out.agents.push({ kind: 'agents', name, path: `${S}/agents/${f}`, description: data.description || name, fits: data.fits || null });
  }
  for (const f of files(`${S}/output-styles`, '.md')) {
    const { data } = parseFrontmatter(readText(`${S}/output-styles/${f}`));
    const name = f.slice(0, -3);
    out['output-styles'].push({ kind: 'output-styles', name, path: `${S}/output-styles/${f}`, description: data.description || name, fits: data.fits || null });
  }
  for (const name of dirs(`${S}/hooks`)) {
    if (name === 'lib') continue;
    let meta = readJson(`${S}/hooks/${name}/hook.json`, null);
    if (!meta) continue;
    const events = Array.isArray(meta.event) ? meta.event : meta.event ? [meta.event] : ['PreToolUse'];
    meta = { ...meta, events, event: events[0], args: Array.isArray(meta.args) ? meta.args.map(String) : [] };
    out.hooks.push({ kind: 'hooks', name, path: `${S}/hooks/${name}`, description: meta.description || name, fits: meta.fits || null, meta, script: `${S}/hooks/${name}/${name}.js` });
  }
  for (const f of files(`${S}/mcp`, '.json')) {
    const def = readJson(`${S}/mcp/${f}`, null);
    if (!def || !def.server) continue;
    const name = f.slice(0, -5);
    out.mcp.push({ kind: 'mcp', name, path: `${S}/mcp/${f}`, description: def.description || name, fits: def.fits || null, server: def.server });
  }
  return out;
}

function getItem(P, kind, name) {
  const list = listItems(P)[kind] || [];
  return list.find((i) => i.name === name) || null;
}

module.exports = { KINDS, parseFrontmatter, listItems, getItem, norm };
