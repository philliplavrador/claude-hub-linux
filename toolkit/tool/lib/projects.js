'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { git } = require('./gitx');
const { saveRegistry } = require('./registry');
const { norm } = require('./paths');
const pointer = require('./pointer');
const sync = require('./sync');

function starterAgentsMd(name) {
  return `# ${name}\n\n## What this is\n\nOne paragraph: what the project does and who it is for.\n\n## How to work here\n\nProject-specific rules only. Shared rules (how to talk to the user, git habits, Windows processes) come from Toolkit and are not repeated here.\n`;
}

function newProject(P, ctx, name, { noGit } = {}) {
  if (!/^[A-Za-z0-9 ._-]+$/.test(name)) throw new Error(`Project name must be letters, digits, space, dot, dash or underscore: ${name}`);
  if (ctx.reg.projects[name]) throw new Error(`Project already exists: ${name}`);
  const dir = `${ctx.reg.projectsRoot}/${name}`;
  if (fs.existsSync(dir)) throw new Error(`Folder already exists: ${dir}`);
  fs.mkdirSync(dir, { recursive: true });
  if (!noGit) { git(['init', '-q', '-b', 'main'], dir); fs.writeFileSync(`${dir}/.gitignore`, 'node_modules/\n.env\n'); }
  fs.writeFileSync(`${dir}/AGENTS.md`, starterAgentsMd(name));
  fs.writeFileSync(`${dir}/CLAUDE.md`, '@AGENTS.md\n');
  pointer.createPointer(dir, name);
  ctx.reg.projects[name] = { path: dir };
  saveRegistry(P, ctx.reg);
  sync.syncProject(P, ctx, { root: dir, projectName: name, quiet: true });
  return { path: dir };
}

function register(P, ctx, name, dir, { anywhere } = {}) {
  if (ctx.reg.projects[name]) throw new Error(`Project already registered: ${name}`);
  const root = norm(path.resolve(dir));
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) throw new Error(`No such folder: ${dir}`);
  if (!anywhere) {
    const base = norm(ctx.reg.projectsRoot).toLowerCase();
    const r = root.toLowerCase();
    if (r !== base && !r.startsWith(`${base}/`)) {
      throw new Error(`${root} is outside ${ctx.reg.projectsRoot}; pass --anywhere to register it anyway`);
    }
  }
  if (!fs.existsSync(`${root}/.git`)) throw new Error(`${dir} is not a git repository (no .git)`);
  ctx.reg.projects[name] = { path: root };
  saveRegistry(P, ctx.reg);
  if (!pointer.loadPointer(root)) pointer.createPointer(root, name);
  sync.syncProject(P, ctx, { root, projectName: name, quiet: true });
  return { path: root };
}

module.exports = { newProject, register, starterAgentsMd };
