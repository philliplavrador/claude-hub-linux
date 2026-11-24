'use strict';
const fs = require('node:fs');

const PATTERNS = [
  { name: 'database URL with password', re: /postgres(?:ql)?:\/\/[^:\s/]+:[^@\s]+@/i },
  { name: 'OpenAI-style key', re: /\bsk-[A-Za-z0-9_-]{20,}/ },
  { name: 'GitHub token', re: /\bghp_[A-Za-z0-9]{20,}/ },
  { name: 'Google API key', re: /\bAIza[0-9A-Za-z_-]{30,}/ },
  { name: 'private key', re: /BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY/ },
  { name: 'password assignment', re: /password\s*=\s*(?!\$\{)(?!<)\S{4,}/i },
];

const FAKE = /hunter2|example|not-real|fake|placeholder|redacted|\*\*\*|abcdefghij|1234567890|0123456789/i;

function isPlaceholder(p, m, line, next) {
  if (FAKE.test(m[0])) return true;
  if (p.name === 'database URL with password') {
    const pass = /:\/\/[^:\s/]+:([^@\s]+)@$/.exec(m[0]);
    return !!pass && (pass[1].length <= 3 || /^(?:pass(?:word)?|secret)$/i.test(pass[1]) || /[<>${}]/.test(pass[1]));
  }
  if (p.name === 'private key') {
    const after = line.slice(m.index + m[0].length);
    const body = /^-*(?:\\r)?\\n([A-Za-z0-9+/=]{20,})/.exec(after) || (/^-*\s*$/.test(after) && /^\s*[A-Za-z0-9+/=]{20,}\s*$/.exec(next || ''));
    return !body;
  }
  return false;
}

function findSecret(text, { allowPlaceholders = false } = {}) {
  const lines = String(text || '').split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const p of PATTERNS) {
      const re = new RegExp(p.re.source, p.re.flags.includes('g') ? p.re.flags : p.re.flags + 'g');
      for (const m of line.matchAll(re)) {
        const before = line.slice(0, m.index);
        if (/\$\{[^}]*$/.test(before)) continue; // inside an env-var reference
        if (allowPlaceholders && isPlaceholder(p, m, line, lines[i + 1])) continue;
        return { pattern: p.name, line: i + 1 };
      }
    }
  }
  return null;
}

function assertNoSecret(file) {
  const hit = findSecret(fs.readFileSync(file, 'utf8'));
  if (!hit) return;
  const err = new Error(`Refusing to copy ${file} into the store: looks like a ${hit.pattern} on line ${hit.line}`);
  err.code = 'SECRET';
  throw err;
}

module.exports = { findSecret, assertNoSecret, PATTERNS };
