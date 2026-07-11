const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const main = fs.readFileSync(path.join(__dirname, '..', 'electron', 'main.js'), 'utf8');

assert.match(main, /minWidth:\s*720\b/);
assert.match(main, /minHeight:\s*480\b/);

console.log('window constraints contract passed');
