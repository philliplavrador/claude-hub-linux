#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');

function destFor(root, p) {
  const n = path.resolve(p).replace(/\\/g, '/');
  const m = /^([A-Za-z]):\/(.*)$/.exec(n);
  return path.join(root, m ? `${m[1].toUpperCase()}/${m[2]}` : n.replace(/^\/+/, ''));
}

function copy(src, dst) {
  const st = fs.lstatSync(src);
  if (st.isSymbolicLink()) { fs.mkdirSync(path.dirname(dst), { recursive: true }); fs.writeFileSync(`${dst}.link.txt`, fs.readlinkSync(src)); return 1; }
  if (st.isDirectory()) {
    let n = 0;
    for (const e of fs.readdirSync(src)) { if (e === 'node_modules' || e === '.git') continue; n += copy(path.join(src, e), path.join(dst, e)); }
    return n;
  }
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
  return 1;
}

function main(argv) {
  const [root, ...paths] = argv;
  if (!root || !paths.length) { process.stderr.write('usage: backup.js <dest-root> <path>...\n'); return 2; }
  let total = 0;
  for (const p of paths) {
    if (!fs.existsSync(p)) { process.stdout.write(`skip (missing): ${p}\n`); continue; }
    const n = copy(p, destFor(root, p));
    total += n;
    process.stdout.write(`${n}\t${p}\n`);
  }
  process.stdout.write(`total files: ${total}\n`);
  return 0;
}

if (require.main === module) process.exit(main(process.argv.slice(2)));
module.exports = { destFor, copy };
