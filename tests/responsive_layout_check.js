const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { _electron: electron } = require('playwright-core');

const root = path.join(__dirname, '..');
const staticRoot = path.join(root, 'static');
const previewRequests = [];
const copyRequests = [];
const packageCopyRequests = [];
const projects = Array.from({ length: 10 }, (_, index) => {
  const cwd = `C:\\source\\Project${index + 1}`;
  return {
    cwd,
    label: `Project${index + 1}`,
    threads: [{ id: `thread-${index + 1}` }],
  };
});
const packageThreads = projects.map((project, index) => ({
  id: project.threads[0].id,
  title: `Session ${index + 1}`,
  preview: `Preview ${index + 1}`,
  source: 'vscode',
  model_provider: 'source-provider',
  cwd: project.cwd,
  rollout_exists: true,
  archived: false,
  child_count: 0,
}));
const localThreads = Array.from({ length: 3 }, (_, index) => ({
  id: `local-thread-${index + 1}`,
  title: `Local session ${index + 1}`,
  preview: `Local preview ${index + 1}`,
  source: 'cli',
  model_provider: 'source-provider',
  cwd: 'C:\\local\\Project',
  rollout_exists: true,
  archived: false,
  child_count: 0,
}));

const provider = {
  model_provider: 'source-provider',
  active: packageThreads.length,
  archived: 0,
  total: packageThreads.length,
};
const alternateProvider = {
  model_provider: 'alternate-source',
  active: 0,
  archived: 0,
  total: 0,
};
const status = {
  codex_home: 'C:\\Codex',
  sqlite_home: 'C:\\Codex',
  db_path: 'C:\\Codex\\state_5.sqlite',
  db_exists: true,
  integrity_check: 'ok',
  blocking_processes: [],
  wal_files: [],
  session_index: { exists: true, entries: packageThreads.length },
  current_config: {
    exists: true,
    model_provider: 'target-provider',
    model: 'test-model',
    configured_provider_ids: ['target-provider'],
  },
  session_stats: {
    totals: { total: 0, active: 0, archived: 0, projects: 0 },
    by_provider: {},
    by_project: [],
  },
  providers: [provider, alternateProvider],
  target_providers: [
    { value: 'target-provider', label: 'Target', current: true, session_total: 0 },
  ],
  package_source: {
    loaded: true,
    package_path: 'C:\\packages\\layout-test.zip',
    providers: [provider],
    session_stats: {
      totals: { total: packageThreads.length, active: packageThreads.length, archived: 0 },
      by_provider: { 'source-provider': provider },
      by_project: projects.map((project) => ({ ...project, total: 1, active: 1, archived: 0 })),
    },
    manifest: { projects, thread_count: packageThreads.length },
  },
  skills: {
    root: 'C:\\Codex\\skills',
    exists: true,
    total: 1,
    package_source: { loaded: false, manifest: null, skills: [] },
  },
};

function json(response, value) {
  response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(value));
}

function contentType(filePath) {
  if (filePath.endsWith('.html')) return 'text/html; charset=utf-8';
  if (filePath.endsWith('.css')) return 'text/css; charset=utf-8';
  if (filePath.endsWith('.js')) return 'text/javascript; charset=utf-8';
  if (filePath.endsWith('.png')) return 'image/png';
  return 'application/octet-stream';
}

function startFixtureServer() {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname === '/api/status') return json(response, status);
    if (url.pathname === '/api/package-threads') return json(response, packageThreads);
    if (url.pathname === '/api/threads') {
      return json(
        response,
        ['target-provider', 'alternate-source'].includes(url.searchParams.get('source_provider'))
          ? []
          : localThreads,
      );
    }
    if (url.pathname === '/api/skills') return json(response, []);
    if (url.pathname === '/api/copy-progress' && request.method === 'POST') {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', () => {
        const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        copyRequests.push(payload);
        const events = [
          { type: 'progress', phase: 'checking', current: 0, total: 3 },
          { type: 'progress', phase: 'planning', current: 0, total: 3 },
          { type: 'progress', phase: 'ready', current: 0, total: 3 },
          ...localThreads.map((thread, index) => ({
            type: 'progress',
            phase: 'copying',
            current: index + 1,
            total: localThreads.length,
            item_title: thread.title,
            source_id: thread.id,
          })),
          { type: 'progress', phase: 'committing', current: 3, total: 3 },
          { type: 'progress', phase: 'done', current: 3, total: 3 },
          {
            type: 'complete',
            result: {
              ok: true,
              blocked: false,
              overwrite: false,
              item_total: localThreads.length,
              session_index_entries: localThreads.length,
              manifest_path: 'C:\\Codex\\session-transfer\\manifest.json',
              items: localThreads.map((thread, index) => ({
                source_id: thread.id,
                target_id: `target-${index + 1}`,
                display_title: thread.title,
              })),
            },
          },
        ];
        response.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
        response.end(`${events.map((event) => JSON.stringify(event)).join('\n')}\n`);
      });
      return;
    }
    if (url.pathname === '/api/copy-package-progress' && request.method === 'POST') {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', () => {
        const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        packageCopyRequests.push(payload);
        const events = [
          { type: 'progress', phase: 'checking', current: 0, total: packageThreads.length },
          { type: 'progress', phase: 'planning', current: 0, total: packageThreads.length },
          { type: 'progress', phase: 'ready', current: 0, total: packageThreads.length },
          ...packageThreads.map((thread, index) => ({
            type: 'progress',
            phase: 'copying',
            current: index + 1,
            total: packageThreads.length,
            item_title: thread.title,
            source_id: thread.id,
          })),
          { type: 'progress', phase: 'committing', current: packageThreads.length, total: packageThreads.length },
          { type: 'progress', phase: 'done', current: packageThreads.length, total: packageThreads.length },
          {
            type: 'complete',
            result: {
              ok: true,
              blocked: false,
              overwrite: false,
              item_total: packageThreads.length,
              session_index_entries: packageThreads.length,
              manifest_path: 'C:\\Codex\\session-transfer\\manifest.json',
              items: packageThreads.map((thread, index) => ({
                source_id: thread.id,
                target_id: `imported-${index + 1}`,
                display_title: thread.title,
              })),
            },
          },
        ];
        response.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
        response.end(`${events.map((event) => JSON.stringify(event)).join('\n')}\n`);
      });
      return;
    }
    if (url.pathname === '/api/preview-package-copy' && request.method === 'POST') {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', () => {
        const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        previewRequests.push(payload);
        json(response, {
          can_execute: true,
          errors: [],
          warnings: [],
          workspace_mappings: [{
            source_cwd: projects[0].cwd,
            target_cwd: payload.workspace_mapping.overrides[projects[0].cwd],
            project_label: projects[0].label,
            session_count: 1,
            overridden: true,
          }],
          items: [{
            source_id: packageThreads[0].id,
            target_id: 'target-thread-1',
            display_title: packageThreads[0].title,
            source_provider: payload.source_provider,
            target_provider: payload.target_provider,
            source_cwd: projects[0].cwd,
            target_cwd: payload.workspace_mapping.overrides[projects[0].cwd],
            cwd_rewritten: true,
          }],
        });
      });
      return;
    }

    const relative = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname.slice(1));
    const filePath = path.resolve(staticRoot, relative);
    if (!filePath.startsWith(`${staticRoot}${path.sep}`) || !fs.existsSync(filePath)) {
      response.writeHead(404);
      response.end('Not found');
      return;
    }
    response.writeHead(200, { 'Content-Type': contentType(filePath) });
    fs.createReadStream(filePath).pipe(response);
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

async function setContentSize(electronApp, page, width, height) {
  await electronApp.evaluate(({ BrowserWindow }, size) => {
    BrowserWindow.getAllWindows()[0].setContentSize(size.width, size.height);
  }, { width, height });
  await page.waitForFunction(
    (size) => innerWidth === size.width && innerHeight === size.height,
    { width, height },
  );
}

(async () => {
  const css = fs.readFileSync(path.join(staticRoot, 'styles.css'), 'utf8');
  const wideRule = css.match(/@media\s*\(min-width:\s*1241px\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(wideRule, 'missing wide-layout scroll-safety breakpoint');
  assert.match(wideRule[1], /\.main-surface\s*\{[^}]*overflow-y:\s*auto/s);
  assert.match(wideRule[1], /#sessionTransferGrid\s*\{[^}]*min-height:\s*240px/s);
  assert.match(wideRule[1], /#sessionTransferGrid\s*\{[^}]*flex:\s*1\s+0\s+240px/s);

  const server = await startFixtureServer();
  const address = server.address();
  const electronApp = await electron.launch({
    args: [path.join(__dirname, 'electron_layout_harness.js')],
    env: {
      ...process.env,
      LAYOUT_TEST_URL: `http://127.0.0.1:${address.port}`,
    },
  });

  try {
    const page = await electronApp.firstWindow();
    const pageErrors = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    await page.waitForSelector('#sessionTransferGrid');

    // Leave enough physical width for the 1241px CSS breakpoint on Retina runners.
    await setContentSize(electronApp, page, 1800, 768);
    await page.click('#packageModeButton');
    await page.check('#selectAll');
    await page.waitForFunction(() => document.querySelectorAll('.workspace-mapping-row').length === 10);
    page.once('dialog', (dialog) => dialog.accept());
    await page.click('#copyButton');
    await page.waitForFunction(
      () => document.querySelector('#copyProgressPhase').textContent === 'Complete'
        && document.querySelector('#copyProgressPercent').textContent === '100%',
    );
    await page.waitForFunction(() => document.querySelector('#copyResult').textContent.includes('Imported 10 session(s)'));
    const importedUi = await page.evaluate(() => ({
      progressPhase: document.querySelector('#copyProgressPhase').textContent,
      progressPercent: document.querySelector('#copyProgressPercent').textContent,
      previewItems: document.querySelectorAll('#previewItems .preview-session-item').length,
    }));
    assert.equal(importedUi.progressPhase, 'Complete');
    assert.equal(importedUi.progressPercent, '100%');
    assert.equal(importedUi.previewItems, 0);
    assert.equal(packageCopyRequests.length, 1);
    assert.equal(packageCopyRequests[0].thread_ids.length, 10);
    const compact = await page.evaluate(() => {
      const main = document.querySelector('.main-surface');
      const grid = document.querySelector('#sessionTransferGrid');
      main.scrollTop = main.scrollHeight;
      const mainRect = main.getBoundingClientRect();
      const gridRect = grid.getBoundingClientRect();
      return {
        overflowY: getComputedStyle(main).overflowY,
        bodyOverflowY: getComputedStyle(document.body).overflowY,
        scrollRange: main.scrollHeight - main.clientHeight,
        scrollTop: main.scrollTop,
        gridHeight: gridRect.height,
        gridBottom: gridRect.bottom,
        mainBottom: mainRect.bottom,
        horizontalFits: document.documentElement.scrollWidth <= innerWidth,
        documentScrollable: document.documentElement.scrollHeight > document.documentElement.clientHeight,
        wideMedia: matchMedia('(min-width: 1241px)').matches,
      };
    });
    if (compact.wideMedia) {
      assert.equal(compact.overflowY, 'auto');
      assert.ok(compact.scrollRange > 0, `expected compact scroll range, got ${compact.scrollRange}`);
      assert.equal(compact.scrollTop, compact.scrollRange);
      assert.ok(compact.gridBottom <= compact.mainBottom + 1, 'grid bottom is unreachable after scrolling');
    } else {
      assert.equal(compact.overflowY, 'hidden');
      assert.equal(compact.bodyOverflowY, 'auto');
      assert.equal(compact.documentScrollable, true);
    }
    assert.ok(compact.gridHeight >= 240, `grid collapsed to ${compact.gridHeight}px`);
    assert.equal(compact.horizontalFits, true);

    await page.selectOption('#workspaceModeSelect', 'single_workspace');
    await page.fill('#workspaceTargetRoot', 'D:\\');
    await page.locator('.workspace-override-field input').first().fill('D:\\ProjectOne');
    await page.click('#previewButton');
    await page.waitForFunction(() => document.querySelector('#copyResult').textContent.includes('Preview ready'));
    assert.equal(previewRequests.length, 1);
    assert.equal(previewRequests[0].workspace_mapping.mode, 'single_workspace');
    assert.equal(previewRequests[0].workspace_mapping.target_root, 'D:\\');
    assert.deepEqual(
      previewRequests[0].workspace_mapping.overrides,
      { 'C:\\source\\Project1': 'D:\\ProjectOne' },
    );

    await page.click('#localModeButton');
    await setContentSize(electronApp, page, 1440, 920);
    await page.waitForFunction(
      () => document.querySelectorAll('#sourceThreadsBody tr').length === 3,
    );
    await page.waitForFunction(() => !document.querySelector('#selectAll').checked);
    await page.check('#selectAll');
    await page.waitForFunction(() => document.querySelector('#selectedCount').textContent === '3 selected');
    const copyReady = await page.evaluate(() => ({
      disabled: document.querySelector('#copyButton').disabled,
      hint: document.querySelector('#copyDisabledReason').textContent,
      previewItems: document.querySelectorAll('#previewItems .preview-session-item').length,
    }));
    assert.equal(copyReady.disabled, false);
    assert.doesNotMatch(copyReady.hint, /Run Preview/i);
    assert.equal(copyReady.previewItems, 0);
    page.once('dialog', (dialog) => dialog.accept());
    await page.click('#copyButton');
    await page.waitForFunction(
      () => document.querySelector('#copyProgressPhase').textContent === 'Complete'
        && document.querySelector('#copyProgressPercent').textContent === '100%',
    );
    await page.waitForFunction(() => document.querySelector('#copyResult').textContent.includes('Copied 3 session(s)'));
    const copiedUi = await page.evaluate(() => ({
      progressPhase: document.querySelector('#copyProgressPhase').textContent,
      progressPercent: document.querySelector('#copyProgressPercent').textContent,
      previewItems: document.querySelectorAll('#previewItems .preview-session-item').length,
      result: document.querySelector('#copyResult').textContent,
    }));
    assert.equal(copiedUi.progressPhase, 'Complete');
    assert.equal(copiedUi.progressPercent, '100%');
    assert.equal(copiedUi.previewItems, 0);
    assert.equal(copyRequests.length, 1);
    assert.equal(copyRequests[0].thread_ids.length, 3);
    assert.equal(previewRequests.length, 1);
    await page.selectOption('#sourceProvider', 'alternate-source');
    const clearedSelection = await page.evaluate(() => ({
      selectedCount: document.querySelector('#selectedCount').textContent,
      copyDisabled: document.querySelector('#copyButton').disabled,
      previewItems: document.querySelectorAll('#previewItems .preview-session-item').length,
    }));
    assert.equal(clearedSelection.selectedCount, '0 selected');
    assert.equal(clearedSelection.copyDisabled, true);
    assert.equal(clearedSelection.previewItems, 0);
    await page.selectOption('#sourceProvider', 'source-provider');
    await page.waitForFunction(
      () => document.querySelectorAll('#sourceThreadsBody tr').length === 3,
    );
    const tall = await page.evaluate(() => {
      const main = document.querySelector('.main-surface');
      const grid = document.querySelector('#sessionTransferGrid');
      const mainRect = main.getBoundingClientRect();
      const gridRect = grid.getBoundingClientRect();
      return {
        gridHeight: gridRect.height,
        unused: mainRect.bottom - gridRect.bottom,
      };
    });
    assert.ok(tall.gridHeight > 240, `tall desktop grid did not grow: ${tall.gridHeight}px`);
    assert.ok(tall.unused <= 2, `tall desktop left ${tall.unused}px unused`);

    // Keep the CSS viewport below 1240px on both standard and Retina runners.
    await setContentSize(electronApp, page, 800, 600);
    const narrow = await page.evaluate(() => ({
      bodyOverflowY: getComputedStyle(document.body).overflowY,
      horizontalFits: document.documentElement.scrollWidth <= innerWidth,
      documentScrollable: document.documentElement.scrollHeight > document.documentElement.clientHeight,
    }));
    assert.equal(narrow.bodyOverflowY, 'auto');
    assert.equal(narrow.horizontalFits, true);
    assert.equal(narrow.documentScrollable, true);

    await page.click('#skillsPageButton');
    await setContentSize(electronApp, page, 1366, 600);
    const skills = await page.evaluate(() => {
      const button = document.querySelector('#previewSkillsButton').getBoundingClientRect();
      return {
        visible: button.top >= 0 && button.bottom <= innerHeight,
        horizontalFits: document.documentElement.scrollWidth <= innerWidth,
      };
    });
    assert.equal(skills.visible, true);
    assert.equal(skills.horizontalFits, true);
    assert.deepEqual(pageErrors, []);
  } finally {
    await electronApp.close();
    await new Promise((resolve) => server.close(resolve));
  }

  console.log('responsive Electron layout check passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
