'use strict';

const GITIGNORE_BEGIN = '# >>> toolkit (managed; do not edit between these lines)';
const GITIGNORE_END = '# <<< toolkit';
const TOML_BEGIN = GITIGNORE_BEGIN;
const TOML_END = GITIGNORE_END;

function eolOf(text) {
  return /\r\n/.test(text) ? '\r\n' : '\n';
}

function setBlock(text, begin, end, bodyLines) {
  const eol = eolOf(text);
  const lines = text.split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const bi = lines.indexOf(begin);
  const ei = bi >= 0 ? lines.indexOf(end, bi + 1) : -1;
  if (bi >= 0 && ei < 0) throw new Error('managed block: begin marker without end marker');
  let before = lines; let after = [];
  if (bi >= 0 && ei >= 0) {
    before = lines.slice(0, bi);
    after = lines.slice(ei + 1);
    while (before.length && before[before.length - 1] === '') before.pop();
  }
  const block = bodyLines.length ? [begin, ...bodyLines, end] : [];
  let out = [...before];
  if (block.length) {
    if (out.length) out.push('');
    out.push(...block);
  }
  out.push(...after);
  const result = out.length ? out.join(eol) + eol : '';
  return result === text ? text : result;
}

function setGitignoreBlock(text, lines) {
  return setBlock(text || '', GITIGNORE_BEGIN, GITIGNORE_END, lines);
}

function countOccurrences(line, token) {
  let n = 0; let i = 0;
  while ((i = line.indexOf(token, i)) !== -1) { n++; i += token.length; }
  return n;
}

const TABLE_HEADER = /^\s*\[[^\]]*\]\s*(#.*)?$/;
function findFirstTableHeader(lines) {
  let delim = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (delim) {
      if (countOccurrences(line, delim) % 2 === 1) delim = null;
      continue;
    }
    if (TABLE_HEADER.test(line)) return i;
    if (countOccurrences(line, '"""') % 2 === 1) { delim = '"""'; continue; }
    if (countOccurrences(line, "'''") % 2 === 1) delim = "'''";
  }
  return -1;
}

function setTomlRegion(text, regionBody) {
  const body = (regionBody || '').split(/\r?\n/);
  while (body.length && body[body.length - 1] === '') body.pop();
  const src = text || '';
  const eol = eolOf(src);
  const lines = src.split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const bi = lines.indexOf(TOML_BEGIN);
  if (bi >= 0) return setBlock(src, TOML_BEGIN, TOML_END, body); // replace in place (setBlock keeps position)
  const headerAt = findFirstTableHeader(lines);
  if (!body.length) return src;
  if (headerAt < 0) return setBlock(src, TOML_BEGIN, TOML_END, body);
  const before = lines.slice(0, headerAt);
  while (before.length && before[before.length - 1] === '') before.pop();
  const after = lines.slice(headerAt);
  const out = [...before, ...(before.length ? [''] : []), TOML_BEGIN, ...body, TOML_END, '', ...after];
  return out.join(eol) + eol;
}

function tomlKeyPath(text) {
  const parts = [];
  let rest = text.trim();
  while (rest) {
    const match = /^(?:([A-Za-z0-9_-]+)|'([^']*)'|"((?:[^"\\]|\\.)*)")/.exec(rest);
    if (!match) throw new Error('managed TOML: unsupported key syntax; configuration left untouched');
    let part = match[1] ?? match[2];
    if (part === undefined) {
      part = JSON.parse(`"${match[3].replace(/\\U([0-9a-fA-F]{8})/g, (_, hex) => JSON.stringify(String.fromCodePoint(parseInt(hex, 16))).slice(1, -1))}"`);
    }
    parts.push(part);
    rest = rest.slice(match[0].length).trimStart();
    if (!rest) break;
    if (!rest.startsWith('.')) throw new Error('managed TOML: unsupported key syntax; configuration left untouched');
    rest = rest.slice(1).trimStart();
  }
  return parts;
}

function tomlSections(text) {
  const root = { id: '[]', path: [], items: [] };
  const sections = [root];
  const lines = (text || '').replace(/\r\n/g, '\n').split('\n');
  if (lines.at(-1) === '') lines.pop();
  let section = root;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*(?:#.*)?$/.test(line)) { section.items.push({ raw: line }); continue; }
    const header = /^\s*(\[\[?)(.*?)(\]\]?)\s*(?:#.*)?$/.exec(line);
    if (header && header[1].length === header[3].length) {
      const parts = tomlKeyPath(header[2]);
      section = { id: JSON.stringify(parts), path: parts, header: line, array: header[1].length === 2, items: [] };
      sections.push(section);
      continue;
    }
    const assignment = /^\s*((?:(?:"(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_-]+)\s*\.?\s*)+)=(.*)$/.exec(line);
    if (!assignment) throw new Error('managed TOML: unsupported statement; configuration left untouched');
    const key = JSON.stringify(tomlKeyPath(assignment[1]));
    const raw = [line];
    let quote = null; let depth = 0; let value = assignment[2];
    while (true) {
      for (let j = 0; j < value.length; j++) {
        const ch = value[j];
        if (quote) {
          if (quote[0] === '"' && ch === '\\') { j++; continue; }
          if (value.startsWith(quote, j)) { j += quote.length - 1; quote = null; }
          continue;
        }
        if (ch === '#') break;
        if (ch === '"' || ch === "'") {
          quote = value.startsWith(ch.repeat(3), j) ? ch.repeat(3) : ch;
          j += quote.length - 1;
        } else if (ch === '[' || ch === '{') depth++;
        else if (ch === ']' || ch === '}') depth--;
      }
      if (!quote && depth === 0) break;
      if (depth < 0 || (quote && quote.length === 1) || ++i >= lines.length) {
        throw new Error('managed TOML: unfinished value; configuration left untouched');
      }
      value = lines[i];
      raw.push(value);
    }
    section.items.push({ key, raw: raw.join('\n') });
  }
  return sections;
}

function sectionText(section) {
  const lines = section.items.map(item => item.raw);
  while (lines.length && lines[0] === '') lines.shift();
  while (lines.length && lines.at(-1) === '') lines.pop();
  return [...(section.header ? [section.header] : []), ...lines].join('\n');
}

function preserveTomlRegion(text, newBody, { previousBody, legacyMcpNames = [], warnings = [] } = {}) {
  const src = text || '';
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  const begin = lines.indexOf(TOML_BEGIN);
  const end = begin < 0 ? -1 : lines.indexOf(TOML_END, begin + 1);
  if (begin >= 0 && end < 0) throw new Error('managed block: begin marker without end marker');
  const inside = tomlSections(begin < 0 ? '' : lines.slice(begin + 1, end).join('\n'));
  const outside = tomlSections(begin < 0 ? src : [...lines.slice(0, begin), ...lines.slice(end + 1)].join('\n'));
  const generated = tomlSections(newBody || '');
  const previous = tomlSections(previousBody || '');
  const ownership = new Map();
  const generatedComments = new Map();
  for (const section of [...previous, ...generated]) {
    const keys = ownership.get(section.id) || new Set();
    for (const item of section.items) if (item.key) keys.add(item.key);
    ownership.set(section.id, keys);
    const comments = generatedComments.get(section.id) || new Set();
    for (const item of section.items) if (!item.key && /^\s*#/.test(item.raw)) comments.add(item.raw);
    generatedComments.set(section.id, comments);
  }
  const owned = (section, item) => {
    if (!item.key) return generatedComments.get(section.id)?.has(item.raw) || false;
    if (ownership.get(section.id)?.has(item.key)) return true;
    if (previousBody !== undefined) return false;
    if (!section.path.length && item.key === '["project_doc_max_bytes"]') return true;
    if (section.path[0] === 'projects' && section.path.length === 2 && item.key === '["trust_level"]') return true;
    return section.path[0] === 'mcp_servers' && legacyMcpNames.includes(section.path[1]);
  };
  const ownedTable = section => ownership.has(section.id) || (previousBody === undefined
    && ((section.path[0] === 'projects' && section.path.length === 2)
      || (section.path[0] === 'mcp_servers' && legacyMcpNames.includes(section.path[1]))));
  const serverName = section => section.path[0] === 'mcp_servers' && section.path.length >= 2 ? section.path[1] : undefined;
  const nextServers = new Set(generated.map(serverName).filter(name => name !== undefined));
  const oldServers = new Set([...previous.map(serverName), ...(previousBody === undefined ? legacyMcpNames : [])]
    .filter(name => name !== undefined));
  const retainedServers = new Set();
  for (const name of oldServers) {
    if (nextServers.has(name)) continue;
    const current = inside.filter(section => serverName(section) === name);
    if (!current.length) continue;
    const customized = outside.some(section => serverName(section) === name) || current.some(section => {
      if (!ownedTable(section)) return true;
      const old = previous.find(part => part.id === section.id);
      return section.items.some(item => item.key && (!owned(section, item)
        || (old?.items.some(was => was.key === item.key && was.raw !== item.raw))));
    });
    if (!customized) continue;
    retainedServers.add(name);
    const warning = `retained customized MCP server ${name} after removal`;
    if (!warnings.includes(warning)) warnings.push(warning);
  }
  const foreign = inside.map(section => ({ ...section,
    items: section.items.filter(item => retainedServers.has(serverName(section)) || !owned(section, item)),
  })).filter(section => !section.header || retainedServers.has(serverName(section)) || !ownedTable(section)
    || section.items.some(item => item.key) || section.array);
  const mergeItems = (target, extra) => {
    const keys = new Set(target.items.filter(item => item.key).map(item => item.key));
    target.items.push(...extra.items.filter(item => !item.key || !keys.has(item.key)));
  };

  mergeItems(outside[0], foreign[0] || { items: [] });
  const foreignTables = [];
  for (const section of foreign.filter(part => part.header)) {
    const existing = outside.find(part => !part.array && !section.array && part.id === section.id);
    if (existing) mergeItems(existing, section); else foreignTables.push(section);
  }
  const bodySections = [];
  for (const section of generated) {
    const existing = outside.find(part => !part.array && part.id === section.id);
    if (!section.header) {
      const keys = new Set(outside[0].items.filter(item => item.key).map(item => item.key));
      section.items = section.items.filter(item => !item.key || !keys.has(item.key));
    } else if (existing) continue;
    const index = foreignTables.findIndex(part => !part.array && part.id === section.id);
    if (index >= 0) mergeItems(section, foreignTables.splice(index, 1)[0]);
    bodySections.push(section);
  }
  const body = bodySections.map(sectionText).filter(Boolean).join('\n\n');
  const pieces = [sectionText(outside[0])];
  if (body) pieces.push(`${TOML_BEGIN}\n${body}\n${TOML_END}`);
  pieces.push(...foreignTables.map(sectionText), ...outside.slice(1).map(sectionText));
  const result = pieces.filter(Boolean).join('\n\n');
  return result ? (result + '\n').replace(/\n/g, eolOf(src)) : '';
}

function unescapeBasic(s) {
  return s.replace(/\\(["\\])/g, '$1');
}

function tomlProjectTables(text) {
  const lines = (text || '').split(/\r?\n/);
  const out = [];
  let inRegion = false;
  for (const line of lines) {
    if (line === TOML_BEGIN) { inRegion = true; continue; }
    if (line === TOML_END) { inRegion = false; continue; }
    if (inRegion) continue;
    const m = /^\s*\[projects\.(?:'([^']*)'|"((?:[^"\\]|\\.)*)")\]\s*$/.exec(line);
    if (m) out.push(m[1] != null ? m[1] : unescapeBasic(m[2]));
  }
  return out;
}

function mentionsStore(command, storeRoot) {
  const c = String(command || '').replace(/\\/g, '/').toLowerCase();
  return c.includes(String(storeRoot).replace(/\\/g, '/').toLowerCase());
}

function applyManagedJson(input, spec, storeRoot) {
  const obj = JSON.parse(JSON.stringify(input || {}));
  const before = JSON.stringify(obj);
  if (spec.outputStyle !== undefined) {
    if (spec.outputStyle === null) delete obj.outputStyle; else obj.outputStyle = spec.outputStyle;
  }
  if (spec.enabledPlugins) {
    obj.enabledPlugins = obj.enabledPlugins || {};
    for (const [k, v] of Object.entries(spec.enabledPlugins)) {
      if (v === null) delete obj.enabledPlugins[k]; else obj.enabledPlugins[k] = v;
    }
    if (!Object.keys(obj.enabledPlugins).length) delete obj.enabledPlugins;
  }
  if (spec.hooks) {
    obj.hooks = obj.hooks || {};
    for (const event of Object.keys(obj.hooks)) {
      obj.hooks[event] = (obj.hooks[event] || [])
        .map((e) => {
          if (!Array.isArray(e.hooks)) return e;
          const kept = e.hooks.filter((h) => !mentionsStore(h.command, storeRoot));
          return kept.length === e.hooks.length ? e : { ...e, hooks: kept };
        })
        .filter((e) => !Array.isArray(e.hooks) || e.hooks.length);
    }
    for (const [event, entries] of Object.entries(spec.hooks)) {
      obj.hooks[event] = [...(obj.hooks[event] || []), ...entries];
    }
    for (const event of Object.keys(obj.hooks)) {
      if (!obj.hooks[event].length) delete obj.hooks[event];
    }
    if (!Object.keys(obj.hooks).length) delete obj.hooks;
  }
  return { obj, changed: JSON.stringify(obj) !== before };
}

module.exports = { GITIGNORE_BEGIN, GITIGNORE_END, TOML_BEGIN, TOML_END, setGitignoreBlock, setTomlRegion, preserveTomlRegion, tomlProjectTables, applyManagedJson };
