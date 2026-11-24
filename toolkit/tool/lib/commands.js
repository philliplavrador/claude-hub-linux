'use strict';
const sync = require('./sync');

function needs(rest, n, usage) {
  if (rest.length < n) throw new Error(`Usage: ${usage}`);
}

function newProject(P, rest, flags, out) {
  needs(rest, 1, 'toolkit new-project <name> [--no-git]');
  const ctx = sync.loadContext(P);
  const r = require('./projects').newProject(P, ctx, rest[0], { noGit: !!flags['no-git'] });
  out.write(`Created ${r.path}\n`);
  return 0;
}

function register(P, rest, flags, out) {
  needs(rest, 2, 'toolkit register <name> <path> [--anywhere]');
  const ctx = sync.loadContext(P);
  const r = require('./projects').register(P, ctx, rest[0], rest[1], { anywhere: !!flags.anywhere });
  out.write(`Registered ${rest[0]} at ${r.path}\n`);
  return 0;
}

function catalogue(P, rest, flags, out) {
  const ctx = sync.loadContext(P);
  const data = require('./catalogue').build(P, ctx);
  out.write(`${JSON.stringify(data, null, 2)}\n`);
  return 0;
}

function fit(P, rest, flags, out) {
  if (!flags.set) throw new Error('Usage: toolkit fit --set <file> [--by claude|codex] [--project <name> | --cwd <dir>]');
  const ctx = sync.loadContext(P);
  const set = require('./fsx').readJson(flags.set, null);
  if (set == null) throw new Error(`fit --set file not found: ${flags.set}`);

  let root; let name;
  if (flags.project) {
    const p = ctx.reg.projects[flags.project];
    if (!p) throw new Error(`Unknown project: ${flags.project}`);
    root = p.path;
    name = flags.project;
  } else {
    const pointer = require('./pointer');
    const found = pointer.findRoot(flags.cwd || process.cwd());
    if (!found) throw new Error('No project here: no pointer and no git root');
    root = found.root;
    const registered = require('./registry').findProject(ctx.reg, root);
    name = registered ? registered.name : require('node:path').basename(root);
  }

  const by = flags.by || 'claude';
  if (by !== 'claude' && by !== 'codex') throw new Error('--by is claude or codex');
  const r = require('./fit').apply(P, ctx, { root, name, set, by });
  out.write(`${r.line}\n`);
  return 0;
}

function unlink(P, rest, flags, out) {
  needs(rest, 1, 'toolkit unlink <folder>');
  const fs = require('node:fs');
  const { isLink, removeLink } = require('./fsx');
  const { norm } = require('./paths');
  const dir = require('node:path').resolve(rest[0]);
  const removed = [];
  const kept = [];
  const walk = (d, rel, keep) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      if (e.name === '.git') continue;
      const full = `${d}/${e.name}`;
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (isLink(full)) {
        if (keep && keep.has(r)) { kept.push(norm(full)); continue; }
        removeLink(full); removed.push(norm(full)); continue;
      }
      if (!e.isDirectory()) continue;
      if (fs.existsSync(`${full}/.git`)) walk(full, '', trackedLinks(full));
      else walk(full, r, keep);
    }
  };
  walk(dir, '', trackedLinks(dir));
  if (!removed.length && !kept.length) out.write(`no links under ${dir}\n`);
  for (const p of removed) out.write(`unlinked ${p}\n`);
  if (kept.length) out.write(`kept ${kept.length} links git tracks (git worktree remove handles them)\n`);
  return 0;
}

function trackedLinks(dir) {
  let raw;
  try {
    raw = require('node:child_process').execFileSync('git', ['-C', dir, 'ls-files', '-s', '-z'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true, maxBuffer: 256 * 1024 * 1024 });
  } catch { return null; }
  const set = new Set();
  for (const ent of raw.split('\0')) {
    const m = /^120000 [0-9a-f]+ \d\t(.*)$/s.exec(ent);
    if (m) set.add(m[1]);
  }
  return set;
}

function item(P, rest, flags, out) {
  const items = require('./items');
  const sub = rest[0];
  if (sub === 'new') {
    needs(rest, 3, 'toolkit item new <kind> <name> [--description <d>] [--user-only]');
    out.write(`Created ${items.newItem(P, rest[1], rest[2], { description: flags.description, userOnly: !!flags['user-only'] })}\n`);
    return 0;
  }
  if (sub === 'default') {
    needs(rest, 4, 'toolkit item default <kind> <name> on|off');
    const r = items.setDefault(P, sync.loadContext(P), rest[1], rest[2], rest[3] === 'on');
    out.write(`${rest[1]}/${rest[2]} default ${rest[3]}; global: ${r.skipped ? 'unchanged' : r.changes.length + ' change(s)'}\n`);
    return 0;
  }
  throw new Error('Usage: toolkit item new|default');
}

function assign(P, rest, flags, out, on) {
  needs(rest, 3, `toolkit ${on ? 'assign' : 'unassign'} <project> <kind> <name>`);
  const r = require('./items').assign(P, sync.loadContext(P), rest[0], rest[1], rest[2], on);
  out.write(`${rest[1]}/${rest[2]} ${on ? 'assigned to' : 'removed from'} ${rest[0]}; ${r.changes.length} change(s)\n`);
  return 0;
}

function plugins(P, rest, flags, out) {
  const sub = rest[0];
  if (sub === 'list' || !sub) {
    const { installedPlugins } = require('./registry');
    const ctx = sync.loadContext(P);
    const settings = require('./fsx').readJson(`${P.claudeHome}/settings.json`, {});
    for (const p of installedPlugins(P)) {
      const on = settings.enabledPlugins && settings.enabledPlugins[p.key];
      out.write(`${(on ? 'ON ' : 'off')}  ${p.name.padEnd(28)} ${p.marketplace}${ctx.reg.defaults.plugins.includes(p.name) ? '  (default)' : ''}\n`);
    }
    return 0;
  }
  if (sub === 'on' || sub === 'off') {
    needs(rest, 2, 'toolkit plugins on|off <name> [--project <p>]');
    const r = require('./items').pluginToggle(P, sync.loadContext(P), rest[1], sub === 'on', { project: flags.project });
    out.write(`${rest[1]} ${sub}${flags.project ? ` for ${flags.project}` : ' by default'}; ${r.skipped ? 'unchanged' : r.changes.length + ' change(s)'}. Restart Claude Code for plugins to take effect.\n`);
    return 0;
  }
  throw new Error('Usage: toolkit plugins list|on|off ...');
}

module.exports = { newProject, register, catalogue, fit, unlink, item, assign, plugins };
