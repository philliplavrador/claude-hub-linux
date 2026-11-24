'use strict';
function parse(argv) {
  const out = { cmd: 'help', rest: [], flags: {} };
  const a = [...argv];
  if (a.length && !a[0].startsWith('--')) out.cmd = a.shift();
  while (a.length) {
    const t = a.shift();
    if (t.startsWith('--')) {
      const key = t.slice(2);
      if (a.length && !a[0].startsWith('--')) out.flags[key] = a.shift(); else out.flags[key] = true;
    } else out.rest.push(t);
  }
  return out;
}
module.exports = { parse };
