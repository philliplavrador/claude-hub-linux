'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const norm = (p) => String(p).replace(/\\/g, '/');
const BLOCK_HEADER = /^# Compaction snapshot /;
const SUMMARY_HEADER = /^## Summary Claude Code wrote /;

const RULES = "Keep to the task you were given and the files it names. Every subagent or background job the summary mentions is unverified since the compaction: check before trusting it.";

function snapshotDir(env = process.env) {
  const config = env.CLAUDE_CONFIG_DIR || path.join(env.HOME || os.homedir(), '.claude');
  return norm(path.join(config, 'compactions'));
}

const safe = (s) => String(s || '').replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^[._]+/, '').slice(0, 80);

function snapshotKey(sessionId, agentId) {
  const sid = safe(sessionId) || 'unknown-session';
  const aid = safe(agentId);
  return aid ? `${sid}.agent-${aid}` : sid;
}

function snapshotPath(dir, key) {
  return norm(path.join(dir, `${safe(key) || 'unknown-session'}.md`));
}

function cardPath(dir, key) {
  return norm(path.join(dir, `${safe(key) || 'unknown-session'}.card.md`));
}

function subagentTranscript(mainTranscript, sessionId, agentId) {
  if (!mainTranscript || !sessionId || !agentId) return null;
  return norm(path.join(path.dirname(String(mainTranscript)), String(sessionId), 'subagents', `agent-${safe(agentId)}.jsonl`));
}

function describeSnapshot(file, deps = {}) {
  const read = deps.readFileSync || fs.readFileSync;
  let text;
  try {
    text = read(file, 'utf8');
  } catch {
    return { exists: false, blocks: 0, lastBlockLine: 0, summaries: 0, summaryOnly: false };
  }
  let blocks = 0;
  let lastBlockLine = 0;
  let summaries = 0;
  String(text).split(/\r?\n/).forEach((line, i) => {
    if (BLOCK_HEADER.test(line)) { blocks += 1; lastBlockLine = i + 1; }
    if (SUMMARY_HEADER.test(line)) summaries += 1;
  });
  return { exists: true, blocks, lastBlockLine, summaries, summaryOnly: blocks === 0 && summaries > 0 };
}

module.exports = { RULES, snapshotDir, snapshotKey, snapshotPath, cardPath, subagentTranscript, describeSnapshot };
