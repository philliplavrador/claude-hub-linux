'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { readJson, writeJson } = require('./fsx');
const { norm } = require('./paths');
const { resolvePluginKey } = require('./registry');

const POINTER_REL = '.agents/toolkit.json';
const STATE_REL = '.agents/toolkit-state.json';
const LIST_KINDS = ['skills', 'rules', 'agents', 'hooks', 'mcp', 'plugins'];

function findRoot(startDir) {
  let dir = norm(path.resolve(startDir));
  let gitRoot = null;
  let pointerRoot = null;
  for (;;) {
    if (!pointerRoot && fs.existsSync(`${dir}/${POINTER_REL}`)) pointerRoot = dir;
    if (!gitRoot && fs.existsSync(`${dir}/.git`)) gitRoot = dir;
    if (pointerRoot && gitRoot) break;
    const parent = norm(path.dirname(dir));
    if (parent === dir) break;
    dir = parent;
  }
  if (pointerRoot) return { root: pointerRoot, hasPointer: true, gitRoot };
  if (gitRoot) return { root: gitRoot, hasPointer: false, gitRoot };
  return null;
}

function emptyLists() {
  const o = {};
  for (const k of LIST_KINDS) o[k] = [];
  return o;
}

function normalise(raw) {
  const ptr = {
    version: raw.version || 1,
    name: raw.name || null,
    add: { ...emptyLists(), ...(raw.add || {}) },
    remove: { ...emptyLists(), ...(raw.remove || {}) },
    outputStyle: raw.outputStyle || null,
    why: raw.why || {},
    fitted: raw.fitted || null,
    options: raw.options || {},
  };
  for (const k of LIST_KINDS) {
    if (!Array.isArray(ptr.add[k])) ptr.add[k] = [];
    if (!Array.isArray(ptr.remove[k])) ptr.remove[k] = [];
  }
  return ptr;
}

function loadPointer(root) {
  const file = `${norm(root)}/${POINTER_REL}`;
  let raw;
  try {
    raw = readJson(file, null);
  } catch (e) {
    const err = new Error(`pointer ${file}: ${e.jsonMessage || e.message}`);
    err.code = 'EPOINTER';
    err.file = file;
    throw err;
  }
  return raw ? normalise(raw) : null;
}

function savePointer(root, ptr) {
  const out = { version: ptr.version || 1, name: ptr.name };
  const add = {}; const remove = {};
  for (const k of LIST_KINDS) {
    if (ptr.add[k] && ptr.add[k].length) add[k] = ptr.add[k];
    if (ptr.remove[k] && ptr.remove[k].length) remove[k] = ptr.remove[k];
  }
  if (Object.keys(add).length) out.add = add;
  if (Object.keys(remove).length) out.remove = remove;
  if (ptr.outputStyle) out.outputStyle = ptr.outputStyle;
  if (ptr.why && Object.keys(ptr.why).length) out.why = ptr.why;
  if (ptr.fitted) out.fitted = ptr.fitted;
  if (ptr.options && Object.keys(ptr.options).length) out.options = ptr.options;
  writeJson(`${norm(root)}/${POINTER_REL}`, out);
}

function createPointer(root, name) {
  const ptr = normalise({ version: 1, name });
  savePointer(root, ptr);
  return ptr;
}

function effectiveSet(P, reg, ptr, items) {
  const unknown = [];
  const known = (kind) => new Set((items[kind] || []).map((i) => i.name));
  const pick = (kind) => {
    const have = known(kind);
    const dropped = new Set((ptr.remove && ptr.remove[kind]) || []);
    const out = [];
    for (const n of ptr.add[kind] || []) {
      if (dropped.has(n)) continue;
      if (have.has(n)) { if (!out.includes(n)) out.push(n); } else unknown.push({ kind, name: n });
    }
    return out;
  };
  const eff = {
    skills: pick('skills'),
    rules: pick('rules'),
    agents: pick('agents'),
    hooks: pick('hooks'),
    mcp: pick('mcp'),
    plugins: {},
    outputStyle: null,
    unknown,
  };
  for (const n of ptr.add.plugins || []) {
    try { eff.plugins[resolvePluginKey(P, n)] = true; } catch { unknown.push({ kind: 'plugins', name: n }); }
  }
  for (const n of ptr.remove.plugins || []) {
    try { eff.plugins[resolvePluginKey(P, n)] = false; } catch { unknown.push({ kind: 'plugins', name: n }); }
  }
  if (ptr.outputStyle) {
    if (known('output-styles').has(ptr.outputStyle)) eff.outputStyle = ptr.outputStyle;
    else unknown.push({ kind: 'output-styles', name: ptr.outputStyle });
  }
  return eff;
}

module.exports = { POINTER_REL, STATE_REL, LIST_KINDS, findRoot, loadPointer, savePointer, createPointer, effectiveSet };
