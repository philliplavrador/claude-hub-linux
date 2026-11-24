#!/usr/bin/env node
'use strict';

const { readHookInput } = require('../lib/input');

function blankQuotes(cmd) {
  const out = cmd.split('');
  const blank = (a, b) => { for (let i = a; i < b; i++) if (out[i] !== '\n') out[i] = ' '; };
  const heredocs = [];
  let i = 0;
  while (i < cmd.length) {
    const c = cmd[i];
    if (c === '\\') { i += 2; continue; }
    if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < cmd.length && cmd[j] !== c) j += cmd[j] === '\\' && c !== "'" ? 2 : 1;
      if (c !== '"' || !/\$\(/.test(cmd.slice(i, j))) blank(i + 1, Math.min(j, cmd.length));
      i = j + 1;
      continue;
    }
    const h = /^<<-?\s*(['"]?)([A-Za-z_][\w]*)\1/.exec(cmd.slice(i));
    if (h) {
      heredocs.push(h[2]);
      i += h[0].length;
      continue;
    }
    if (c === '\n' && heredocs.length) {
      let j = i + 1;
      for (const delim of heredocs.splice(0)) {
        const end = new RegExp('^\\s*' + delim + '\\s*$', 'm').exec(cmd.slice(j));
        const stop = end ? j + end.index : cmd.length;
        blank(j, stop);
        j = end ? stop + end[0].length : cmd.length;
      }
      i = j;
      continue;
    }
    i++;
  }
  return out.join('');
}

const CMD_POS = String.raw`(?:^|[\n;|&(]|&&|\|\||\bthen\b|\bdo\b|\bsudo\s+(?:-\S+\s+(?:[a-z_][\w-]*\s+)?)*|\bcommand\s+|\btimeout\s+\S+\s+|\benv\s+(?:\S+=\S*\s+)*)\s*(?:\S+=\S*\s+)*(?:\S*\/)?`;
const RULES = [
  [/claude-desks\s+(?:goto|resume|focus)\b/, 'claude-desks goto|resume|focus'],
  [/claude-desks\s+adopt\b[^\n;|&]*--goto\b/, 'claude-desks adopt --goto'],
  [/claude-desks\s+new\b(?![^\n;|&]*--stay\b)/, 'claude-desks new without --stay'],
  [/wmctrl\s+(?:[^\n;|&]*\s)?-[so]\b/, 'wmctrl -s'],
  [/xdotool\s+(?:set_desktop|key\b[^\n;|&]*(?:super|meta)\+\d)/i, 'xdotool desktop switch'],
  [/(?:qdbus6?|qdbus-qt\d)\s+org\.kde\.KWin\s+\/VirtualDesktopManager\s+\S*\.current\s+\S/, 'qdbus VirtualDesktopManager.current <id>'],
  [/(?:qdbus6?|qdbus-qt\d)\s+org\.kde\.KWin\s+\/KWin\s+\S*(?:setCurrentDesktop|nextDesktop|previousDesktop)/, 'KWin setCurrentDesktop'],
  [/(?:gdbus\s+call|dbus-send)\b[^\n;|&]*(?:setCurrentDesktop|VirtualDesktopManager)[^\n;|&]*current/, 'dbus current-desktop set'],
  [/(?:qdbus6?|gdbus\s+call\b[^\n]*)\s*org\.kde\.kglobalaccel\b[^\n;|&]*Switch to Desktop/i, 'KWin switch-desktop shortcut'],
];

function verdict(command) {
  if (/(?:^|\s)CLAUDE_HUB_DESKTOP_SWITCH_OK=1\b/.test(command.split('\n')[0] || '')) return null;
  const text = blankQuotes(command);
  for (const [re, label] of RULES) {
    const at = new RegExp(CMD_POS + re.source, re.flags);
    if (at.test(text)) {
      return `Refused: ${label} would switch the desktop the user is looking at, and no Claude may do that. `
        + 'Place windows by desktop id instead (`claude-desks whoami`, `claim`, `place`), start sessions with `claude-desks new --stay`, '
        + 'and read the current desktop without setting it. If he asked in this turn to be taken to a desktop, start the command with CLAUDE_HUB_DESKTOP_SWITCH_OK=1.';
    }
  }
  return null;
}

function main() {
  const input = readHookInput();
  const command = String((input.input && (input.input.command || input.input.cmd)) || '');
  if (!command) return;
  const reason = verdict(command);
  if (!reason) return;
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason },
  }));
}

if (require.main === module) {
  try { main(); } catch { /* fail open */ }
}
module.exports = { verdict, blankQuotes };
