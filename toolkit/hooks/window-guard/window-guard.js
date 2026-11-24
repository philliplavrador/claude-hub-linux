#!/usr/bin/env node
/**
 * PreToolUse hook (Bash | PowerShell): refuse a command that would open a visible terminal window on the user's
 * desktop. Windows that pile up get closed, and closing one kills whatever runs inside it.
 *
 * It refuses:
 *   - Start-Process / start / saps that launches a console program (cmd, powershell,
 *     pwsh, node, npm, npx, python, bash, a .cmd/.bat/.ps1) without -WindowStyle
 *     Hidden or -NoNewWindow
 *   - cmd /c start ... (except start /b, which stays in the same console)
 *   - wt / wt.exe and conhost.exe
 * GUI programs and URLs (chrome, code, explorer, https://...) pass: they open no
 * console. Anything it cannot recognise passes too. The guard is only there to catch
 * the obvious cases.
 *
 * Lives in the Toolkit store; ~/.claude/tools/window-guard.js is a forwarding shim.
 */
'use strict';

const path = require('path');
const { readHookInput } = require('../lib/input');

const BG = path.join(__dirname, '..', 'bg', 'bg.js').replace(/\\/g, '/');
const CONSOLE_PROGRAM = /^["']?(?:.*[\\/])?(cmd|powershell|pwsh|node|npm|npx|pnpm|yarn|python\d*|py|bash|sh|wsl|git-bash|conhost|openconsole|wt)(\.exe)?["']?$|\.(cmd|bat|ps1)["']?$/i;

function refusals(command) {
  const text = command.replace(/`\r?\n/g, ' '); // PowerShell line continuations
  const found = [];

  const sp = /(^|[\n;|&({=]|&&|\|\|)\s*(?:Start-Process|saps|start)\s+([^\n;|]*)/gi;
  let m;
  while ((m = sp.exec(text))) {
    const args = m[2];
    if (/-(WindowStyle|win\w*|w)\s+['"]?hidden/i.test(args) || /-NoNewWindow\b/i.test(args)) continue;
    if (/^\/b\b/i.test(args.trim())) continue; // cmd's start /b: same console, no window
    const fp = /-FilePath\s+("[^"]+"|'[^']+'|\S+)/i.exec(args);
    const target = fp ? fp[1] : (args.trim().match(/^("[^"]*"|'[^']*'|\S+)/) || [''])[0];
    const afterTitle = /^"[^"]*"\s+("[^"]*"|\S+)/.exec(args.trim());
    const candidates = [target, afterTitle ? afterTitle[1] : ''].filter(Boolean);
    if (candidates.some((c) => CONSOLE_PROGRAM.test(c))) found.push(m[0].trim().slice(0, 120));
  }

  const cmdStart = /\bcmd(\.exe)?\s+\/[ck]\s+"?\s*start\s+(?!\/b\b)[^\n;|]*/gi;
  while ((m = cmdStart.exec(text))) found.push(m[0].trim().slice(0, 120));

  const terminal = /(^|[\n;|&])\s*(wt|wt\.exe|conhost\.exe|conhost)\s/gi;
  while ((m = terminal.exec(text))) found.push(m[0].trim().slice(0, 120));

  return [...new Set(found)];
}

function main() {
  const input = readHookInput();
  const command = String((input.input && (input.input.command || input.input.cmd)) || '');
  if (!command) return;
  const hits = refusals(command);
  if (!hits.length) return;
  const reason = [
    `This would open a visible terminal window: ${hits.join(' | ')}`,
    'Closing a window kills what runs inside it.',
    `Run it with no window instead:  node "${BG}" start --name <short-name> -- <command>`,
    `(or add -WindowStyle Hidden to Start-Process). Only if a human must watch or type into it: node "${BG}" start --visible --name <name> -- <command>`,
    'and expect it to be closed at any time.',
  ].join('\n');
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason },
  }));
}

if (require.main === module) main();
module.exports = { refusals, main };
