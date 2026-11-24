'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

function norm(p) {
  if (p == null) return p;
  let s = String(p).replace(/\\/g, '/');
  if (s.length > 1) s = s.replace(/\/+$/, '');
  return s;
}

function same(a, b) {
  if (a == null || b == null) return false;
  return norm(a).toLowerCase() === norm(b).toLowerCase();
}

function findCodexExe(home) {
  const ext = path.join(home, '.vscode', 'extensions');
  if (!fs.existsSync(ext)) return null;
  const dirs = fs.readdirSync(ext)
    .filter((d) => d.startsWith('openai.chatgpt-'));

  dirs.sort((a, b) => {
    const versionA = a.match(/^openai\.chatgpt-(\d+(?:\.\d+)*)-/)?.[1];
    const versionB = b.match(/^openai\.chatgpt-(\d+(?:\.\d+)*)-/)?.[1];
    if (!versionA || !versionB) return 0;

    const partsA = versionA.split('.').map(Number);
    const partsB = versionB.split('.').map(Number);
    const maxLen = Math.max(partsA.length, partsB.length);

    for (let i = 0; i < maxLen; i++) {
      const pA = partsA[i] || 0;
      const pB = partsB[i] || 0;
      if (pA !== pB) return pB - pA; // Descending order (highest first)
    }
    return 0;
  });

  for (const d of dirs) {
    const exe = path.join(ext, d, 'bin', 'windows-x86_64', 'codex.exe');
    if (fs.existsSync(exe)) return norm(exe);
  }
  return null;
}

function resolvePaths(opts = {}) {
  const env = opts.env || process.env;
  const home = norm(opts.home || env.TOOLKIT_HOME || os.homedir());
  const agentsHome = `${home}/.agents`;
  const machineFile = `${agentsHome}/toolkit-machine.json`;
  let machine = {};
  if (fs.existsSync(machineFile)) {
    machine = JSON.parse(fs.readFileSync(machineFile, 'utf8'));
  }
  const store = norm(opts.storeOverride || env.TOOLKIT_STORE || machine.store || path.resolve(__dirname, '..', '..'));
  const localAppData = norm(env.LOCALAPPDATA || `${home}/AppData/Local`);
  const backupDir = norm(machine.backupDir || `${localAppData}/Toolkit/backups`);
  const claudeHome = norm(env.CLAUDE_CONFIG_DIR || `${home}/.claude`);
  const codexHome = norm(env.CODEX_HOME || `${home}/.codex`);
  const scopedHomes = !!(env.CLAUDE_CONFIG_DIR || env.CODEX_HOME);
  const profileKey = scopedHomes
    ? crypto.createHash('sha256').update(JSON.stringify([claudeHome, codexHome])).digest('hex').slice(0, 20)
    : null;
  return {
    home,
    store,
    machine,
    machineFile,
    claudeHome,
    codexHome,
    claudeConfigDirExplicit: !!env.CLAUDE_CONFIG_DIR,
    claudeConfigFile: env.CLAUDE_CONFIG_DIR ? `${claudeHome}/.claude.json` : `${home}/.claude.json`,
    agentsHome,
    registryFile: `${store}/toolkit.json`,
    stateDir: `${store}/.state`,
    globalStateFile: `${store}/.state/global${profileKey ? `-${profileKey}` : ''}.json`,
    backupDir,
    logFile: `${store}/.state/log.jsonl`,
    codexExe: findCodexExe(home),
    norm,
    same,
  };
}

module.exports = { resolvePaths, norm, same };
