const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const packageJson = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));

assert.equal(packageJson.build.nsis.include, 'electron/installer.nsh');

const installer = fs.readFileSync(path.join(root, 'electron', 'installer.nsh'), 'utf8');
assert.match(installer, /!macro\s+customCheckAppRunning\b/);
assert.match(installer, /Codex Session Transfer\.exe/i);
assert.match(installer, /codex-session-transfer-server\.exe/i);
assert.match(installer, /ExecutablePath/);
assert.match(installer, /Stop-Process/);

console.log('installer upgrade process cleanup checks passed');
