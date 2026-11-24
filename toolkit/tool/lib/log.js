'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { ensureDir, readText } = require('./fsx');

function appendLog(P, entry) {
  ensureDir(path.dirname(P.logFile));
  fs.appendFileSync(P.logFile, JSON.stringify({ time: new Date().toISOString(), ...entry }) + '\n');
}

function readLog(P, n = 200) {
  const text = readText(P.logFile);
  if (!text) return [];
  const lines = text.split('\n').filter(Boolean);
  const lastLines = lines.slice(-n);
  const entries = [];
  for (const line of lastLines) {
    try {
      entries.push(JSON.parse(line));
    } catch {
    }
  }
  return entries;
}

module.exports = { appendLog, readLog };
