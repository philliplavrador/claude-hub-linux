'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const fsx = require('./fsx');
const { loadRegistry, resolvePluginKey, pluginInstallPath } = require('./registry');
const { listItems } = require('./store');
const pointer = require('./pointer');
const state = require('./state');
const { findSecret } = require('./secrets');
const { git } = require('./gitx');

const FIXTURE = /(^|\/)[^/]*\.test\.[cm]?js$|(^|\/)(docs\/superpowers|\.superpowers)\//;
const GENERATED = ['.state', '.backups'];

function storeFiles(P) {
  const out = [];
  const walk = (rel) => {
    let entries;
    try { entries = fs.readdirSync(rel ? `${P.store}/${rel}` : P.store, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const next = rel ? `${rel}/${e.name}` : e.name;
      if (e.isSymbolicLink() || e.name === '.git' || e.name === 'node_modules') continue;
      if (e.isDirectory()) walk(next); else out.push(next);
    }
  };
  let tracked = null;
  try { tracked = git(['ls-files'], P.store).split(/\r?\n/).filter(Boolean); } catch { /* not a git repo */ }
  if (tracked) out.push(...tracked);
  else walk('');
  for (const g of GENERATED) if (fs.existsSync(`${P.store}/${g}`)) walk(g);
  return [...new Set(out)];
}

function secretScan(P) {
  const files = storeFiles(P);
  const hits = [];
  for (const rel of files) {
    let text;
    try { text = fs.readFileSync(`${P.store}/${rel}`, 'utf8'); } catch { continue; }
    if (text.includes('\u0000')) continue; // binary
    const hit = findSecret(text, { allowPlaceholders: FIXTURE.test(rel) });
    if (hit) hits.push(`${rel}:${hit.line} (${hit.pattern})`);
  }
  return { ok: hits.length === 0, detail: hits.length ? hits.join('; ') : `${files.length} file(s) scanned, none matched a credential pattern` };
}

function pluginSkillCounts(P, reg) {
  let expected = 0;
  for (const n of reg.defaults.plugins || []) {
    let key;
    try { key = resolvePluginKey(P, n); } catch { continue; }
    const installPath = pluginInstallPath(P, key);
    if (!installPath) continue;
    const skillsDir = `${installPath}/skills`;
    if (!fs.existsSync(skillsDir)) continue;
    for (const name of fs.readdirSync(skillsDir)) {
      if (fs.existsSync(`${skillsDir}/${name}/SKILL.md`)) expected++;
    }
  }
  let actual = 0;
  const agentsSkillsDir = `${P.agentsHome}/skills`;
  if (fs.existsSync(agentsSkillsDir)) {
    for (const name of fs.readdirSync(agentsSkillsDir)) {
      if (!name.includes('--')) continue;
      const p = `${agentsSkillsDir}/${name}`;
      if (fsx.isLink(p) || fs.existsSync(`${p}/SKILL.md`)) actual++;
    }
  }
  return { expected, actual };
}

function run(P) {
  const checks = [];
  const add = (check, ok, detail, opts) => checks.push({ check, ok: !!ok, detail: detail || '', blocking: !(opts && opts.blocking === false) });
  add('store exists', fs.existsSync(P.store), P.store);
  let reg = null;
  try { reg = loadRegistry(P); add('registry parses', true, P.registryFile); } catch (e) { add('registry parses', false, e.message); }
  try {
    const d = fs.mkdtempSync(path.join(os.tmpdir(), 'toolkit-doctor-'));
    const t = path.join(d, 't'); fs.mkdirSync(t);
    const link = path.join(d, 'l');
    fsx.makeJunction(P, t, link);
    const removed = fsx.removeLink(link);
    if (removed) fs.rmSync(d, { recursive: true, force: true });
    add('junctions work', true);
  } catch (e) { add('junctions work', false, e.message); }
  add('codex binary found', !!P.codexExe, P.codexExe || 'no openai.chatgpt extension with codex.exe under ~/.vscode/extensions', { blocking: false });
  try {
    const s = secretScan(P);
    add('no secret in the store tree', s.ok, s.detail, { blocking: false });
  } catch (e) {
    add('no secret in the store tree', false, e.message, { blocking: false });
  }
  add('claude settings present', fs.existsSync(`${P.claudeHome}/settings.json`), `${P.claudeHome}/settings.json`);
  if (reg) {
    try {
      const items = listItems(P);
      add('store has items', Object.values(items).some((l) => l.length), Object.entries(items).map(([k, l]) => `${k}: ${l.length}`).join(', '));
    } catch (e) {
      add('store has items', false, e.message);
    }
    for (const [name, p] of Object.entries(reg.projects)) {
      try {
        if (!fs.existsSync(p.path)) { add(`project ${name} exists`, false, p.path); continue; }
        const ptr = pointer.loadPointer(p.path);
        add(`project ${name} has pointer`, !!ptr, `${p.path}/${pointer.POINTER_REL}`);
        const st = state.loadProjectState(p.path);
        const dangling = (st.created || []).filter((c) => fsx.isLink(c) && !fs.existsSync(fsx.linkTarget(c)));
        add(`project ${name} links resolve`, dangling.length === 0, dangling.join(', '));

        const agentsMdPath = `${p.path}/AGENTS.md`;
        const agentsMdText = fsx.readText(agentsMdPath);
        const agentsMdGenerated = agentsMdText != null && agentsMdText.startsWith('<!-- Generated by Toolkit');
        add(`project ${name} has AGENTS.md`, agentsMdText != null && !agentsMdGenerated, agentsMdPath);

        const claudeMdText = fsx.readText(`${p.path}/CLAUDE.md`);
        if (claudeMdText == null) {
          if (agentsMdText != null) {
            add(`project ${name} CLAUDE.md`, false, 'CLAUDE.md missing; Claude Code reads no project instructions here — write @AGENTS.md into it', { blocking: false });
          }
        } else if (claudeMdText !== '@AGENTS.md\n') {
          add(`project ${name} CLAUDE.md`, true, 'CLAUDE.md is hand-written; Codex does not read it', { blocking: false });
        }

        const rulesDir = `${p.path}/.claude/rules`;
        if (fs.existsSync(rulesDir)) {
          const handWritten = fs.readdirSync(rulesDir, { withFileTypes: true })
            .filter((e) => e.isFile() && e.name.endsWith('.md'))
            .map((e) => e.name)
            .sort();
          if (handWritten.length) {
            add(`project ${name} Claude-only rules`, true, `Claude-only instructions: ${handWritten.join(', ')}`, { blocking: false });
          }
        }

        const commandsDir = `${p.path}/.claude/commands`;
        if (fs.existsSync(commandsDir)) {
          const commandFiles = fs.readdirSync(commandsDir, { withFileTypes: true })
            .filter((e) => e.isFile() && e.name.endsWith('.md'))
            .map((e) => e.name)
            .sort();
          for (const file of commandFiles) {
            add(`not shared: .claude/commands/${file}`, false, `${commandsDir}/${file} — move it to the shelf as a skill`, { blocking: false });
          }
        }

        const skillsDir = `${p.path}/.claude/skills`;
        if (fs.existsSync(skillsDir)) {
          const skillNames = fs.readdirSync(skillsDir).sort();
          for (const skillName of skillNames) {
            const full = `${skillsDir}/${skillName}`;
            if (!fsx.isLink(full) && fs.statSync(full).isDirectory()) {
              add(`not shared: .claude/skills/${skillName} is a real folder`, false, `${full} — edit the shelf skill instead, not a project copy`, { blocking: false });
            }
          }
        }
      } catch (e) {
        add(`project checks (${name})`, false, e.message);
      }
    }
    const g = state.loadGlobalState(P);
    const gd = (g.created || []).filter((c) => fsx.isLink(c) && !fs.existsSync(fsx.linkTarget(c)));
    add('global links resolve', gd.length === 0, gd.join(', '));
    try {
      const { expected, actual } = pluginSkillCounts(P, reg);
      add('plugin skills mirrored', actual === expected, `${actual}/${expected} plugin skill(s) mirrored`, { blocking: false });
    } catch (e) {
      add('plugin skills mirrored', false, e.message, { blocking: false });
    }
  }
  return checks;
}

module.exports = { run };
