const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const runtime = require('../scripts/run_python');

assert.deepEqual(runtime.requiredModules(['scripts/generate_icons.py']), []);
assert.deepEqual(runtime.requiredModules(['scripts/collect_release.py', '--platform', 'win']), []);
assert.deepEqual(runtime.requiredModules(['scripts/build_server.py', '--platform', 'win']), ['PyInstaller']);

const bundled = runtime.bundledPythonPath();
if (fs.existsSync(bundled) && !runtime.usable({ command: bundled, prefix: [] }, ['PyInstaller'])) {
  const selected = runtime.selectCandidate(['scripts/build_server.py', '--platform', 'win']);
  assert.ok(selected, 'a Python interpreter with PyInstaller should be available for server builds');
  assert.notEqual(path.resolve(selected.command), path.resolve(bundled));
}

console.log('python runtime capability checks passed');
