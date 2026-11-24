'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { norm, same } = require('./paths');

function sha256(data) {
  return crypto.createHash('sha256').update(data).digest('hex');
}

function fileHash(file) {
  try {
    return sha256(fs.readFileSync(file));
  } catch (e) {
    if (e.code === 'ENOENT') return null;
    throw e;
  }
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function isLink(p) {
  try {
    return fs.lstatSync(p).isSymbolicLink();
  } catch (e) {
    if (e.code === 'ENOENT') return false;
    throw e;
  }
}

function linkTarget(p) {
  if (!isLink(p)) return null;
  return norm(fs.readlinkSync(p));
}

function makeJunction(P, target, link, ops = fs) {
  const t = norm(path.resolve(target));
  if (isLink(link)) {
    if (same(linkTarget(link), t)) return 'unchanged';
    removeLink(link, ops);
    fs.symlinkSync(t, link, 'junction');
    return 'replaced';
  }
  if (fs.existsSync(link)) {
    const err = new Error(`${link} exists and is not a link`);
    err.code = 'EEXIST_REAL';
    throw err;
  }
  ensureDir(path.dirname(link));
  fs.symlinkSync(t, link, 'junction');
  return 'created';
}

function removeLink(p, ops = fs) {
  if (!isLink(p)) return false;
  try { ops.rmdirSync(p); } catch { ops.unlinkSync(p); }
  return true;
}

function detachLinks(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === '.git') continue;
    const full = `${dir}/${e.name}`;
    if (isLink(full)) { removeLink(full); out.push(norm(full)); continue; }
    if (e.isDirectory()) detachLinks(full, out);
  }
  return out;
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (e) {
    if (e.code === 'ENOENT') return null;
    throw e;
  }
}

function readJson(file, fallback) {
  const text = readText(file);
  if (text == null) return fallback;
  const clean = text.charCodeAt(0) === 0xFEFF ? text.slice(1) : text;
  try {
    return JSON.parse(clean);
  } catch (e) {
    const err = new Error(`${norm(file)}: ${e.message}`);
    err.code = 'EJSON';
    err.file = norm(file);
    err.jsonMessage = e.message;
    throw err;
  }
}

function writeTarget(file) {
  let target = file;
  for (let hops = 0; hops < 40; hops++) {
    let st;
    try { st = fs.lstatSync(target); } catch (e) { if (e.code === 'ENOENT') return target; throw e; }
    if (!st.isSymbolicLink()) return target;
    target = path.resolve(path.dirname(target), fs.readlinkSync(target));
  }
  const err = new Error(`${file}: too many levels of symbolic links`);
  err.code = 'ELOOP';
  throw err;
}

function writeFileAtomic(file, content) {
  const target = writeTarget(file);
  const dir = path.dirname(target);
  let old = null; // the old file's stat; a new file gets the usual 0666 minus the umask and its creator's owner
  try { old = fs.statSync(target); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const mode = old === null ? null : old.mode & 0o7777;
  const tmp = path.join(dir, `.${path.basename(target)}.tmp-${process.pid}-${crypto.randomBytes(6).toString('hex')}`);
  let fd = null;
  try {
    fd = fs.openSync(tmp, 'wx', mode === null ? 0o666 : mode);
    if (mode !== null) {
      const made = fs.fstatSync(fd);
      if (made.uid !== old.uid || made.gid !== old.gid) {
        try { fs.fchownSync(fd, old.uid, old.gid); } catch (e) {
          const err = new Error(`${norm(target)} not written: this user cannot give a new file its owner and group (uid ${old.uid}, gid ${old.gid}); the file is left as it was`);
          err.code = 'EOWNERSHIP';
          err.file = norm(target);
          err.cause = e;
          throw err;
        }
      }
    }
    fs.writeFileSync(fd, content);
    if (mode !== null) fs.fchmodSync(fd, mode);
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;
    fs.renameSync(tmp, target);
  } catch (e) {
    if (fd !== null) { try { fs.closeSync(fd); } catch { /* already failing */ } }
    try { fs.unlinkSync(tmp); } catch { /* never created, or already gone */ }
    throw e;
  }
  try { // make the rename itself durable; best effort, a few filesystems refuse to fsync a folder
    const dfd = fs.openSync(dir, 'r');
    try { fs.fsyncSync(dfd); } finally { fs.closeSync(dfd); }
  } catch { /* durability only */ }
}

function writeJson(file, obj) {
  ensureDir(path.dirname(file));
  writeFileAtomic(file, JSON.stringify(obj, null, 2) + '\n');
}

function stamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

let currentStamp = null;
function backupFile(P, file) {
  if (!fs.existsSync(file)) return null;
  if (!currentStamp) currentStamp = stamp();
  const encoded = norm(path.resolve(file)).replace(/:/g, '').replace(/\//g, '__');
  const dest = path.join(P.backupDir, currentStamp, encoded);
  ensureDir(path.dirname(dest));
  fs.copyFileSync(file, dest);
  return norm(dest);
}

function writeFileBackup(P, file, content) {
  const existing = readText(file);
  if (existing === content) return 'unchanged';
  if (existing != null) backupFile(P, file);
  ensureDir(path.dirname(file));
  writeFileAtomic(file, content);
  return 'written';
}

function copyFile(src, dest) {
  const a = fileHash(src);
  if (a === null) {
    const err = new Error(`Source file does not exist: ${src}`);
    err.code = 'ENOENT';
    throw err;
  }
  const b = fileHash(dest);
  if (a === b) return 'unchanged';
  ensureDir(path.dirname(dest));
  fs.copyFileSync(src, dest);
  return 'written';
}

module.exports = {
  sha256, fileHash, ensureDir, isLink, linkTarget, makeJunction, removeLink, detachLinks,
  readText, readJson, writeJson, writeFileAtomic, stamp, backupFile, writeFileBackup, copyFile,
};
