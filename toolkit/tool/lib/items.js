'use strict';
const fs = require('node:fs');
const path = require('node:path');
const fsx = require('./fsx');
const { KINDS, parseFrontmatter, listItems } = require('./store');
const { saveRegistry, resolvePluginKey } = require('./registry');
const pointer = require('./pointer');
const sync = require('./sync');
const codex = require('./codex');

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

function checkKind(kind) { if (!KINDS.includes(kind)) throw new Error(`Unknown kind: ${kind} (kinds: ${KINDS.join(', ')})`); }
function checkName(name) { if (!NAME_RE.test(name)) throw new Error(`Bad item name "${name}": use lowercase letters, digits and dashes`); }

function commandToSkill(mdText, name) {
  const { data, body } = parseFrontmatter(mdText);
  const lines = [];
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(mdText);
  if (m) lines.push(...m[1].split(/\r?\n/).filter((l) => l.trim()));
  if (!data.name) lines.push(`name: ${name}`);
  if (!data.description) {
    const first = body.split(/\r?\n/).find((l) => l.trim()) || name;
    lines.push(`description: ${first.replace(/^#+\s*/, '').trim().slice(0, 120)}`);
  }
  if (!data['disable-model-invocation']) lines.push('disable-model-invocation: true');
  return `---\n${lines.join('\n')}\n---\n${body}`;
}

function itemPath(P, kind, name) {
  if (kind === 'skills' || kind === 'rules' || kind === 'hooks') return `${P.store}/${kind}/${name}`;
  if (kind === 'mcp') return `${P.store}/mcp/${name}.json`;
  return `${P.store}/${kind}/${name}.md`;
}

function newItem(P, kind, name, { description, userOnly } = {}) {
  checkKind(kind); checkName(name);
  const dest = itemPath(P, kind, name);
  if (fs.existsSync(dest)) throw new Error(`${kind}/${name} already exists in the store`);
  const desc = description || name;
  if (kind === 'skills') {
    fsx.ensureDir(dest);
    const fm = [`name: ${name}`, `description: ${desc}`, ...(userOnly ? ['disable-model-invocation: true'] : [])];
    fs.writeFileSync(`${dest}/SKILL.md`, `---\n${fm.join('\n')}\n---\n\n# ${name}\n\nWhat to do when this skill is invoked.\n`);
    fsx.ensureDir(`${dest}/agents`);
    fs.writeFileSync(`${dest}/agents/openai.yaml`, codex.openaiYaml({ userOnly: !!userOnly }));
  } else if (kind === 'rules') {
    fsx.ensureDir(dest);
    fs.writeFileSync(`${dest}/README.md`, `${desc}\n`);
    fs.writeFileSync(`${dest}/${name}.md`, `# ${desc}\n\n- Rule one.\n`);
  } else if (kind === 'agents') {
    fsx.ensureDir(path.dirname(dest));
    fs.writeFileSync(dest, `---\nname: ${name}\ndescription: ${desc}\n---\n\nYou are ${name}. ${desc}\n`);
  } else if (kind === 'output-styles') {
    fsx.ensureDir(path.dirname(dest));
    fs.writeFileSync(dest, `---\nname: ${name}\ndescription: ${desc}\nkeep-coding-instructions: true\n---\n\n# ${name}\n\nHow to respond.\n`);
  } else if (kind === 'hooks') {
    fsx.ensureDir(dest);
    fsx.writeJson(`${dest}/hook.json`, { description: desc, event: 'PreToolUse', matcher: 'Bash|PowerShell', timeout: 10, codex: { matcher: '^Bash$' } });
    fs.writeFileSync(`${dest}/${name}.js`, `#!/usr/bin/env node\n'use strict';\n// ${desc}\nconst { readHookInput } = require('../lib/input');\nconst input = readHookInput();\n// Exit 2 with a message on stderr to block the tool call; exit 0 to allow.\nprocess.exit(0);\n`);
  } else if (kind === 'mcp') {
    fsx.writeJson(dest, { description: desc, server: { command: 'npx', args: ['-y', 'CHANGE-ME'] } });
  }
  return dest;
}

function setDefault(P, ctx, kind, name, on) {
  checkKind(kind);
  const items = listItems(P);
  if (!items[kind].some((i) => i.name === name)) throw new Error(`${kind}/${name} is not in the store`);
  const d = ctx.reg.defaults;
  if (kind === 'output-styles') d.outputStyle = on ? name : (d.outputStyle === name ? null : d.outputStyle);
  else { d[kind] = d[kind].filter((n) => n !== name); if (on) d[kind].push(name); }
  saveRegistry(P, ctx.reg);
  return sync.syncGlobal(P, sync.loadContext(P), {});
}

function projectRoot(ctx, project) {
  const p = ctx.reg.projects[project];
  if (!p) throw new Error(`Unknown project: ${project}`);
  return p.path;
}

function assign(P, ctx, project, kind, name, on) {
  checkKind(kind);
  const root = projectRoot(ctx, project);
  const items = listItems(P);
  if (!items[kind].some((i) => i.name === name)) throw new Error(`${kind}/${name} is not in the store`);
  const ptr = pointer.loadPointer(root) || pointer.createPointer(root, project);
  if (kind === 'output-styles') ptr.outputStyle = on ? name : (ptr.outputStyle === name ? null : ptr.outputStyle);
  else { ptr.add[kind] = ptr.add[kind].filter((n) => n !== name); if (on) ptr.add[kind].push(name); }
  pointer.savePointer(root, ptr);
  return sync.syncProject(P, sync.loadContext(P), { root, projectName: project, quiet: true });
}

function pluginToggle(P, ctx, name, on, { project } = {}) {
  const key = resolvePluginKey(P, name);
  const short = key.slice(0, key.indexOf('@'));
  if (!project) {
    const d = ctx.reg.defaults;
    d.plugins = d.plugins.filter((n) => n !== short && n !== key);
    if (on) d.plugins.push(short);
    saveRegistry(P, ctx.reg);
    return sync.syncGlobal(P, sync.loadContext(P), {});
  }
  const root = projectRoot(ctx, project);
  const ptr = pointer.loadPointer(root) || pointer.createPointer(root, project);
  const strip = (l) => l.filter((n) => n !== short && n !== key);
  ptr.add.plugins = strip(ptr.add.plugins); ptr.remove.plugins = strip(ptr.remove.plugins);
  (on ? ptr.add.plugins : ptr.remove.plugins).push(short);
  pointer.savePointer(root, ptr);
  return sync.syncProject(P, sync.loadContext(P), { root, projectName: project, quiet: true });
}

module.exports = { commandToSkill, newItem, setDefault, assign, pluginToggle, itemPath, NAME_RE };
