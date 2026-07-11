const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const workflow = fs.readFileSync(
  path.join(__dirname, '..', '.github', 'workflows', 'release-build.yml'),
  'utf8',
);

assert.doesNotMatch(workflow, /path:\s*release\/v\*\/\*\*/);
assert.match(workflow, /id:\s*release_dir/);
assert.match(workflow, /path:\s*\$\{\{ steps\.release_dir\.outputs\.path \}\}\/\*\*/);

console.log('release workflow scope check passed');
