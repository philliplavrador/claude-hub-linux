'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const MALFORMED_GRACE_MS = 120000;
const OWNER_FILE = /^owner-[a-zA-Z0-9-]+\.json$/;
const CHANGING = ['ENOENT', 'EPERM', 'EACCES'];

function isPidAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== 'ESRCH'; }
}

function skipped(warning) {
  return {
    skipped: true, reason: 'sync already running',
    changes: [], warnings: warning ? [warning] : [], conflicts: [],
  };
}

function inspect(dir, alive) {
  let stat;
  try { stat = fs.lstatSync(dir); }
  catch (error) {
    if (error.code === 'ENOENT') return { absent: true };
    if (CHANGING.includes(error.code)) return { warning: 'Sync lock is temporarily inaccessible; left untouched.' };
    throw error;
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) return { warning: 'Sync lock is not a regular directory; left untouched.' };
  let entries;
  try { entries = fs.readdirSync(dir); }
  catch (error) {
    if (error.code === 'ENOENT') return { absent: true };
    if (CHANGING.includes(error.code)) return { warning: 'Sync lock is temporarily inaccessible; left untouched.' };
    throw error;
  }
  if (entries.some(name => !OWNER_FILE.test(name))) return { warning: 'Sync lock contains unexpected files; left untouched.' };

  const files = [];
  let malformed = entries.length !== 1;
  let modified = stat.mtimeMs;
  for (const name of entries) {
    const file = path.join(dir, name);
    let fileStat;
    let raw;
    try {
      fileStat = fs.lstatSync(file);
      if (!fileStat.isFile() || fileStat.isSymbolicLink()) return { warning: 'Sync lock owner is not a regular file; left untouched.' };
      raw = fs.readFileSync(file, 'utf8');
    } catch (error) {
      if (CHANGING.includes(error.code)) return { warning: 'Sync lock changed during inspection; retry on the next sync.' };
      throw error;
    }
    modified = Math.max(modified, fileStat.mtimeMs);
    let owner;
    try { owner = JSON.parse(raw); } catch { /* Incomplete metadata gets a grace period below. */ }
    const validPid = Number.isSafeInteger(owner?.pid) && owner.pid > 0;
    if (validPid && alive(owner.pid)) return { live: true };
    if (!validPid || typeof owner?.token !== 'string' || name !== `owner-${owner.token}.json`
      || !Number.isFinite(Date.parse(owner?.createdAt))) malformed = true;
    files.push({ file, raw });
  }
  if (malformed && Date.now() - modified < MALFORMED_GRACE_MS) {
    return { warning: 'Sync lock metadata is incomplete; allowing two minutes before recovery.' };
  }
  return { stale: true, files };
}

function reclaim(dir, files) {
  for (const { file, raw } of files) {
    try {
      if (fs.readFileSync(file, 'utf8') !== raw) return false;
      fs.unlinkSync(file);
    } catch (error) {
      if (error.code === 'ENOENT') continue;
      if (CHANGING.includes(error.code)) return false;
      throw error;
    }
  }
  try { fs.rmdirSync(dir); return true; }
  catch (error) {
    if (error.code === 'ENOENT') return true;
    if (['ENOTEMPTY', 'EEXIST', 'EPERM', 'EACCES'].includes(error.code)) return false;
    throw error;
  }
}

function release(dir, token) {
  const file = path.join(dir, `owner-${token}.json`);
  let owner;
  try { owner = JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (error) {
    if (error.code === 'ENOENT' || error instanceof SyntaxError) return;
    throw error;
  }
  if (owner.token !== token || owner.pid !== process.pid) return;
  reclaim(dir, [{ file, raw: fs.readFileSync(file, 'utf8') }]);
}

function withSyncLock(P, fn, { isPidAlive: alive = isPidAlive } = {}) {
  fs.mkdirSync(P.agentsHome, { recursive: true });
  const dir = path.join(P.agentsHome, 'toolkit-sync.lock');
  const current = inspect(dir, alive);
  if (current.live || current.warning) return skipped(current.warning);
  if (current.stale && !reclaim(dir, current.files)) return skipped();

  const token = randomUUID();
  const pending = path.join(P.agentsHome, `toolkit-sync.pending-${process.pid}-${token}`);
  const owner = { pid: process.pid, token, createdAt: new Date().toISOString() };
  fs.mkdirSync(pending);
  try {
    fs.writeFileSync(path.join(pending, `owner-${token}.json`), JSON.stringify(owner) + '\n', { flag: 'wx' });
    try { fs.renameSync(pending, dir); }
    catch (error) {
      if (['EEXIST', 'ENOTEMPTY', 'EPERM', 'EACCES'].includes(error.code)) return skipped();
      throw error;
    }
    try { return fn(); }
    finally { release(dir, token); }
  } finally {
    release(pending, token);
    try { fs.rmdirSync(pending); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

module.exports = { withSyncLock };
