# Responsive Cross-Machine Workspace Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make compact displays fully scrollable and let cross-machine packages import any selected session tree into validated custom Codex App working directories with all authoritative metadata updated.

**Architecture:** Keep `server.py` as the transaction boundary and add a typed workspace-mapping request that resolves every final selected cwd after descendant expansion. Move frontend mapping calculations into a small browser/Node-compatible helper, expose only a native directory-picker IPC method from Electron, and add height-aware CSS without disturbing the existing width-based stacked layout.

**Tech Stack:** Python 3.12 `unittest` and SQLite, vanilla JavaScript, Electron 39 IPC/context bridge, CSS media queries, Playwright CLI, PyInstaller, Electron Builder, GitHub Actions.

---

## File Map

- `server.py`: parse workspace mapping, validate paths, resolve per-session target cwd, and rewrite current rollout/SQLite metadata.
- `tests/test_session_transfer.py`: backend regression and transaction tests.
- `static/workspace-mapping.js`: pure mapping calculations shared by the browser and Node tests.
- `tests/test_workspace_mapping.js`: pure frontend mapping tests.
- `static/index.html`: mapping mode controls, target path input, picker, and mapping list.
- `static/app.js`: state, request construction, rendering, and directory-picker integration.
- `static/styles.css`: mapping layout and compact-height scrolling.
- `electron/main.js`: compact minimum window dimensions and directory-picker IPC handler.
- `electron/preload.js`: narrow `chooseDirectory` context bridge.
- `tests/responsive_layout_check.js`: real-browser compact viewport regression check.
- `tests/window_constraints_check.js`: Electron minimum-dimension contract check.
- `package.json`, `package-lock.json`: JavaScript test scripts and `0.4.0` version.
- `README.md`, `docs/releases/v0.4.0.md`: user-facing behavior and release notes.

### Task 1: Isolated Workspace and Baseline

**Files:**
- Modify if needed: `.gitignore`

- [ ] **Step 1: Create an isolated feature worktree**

Detect linked-worktree state with `git rev-parse --git-dir`, `git rev-parse --git-common-dir`, and the submodule guard. If this is a normal checkout, verify `.worktrees/` is ignored, add it to `.gitignore` and commit only if required, then create branch `codex/v0.4.0-responsive-workspaces` at `.worktrees/v0.4.0-responsive-workspaces`.

- [ ] **Step 2: Install exact dependencies**

Run:

```powershell
npm ci
```

Expected: exit 0 with the lockfile unchanged.

- [ ] **Step 3: Verify the baseline**

Run:

```powershell
& 'C:\Users\hd\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -p 'test_*.py'
```

Expected: all existing tests pass before feature edits.

### Task 2: Workspace Mapping Request and Resolution

**Files:**
- Modify: `server.py`
- Modify: `tests/test_session_transfer.py`

- [ ] **Step 1: Write failing request parsing tests**

Add tests that parse this payload and reject invalid object types, modes, relative target roots, and simultaneous legacy/new mapping:

```python
request = CopyRequest.from_json(
    {
        "source_provider": "ProviderA",
        "target_provider": "ProviderB",
        "thread_ids": ["thread-a"],
        "workspace_mapping": {
            "mode": "preserve_projects",
            "target_root": str(target_root),
            "overrides": {str(source_cwd): str(target_project)},
        },
    }
)
self.assertEqual(request.workspace_mapping.mode, "preserve_projects")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run the new `CopyRequest` tests with `python -m unittest`. Expected: failure because `workspace_mapping` is not parsed.

- [ ] **Step 3: Implement the typed request contract**

Add:

```python
WORKSPACE_MAPPING_MODES = {"preserve_projects", "single_workspace"}

@dataclass(frozen=True)
class WorkspaceMapping:
    mode: str
    target_root: str
    overrides: dict[str, str]

    @classmethod
    def from_json(cls, data: Any) -> "WorkspaceMapping":
        if not isinstance(data, dict):
            raise ValueError("workspace_mapping must be an object")
        mode = str(data.get("mode", "")).strip()
        if mode not in WORKSPACE_MAPPING_MODES:
            raise ValueError("workspace_mapping.mode is invalid")
        target_root = str(data.get("target_root", "")).strip()
        overrides_raw = data.get("overrides", {})
        if not isinstance(overrides_raw, dict):
            raise ValueError("workspace_mapping.overrides must be an object")
        overrides = {
            str(source).strip(): str(target).strip()
            for source, target in overrides_raw.items()
            if str(source).strip() and str(target).strip()
        }
        return cls(mode, target_root, overrides)
```

Add `workspace_mapping: WorkspaceMapping | None = None` to `CopyRequest`; reject requests that supply both non-empty `cwd_map` and `workspace_mapping`.

- [ ] **Step 4: Write failing resolution tests**

Cover preserve-project mapping, single-workspace mapping, an explicit override, two same-named projects, a descendant with a different cwd, missing directories, file targets, and Windows/POSIX source-path comparison.

- [ ] **Step 5: Run focused tests and verify RED**

Expected: failures because `_resolve_workspace_cwds` does not exist and legacy resolution leaves descendant paths unchanged.

- [ ] **Step 6: Implement platform-aware resolution**

Implement helpers equivalent to:

```python
def _source_path_match_key(self, value: str) -> tuple[str, str]:
    clean = self._normalize_windows_path(value).rstrip("\\/")
    if re.match(r"^[A-Za-z]:[\\/]", clean) or clean.startswith("\\\\"):
        return "windows", clean.replace("/", "\\").casefold()
    return "posix", str(PurePosixPath(clean.replace("\\", "/")))

def _validate_target_directory(self, value: str, label: str, errors: list[str]) -> Path | None:
    target = Path(value)
    if not target.is_absolute():
        errors.append(f"{label} must be an absolute path: {value}")
    elif not target.exists():
        errors.append(f"{label} does not exist: {value}")
    elif not target.is_dir():
        errors.append(f"{label} is not a directory: {value}")
    else:
        return target
    return None
```

Resolve cwd values after descendants are expanded. Apply exact overrides first, then `target_root / project_label` for preserve mode or `target_root` for single mode. Reject duplicate preserve-mode destinations and include a public `workspace_mappings` summary in preview.

- [ ] **Step 7: Run focused and full Python tests**

Expected: all workspace mapping tests and the existing suite pass.

- [ ] **Step 8: Commit backend mapping**

```powershell
git add server.py tests/test_session_transfer.py
git commit -m "feat: map imported sessions to target workspaces"
```

### Task 3: Current Session Metadata Consistency

**Files:**
- Modify: `server.py`
- Modify: `tests/test_session_transfer.py`

- [ ] **Step 1: Write a failing modern-rollout test**

Create a rollout whose first metadata payload contains both ids:

```python
payload = {
    "session_id": source_id,
    "id": source_id,
    "cwd": str(source_cwd),
    "model_provider": "ProviderA",
}
```

After import, assert `session_id == id == target_id`, rollout `cwd == target_cwd`, and the SQLite row has the same cwd and destination rollout path.

- [ ] **Step 2: Run the test and verify RED**

Expected: `session_id` still equals the source id.

- [ ] **Step 3: Implement the minimal metadata rewrite**

In `_write_rollout_copy`, after assigning `payload["id"]`, set `payload["session_id"]` to the new id only when the field exists. Keep historical `TurnContext`, command execution items, message text, and tool output unchanged.

- [ ] **Step 4: Add rollback and index-shape assertions**

Verify `session_index.jsonl` output keys are exactly `id`, `thread_name`, and `updated_at`; force a post-rollout failure and assert SQLite rollback, index restoration, and copied-rollout cleanup.

- [ ] **Step 5: Run focused and full Python tests**

Expected: all tests pass with no leftover copied files.

- [ ] **Step 6: Commit metadata consistency**

```powershell
git add server.py tests/test_session_transfer.py
git commit -m "fix: keep imported Codex metadata consistent"
```

### Task 4: Pure Frontend Mapping Model

**Files:**
- Create: `static/workspace-mapping.js`
- Create: `tests/test_workspace_mapping.js`
- Modify: `static/index.html`
- Modify: `package.json`

- [ ] **Step 1: Write failing Node tests**

Test selection and computation through a pure API:

```javascript
const mapping = require('../static/workspace-mapping.js');
const projects = mapping.selectedProjects(manifest, new Set(['thread-a']));
assert.deepEqual(projects.map((project) => project.cwd), ['C:\\old\\ProjectA']);
assert.equal(
  mapping.computedTarget('D:\\CodexWork', 'C:\\old\\ProjectA', 'preserve_projects'),
  'D:\\CodexWork\\ProjectA'
);
```

Also cover POSIX paths, trailing separators, single-workspace mode, overrides, and duplicate target detection.

- [ ] **Step 2: Run Node tests and verify RED**

Expected: module-not-found failure.

- [ ] **Step 3: Implement the UMD-style pure helper**

Expose `selectedProjects`, `projectLabel`, `joinPath`, `computedTarget`, `effectiveMappings`, and `duplicateTargets` to `window.WorkspaceMapping` and `module.exports` without DOM access.

- [ ] **Step 4: Load helper before the app**

Add `<script src="./workspace-mapping.js"></script>` immediately before `app.js`.

- [ ] **Step 5: Add JavaScript test scripts**

Add:

```json
"test:js": "node tests/test_workspace_mapping.js",
"test": "npm run test:js"
```

- [ ] **Step 6: Run JavaScript tests and syntax checks**

Run `npm test`, `node --check static/workspace-mapping.js`, and `node --check static/app.js`. Expected: exit 0.

- [ ] **Step 7: Commit the frontend model**

```powershell
git add static/workspace-mapping.js static/index.html tests/test_workspace_mapping.js package.json
git commit -m "feat: add workspace mapping model"
```

### Task 5: Mapping UI and Native Directory Picker

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `electron/main.js`
- Modify: `electron/preload.js`
- Modify: `tests/test_workspace_mapping.js`

- [ ] **Step 1: Add failing request-construction tests to the pure model**

Assert a preserve-project payload contains mode, root, and only changed overrides; assert single-workspace mode uses the exact root.

- [ ] **Step 2: Run JavaScript tests and verify RED**

Expected: missing `requestPayload` export.

- [ ] **Step 3: Implement the mapping controls**

Replace the single target-cwd field with:

```html
<div class="workspace-mode" role="radiogroup" aria-label="Target workspace mapping">
  <label><input type="radio" name="workspaceMode" value="preserve_projects" checked>Preserve projects</label>
  <label><input type="radio" name="workspaceMode" value="single_workspace">Single workspace</label>
</div>
<div class="workspace-root-row">
  <label class="field">Target workspace<input id="targetCwdInput"></label>
  <button id="chooseTargetCwdButton" type="button">Choose folder</button>
</div>
<div id="workspaceMappingList" class="workspace-mapping-list"></div>
```

Use existing button icons and compact panel styling. Render source and target paths without nesting cards.

- [ ] **Step 4: Construct the new API request**

`copyRequest()` sends `workspace_mapping` when package mode is loaded and a target root is present. Rendering derives selected projects from the manifest, preserves typed overrides after preview errors, and resets mappings only when the package changes or unloads.

- [ ] **Step 5: Add the secure directory picker**

Register one IPC channel in `electron/main.js`:

```javascript
ipcMain.handle('codex-session-transfer:choose-directory', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory'] });
  return result.canceled ? '' : result.filePaths[0] || '';
});
```

Expose only `chooseDirectory()` through `contextBridge` in preload. Hide the picker button when the bridge is unavailable so browser users can type a path.

- [ ] **Step 6: Verify frontend behavior**

Run JavaScript tests and syntax checks, then use Playwright to load a package fixture or mocked package status and inspect preserve, single, override, and validation states.

- [ ] **Step 7: Commit mapping UI**

```powershell
git add static/index.html static/app.js static/styles.css electron/main.js electron/preload.js tests/test_workspace_mapping.js
git commit -m "feat: add cross-machine workspace mapping UI"
```

### Task 6: Compact-Display Scrolling

**Files:**
- Modify: `static/styles.css`
- Modify: `electron/main.js`
- Create: `tests/responsive_layout_check.js`
- Create: `tests/window_constraints_check.js`

- [ ] **Step 1: Add and run the failing browser regression check**

At `1366x600` and `1280x720`, assert `.main-surface` has `overflow-y: auto`, its scroll range is positive, `#sessionTransferGrid` is at least 240 pixels high, and the list bottom is reachable after scrolling. Verify the existing implementation fails because overflow is hidden and the list collapses.

- [ ] **Step 2: Add and run the failing window-constraint check**

Parse `electron/main.js` and require `minWidth <= 720` and `minHeight <= 480`. Verify the current `980x760` configuration fails.

- [ ] **Step 3: Implement compact-height CSS**

Add a non-conflicting media query:

```css
@media (min-width: 1241px) and (max-height: 760px) {
  .main-surface {
    overflow-y: auto;
    overscroll-behavior-y: contain;
    scrollbar-gutter: stable;
  }

  #sessionTransferGrid {
    min-height: 240px;
    flex: 0 0 240px;
  }
}
```

Change BrowserWindow minimums to `720x480`.

Extend `test:js` to run `tests/window_constraints_check.js` after the workspace-mapping test.

- [ ] **Step 4: Verify desktop, compact, and narrow viewports**

Run the responsive check at `1366x768`, `1366x600`, `1280x720`, `1024x600`, and `700x800`. Capture screenshots under `output/playwright/`, inspect them, and confirm no overlap or unreachable controls on Sessions and Skills pages.

- [ ] **Step 5: Run all JavaScript and Python tests**

Expected: all tests pass.

- [ ] **Step 6: Commit responsive fixes**

```powershell
git add static/styles.css electron/main.js tests/responsive_layout_check.js tests/window_constraints_check.js
git commit -m "fix: keep compact displays scrollable"
```

### Task 7: Version, Documentation, and Full Verification

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `README.md`
- Create: `docs/releases/v0.4.0.md`

- [ ] **Step 1: Bump the version**

Run `npm version 0.4.0 --no-git-tag-version` and verify package and lockfile agree.

- [ ] **Step 2: Update user documentation**

Document compact-display scrolling, preserve-project/single-workspace modes, per-project overrides, target-directory validation, and the fact that repositories are not copied.

- [ ] **Step 3: Run fresh full verification**

Run:

```powershell
npm ci
npm test
node --check static/app.js
node --check static/workspace-mapping.js
& $python -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 4: Commit release metadata**

```powershell
git add package.json package-lock.json README.md docs/releases/v0.4.0.md
git commit -m "chore: prepare v0.4.0 release"
```

### Task 8: Windows Build and Artifact Audit

**Files:**
- Generated: `build/server/win/**`
- Generated: `output/electron/**`
- Generated: `release/v0.4.0/windows/**`

- [ ] **Step 1: Install build requirements**

Run bundled Python with `-m pip install -r requirements-build.txt` and `npm ci`.

- [ ] **Step 2: Build Windows release artifacts**

Run:

```powershell
npm run release:win
```

Expected: Setup EXE, Portable EXE, `latest.yml`, checksums, and manifest under `release/v0.4.0/windows`.

- [ ] **Step 3: Audit checksums and manifests**

Recompute SHA-256 for every listed artifact, verify byte counts, version `0.4.0`, platform `win`, and that each file is non-empty.

- [ ] **Step 4: Smoke-test the packaged server**

Start the built server on an unused port, request `/api/status` and `/`, confirm HTTP 200, then terminate only that process.

- [ ] **Step 5: Commit release manifests only when repository policy tracks them**

Inspect existing release tracking. Add generated `release/v0.4.0/windows` files consistent with prior releases and commit `build: add v0.4.0 Windows artifacts`; do not commit transient `build/` or `output/` files.

### Task 9: Integrate, Push, Tag, and Verify GitHub Release

**Files:**
- Git history and GitHub Release state

- [ ] **Step 1: Review the complete diff and commit history**

Confirm only scoped files changed, no secrets or Playwright temp state are tracked, and the feature branch is clean.

- [ ] **Step 2: Merge the feature branch into local `main`**

Update local `main` from `origin/main`, merge without force, and rerun the full test suite on the merged result.

- [ ] **Step 3: Push `main`**

Push normally to `origin/main`; do not force-push.

- [ ] **Step 4: Create and push annotated tag**

Create `v0.4.0` only after verifying no remote tag exists, then push the tag to trigger `.github/workflows/release-build.yml`.

- [ ] **Step 5: Monitor GitHub Actions**

Use `gh run list` and `gh run watch --exit-status` until the tag workflow completes. If a platform job fails, inspect logs, fix the cause in a follow-up commit, retag only after deleting the failed unpublished tag/release safely, and rerun verification.

- [ ] **Step 6: Audit the published release**

Verify `gh release view v0.4.0` is published and contains Windows Setup/Portable, macOS DMG/ZIP, both platform manifests, and checksum files. Download manifests/checksums to a temporary directory and verify asset names and versions.

- [ ] **Step 7: Clean owned temporary state**

Remove the temporary official-source checkout and Playwright CLI session artifacts created by this task. Preserve tracked screenshots and release artifacts. Remove the owned worktree only after successful merge and release verification.
