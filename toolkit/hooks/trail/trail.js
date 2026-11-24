#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { readHookInput } = require('../lib/input');
const pointer = require('../../tool/lib/pointer');

const TRAIL_REL = '.agents/trail.md';

const MD_LINK_RE = /\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

function stripNonHttpLinkTargets(s) {
  return String(s == null ? '' : s).replace(MD_LINK_RE, (whole, text, url) => (
    /^https?:\/\//i.test(url) ? whole : text
  ));
}

function trim400(s) {
  return stripNonHttpLinkTargets(s).replace(/\s+/g, ' ').trim().slice(0, 400);
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

function fmtAt(d) {
  const dt = d instanceof Date ? d : new Date(d);
  return `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())} ${pad2(dt.getHours())}:${pad2(dt.getMinutes())}`;
}

function shortName(n) {
  const s = String(n);
  return s.length > 60 ? '\u2026' + s.slice(-59) : s;
}

function fmtDirty(dirty) {
  if (dirty === '?') return '? uncommitted';
  const count = dirty && typeof dirty.count === 'number' ? dirty.count : 0;
  if (count === 0) return '0 uncommitted';
  const names = (dirty && dirty.names) || [];
  const shown = names.slice(0, 3).map(shortName);
  const rest = count - shown.length;
  const list = shown.join(', ') + (rest > 0 ? `, +${rest}` : '');
  return `${count} uncommitted (${list})`;
}

function gitInfo(root, spawn) {
  let branch = '?';
  let dirty = '?';
  try {
    const b = spawn('git', ['-C', root, 'rev-parse', '--abbrev-ref', 'HEAD'], { encoding: 'utf8', windowsHide: true });
    if (b && b.status === 0) {
      const t = String(b.stdout || '').trim();
      if (t) branch = t;
    }
  } catch { /* branch stays '?' */ }
  try {
    const s = spawn('git', ['-C', root, 'status', '--porcelain'], { encoding: 'utf8', windowsHide: true });
    if (s && s.status === 0) {
      const lines = String(s.stdout || '').split(/\r?\n/).filter((l) => l.trim());
      dirty = { count: lines.length, names: lines.map((l) => l.slice(3).trim()) };
    }
  } catch { /* dirty stays '?' */ }
  return { branch, dirty };
}

function parseJsonl(text) {
  const out = [];
  for (const line of String(text).split(/\r?\n/)) {
    if (!line.trim()) continue;
    try { out.push(JSON.parse(line)); } catch { /* skip a broken line */ }
  }
  return out;
}

function textFromClaudeContent(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.filter((p) => p && p.type === 'text' && typeof p.text === 'string').map((p) => p.text).join(' ');
  }
  return '';
}

function commandText(t) {
  if (typeof t !== 'string') return t;
  const m = /<command-name>\s*([^<]*?)\s*<\/command-name>/.exec(t);
  if (!m) return t;
  const a = /<command-args>\s*([\s\S]*?)\s*<\/command-args>/.exec(t);
  const name = m[1].startsWith('/') ? m[1] : '/' + m[1];
  return a && a[1] ? `${name} ${a[1]}` : name;
}

function claudeMessages(raw, opts) {
  const readFile = opts.readFileSync || fs.readFileSync;
  let asked = '';
  let answered = '';
  try {
    const text = readFile(raw.transcript_path, 'utf8');
    for (const line of parseJsonl(text)) {
      if (!line || typeof line !== 'object') continue;
      if (line.type === 'user' && !line.isMeta && !line.isSidechain && !line.isCompactSummary) {
        const t = commandText(textFromClaudeContent(line.message && line.message.content));
        if (t && !t.trim().startsWith('<')) asked = t;
      } else if (line.type === 'assistant' && !line.isSidechain) {
        const content = line.message && line.message.content;
        const t = textFromClaudeContent(content);
        if (Array.isArray(content) && t) answered = t;
      }
    }
  } catch { /* no transcript, or unreadable: leave blank, never throw */ }
  const fromPayload = typeof raw.last_assistant_message === 'string' ? raw.last_assistant_message : '';
  return { asked: trim400(asked), answered: trim400(fromPayload || answered) };
}

function textFromCodexContent(content, type) {
  if (!Array.isArray(content)) return '';
  return content.filter((p) => p && p.type === type && typeof p.text === 'string').map((p) => p.text).join(' ');
}

function codexMessages(raw, opts) {
  const readFile = opts.readFileSync || fs.readFileSync;
  const rawAnswered = typeof raw.last_assistant_message === 'string' ? raw.last_assistant_message : '';
  let asked = '';
  let fallbackAnswered = '';
  try {
    const text = readFile(raw.transcript_path, 'utf8');
    for (const line of parseJsonl(text)) {
      const payload = line && line.payload;
      if (!payload || payload.type !== 'message') continue;
      if (payload.role === 'user') {
        const t = textFromCodexContent(payload.content, 'input_text');
        if (t && !t.trim().startsWith('<')) asked = t;
      } else if (payload.role === 'assistant') {
        const t = textFromCodexContent(payload.content, 'output_text');
        if (t) fallbackAnswered = t;
      }
    }
  } catch { /* no rollout, or unreadable: fine, we may still have last_assistant_message */ }
  return { asked: trim400(asked), answered: trim400(rawAnswered || fallbackAnswered) };
}

function entryFrom(input, opts = {}) {
  try {
    const raw = (input && input.raw) || {};
    let root = opts.root;
    if (!root) {
      const findRootFn = opts.findRoot || pointer.findRoot;
      const found = findRootFn(input && input.cwd);
      if (!found || !found.root) return null;
      root = found.root;
    }
    const tool = raw.turn_id !== undefined ? 'codex' : 'claude';
    const spawn = opts.spawnSync || spawnSync;
    const { branch, dirty } = gitInfo(root, spawn);
    const { asked, answered } = tool === 'codex' ? codexMessages(raw, opts) : claudeMessages(raw, opts);
    return { at: opts.now || new Date(), tool, branch, dirty, asked, answered };
  } catch {
    return null;
  }
}

function render(entries) {
  const blocks = entries.map((e) => {
    const header = `## ${fmtAt(e.at)} · ${e.tool} · ${e.branch} · ${fmtDirty(e.dirty)}`;
    return `${header}\n**Asked:** ${e.asked}\n**Answered:** ${e.answered}`;
  });
  return blocks.join('\n\n') + '\n';
}

function splitBlocks(text) {
  const trimmed = String(text || '').replace(/\s+$/, '');
  if (!trimmed) return [];
  return trimmed.split(/\n\n(?=## )/).filter((b) => b.startsWith('## '));
}

const MAX_BYTES = 4096;

function clampToBytes(blocks, maxBytes) {
  let kept = blocks;
  if (!kept.length) return '';
  let out = kept.join('\n\n') + '\n';
  while (Buffer.byteLength(out, 'utf8') > maxBytes && kept.length > 1) {
    kept = kept.slice(1);
    out = kept.join('\n\n') + '\n';
  }
  return out;
}

function append(root, entry, { max = 5, maxBytes = MAX_BYTES } = {}) {
  const file = path.join(root, TRAIL_REL);
  let existing = [];
  try {
    existing = splitBlocks(fs.readFileSync(file, 'utf8'));
  } catch { existing = []; }
  const newBlock = render([entry]).replace(/\n$/, '');
  let blocks = [...existing, newBlock];
  if (blocks.length > max) blocks = blocks.slice(blocks.length - max);
  const out = clampToBytes(blocks, maxBytes);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, out, 'utf8');
  return out;
}

function lastEntries(root, n, { maxBytes = MAX_BYTES } = {}) {
  let text;
  try {
    text = fs.readFileSync(path.join(root, TRAIL_REL), 'utf8');
  } catch {
    return '';
  }
  return clampToBytes(splitBlocks(text).slice(-n), maxBytes);
}

function run(input, deps = {}) {
  try {
    const findRootFn = deps.findRoot || pointer.findRoot;
    let root = deps.root;
    if (!root) {
      const found = findRootFn(input && input.cwd);
      if (!found || !found.root) return 0;
      root = found.root;
    }
    const entryFromFn = deps.entryFrom || entryFrom;
    const entry = entryFromFn(input, { ...deps, root });
    if (!entry) return 0;
    const appendFn = deps.append || append;
    appendFn(root, entry, deps.appendOpts);
  } catch { /* a broken trail must never block Stop */ }
  return 0;
}

function parseNoteArgs(argv) {
  const out = { asked: '', answered: '', tool: 'codex' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--asked') out.asked = argv[++i];
    else if (a === '--answered') out.answered = argv[++i];
    else if (a === '--tool') out.tool = argv[++i];
  }
  if (out.tool !== 'claude' && out.tool !== 'codex') out.tool = 'codex';
  return out;
}

function noteEntry(argv, deps = {}) {
  try {
    const findRootFn = deps.findRoot || pointer.findRoot;
    let root = deps.root;
    if (!root) {
      const found = findRootFn(deps.cwd || process.cwd());
      if (!found || !found.root) return 0;
      root = found.root;
    }
    const { asked, answered, tool } = parseNoteArgs(argv);
    const spawn = deps.spawnSync || spawnSync;
    const { branch, dirty } = gitInfo(root, spawn);
    const entry = { at: deps.now || new Date(), tool, branch, dirty, asked: trim400(asked), answered: trim400(answered) };
    const appendFn = deps.append || append;
    appendFn(root, entry, deps.appendOpts);
  } catch { /* never throw */ }
  return 0;
}

function main() {
  try {
    const argv = process.argv.slice(2);
    if (argv[0] === 'note') {
      noteEntry(argv.slice(1));
    } else {
      run(readHookInput());
    }
  } catch { /* never throw */ }
  process.exit(0);
}

if (require.main === module) main();

module.exports = {
  entryFrom, render, append, lastEntries, run, noteEntry, parseNoteArgs, gitInfo,
  trim400, stripNonHttpLinkTargets,
};
