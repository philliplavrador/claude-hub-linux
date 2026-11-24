#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { readHookInput } = require('../lib/input');

const POINTER_REL = '.agents/toolkit.json';
const DEFAULT_ALLOW = ['.scratch/', '.playwright-mcp/'];

function norm(p) {
  const value = String(p).replace(/\\/g, '/');
  return value === '/' || /^[a-z]:\/$/i.test(value) ? value : value.replace(/\/+$/, '');
}

function pathFlavor(p) {
  const value = String(p);
  if (/^(?:[a-z]:[\\/]|\\\\|\/\/)/i.test(value)) return path.win32;
  return value.startsWith('/') ? path.posix : path;
}

function resolved(p) { return norm(pathFlavor(p).resolve(p)); }

function findPointer(startDir) {
  let dir;
  try { dir = resolved(startDir); } catch { return null; }
  for (;;) {
    const file = `${dir}/${POINTER_REL}`;
    if (fs.existsSync(file)) {
      let allow = null;
      try {
        const ptr = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
        const opt = ptr && ptr.options && ptr.options.hooks && ptr.options.hooks['screenshot-guard'];
        if (opt && Array.isArray(opt.allow)) allow = opt.allow.filter((d) => typeof d === 'string' && d.trim());
      } catch { allow = null; }
      return { root: dir, allow };
    }
    const parent = norm(pathFlavor(dir).dirname(dir));
    if (parent === dir) return null;
    dir = parent;
  }
}

function isAbsolute(given) {
  return /^([a-zA-Z]:[\\/]|\\\\|\/)/.test(given);
}

function isAllowed(given, repo, allow) {
  if (!isAbsolute(given)) return false;

  const abs = resolved(given); // collapses ../ using the path's own platform syntax
  const rootPath = resolved(repo);
  const fold = pathFlavor(rootPath) === path.win32 ? (s) => s.toLowerCase() : (s) => s;
  const lower = fold(abs);
  const root = fold(rootPath);
  const prefix = root.endsWith('/') ? root : `${root}/`;
  const inRepo = lower === root || lower.startsWith(prefix);
  if (!inRepo) return true; // outside the repo entirely — the session scratchpad case

  const rel = fold(abs.slice(prefix.length));
  return allow.some((dir) => rel.startsWith(`${fold(norm(dir))}/`));
}

function reasonFor(given, repo, allow) {
  const dirs = allow.map((d) => `  • ${repo}/${norm(d)}/`).join('\n');
  const head = isAbsolute(given)
    ? `Screenshot path "${given}" is inside ${repo} but not in a folder screenshots may land in, ` +
      `where a stray PNG ships with the next commit.`
    : `Screenshot path "${given}" would land somewhere it must not. A bare or relative name ` +
      `resolves against the repo root, so it writes ${repo}/${path.basename(given)} — where a ` +
      `stray PNG ships with the next commit.`;
  return (
    `${head} Pass an ABSOLUTE path in one of:\n` +
    `${dirs}\n` +
    `  • throwaway → your session scratchpad, outside the repo (chrome-devtools only)\n` +
    `Best of all, for chrome-devtools' take_screenshot: pass NO path — it returns the image ` +
    `inline and never touches disk. That is usually the right answer.\n` +
    `For Playwright's browser_take_screenshot the scratchpad is NOT an option: it refuses ` +
    `absolute paths outside its own allowed roots. Use an absolute path from the list above, ` +
    `or one under PLAYWRIGHT_MCP_OUTPUT_DIR.`
  );
}

function decide(input, deps = {}) {
  try {
    const find = deps.findPointer || findPointer;
    const tool = input && input.tool;
    if (typeof tool === 'string' && tool && !/screenshot/i.test(tool)) return null;

    const ti = (input && input.input) || {};
    const raw = ti.filename != null ? ti.filename : ti.filePath; // Playwright ?? chrome-devtools
    if (typeof raw !== 'string' || !raw.trim()) return null; // no path → inline image

    const given = raw.trim();
    const found = find((input && input.cwd) || process.cwd()) || {};
    const repo = norm(found.root || process.env.CLAUDE_PROJECT_DIR || (input && input.cwd) || process.cwd());
    const allow = found.allow && found.allow.length ? found.allow : DEFAULT_ALLOW;
    if (isAllowed(given, repo, allow)) return null;

    return {
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'deny',
        permissionDecisionReason: reasonFor(given, repo, allow),
      },
    };
  } catch {
    return null; // a broken guard must never block a screenshot it has no opinion on
  }
}

function main() {
  try {
    const decision = decide(readHookInput());
    if (decision) process.stdout.write(JSON.stringify(decision));
  } catch {
  }
  process.exit(0);
}

if (require.main === module) main();

module.exports = { decide, findPointer, isAllowed, DEFAULT_ALLOW, main };
