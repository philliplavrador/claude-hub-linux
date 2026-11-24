#!/usr/bin/env node
'use strict';
const { parse } = require('./lib/args');
const { resolvePaths } = require('./lib/paths');

const HELP = `toolkit — one place for everything that shapes Claude Code and Codex

  sync [--cwd <dir> | --project <name> | --all | --global] [--quiet] [--dry-run] [--init] [--force] [--codex-only]
  hook                          session-start hook entry (reads JSON on stdin)
  status [--all]                in-sync / needs-apply / problem, per project
  doctor                        environment and link checks
  new-project <name> [--no-git]
  register <name> <path> [--anywhere]   add an existing git folder as a project
  unlink <folder>               remove every link under a folder (before git worktree remove)
  item new <kind> <name> [--description <d>] [--user-only] | default <kind> <name> on|off
  assign <project> <kind> <name> | unassign <project> <kind> <name>
  plugins list | on <name> [--project <p>] | off <name> [--project <p>]
  catalogue                     everything the shelf and the plugins offer, as JSON (what /fit reads)
  fit --set <file> [--by claude|codex] [--project <name>]   write the AI's choices for a project (what /fit runs)
  mods check [--json] [--tests] [--record] [--reuse] | types   validate every mod on the shelf (--tests adds claude plugin test and tsc); copy the engine types tsc reads
                                a session-start re-check that could not finish is not tried again for 6 h, unless Claude Code or a mod file changes
kinds: skills rules agents output-styles hooks mcp
`;

function printResult(out, label, r) {
  if (r.skipped) { out.write(`${label}: nothing to do (${r.reason || 'unchanged'})\n`); }
  else {
    out.write(`${label}: ${r.changes.length} change(s)\n`);
    for (const c of r.changes) out.write(`  ${c}\n`);
  }
  for (const w of r.warnings || []) out.write(`  warning: ${w}\n`);
  for (const c of r.conflicts || []) out.write(`  conflict: ${c.dest} vs ${c.src}\n`);
}

function main(argv, deps = {}) {
  const P = deps.P || resolvePaths();
  const out = deps.stdout || process.stdout;
  const err = deps.stderr || process.stderr;
  const { cmd, rest, flags } = parse(argv);
  try {
    switch (cmd) {
      case 'help': case '--help': case '-h': out.write(HELP); return 0;
      case 'sync': {
        const sync = require('./lib/sync');
        const ctx = sync.loadContext(P);
        const common = { dryRun: !!flags['dry-run'], force: !!flags.force, quiet: !!flags.quiet, codexOnly: !!flags['codex-only'] };
        if (flags.global || flags.all || !(flags.cwd || flags.project)) printResult(out, 'global', sync.syncGlobal(P, ctx, common));
        if (flags.all) {
          for (const name of Object.keys(ctx.reg.projects)) {
            try {
              printResult(out, name, sync.syncProject(P, ctx, { root: ctx.reg.projects[name].path, projectName: name, init: true, ...common }));
            } catch (e) {
              out.write(`failed ${name}: ${e.message}\n`);
            }
          }
        } else if (flags.project) {
          const p = ctx.reg.projects[flags.project];
          if (!p) throw new Error(`Unknown project: ${flags.project}`);
          printResult(out, flags.project, sync.syncProject(P, ctx, { root: p.path, projectName: flags.project, init: true, ...common }));
        } else if (!flags.global) {
          printResult(out, 'project', sync.syncProject(P, ctx, { cwd: flags.cwd || process.cwd(), init: !!flags.init, ...common }));
        }
        return 0;
      }
      case 'hook': {
        const { readHookInput } = require('../hooks/lib/input');
        err.write('toolkit hook: not included in this copy' + String.fromCharCode(10)); return 2;
      }
      case 'status': {
        const sync = require('./lib/sync'); const status = require('./lib/status');
        const ctx = sync.loadContext(P);
        const all = status.allStatus(P, ctx);
        const line = (label, s) => {
          out.write(`${label.padEnd(28)} ${s.state}${s.conflicts.length ? `  (${s.conflicts.length} conflict)` : ''}${s.warnings.length ? `  (${s.warnings.length} warning)` : ''}${(s.skipped || []).length ? `  (${s.skipped.length} skipped)` : ''}\n`);
          for (const p of s.skipped || []) out.write(`    skipped: ${p}\n`);
        };
        line('global', all.global);
        for (const p of all.projects) line(p.name, p);
        return 0;
      }
      case 'doctor': {
        const doctor = require('./lib/doctor');
        let bad = 0;
        for (const c of doctor.run(P)) { out.write(`${c.check.padEnd(36)} ${c.ok ? 'ok' : c.blocking === false ? 'note' : 'FAIL'}  ${c.detail}\n`); if (!c.ok && c.blocking !== false) bad++; }
        return bad ? 1 : 0;
      }
      case 'new-project': return require('./lib/commands').newProject(P, rest, flags, out);
      case 'register': return require('./lib/commands').register(P, rest, flags, out);
      case 'unlink': return require('./lib/commands').unlink(P, rest, flags, out);
      case 'item': return require('./lib/commands').item(P, rest, flags, out);
      case 'assign': return require('./lib/commands').assign(P, rest, flags, out, true);
      case 'unassign': return require('./lib/commands').assign(P, rest, flags, out, false);
      case 'plugins': return require('./lib/commands').plugins(P, rest, flags, out);
      case 'catalogue': return require('./lib/commands').catalogue(P, rest, flags, out);
      case 'fit': return require('./lib/commands').fit(P, rest, flags, out);
      case 'mods': return require('./lib/mods').command(P, rest, flags, out);
      default: err.write(`Unknown command: ${cmd}\n${HELP}`); return 2;
    }
  } catch (e) {
    err.write(`${e.message}\n`);
    return 1;
  }
}

module.exports = { main, HELP };
if (require.main === module) process.exit(main(process.argv.slice(2)));
