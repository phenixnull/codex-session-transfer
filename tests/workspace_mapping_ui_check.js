const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const main = fs.readFileSync(path.join(root, 'electron', 'main.js'), 'utf8');
const preload = fs.readFileSync(path.join(root, 'electron', 'preload.js'), 'utf8');

for (const id of [
  'workspaceModeSelect',
  'workspaceTargetRoot',
  'chooseWorkspaceDirectoryButton',
  'workspaceMappingList',
  'overwriteSessions',
]) {
  assert.match(html, new RegExp(`id=["']${id}["']`), `missing mapping control ${id}`);
}

assert.match(app, /request\.workspace_mapping\s*=/);
assert.match(app, /overwrite:\s*Boolean\(\$\("overwriteSessions"\)\?\.checked\)/);
assert.match(app, /window\.codexDesktop\?\.chooseDirectory/);
assert.match(main, /ipcMain\.handle\(["']codex-session-transfer:choose-directory["']/);
assert.match(main, /properties:\s*\[["']openDirectory["']\]/);
assert.match(preload, /contextBridge\.exposeInMainWorld\(["']codexDesktop["']/);
assert.match(preload, /ipcRenderer\.invoke\(["']codex-session-transfer:choose-directory["']\)/);

console.log('workspace mapping UI contract passed');
