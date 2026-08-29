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
  'overwriteResolution',
  'overwriteResolutionItems',
  'applyOverwriteChoicesButton',
]) {
  assert.match(html, new RegExp(`id=["']${id}["']`), `missing mapping control ${id}`);
}

assert.match(app, /request\.workspace_mapping\s*=/);
assert.match(app, /function usingLocalMirror\(\)/);
assert.match(app, /mirror_target:\s*mirrorTarget/);
assert.match(app, /thread_ids:\s*mirrorTarget\s*\?\s*\[\]\s*:/);
assert.match(app, /include_archived:\s*mirrorTarget\s*\|\|/);
assert.match(app, /overwrite:\s*mirrorTarget\s*\?\s*false\s*:/);
assert.match(app, /overwrite_selections/);
assert.match(app, /overwrite_ambiguities/);
assert.match(app, /renderOverwriteResolution\(/);
assert.match(app, /Resolve .*ambiguous overwrite match/);
assert.match(app, /\/api\/copy-package-progress/);
assert.match(app, /streamApi\(/);
assert.doesNotMatch(app, /reasons\.push\(["']Run Preview first\./);
assert.match(app, /Preview is optional and loads on demand/);
assert.match(app, /Back up and replace entire target/);
assert.match(app, /PREVIEW_RENDER_PAGE_SIZE/);
assert.match(app, /preview_offset/);
assert.match(app, /next_preview_offset/);
assert.match(app, /loadMorePreview\(/);
assert.match(app, /state\.preview = null;\s*renderPreview\(null\);/);
assert.doesNotMatch(app, /state\.preview = result;\s*renderPreview\(result\);/);
assert.match(app, /result\.item_total/);
assert.match(app, /provider_configs/);
assert.match(app, /function providerWillSync\(value\)/);
assert.match(app, /sync on import/);
assert.match(app, /authentication credentials stay on the target machine/);
assert.match(app, /Added provider.*config\.toml/);
assert.match(app, /event\?\.type === ["']complete["']/);
assert.match(html, /id=["']copyProgress["']/);
assert.match(app, /window\.codexDesktop\?\.chooseDirectory/);
assert.match(main, /ipcMain\.handle\(["']codex-session-transfer:choose-directory["']/);
assert.match(main, /properties:\s*\[["']openDirectory["']\]/);
assert.match(preload, /contextBridge\.exposeInMainWorld\(["']codexDesktop["']/);
assert.match(preload, /ipcRenderer\.invoke\(["']codex-session-transfer:choose-directory["']\)/);

console.log('workspace mapping UI contract passed');
