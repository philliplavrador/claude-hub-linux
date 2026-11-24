'use strict';
const { spawnSync } = require('node:child_process');

function git(args, cwd) {
  const r = spawnSync('git', args, { cwd, windowsHide: true, encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`git ${args.join(' ')} failed in ${cwd}: ${(r.stderr || r.stdout || '').trim()}`);
  return r.stdout;
}

function refExists(repo, ref) {
  try { git(['show-ref', '--verify', '--quiet', ref], repo); return true; } catch { return false; }
}

function defaultBranch(repo) {
  if (refExists(repo, 'refs/heads/main')) return 'main';
  if (refExists(repo, 'refs/heads/master')) return 'master';
  try {
    const ref = git(['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], repo).trim();
    if (ref) return ref.replace(/^origin\//, '');
  } catch { /* no remote-tracked default branch */ }
  return git(['rev-parse', '--abbrev-ref', 'HEAD'], repo).trim();
}

module.exports = { git, defaultBranch };
