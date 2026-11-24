'use strict';
const { readJson, readText } = require('./fsx');
const { listItems, KINDS } = require('./store');
const { installedPlugins, pluginInstallPath, pluginHints, MOD_MARKETPLACE } = require('./registry');

function firstHeading(text) {
  const m = /^#\s+(.+)$/m.exec(text || '');
  return m ? m[1].trim() : null;
}

function pluginDescription(installPath, name) {
  if (installPath) {
    const manifest = readJson(`${installPath}/.claude-plugin/plugin.json`, null);
    if (manifest && manifest.description) return manifest.description;
    const pkg = readJson(`${installPath}/package.json`, null);
    if (pkg && pkg.description) return pkg.description;
    const heading = firstHeading(readText(`${installPath}/README.md`));
    if (heading) return heading;
  }
  return name;
}

function itemDefault(reg, kind, name) {
  if (kind === 'output-styles') return reg.defaults.outputStyle === name;
  return (reg.defaults[kind] || []).includes(name);
}

function build(P, ctx) {
  const store = ctx.items || listItems(P);
  const items = [];
  for (const kind of KINDS) {
    for (const it of store[kind] || []) {
      items.push({ kind, name: it.name, description: it.description, fits: it.fits ?? null, default: itemDefault(ctx.reg, kind, it.name) });
    }
  }

  const hints = pluginHints(ctx.reg);
  const plugins = installedPlugins(P).filter((p) => p.marketplace !== MOD_MARKETPLACE).map((p) => {
    const installPath = pluginInstallPath(P, p.key);
    return {
      name: p.name,
      key: p.key,
      marketplace: p.marketplace,
      description: pluginDescription(installPath, p.name),
      fits: hints[p.name] || null,
      default: (ctx.reg.defaults.plugins || []).includes(p.name),
      installPath,
    };
  });

  return { items, plugins };
}

module.exports = { build };
