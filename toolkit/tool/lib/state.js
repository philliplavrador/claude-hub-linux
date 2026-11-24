'use strict';
const { readJson, writeJson } = require('./fsx');
const { norm } = require('./paths');
const { STATE_REL } = require('./pointer');

function emptyState() {
  return { version: 1, inputsHash: null, created: [], copies: {}, conflicts: [], lastSync: null, warnings: [], incomplete: false, skipped: [], managedMods: [] };
}
function fill(raw) {
  return { ...emptyState(), ...(raw || {}) };
}
function loadProjectState(root) {
  return fill(readJson(`${norm(root)}/${STATE_REL}`, null));
}
function saveProjectState(root, state) {
  writeJson(`${norm(root)}/${STATE_REL}`, state);
}
function loadGlobalState(P) {
  return fill(readJson(P.globalStateFile || `${P.stateDir}/global.json`, null));
}
function saveGlobalState(P, state) {
  writeJson(P.globalStateFile || `${P.stateDir}/global.json`, state);
}
module.exports = { emptyState, loadProjectState, saveProjectState, loadGlobalState, saveGlobalState };
