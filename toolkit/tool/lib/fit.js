'use strict';
const { LIST_KINDS, POINTER_REL, loadPointer, savePointer, createPointer } = require('./pointer');
const { resolvePluginKey } = require('./registry');
const { backupFile } = require('./fsx');
const catalogue = require('./catalogue');
const sync = require('./sync');

function emptyKinds() {
  const o = {};
  for (const k of LIST_KINDS) o[k] = [];
  return o;
}

function listOf(obj, kind) {
  const v = obj ? obj[kind] : null;
  return Array.isArray(v) ? v : [];
}

function installed(P, name) {
  try { resolvePluginKey(P, name); return true; } catch { return false; }
}

function buildLine(name, added, removed) {
  const parts = [];
  for (const e of added) parts.push(`+${e.kind}/${e.name} (${e.why})`);
  for (const e of removed) parts.push(`-${e.kind}/${e.name} (${e.why})`);
  const body = parts.length ? parts.join(' ') : 'nothing beyond the shared shelf';
  return `Fit ${name}: ${body}. /fit redoes it.`;
}

function apply(P, ctx, { root, name, set, by }) {
  if (!set || typeof set !== 'object') throw new Error('fit --set needs add, remove and why');
  if ('options' in set || 'outputStyle' in set) {
    throw new Error('fit never touches options or the output style');
  }
  for (const k of Object.keys(set)) {
    if (!['add', 'remove', 'why'].includes(k)) throw new Error(`fit --set does not know the key ${k}; it takes add, remove and why`);
  }

  const add = set.add || {};
  const remove = set.remove || {};
  const why = set.why || {};

  const cat = catalogue.build(P, ctx);
  const knownItem = (kind, n) => cat.items.some((i) => i.kind === kind && i.name === n);

  const existing = loadPointer(root);
  const currentAdd = existing ? existing.add : emptyKinds();

  const addEntries = [];
  for (const kind of LIST_KINDS) {
    for (const n of listOf(add, kind)) {
      if (kind === 'plugins') {
        if (!installed(P, n)) throw new Error(`plugins/${n} is not installed`);
      } else if (!knownItem(kind, n)) {
        throw new Error(`${kind}/${n} is not in the store`);
      }
      addEntries.push({ kind, name: n });
    }
  }

  const removeEntries = [];
  for (const kind of LIST_KINDS) {
    for (const n of listOf(remove, kind)) {
      if (kind === 'plugins') {
        if (!installed(P, n)) throw new Error(`plugins/${n} is not installed`);
      } else if (!listOf(currentAdd, kind).includes(n)) {
        throw new Error(`remove accepts plugins, or a kind this project already adds: ${kind}/${n}`);
      }
      removeEntries.push({ kind, name: n });
    }
  }

  for (const e of [...addEntries, ...removeEntries]) {
    const key = `${e.kind}/${e.name}`;
    const reason = why[key];
    if (!reason) throw new Error(`every entry needs a why: ${key}`);
    if (typeof reason !== 'string' || /[\r\n]/.test(reason)) throw new Error(`a why is one line of text: ${key}`);
    e.why = reason;
  }

  backupFile(P, `${root}/${POINTER_REL}`);
  const ptr = existing || createPointer(root, name);
  ptr.add = { ...emptyKinds(), ...add };
  ptr.remove = { ...emptyKinds(), ...remove };
  for (const k of LIST_KINDS) {
    if (!Array.isArray(ptr.add[k])) ptr.add[k] = [];
    if (!Array.isArray(ptr.remove[k])) ptr.remove[k] = [];
  }
  ptr.why = { ...why };
  ptr.fitted = { at: new Date().toISOString(), by };
  savePointer(root, ptr);

  const line = buildLine(name, addEntries, removeEntries);
  const syncResult = sync.syncProject(P, ctx, { root, projectName: name, quiet: true });

  return { line, added: addEntries, removed: removeEntries, sync: syncResult };
}

module.exports = { apply };
