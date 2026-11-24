'use strict';
const fs = require('node:fs');
const fsx = require('./fsx');
const { same } = require('./paths');
const pointer = require('./pointer');
const claude = require('./claude');
const state = require('./state');
const { planHash } = require('./sync');

function linksIntact(st, builtActions) {
  const allExist = (st.created || []).every((p) => {
    if (fsx.isLink(p)) {
      const target = fsx.linkTarget(p);
      return !!target && fs.existsSync(target);
    }
    return fs.existsSync(p);
  });
  if (!allExist) return false;
  return (builtActions || []).every((a) => a.type !== 'junction' || (fsx.isLink(a.link) && same(fsx.linkTarget(a.link), a.target)));
}

function settle(out, st, built, extraProblem) {
  const skipped = st.skipped || [];
  const res = { ...out, skipped };
  if (out.conflicts.length) return { ...res, state: 'problem' };
  if (st.incomplete) return { ...res, state: 'needs-apply' };
  if (out.warnings.length || extraProblem) return { ...res, state: 'problem' };
  if (st.inputsHash === planHash(built) && linksIntact(st, built.actions)) return { ...res, state: 'in-sync' };
  return { ...res, state: 'needs-apply' };
}

function projectStatus(P, ctx, name) {
  const { reg, items, codexSkills } = ctx;
  const p = reg.projects[name];
  const base = { name, path: p ? p.path : null, lastSync: null, warnings: [], conflicts: [], skipped: [] };
  if (!p || !fs.existsSync(p.path)) return { ...base, state: 'missing' };
  let ptr;
  try {
    ptr = pointer.loadPointer(p.path);
  } catch (e) {
    return { ...base, state: 'problem', warnings: [e.message] };
  }
  if (!ptr) return { ...base, state: 'no-pointer' };
  const st = state.loadProjectState(p.path);
  const eff = pointer.effectiveSet(P, reg, ptr, items);
  const built = claude.projectActions(P, reg, items, { root: p.path, ptr, eff, prevState: st, codexSkills });
  const out = { ...base, lastSync: st.lastSync, warnings: st.warnings || [], conflicts: st.conflicts || [] };
  return settle(out, st, built, eff.unknown.length > 0);
}

function globalStatus(P, ctx) {
  const { reg, items, codexSkills } = ctx;
  const st = state.loadGlobalState(P);
  const built = claude.globalActions(P, reg, items, { prevState: st, existingCodexConfig: fsx.readText(`${P.codexHome}/config.toml`) || '', codexSkills });
  const out = { lastSync: st.lastSync, warnings: st.warnings || [], conflicts: st.conflicts || [], skipped: [] };
  return settle(out, st, built, false);
}

function allStatus(P, ctx) {
  return {
    global: globalStatus(P, ctx),
    projects: Object.keys(ctx.reg.projects).map((n) => {
      try {
        return projectStatus(P, ctx, n);
      } catch (e) {
        const p = ctx.reg.projects[n];
        return { name: n, path: p ? p.path : null, lastSync: null, warnings: [e.message], conflicts: [], skipped: [], state: 'problem' };
      }
    }),
  };
}

module.exports = { projectStatus, globalStatus, allStatus };
