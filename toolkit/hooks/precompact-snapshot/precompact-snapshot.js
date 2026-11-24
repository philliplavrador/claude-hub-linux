#!/usr/bin/env node
'use strict';
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { readHookInput } = require('../lib/input');
const { RULES, snapshotDir, snapshotKey, snapshotPath, cardPath, subagentTranscript } = require('../lib/compactions');
const { isHelperCompaction, writeManualMarker } = require('../lib/helper-compaction');

const MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024;
const MAX_MESSAGE_CHARS = 2000;
const MAX_FIRST_ASK_CHARS = 3000;
const MAX_BRIEF_CHARS = 40000;
const MAX_RESULT_CHARS = 600;
const MAX_STATUS_LINES = 60;
const MAX_JOB_LINES = 40;
const MAX_FILES = 60;
const MAX_TASKS = 30;
const MAX_ANSWERS = 8;
const MAX_SENT = 10;
const LAST_MESSAGES = 5;
const CARD_CHARS = 3000;
const INSTRUCTION_CHARS = 1500;
const SPAWN_TIMEOUT_MS = 3000;
const NOISE = /^\s*(<task-notification|<local-command|<command-name|<command-message|<command-args|<system-reminder|<bash-(input|stdout|stderr)|\[Request interrupted)/;
const BRIEF_MARK = /<!-- brief ([0-9a-f]{12}) -->/g;
const EDIT_TOOLS = new Set(['Edit', 'Write', 'NotebookEdit', 'MultiEdit']);
const STATE_FILE = /(^|\/)(STATE|PLAN|plan|spec|TODO|HANDOFF)[^/]*\.md$|\/knowledge\/(research|handoffs)\/[^/]+\/[^/]+\.md$/;

function parseJsonl(text) {
  const out = [];
  for (const line of String(text || '').split(/\r?\n/)) {
    if (!line.trim()) continue;
    try { out.push(JSON.parse(line)); } catch { /* skip */ }
  }
  return out;
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((b) => b && b.type === 'text' && typeof b.text === 'string').map((b) => b.text).join('\n');
}

function resultText(block) {
  if (!block) return '';
  if (typeof block.content === 'string') return block.content;
  return textOf(block.content);
}

function cap(s, n) {
  const t = String(s || '');
  return t.length > n ? `${t.slice(0, n)}\n… [${t.length - n} more characters in the transcript]` : t;
}

const oneLine = (s) => String(s || '').replace(/\s+/g, ' ').trim();
const briefKey = (prompt) => crypto.createHash('sha1').update(String(prompt)).digest('hex').slice(0, 12);

function scope(lines, opts = {}) {
  return opts.includeSidechain ? lines : lines.filter((l) => !(l && l.isSidechain));
}

function toolUses(lines, names, opts = {}) {
  const out = [];
  for (const line of scope(lines, opts)) {
    if (!line || line.type !== 'assistant') continue;
    const content = line.message && line.message.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (!block || block.type !== 'tool_use' || (names && !names.has(block.name))) continue;
      out.push({ id: block.id || null, name: block.name, input: block.input || {}, at: line.timestamp || null });
    }
  }
  return out;
}

function toolResults(lines, opts = {}) {
  const map = new Map();
  for (const line of scope(lines, opts)) {
    if (!line || line.type !== 'user') continue;
    const content = line.message && line.message.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block && block.type === 'tool_result' && block.tool_use_id) map.set(block.tool_use_id, { text: resultText(block), at: line.timestamp || null });
    }
  }
  return map;
}

function agentBriefs(lines, opts = {}) {
  const results = toolResults(lines, opts);
  return toolUses(lines, new Set(['Agent', 'Task']), opts).map((u) => {
    const prompt = String(u.input.prompt || '');
    const r = results.get(u.id);
    const m = r && /agentId: ([A-Za-z0-9_-]+)/.exec(r.text);
    const launched = Boolean(r && /Async agent launched/.test(r.text));
    return {
      at: u.at,
      toolUseId: u.id,
      agentId: m ? m[1] : null,
      description: oneLine(u.input.description),
      subagentType: oneLine(u.input.subagent_type),
      model: oneLine(u.input.model),
      prompt,
      key: briefKey(prompt),
      report: r && !launched && r.text.trim() ? cap(r.text.trim(), MAX_RESULT_CHARS) : null,
    };
  });
}

function notifications(lines, opts = {}) {
  const map = new Map();
  const grab = (text) => {
    const t = String(text || '');
    for (const m of t.matchAll(/<task-notification>([\s\S]*?)<\/task-notification>/g)) {
      const body = m[1];
      const id = (/<task-id>([^<]+)<\/task-id>/.exec(body) || [])[1];
      if (!id) continue;
      map.set(id.trim(), {
        status: ((/<status>([^<]+)<\/status>/.exec(body) || [])[1] || 'unknown').trim(),
        summary: oneLine((/<summary>([\s\S]*?)<\/summary>/.exec(body) || [])[1] || ''),
        result: cap(String((/<result>([\s\S]*?)<\/result>/.exec(body) || [])[1] || '').trim(), MAX_RESULT_CHARS),
      });
    }
  };
  for (const line of scope(lines, opts)) {
    if (!line || line.type !== 'user') continue;
    grab(textOf(line.message && line.message.content));
    if (line.attachment && line.attachment.type === 'queued_command') grab(line.attachment.prompt);
  }
  return map;
}

function humanTexts(line) {
  const out = [];
  const origin = line.origin && line.origin.kind;
  const content = line.message && line.message.content;
  const toolResult = Array.isArray(content) && content.some((b) => b && b.type === 'tool_result');
  if (!toolResult && origin !== 'task-notification' && !line.isMeta) {
    const text = textOf(content).trim();
    if (text && (origin === 'human' || !NOISE.test(text))) out.push(text);
  }
  const attachment = line.attachment;
  if (attachment && attachment.type === 'queued_command' && typeof attachment.prompt === 'string') {
    const text = attachment.prompt.trim();
    if (text && !NOISE.test(text)) out.push(text);
  }
  return out;
}

function humanMessages(lines, opts = {}) {
  const texts = [];
  for (const line of scope(lines, opts)) {
    if (!line || line.type !== 'user' || line.isCompactSummary) continue;
    for (const text of humanTexts(line)) texts.push({ at: line.timestamp || null, text });
  }
  return texts;
}

function lastUserMessages(lines, n = LAST_MESSAGES) {
  return humanMessages(lines).slice(-n).map((m) => ({ at: m.at, text: cap(m.text, MAX_MESSAGE_CHARS) }));
}

function decisions(lines, opts = {}) {
  const results = toolResults(lines, opts);
  const out = [];
  for (const u of toolUses(lines, new Set(['AskUserQuestion']), opts)) {
    const r = results.get(u.id);
    if (!r) continue;
    const questions = Array.isArray(u.input.questions) ? u.input.questions.map((q) => oneLine(q && q.question)).filter(Boolean) : [];
    out.push({ at: r.at || u.at, question: cap(questions.join(' | '), 300), answer: cap(oneLine(r.text), 500) });
  }
  return out;
}

function messagesToAgents(lines, opts = {}) {
  return toolUses(lines, new Set(['SendMessage']), opts).map((u) => ({
    at: u.at, to: oneLine(u.input.to), summary: oneLine(u.input.summary), message: cap(String(u.input.message || ''), 800),
  }));
}

const inputPath = (input) => String(input.file_path || input.path || input.notebook_path || '');

function filesEdited(lines, opts = {}) {
  const seen = new Set();
  for (const u of toolUses(lines, EDIT_TOOLS, opts)) { const p = inputPath(u.input); if (p) { seen.delete(p); seen.add(p); } }
  return [...seen];
}

function stateFiles(lines, opts = {}) {
  const seen = new Set();
  for (const u of toolUses(lines, new Set(['Read', ...EDIT_TOOLS]), opts)) {
    const p = inputPath(u.input);
    if (p && STATE_FILE.test(p)) { seen.delete(p); seen.add(p); }
  }
  return [...seen];
}

function tasks(lines, opts = {}) {
  const results = toolResults(lines, opts);
  const byId = new Map();
  for (const u of toolUses(lines, new Set(['TaskCreate', 'TaskUpdate']), opts)) {
    if (u.name === 'TaskCreate') {
      const r = results.get(u.id);
      const m = r && /Task #(\d+)/.exec(r.text);
      const id = m ? m[1] : `?${byId.size + 1}`;
      byId.set(id, { id, subject: cap(oneLine(u.input.subject), 120), status: 'pending' });
    } else if (u.input.taskId !== undefined) {
      const id = String(u.input.taskId);
      const t = byId.get(id) || { id, subject: '(created before this transcript)', status: 'pending' };
      if (u.input.status) t.status = String(u.input.status);
      if (u.input.subject) t.subject = cap(oneLine(u.input.subject), 120);
      byId.set(id, t);
    }
  }
  return [...byId.values()].filter((t) => t.status !== 'deleted');
}

function spawnText(spawn, cmd, args, cwd) {
  try {
    const r = spawn(cmd, args, { cwd, encoding: 'utf8', timeout: SPAWN_TIMEOUT_MS, windowsHide: true });
    if (!r || r.error || r.status !== 0) return null;
    return String(r.stdout || '').replace(/\s+$/, '');
  } catch {
    return null;
  }
}

function gitStatus(cwd, spawn = spawnSync) {
  const branch = spawnText(spawn, 'git', ['--no-optional-locks', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd);
  if (branch === null) return null;
  const status = spawnText(spawn, 'git', ['--no-optional-locks', 'status', '--short'], cwd);
  const lines = String(status || '').split(/\r?\n/).filter(Boolean);
  const shown = lines.slice(0, MAX_STATUS_LINES);
  if (lines.length > MAX_STATUS_LINES) shown.push(`… ${lines.length - MAX_STATUS_LINES} more`);
  return `branch ${branch}\n${shown.length ? shown.join('\n') : '(clean)'}`;
}

function bgJobs(env = process.env, deps = {}) {
  const readdir = deps.readdirSync || fs.readdirSync;
  const read = deps.readFileSync || fs.readFileSync;
  const exists = deps.existsSync || fs.existsSync;
  const home = env.BG_HOME || path.join(env.HOME || os.homedir(), '.claude', 'bg');
  let names;
  try { names = readdir(home); } catch { return null; }
  const rows = [];
  for (const name of names.filter((n) => n.endsWith('.entry.json')).sort()) {
    const id = name.slice(0, -'.entry.json'.length);
    if (exists(path.join(home, `${id}.exit.json`))) continue;
    let entry;
    try { entry = JSON.parse(read(path.join(home, name), 'utf8')); } catch { continue; }
    const cmd = Array.isArray(entry.command) ? entry.command.join(' ') : String(entry.command || '');
    const owner = entry.owner && entry.owner.sessionId ? ` owner session ${entry.owner.sessionId}` : '';
    rows.push(`${entry.name || id}  started ${entry.startedAt || '?'}${owner}  cmd: ${cap(cmd, 160).replace(/\n/g, ' ')}`);
    if (rows.length >= MAX_JOB_LINES) { rows.push('…'); break; }
  }
  return rows.length ? rows.join('\n') : null;
}

const stamp = (d) => new Date(d).toISOString().replace(/\.\d{3}Z$/, 'Z');
const fenced = (s) => ['```text', String(s).replace(/```/g, '` ` `'), '```'];

function knownBriefKeys(existingText) {
  const keys = new Set();
  for (const m of String(existingText || '').matchAll(BRIEF_MARK)) keys.add(m[1]);
  return keys;
}

function collect({ lines, subagent, known }) {
  const opts = { includeSidechain: Boolean(subagent) };
  const briefs = agentBriefs(lines, opts);
  const notes = notifications(lines, opts);
  const finished = [];
  const running = [];
  for (const b of briefs) {
    const n = b.agentId ? notes.get(b.agentId) : null;
    if (n && n.status === 'completed') finished.push({ ...b, note: n });
    else if (b.report) finished.push({ ...b, note: { status: 'returned', summary: '', result: b.report } });
    else running.push({ ...b, note: n || null });
  }
  const messages = humanMessages(lines, opts);
  return {
    subagent,
    known: known || new Set(),
    first: messages.length ? { at: messages[0].at, text: cap(messages[0].text, subagent ? MAX_BRIEF_CHARS : MAX_FIRST_ASK_CHARS) } : null,
    latest: messages.slice(1).slice(-LAST_MESSAGES).map((m) => ({ at: m.at, text: cap(m.text, MAX_MESSAGE_CHARS) })),
    answers: decisions(lines, opts).slice(-MAX_ANSWERS),
    running,
    finished,
    sent: messagesToAgents(lines, opts).slice(-MAX_SENT),
    stateFiles: stateFiles(lines, opts).slice(-10),
    edited: filesEdited(lines, opts),
    tasks: tasks(lines, opts).slice(-MAX_TASKS),
  };
}

function briefSection(b, i, known, label) {
  const head = [`${i}. ${b.description || '(no description)'}`, b.subagentType || 'default'];
  if (b.agentId) head.push(`agent ${b.agentId}`);
  if (b.model) head.push(b.model);
  if (b.at) head.push(b.at);
  if (label) head.push(label);
  const out = ['', `### ${head.join(' · ')}`, ''];
  if (b.note && b.note.summary) out.push(`Reported: ${b.note.summary}`, '');
  if (b.note && b.note.result) out.push('Result excerpt:', ...fenced(b.note.result), '');
  if (known.has(b.key)) out.push(`(full brief in an earlier block of this file, marker brief ${b.key})`);
  else out.push(`<!-- brief ${b.key} -->`, ...fenced(cap(b.prompt, MAX_BRIEF_CHARS)));
  return out;
}

function renderSnapshot({ now, raw, data, git, bg, transcriptNote, transcriptRead }) {
  const t = raw || {};
  const d = data;
  const who = d.subagent ? ` · subagent ${d.subagent} of session ${t.session_id || 'unknown'}` : '';
  const out = [`# Compaction snapshot · ${stamp(now)} · trigger ${t.trigger || 'unknown'}${who}`, ''];
  out.push(`- session: ${t.session_id || 'unknown'}`);
  if (d.subagent) out.push(`- agent: ${d.subagent}`);
  out.push(`- transcript: ${transcriptRead || t.transcript_path || 'unknown'}`);
  out.push(`- cwd: ${t.cwd || 'unknown'}`);
  if (t.custom_instructions) out.push(`- compact instructions given: ${oneLine(t.custom_instructions)}`);
  if (transcriptNote) out.push(`- transcript note: ${transcriptNote}`);
  out.push('', '## Rules that survive a compaction', RULES);
  if (d.subagent) {
    out.push('', '## Your brief (the first message this subagent received)');
    if (d.first) out.push(...fenced(d.first.text)); else out.push('(none found)');
    out.push('', `## Messages since, from the orchestrator (${d.latest.length}, oldest first)`);
  } else {
    out.push('', '## What he asked first');
    if (d.first) out.push(`- ${d.first.at || 'no timestamp'}:`, '', ...fenced(d.first.text)); else out.push('(none found)');
    out.push('', `## His latest messages (${d.latest.length}, oldest first)`);
  }
  if (!d.latest.length) out.push('(none)');
  for (const m of d.latest) out.push('', `- ${m.at || 'no timestamp'}:`, '', ...fenced(m.text));
  out.push('', `## The user's answers to questions (${d.answers.length})`);
  if (!d.answers.length) out.push('(none)');
  for (const a of d.answers) out.push(`- ${a.at || 'no timestamp'}: Q: ${a.question} → ${a.answer}`);
  out.push('', `## Agents still running or unverified (${d.running.length})`);
  if (!d.running.length) out.push('(none)');
  d.running.forEach((b, i) => out.push(...briefSection(b, i + 1, d.known, b.note ? b.note.status : 'no completion seen')));
  out.push('', `## Messages sent to agents (${d.sent.length}, oldest first)`);
  if (!d.sent.length) out.push('(none)');
  for (const s of d.sent) out.push('', `- ${s.at || 'no timestamp'} → ${s.to || '?'}${s.summary ? ` (${s.summary})` : ''}:`, '', ...fenced(s.message));
  out.push('', `## State and plan files this session touched (${d.stateFiles.length})`);
  out.push(d.stateFiles.length ? d.stateFiles.map((p) => `- ${p}`).join('\n') : '(none)');
  out.push('', `## Files edited (${d.edited.length})`);
  const shownFiles = d.edited.slice(-MAX_FILES);
  out.push(shownFiles.length ? shownFiles.map((p) => `- ${p}`).join('\n') + (d.edited.length > MAX_FILES ? `\n- … ${d.edited.length - MAX_FILES} more` : '') : '(none)');
  out.push('', `## Tasks (${d.tasks.length})`);
  out.push(d.tasks.length ? d.tasks.map((k) => `- #${k.id} ${k.subject} [${k.status}]`).join('\n') : '(none)');
  out.push('', '## Git status', git || '(not a git checkout, or git did not answer)');
  out.push('', '## Background jobs (bg records without an exit)', bg || '(none)');
  out.push('', `## Finished agents (${d.finished.length})`);
  if (!d.finished.length) out.push('(none)');
  d.finished.forEach((b, i) => out.push(...briefSection(b, i + 1, d.known, b.note && b.note.status === 'returned' ? 'returned its report' : 'completed')));
  out.push('');
  return out.join('\n');
}

function renderCard({ now, raw, data, file, line }) {
  const t = raw || {};
  const d = data;
  const out = [`After compaction · ${stamp(now)} · full snapshot: ${file} (latest block from line ${line}; Read it with offset ${line})`];
  out.push(`Rules: ${RULES}`);
  if (d.subagent) {
    out.push(`You are subagent ${d.subagent} of session ${t.session_id || 'unknown'}. Your brief, in full, is the first section of that block.`);
    if (d.first) out.push(`Your brief begins: ${oneLine(cap(d.first.text, 400))}`);
  } else if (d.first) {
    out.push(`He asked first: ${oneLine(cap(d.first.text, 500))}`);
  }
  if (d.latest.length) out.push(`His latest message: ${oneLine(cap(d.latest[d.latest.length - 1].text, 400))}`);
  for (const a of d.answers.slice(-3)) out.push(`He answered: ${oneLine(cap(a.question, 120))} → ${oneLine(cap(a.answer, 200))}`);
  if (d.running.length) out.push(`Agents still running or unverified (${d.running.length}): ${d.running.map((b) => `${b.agentId ? `${b.agentId} ` : ''}${b.description || '(no description)'}`).join('; ')}`);
  if (d.stateFiles.length) out.push(`State and plan files: ${d.stateFiles.join(', ')}`);
  if (d.edited.length) out.push(`Files edited: ${d.edited.length}, latest ${d.edited.slice(-3).join(', ')}`);
  if (d.tasks.length) out.push(`Tasks open: ${d.tasks.filter((k) => k.status !== 'completed').map((k) => `#${k.id} ${k.subject}`).join('; ') || 'none'}`);
  let text = out.join('\n');
  if (text.length > CARD_CHARS) text = `${text.slice(0, CARD_CHARS)}\n… [card truncated; the snapshot has the rest]`;
  return `${text}\n`;
}

function renderInstructions({ data, file, line }) {
  const d = data;
  const parts = [];
  if (d.running.length) parts.push(`keep word for word the ids and descriptions of the agents still running or unverified (${d.running.map((b) => `${b.agentId || 'id unknown'}: ${b.description || 'no description'}`).join('; ')}) and mark every agent result "status unverified since compaction"`);
  if (d.stateFiles.length) parts.push(`keep these paths: ${d.stateFiles.join(', ')}`);
  if (d.latest.length) parts.push(`keep his latest message word for word: "${oneLine(cap(d.latest[d.latest.length - 1].text, 300))}"`);
  if (d.answers.length) parts.push(`keep the user's answers to questions (${d.answers.length}, listed in the snapshot)`);
  if (d.edited.length) parts.push(`keep the list of files edited (${d.edited.length}, in the snapshot)`);
  parts.push(`state that the full snapshot is at ${file} (latest block from line ${line}).`);
  let text = `Compact instructions from the pre-compaction snapshot hook: ${parts.join('; ')}`;
  if (text.length > INSTRUCTION_CHARS) text = `${text.slice(0, INSTRUCTION_CHARS - 1)}…`;
  return `${text}\n`;
}

function renderSummary({ now, raw }) {
  const t = raw || {};
  return [
    `## Summary Claude Code wrote · ${stamp(now)} · trigger ${t.trigger || 'unknown'}`,
    '',
    String(t.compact_summary || '(PostCompact carried no summary)').replace(/\s+$/, ''),
    '', '',
  ].join('\n');
}

function readTranscript(file, deps) {
  const stat = deps.statSync || fs.statSync;
  const read = deps.readFileSync || fs.readFileSync;
  if (!file) return { lines: [], note: 'no transcript_path in the hook input' };
  try {
    const size = stat(file).size;
    if (size > MAX_TRANSCRIPT_BYTES) return { lines: [], note: `transcript is ${size} bytes, over the ${MAX_TRANSCRIPT_BYTES} byte cap; briefs not extracted` };
    return { lines: parseJsonl(read(file, 'utf8')), note: null };
  } catch (e) {
    return { lines: [], note: `transcript unreadable: ${(e && e.message) || e}` };
  }
}

function ensureDir(dir, mkdir) {
  const resolved = path.resolve(dir);
  let current = path.parse(resolved).root;
  for (const part of resolved.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { mkdir(current, { mode: 0o700 }); } catch (e) { if (!e || e.code !== 'EEXIST') throw e; }
  }
}

function appendBlock(file, text, deps) {
  const mkdir = deps.mkdirSync || fs.mkdirSync;
  const append = deps.appendFileSync || fs.appendFileSync;
  ensureDir(path.dirname(file), mkdir);
  append(file, text, { encoding: 'utf8', mode: 0o600 });
}

function writeCard(file, text, deps) {
  const mkdir = deps.mkdirSync || fs.mkdirSync;
  const write = deps.writeFileSync || fs.writeFileSync;
  ensureDir(path.dirname(file), mkdir);
  write(file, text, { encoding: 'utf8', mode: 0o600 });
}

function readExisting(file, deps) {
  const read = deps.readFileSync || fs.readFileSync;
  try { return read(file, 'utf8'); } catch { return ''; }
}

function run(input, deps = {}) {
  try {
    const raw = (input && input.raw) || {};
    const event = raw.hook_event_name || (input && input.event) || (raw.compact_summary !== undefined ? 'PostCompact' : 'PreCompact');
    const env = deps.env || process.env;
    const cwd = (input && input.cwd) || raw.cwd || process.cwd();
    const sessionId = (input && input.sessionId) || raw.session_id;
    const agentId = raw.agent_id ? String(raw.agent_id) : null;
    const dir = (deps.snapshotDir || snapshotDir)(env);
    const key = snapshotKey(sessionId, agentId);
    const file = snapshotPath(dir, key);
    const card = cardPath(dir, key);
    const now = deps.now || new Date();
    if (event === 'PreCompact') writeManualMarker({ ...raw, hook_event_name: event }, { env, now });
    if (!agentId && isHelperCompaction({ ...raw, hook_event_name: event }, { env, now })) return { event, helper: true };
    if (event === 'PostCompact') {
      appendBlock(file, renderSummary({ now, raw }), deps);
      return { file, card, event, agentId };
    }
    const exists = deps.existsSync || fs.existsSync;
    let transcript = raw.transcript_path || null;
    if (agentId) {
      const own = subagentTranscript(raw.transcript_path, sessionId, agentId);
      if (own && exists(own)) transcript = own;
    }
    const { lines, note } = readTranscript(transcript, deps);
    const existing = readExisting(file, deps);
    const line = existing ? existing.split(/\r?\n/).length : 1;
    const data = collect({ lines, subagent: agentId, known: knownBriefKeys(existing) });
    const spawn = deps.spawnSync || spawnSync;
    const block = renderSnapshot({ now, raw, data, git: gitStatus(cwd, spawn), bg: bgJobs(env, deps), transcriptNote: note, transcriptRead: transcript });
    appendBlock(file, `${block}\n`, deps);
    writeCard(card, renderCard({ now, raw, data, file, line }), deps);
    try { (deps.stdout || process.stdout).write(renderInstructions({ data, file, line })); } catch { /* a closed stdout never blocks */ }
    return { file, card, event: 'PreCompact', agentId };
  } catch (e) {
    try { (deps.stderr || process.stderr).write(`precompact-snapshot: ${(e && e.message) || e}\n`); } catch { /* ignore */ }
    return null;
  }
}

function main() {
  try {
    run(readHookInput());
  } catch { /* never block a compaction */ }
  process.exit(0);
}

if (require.main === module) main();

module.exports = {
  parseJsonl, textOf, agentBriefs, notifications, humanTexts, humanMessages, lastUserMessages, decisions,
  messagesToAgents, filesEdited, stateFiles, tasks, gitStatus, bgJobs, knownBriefKeys, collect,
  renderSnapshot, renderCard, renderInstructions, renderSummary, ensureDir, run,
};
