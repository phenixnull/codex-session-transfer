const state = {
  status: null,
  sessionProviders: [],
  targetProviders: [],
  stats: null,
  page: "sessions",
  mode: "local",
  packageSource: { loaded: false },
  skillsStatus: { total: 0, package_source: { loaded: false } },
  skillPackageSource: { loaded: false },
  sourceThreads: [],
  targetThreads: [],
  sourceSkills: [],
  targetSkills: [],
  selected: new Set(),
  selectedSkills: new Set(),
  lastCopiedTargetIds: new Set(),
  activeSession: null,
  activeThreadDetail: null,
  activeThreadLoading: false,
  activeThreadLoadingMore: false,
  sessionRenderCollapsed: true,
  preview: null,
  previewLoadingMore: false,
  copyInProgress: false,
  skillPreview: null,
  copyResult: "",
  skillsResult: "",
  workspaceMode: "preserve_projects",
  workspaceTargetRoot: "",
  workspaceOverrides: {},
};

const $ = (id) => document.getElementById(id);
const CUSTOM_TARGET = "__custom__";
const SESSION_RENDER_PAGE_SIZE = 80;
const PREVIEW_RENDER_PAGE_SIZE = 48;
const PREVIEW_SCROLL_THRESHOLD = 180;
const EXPORT_DIR_STORAGE_KEY = "codex-session-transfer.exportDir";

function usingPackageSource() {
  return state.mode === "package" && Boolean(state.packageSource?.loaded);
}

function usingSkillPackageSource() {
  return state.page === "skills" && Boolean(state.skillPackageSource?.loaded);
}

function currentSourceOrigin() {
  return usingPackageSource() ? "package" : "local";
}

function currentSourceProviders() {
  return usingPackageSource() ? state.packageSource.providers || [] : state.sessionProviders;
}

function currentSourceStats() {
  return usingPackageSource() ? state.packageSource.session_stats || {} : state.stats;
}

function includeArchivedChecked() {
  return Boolean($("includeArchived")?.checked);
}

function visibleThreadCount(item) {
  const total = Number(item?.total ?? 0);
  const active = Number(item?.active ?? total);
  return includeArchivedChecked() ? total : active;
}

function sourceThreadsEndpoint() {
  return usingPackageSource() ? "/api/package-threads" : "/api/threads";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error((data.errors || [response.statusText]).join("; "));
  }
  return data;
}

async function streamApi(path, options = {}, onEvent = () => {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error((data.errors || [response.statusText]).join("; "));
  }
  if (!response.body) throw new Error("Copy progress stream is unavailable in this browser.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  const consume = (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    onEvent(event);
    if (event.type === "complete") result = event.result;
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) consume(line.replace(/\r$/, ""));
    if (done) break;
  }
  consume(buffer.replace(/\r$/, ""));
  if (!result) throw new Error("Copy ended without a final result.");
  return result;
}

async function uploadPackageFile(path, file) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/zip",
      "X-Package-Filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error((data.errors || [response.statusText]).join("; "));
  }
  return data;
}

function targetProviderValue() {
  const selected = $("targetProviderSelect").value;
  return selected === CUSTOM_TARGET
    ? $("targetProviderCustom").value.trim()
    : selected;
}

function copyRequest() {
  const request = {
    source_provider: $("sourceProvider").value,
    target_provider: targetProviderValue(),
    thread_ids: Array.from(state.selected),
    include_descendants: $("includeDescendants").checked,
    include_archived: $("includeArchived").checked,
    overwrite: Boolean($("overwriteSessions")?.checked),
  };
  const workspaceMapping = packageWorkspaceMapping();
  if (workspaceMapping) request.workspace_mapping = workspaceMapping;
  return request;
}

function rebindRequest() {
  return {
    source_provider: $("sourceProvider").value,
    target_provider: targetProviderValue(),
    thread_ids: Array.from(state.selected),
    include_descendants: $("includeDescendants").checked,
    include_archived: $("includeArchived").checked,
  };
}

function selectedPackageProjects() {
  if (!usingPackageSource()) return [];
  return window.WorkspaceMapping.selectedProjects(
    state.packageSource?.manifest,
    state.selected,
  );
}

function effectiveWorkspaceMappings() {
  return window.WorkspaceMapping.effectiveMappings(
    selectedPackageProjects(),
    state.workspaceTargetRoot,
    state.workspaceMode,
    state.workspaceOverrides,
  );
}

function packageWorkspaceMapping() {
  if (!usingPackageSource() || !state.workspaceTargetRoot.trim()) return null;
  return window.WorkspaceMapping.requestPayload(
    state.workspaceMode,
    state.workspaceTargetRoot,
    effectiveWorkspaceMappings(),
  );
}

function resetWorkspaceMappingState() {
  state.workspaceMode = "preserve_projects";
  state.workspaceTargetRoot = "";
  state.workspaceOverrides = {};
}

function storageGet(key) {
  try {
    return window.localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function storageSet(key, value) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    // Ignore unavailable local storage.
  }
}

function displayPath(value) {
  return String(value || "").replace(/^\\\\\?\\/, "");
}

function appendPath(base, child) {
  const clean = displayPath(base).replace(/[\\/]+$/, "");
  if (!clean) return "";
  const separator = clean.includes("\\") ? "\\" : "/";
  return `${clean}${separator}${child}`;
}

function selectedProjectCwd() {
  return $("projectFilter")?.value || "";
}

function singleSelectedThreadCwd() {
  const selectedCwds = Array.from(new Set(
    state.sourceThreads
      .filter((thread) => state.selected.has(thread.id))
      .map((thread) => thread.cwd)
      .filter(Boolean)
      .map(displayPath),
  ));
  return selectedCwds.length === 1 ? selectedCwds[0] : "";
}

function inferredExportDir() {
  const cwd = selectedProjectCwd() || singleSelectedThreadCwd();
  return appendPath(cwd, "exported");
}

function currentExportDir() {
  return storageGet(EXPORT_DIR_STORAGE_KEY) || inferredExportDir();
}

function syncExportDirInput() {
  const input = $("exportDirInput");
  if (!input || document.activeElement === input) return;
  const value = currentExportDir();
  input.value = value;
  input.title = value;
}

async function openPath(path) {
  if (!path) return null;
  const result = await api("/api/open-path", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  if (!result?.ok) {
    throw new Error((result?.errors || ["Folder was not opened"]).join("; "));
  }
  return result;
}

async function loadAll() {
  await loadStatus();
  ensureProviderSelection();
  renderAllShell();
  if (state.page === "skills") {
    await loadSkillLists();
  } else {
    await loadThreadLists();
  }
}

async function loadStatus() {
  state.status = await api("/api/status");
  state.sessionProviders = state.status.providers || [];
  state.targetProviders = state.status.target_providers || [];
  state.stats = state.status.session_stats || null;
  state.packageSource = state.status.package_source || { loaded: false };
  state.skillsStatus = state.status.skills || { total: 0, package_source: { loaded: false } };
  state.skillPackageSource = state.skillsStatus.package_source || { loaded: false };
}

function threadQueryParams(provider, options = {}) {
  const params = new URLSearchParams({
    source_provider: provider || "",
    include_archived: $("includeArchived").checked ? "true" : "false",
    search: $("searchInput").value.trim(),
  });
  const omitSourceFilters = options.target && usingPackageSource();
  appendSourceThreadFilters(params, { omitSourceFilters });
  return params;
}

function appendSourceThreadFilters(params, { omitSourceFilters = false } = {}) {
  const recentLimit = $("recentLimitFilter").value.trim();
  params.set("source", omitSourceFilters ? "" : $("sourceFilter").value);
  params.set("cwd", omitSourceFilters ? "" : $("projectFilter").value);
  params.set("date_from", omitSourceFilters ? "" : $("dateFromFilter").value);
  params.set("date_to", omitSourceFilters ? "" : $("dateToFilter").value);
  params.set("recent_limit", omitSourceFilters ? "" : recentLimit);
}

async function loadThreadLists(options = {}) {
  await loadSourceThreads(options);
  await loadTargetThreads();
}

async function loadSkillLists(options = {}) {
  await loadSourceSkills(options);
  await loadTargetSkills();
}

async function loadSourceThreads(options = {}) {
  const params = new URLSearchParams({
    source_provider: $("sourceProvider").value,
    include_archived: $("includeArchived").checked ? "true" : "false",
    search: $("searchInput").value.trim(),
  });
  appendSourceThreadFilters(params);
  state.sourceThreads = await api(`${sourceThreadsEndpoint()}?${params.toString()}`);
  renderSourceFilter();
  renderSourceThreads();
  reconcileActiveSession();
  invalidatePreview({ clearResult: !options.preserveResult });
}

async function loadTargetThreads() {
  const target = targetProviderValue();
  if (!target) {
    state.targetThreads = [];
  } else {
    state.targetThreads = await api(`/api/threads?${threadQueryParams(target, { target: true }).toString()}`);
  }
  renderTargetThreads();
  reconcileActiveSession();
}

async function loadSourceSkills(options = {}) {
  const params = new URLSearchParams({
    search: $("skillsSearchInput").value.trim(),
  });
  const endpoint = usingSkillPackageSource() ? "/api/package-skills" : "/api/skills";
  state.sourceSkills = await api(`${endpoint}?${params.toString()}`);
  renderSourceSkills();
  invalidateSkillPreview({ clearResult: !options.preserveResult });
}

async function loadTargetSkills() {
  const params = new URLSearchParams({
    search: $("skillsSearchInput").value.trim(),
  });
  state.targetSkills = await api(`/api/skills?${params.toString()}`);
  renderTargetSkills();
}

function ensureProviderSelection() {
  const sourceSelect = $("sourceProvider");
  const sourceProvider = preferredSourceProvider(sourceSelect.value);
  if (sourceProvider) sourceSelect.value = sourceProvider;

  const targetSelect = $("targetProviderSelect");
  const targetSourceValue = usingPackageSource() ? "" : sourceProvider || sourceSelect.value;
  const targetProvider = preferredTargetProvider(targetSelect.value, targetSourceValue);
  if (targetProvider) targetSelect.value = targetProvider;
}

function preferredSourceProvider(currentValue) {
  const providers = currentSourceProviders();
  const current = providers.find((provider) => provider.model_provider === currentValue);
  if (current && current.active > 0) return current.model_provider;

  const activeProvider = providers.find((provider) => provider.active > 0);
  if (activeProvider) return activeProvider.model_provider;

  return current?.model_provider || providers[0]?.model_provider || "";
}

function preferredTargetProvider(currentValue, sourceValue) {
  const options = targetProviderOptions();
  const current = options.find((provider) => provider.value === currentValue);
  if (current && (current.value !== sourceValue || current.current)) return current.value;

  const liveTarget = options.find((provider) => provider.current);
  if (liveTarget) return liveTarget.value;

  const configuredTarget = options.find(
    (provider) => provider.value !== sourceValue && targetProviderIsConfigured(provider) === true,
  );
  if (configuredTarget) return configuredTarget.value;

  const nonSourceTarget = options.find((provider) => provider.value !== sourceValue);
  if (nonSourceTarget) return nonSourceTarget.value;

  return current?.value || options.find((provider) => provider.current)?.value || options[0]?.value || "";
}

function renderAllShell() {
  renderPageChrome();
  renderModeChrome();
  renderStatus();
  renderStats();
  renderProviders();
  renderTargetProviders();
  renderLiveTargetPanel();
  renderOverwriteSessionsControl();
  renderProjectFilter();
  renderPackagePanel();
  renderSkillsPackagePanel();
  updateRebindButton();
}

function renderPageChrome() {
  const skillsPage = state.page === "skills";
  const sessionButton = $("sessionPageButton");
  const skillsButton = $("skillsPageButton");
  if (sessionButton) {
    sessionButton.classList.toggle("active", !skillsPage);
    sessionButton.setAttribute("aria-selected", String(!skillsPage));
  }
  if (skillsButton) {
    skillsButton.classList.toggle("active", skillsPage);
    skillsButton.setAttribute("aria-selected", String(skillsPage));
  }
  for (const id of ["sessionControlPanel", "sessionTransferGrid", "sessionRail", "rightRail", "sessionRailGutter", "rightRailGutter"]) {
    const node = $(id);
    if (node) node.hidden = skillsPage;
  }
  const skillsWorkbench = $("skillsWorkbench");
  if (skillsWorkbench) skillsWorkbench.hidden = !skillsPage;
}

function renderModeChrome() {
  const localButton = $("localModeButton");
  const packageButton = $("packageModeButton");
  const isPackageMode = state.mode === "package";
  if (localButton) {
    localButton.classList.toggle("active", !isPackageMode);
    localButton.setAttribute("aria-selected", String(!isPackageMode));
  }
  if (packageButton) {
    packageButton.classList.toggle("active", isPackageMode);
    packageButton.setAttribute("aria-selected", String(isPackageMode));
  }
  const panel = $("packagePanel");
  if (panel) panel.hidden = !isPackageMode;
  const title = $("workbenchTitle");
  if (title) title.textContent = usingPackageSource() ? "Package Source Sessions" : "Source Sessions";
  const rebindButton = $("rebindButton");
  if (rebindButton) rebindButton.hidden = isPackageMode;
  const kind = $("sourceSetupKind");
  if (kind) kind.textContent = usingPackageSource() ? "Package" : "Provider";
  const label = $("sourceProviderLabel");
  if (label) label.textContent = usingPackageSource() ? "Package source provider" : "Source session provider";
}

function renderPackagePanel() {
  const line = $("packageStatusLine");
  if (!line) return;
  line.replaceChildren();
  syncExportDirInput();
  const loaded = Boolean(state.packageSource?.loaded);
  const exportButton = $("exportPackageButton");
  const loadButton = $("loadPackageButton");
  const unloadButton = $("unloadPackageButton");
  const pathInput = $("packagePathInput");
  const rewritePanel = $("packageCwdRewritePanel");
  const modeSelect = $("workspaceModeSelect");
  const targetRootInput = $("workspaceTargetRoot");
  const pickerButton = $("chooseWorkspaceDirectoryButton");
  if (exportButton) {
    exportButton.disabled = state.mode !== "package" || loaded || state.selected.size === 0;
    exportButton.title = loaded
      ? "Unload the package source before exporting from local sessions."
      : state.selected.size
        ? "Export the selected local source sessions into a transfer package."
        : "Select one or more local source sessions to export.";
  }
  if (loadButton) {
    loadButton.disabled = state.mode !== "package";
  }
  if (unloadButton) unloadButton.disabled = !loaded;
  if (rewritePanel) rewritePanel.hidden = !loaded;
  if (modeSelect) {
    modeSelect.disabled = !loaded;
    modeSelect.value = state.workspaceMode;
  }
  if (targetRootInput) {
    targetRootInput.disabled = !loaded;
    if (document.activeElement !== targetRootInput) targetRootInput.value = state.workspaceTargetRoot;
  }
  if (pickerButton) {
    pickerButton.hidden = !window.codexDesktop?.chooseDirectory;
    pickerButton.disabled = !loaded;
  }

  if (!loaded) {
    renderWorkspaceMappingList();
    line.append(message("info", "Local source selected for package export."));
    return;
  }

  renderWorkspaceMappingList();
  const manifest = state.packageSource.manifest || {};
  const packagePath = state.packageSource.package_path || "";
  const projectCount = manifest.projects?.length ?? 0;
  const threadCount = manifest.thread_count ?? 0;
  line.append(
    statusPill("Package", packagePath ? shortPath(packagePath) : "Loaded", true),
    statusPill("Contents", `${threadCount} sessions / ${projectCount} projects`, true),
  );
}

function renderWorkspaceMappingList() {
  const list = $("workspaceMappingList");
  if (!list) return;
  list.replaceChildren();
  if (!usingPackageSource()) return;

  const mappings = effectiveWorkspaceMappings();
  if (!mappings.length) {
    list.append(message("info", "Select sessions to preview their workspace destinations."));
    return;
  }

  const duplicates = new Set(window.WorkspaceMapping.duplicateTargets(mappings));
  for (const entry of mappings) {
    const row = document.createElement("div");
    row.className = "workspace-mapping-row";

    const paths = document.createElement("div");
    paths.className = "workspace-mapping-paths";
    const label = document.createElement("strong");
    label.textContent = entry.project.label || window.WorkspaceMapping.projectLabel(entry.sourceCwd);
    const source = document.createElement("code");
    source.textContent = displayPath(entry.sourceCwd);
    source.title = displayPath(entry.sourceCwd);
    const target = document.createElement("code");
    target.className = "workspace-target-path";
    target.textContent = entry.targetCwd ? displayPath(entry.targetCwd) : "Package path unchanged";
    target.title = entry.targetCwd ? displayPath(entry.targetCwd) : "";
    paths.append(label, source, target);

    const overrideField = document.createElement("label");
    overrideField.className = "field workspace-override-field";
    overrideField.textContent = "Project override";
    const overrideInput = document.createElement("input");
    overrideInput.value = state.workspaceOverrides[entry.sourceCwd] || "";
    overrideInput.placeholder = "Use computed target";
    overrideInput.dataset.sourceCwd = entry.sourceCwd;
    overrideInput.addEventListener("input", () => {
      const value = overrideInput.value.trim();
      if (value) state.workspaceOverrides[entry.sourceCwd] = value;
      else delete state.workspaceOverrides[entry.sourceCwd];
      target.textContent = displayPath(
        value || window.WorkspaceMapping.computedTarget(
          state.workspaceTargetRoot,
          entry.sourceCwd,
          state.workspaceMode,
        ),
      ) || "Package path unchanged";
      invalidatePreview();
    });
    overrideInput.addEventListener("change", renderWorkspaceMappingList);
    overrideField.append(overrideInput);
    row.append(paths, overrideField);
    list.append(row);
  }

  if (state.workspaceMode === "preserve_projects" && duplicates.size) {
    list.prepend(message("error", "Multiple projects resolve to the same target. Add a project override."));
  }
}

async function chooseWorkspaceDirectory() {
  const selectedPath = await window.codexDesktop?.chooseDirectory();
  if (!selectedPath) return;
  state.workspaceTargetRoot = selectedPath;
  $("workspaceTargetRoot").value = selectedPath;
  renderWorkspaceMappingList();
  invalidatePreview();
}

function renderSkillsPackagePanel() {
  const line = $("skillsPackageStatusLine");
  if (!line) return;
  line.replaceChildren();
  const loaded = Boolean(state.skillPackageSource?.loaded);
  const exportButton = $("exportSkillsPackageButton");
  const loadButton = $("loadSkillsPackageButton");
  const unloadButton = $("unloadSkillsPackageButton");
  const previewButton = $("previewSkillsButton");
  const importButton = $("importSkillsButton");
  const pathInput = $("skillsPackagePathInput");
  if (exportButton) {
    exportButton.disabled = state.page !== "skills" || loaded || state.selectedSkills.size === 0;
    exportButton.title = loaded
      ? "Unload the skills package source before exporting local skills."
      : state.selectedSkills.size
        ? "Export the selected local skills into a transfer package."
        : "Select one or more local skills to export.";
  }
  if (loadButton) loadButton.disabled = state.page !== "skills";
  if (unloadButton) unloadButton.disabled = !loaded;
  if (previewButton) previewButton.disabled = !loaded || state.selectedSkills.size === 0;
  if (importButton) {
    importButton.disabled = !loaded || !state.skillPreview?.can_execute;
  }

  const title = $("skillsWorkbenchTitle");
  if (title) title.textContent = loaded ? "Skills Package Import" : "Skills Package Export";
  const sourceTitle = $("skillsSourceTitle");
  if (sourceTitle) sourceTitle.textContent = loaded ? "Package skills" : "Local skills";

  if (!loaded) {
    line.append(message("info", "Local skills selected here can be exported into a portable package."));
    return;
  }

  const manifest = state.skillPackageSource.manifest || {};
  line.append(
    statusPill("Skills package", state.skillPackageSource.package_path ? shortPath(state.skillPackageSource.package_path) : "Loaded", true),
    statusPill("Contents", `${manifest.skill_count ?? state.skillPackageSource.total ?? 0} skills`, true),
  );
}

function renderStatus() {
  $("dbPath").textContent = state.status ? state.status.db_path : "";
  const strip = $("statusStrip");
  strip.replaceChildren();
  if (!state.status) return;

  const blocking = state.status.blocking_processes || [];
  updateKillBlockingButton(blocking);
  updateRepairNamesButton(blocking);
  const current = state.status.current_config || {};
  strip.append(
    statusPill("DB", state.status.db_exists ? "Ready" : "Missing", state.status.db_exists),
    statusPill("Integrity", state.status.integrity_check || "Unknown", state.status.integrity_check === "ok"),
    statusPill("Processes", blocking.length ? `${blocking.length} blocking` : "Clear", blocking.length === 0),
    statusPill("WAL", `${(state.status.wal_files || []).length}`, true),
    statusPill("Session index", `${state.status.session_index?.entries ?? 0}`, Boolean(state.status.session_index?.exists)),
    statusPill("Package", state.packageSource?.loaded ? "Loaded" : "None", true),
    statusPill("Skills", `${state.skillsStatus?.total ?? 0}`, true),
    statusPill("Skill package", state.skillPackageSource?.loaded ? "Loaded" : "None", true),
    statusPill("Current provider", current.model_provider || "Unknown", Boolean(current.model_provider)),
    statusPill("Current model", current.model || "(not set)", true),
  );
}

function updateKillBlockingButton(blocking) {
  const button = $("killBlockingButton");
  if (!button) return;
  const count = blocking.length;
  button.disabled = count === 0;
  button.textContent = count ? `Kill blockers (${count})` : "Kill blockers";
  button.title = count
    ? "Terminate the detected Codex/Codex++ blocking processes so copy can write safely."
    : "No blocking Codex/Codex++ processes detected.";
}

function updateRepairNamesButton(blocking) {
  const button = $("repairNamesButton");
  if (!button) return;
  const blocked = blocking.length > 0;
  button.disabled = blocked;
  button.title = blocked
    ? "Close blocking Codex/Codex++ processes before repairing session_index names."
    : "Backfill copied session names from successful transfer manifests.";
}

function statusPill(label, value, good) {
  const node = document.createElement("div");
  node.className = `status-pill ${good ? "good" : "bad"}`;
  const strong = document.createElement("strong");
  strong.textContent = label;
  const span = document.createElement("span");
  span.textContent = value;
  node.append(strong, span);
  return node;
}

function renderStats() {
  const grid = $("statsGrid");
  grid.replaceChildren();
  const totals = state.stats?.totals;
  if (!totals) return;
  grid.append(
    statTile("Sessions", totals.total),
    statTile("Active", totals.active),
    statTile("Archived", totals.archived),
    statTile("Projects", totals.projects),
    statTile("Skills", state.skillsStatus?.total ?? 0),
    statTile("Missing rollout", totals.missing_rollouts),
    statTile("Empty preview", totals.hidden_empty_preview),
  );
}

function statTile(label, value) {
  const node = document.createElement("div");
  node.className = "stat-tile";
  const strong = document.createElement("strong");
  strong.textContent = String(value ?? 0);
  const span = document.createElement("span");
  span.textContent = label;
  node.append(strong, span);
  return node;
}

function renderProviders() {
  renderSourceProviders();
  renderProviderGrid();
}

function renderSourceProviders() {
  const select = $("sourceProvider");
  const current = select.value;
  select.replaceChildren();
  const providers = currentSourceProviders();

  if (!providers.length) {
    select.append(new Option(usingPackageSource() ? "No package providers" : "No providers", ""));
  }

  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = provider.model_provider;
    option.textContent = `${providerDisplayName(provider.model_provider)} / ${visibleProviderCount(provider)}${providerConfigurationSuffix(provider.model_provider)}`;
    option.title = providerTooltip(provider, currentSourceStats());
    select.append(option);
  }

  const preferred = preferredSourceProvider(current);
  if (preferred) select.value = preferred;
}

function renderProviderGrid() {
  const grid = $("providerGrid");
  grid.replaceChildren();

  for (const provider of state.sessionProviders) {
    const card = document.createElement("div");
    card.className = "provider-tile";
    card.title = providerTooltip(provider, state.stats);
    const providerStats = state.stats?.by_provider?.[provider.model_provider];
    const configured = targetProviderIsConfigured({ value: provider.model_provider });
    card.innerHTML = `
      <strong>${escapeHtml(providerDisplayName(provider.model_provider))}</strong>
      <span>id: ${escapeHtml(provider.model_provider)}</span>
      <span>${provider.active} active / ${provider.archived} archived</span>
      <span>${providerStats?.projects ?? 0} projects</span>
      <span>${configured === true ? "configured provider" : configured === false ? "not configured" : "session-db provider"}</span>
    `;
    grid.append(card);
  }
}

function providerMetadata(value) {
  return state.targetProviders.find((provider) => provider.value === value) || null;
}

function providerDisplayName(value, fallbackProvider = null) {
  const provider = fallbackProvider || providerMetadata(value);
  const configuredName = String(provider?.provider_name || "").trim();
  return configuredName && configuredName !== value
    ? `${configuredName} (id: ${value})`
    : value;
}

function visibleProviderCount(provider) {
  const count = visibleThreadCount(provider);
  return `${count} ${includeArchivedChecked() ? "total" : "active"}`;
}

function targetProviderIsConfigured(provider) {
  const current = state.status?.current_config || {};
  if (!current.exists) return null;
  if (!Array.isArray(current.configured_provider_ids)) return null;
  return (current.configured_provider_ids || []).includes(provider.value);
}

function providerConfigurationSuffix(value) {
  return targetProviderIsConfigured({ value }) === false ? " / not configured" : "";
}

function providerTooltip(provider, stats = state.stats) {
  const details = [];
  if (provider.model_provider) {
    details.push(`Provider: ${providerDisplayName(provider.model_provider)}`);
    details.push(`Provider id: ${provider.model_provider}`);
  }
  details.push(`Active: ${provider.active ?? 0}`);
  details.push(`Archived: ${provider.archived ?? 0}`);
  details.push(`Total: ${provider.total ?? 0}`);
  const providerStats = stats?.by_provider?.[provider.model_provider];
  if (providerStats) details.push(`Projects: ${providerStats.projects ?? 0}`);
  const configured = providerMetadata(provider.model_provider);
  if (configured) details.push("Present in provider catalog");
  return details.join(" | ");
}

function renderTargetProviders() {
  const select = $("targetProviderSelect");
  const current = select.value;
  select.replaceChildren();

  for (const provider of targetProviderOptions()) {
    const option = new Option(targetProviderLabel(provider), provider.value);
    option.title = targetProviderTooltip(provider);
    select.append(option);
  }
  select.append(new Option("Custom provider id...", CUSTOM_TARGET));
  const preferred = preferredTargetProvider(current, usingPackageSource() ? "" : $("sourceProvider").value);
  if (preferred) {
    select.value = preferred;
  }
  updateCustomTargetVisibility();
}

function targetProviderOptions() {
  const providers = state.targetProviders.map((provider) => ({ ...provider }));
  if (!usingPackageSource()) return providers;

  for (const source of currentSourceProviders()) {
    const value = source.model_provider;
    if (!value || providers.some((provider) => provider.value === value)) continue;
    providers.push({
      value,
      label: value,
      sources: ["package_source"],
      session_total: 0,
      current: false,
    });
  }
  return providers;
}

function targetProviderLabel(provider) {
  const bits = [providerDisplayName(provider.value, provider)];
  if (provider.current) bits.push("live");
  if (provider.session_total !== undefined || provider.session_active !== undefined) {
    const count = includeArchivedChecked()
      ? Number(provider.session_total ?? 0)
      : Number(provider.session_active ?? provider.session_total ?? 0);
    bits.push(`${count} ${includeArchivedChecked() ? "total" : "active"}`);
  }
  if (targetProviderIsConfigured(provider) === false) bits.push("not configured");
  return bits.join(" / ");
}

function targetProviderTooltip(provider) {
  const details = [];
  if (provider.value) details.push(`Provider: ${providerDisplayName(provider.value, provider)}`);
  if (provider.provider_name) details.push(`Provider id: ${provider.value}`);
  if (provider.model) details.push(`Model: ${provider.model}`);
  if (provider.base_url) details.push(`Base URL: ${provider.base_url}`);
  if (provider.wire_api) details.push(`Wire API: ${provider.wire_api}`);
  if (provider.current) details.push("Current live config");
  if (targetProviderIsConfigured(provider) === false) {
    details.push("Not defined in config.toml; copy is disabled until it is configured");
  }
  if (provider.sources?.includes("session_db")) details.push("Present in session DB");
  if (provider.sources?.includes("package_source")) details.push("Present in package source");
  return details.join(" | ");
}

function renderLiveTargetPanel() {
  const panel = $("liveTargetPanel");
  panel.replaceChildren();
  const current = state.status?.current_config || {};
  const selected = targetProviderValue() === current.model_provider;

  const head = document.createElement("div");
  head.className = "live-target-head";
  const title = document.createElement("strong");
  title.textContent = "Current Live Target";
  const badge = document.createElement("span");
  badge.className = current.model_provider && selected ? "badge ok" : "badge";
  badge.textContent = current.model_provider
    ? selected ? "Selected" : "Detected"
    : current.exists ? "No provider" : "Missing config";
  head.append(title, badge);

  const details = document.createElement("div");
  details.className = "live-target-details";
  details.append(
    liveTargetField("Provider id", current.model_provider),
    liveTargetField("Provider name", current.provider_name || current.model_provider),
    liveTargetField("Model", current.model),
    liveTargetField("Base URL", current.base_url),
    liveTargetField("Wire API", current.wire_api),
    liveTargetField("Config", current.config_path),
  );

  panel.append(head, details);
  if (current.error) {
    panel.append(message("error", current.error));
  }
}

function renderOverwriteSessionsControl() {
  const checkbox = $("overwriteSessions");
  const label = $("overwriteSessionsLabel");
  if (!checkbox || !label) return;
  checkbox.disabled = false;
  label.title = "Replace destination sessions matched by session ID or project and conversation identity.";
}

function liveTargetField(label, value) {
  const node = document.createElement("div");
  const span = document.createElement("span");
  span.textContent = label;
  const code = document.createElement("code");
  code.textContent = value || "(not set)";
  code.title = value || "";
  node.append(span, code);
  return node;
}

function renderProjectFilter() {
  const select = $("projectFilter");
  const current = select.value;
  select.replaceChildren(new Option("All projects", ""));
  for (const project of currentSourceStats()?.by_project || []) {
    const label = `${project.label} (${visibleThreadCount(project)})`;
    const option = new Option(label, project.cwd);
    option.title = project.normalized_cwd || project.cwd;
    select.append(option);
  }
  if (Array.from(select.options).some((option) => option.value === current)) {
    select.value = current;
  }
}

function renderSourceFilter() {
  renderSelectFromThreads("sourceFilter", "source", "All sources");
}

function renderSelectFromThreads(id, key, label) {
  const select = $(id);
  const current = select.value;
  const values = Array.from(new Set(state.sourceThreads.map((thread) => thread[key]).filter(Boolean))).sort();
  select.replaceChildren(new Option(label, ""));
  for (const value of values) {
    select.append(new Option(value, value));
  }
  if (values.includes(current)) select.value = current;
}

function renderSourceThreads() {
  const body = $("sourceThreadsBody");
  body.replaceChildren();
  for (const thread of state.sourceThreads) {
    const row = threadRow(thread, { side: "source" });
    const selectCell = document.createElement("td");
    selectCell.className = "select-cell";
    selectCell.dataset.label = "Select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(thread.id);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(thread.id);
      else state.selected.delete(thread.id);
      renderSelectionState();
      renderPackagePanel();
      invalidatePreview();
    });
    selectCell.append(checkbox);
    row.prepend(selectCell);
    body.append(row);
  }
  renderSelectionState();
  renderListCount("sourceListCount", state.sourceThreads.length);
}

function renderTargetThreads() {
  const body = $("targetThreadsBody");
  body.replaceChildren();
  for (const thread of state.targetThreads) {
    const row = threadRow(thread, { target: true, side: "target" });
    body.append(row);
  }
  renderListCount("targetListCount", state.targetThreads.length);
}

function syncSourceCheckboxes() {
  for (const checkbox of document.querySelectorAll("#sourceThreadsBody input[type=checkbox]")) {
    const threadId = checkbox.closest("tr")?.dataset.threadId;
    if (threadId) checkbox.checked = state.selected.has(threadId);
  }
}

function syncActiveThreadRows() {
  const activeId = state.activeSession?.id || "";
  for (const row of document.querySelectorAll("[data-thread-id]")) {
    row.classList.toggle("active-row", row.dataset.threadId === activeId);
  }
}

function renderSourceSkills() {
  const body = $("skillsSourceBody");
  if (!body) return;
  body.replaceChildren();
  for (const skill of state.sourceSkills) {
    const row = skillRow(skill, { source: true });
    const selectCell = document.createElement("td");
    selectCell.className = "select-cell";
    selectCell.dataset.label = "Select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedSkills.has(skill.id);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedSkills.add(skill.id);
      else state.selectedSkills.delete(skill.id);
      renderSkillSelectionState();
      invalidateSkillPreview();
    });
    selectCell.append(checkbox);
    row.prepend(selectCell);
    body.append(row);
  }
  renderSkillSelectionState();
  renderListCount("skillsSourceCount", state.sourceSkills.length);
}

function renderTargetSkills() {
  const body = $("skillsTargetBody");
  if (!body) return;
  body.replaceChildren();
  for (const skill of state.targetSkills) {
    body.append(skillRow(skill, { target: true }));
  }
  renderListCount("skillsTargetCount", state.targetSkills.length);
}

function skillRow(skill, options = {}) {
  const row = document.createElement("tr");
  const skillCell = document.createElement("td");
  skillCell.className = "session-cell";
  skillCell.dataset.label = "Skill";
  const title = document.createElement("strong");
  title.className = "session-title";
  title.textContent = skill.name || skill.id;
  title.title = skill.id;
  const path = document.createElement("p");
  path.textContent = skill.id;
  path.title = skill.path || skill.id;
  skillCell.append(title, path);

  row.append(
    skillCell,
    textCell(skill.description || "", { className: "origin-cell", title: skill.description || "", label: "Description" }),
    textCell(String(skill.file_count || 0), { className: "count-cell", title: `${skill.file_count || 0} files`, label: "Files" }),
    skillStateCell(skill, options),
  );
  return row;
}

function skillStateCell(skill, options = {}) {
  const cell = document.createElement("td");
  cell.className = "state-cell";
  cell.dataset.label = "State";
  const flags = [];
  if (skill.installed) flags.push("Installed");
  if (usingSkillPackageSource() && options.source && !skill.installed) flags.push("New");
  if (!flags.length) flags.push(options.target ? "Local" : "Ready");
  for (const flag of flags) {
    const badge = document.createElement("span");
    badge.className = flag === "Installed" || flag === "Ready" || flag === "Local" ? "badge ok" : "badge";
    badge.textContent = flag;
    cell.append(badge);
  }
  return cell;
}

function threadRow(thread, options = {}) {
  const row = document.createElement("tr");
  row.dataset.threadId = thread.id;
  if (!thread.rollout_exists) row.classList.add("warn-row");
  if (options.target && state.lastCopiedTargetIds.has(thread.id)) row.classList.add("new-row");
  if (state.activeSession?.id === thread.id) row.classList.add("active-row");
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", `Render ${options.side || "session"} session ${thread.display_title || thread.title || thread.id}`);
  row.addEventListener("click", () => selectThreadForRender(thread, options.side || (options.target ? "target" : "source")));
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectThreadForRender(thread, options.side || (options.target ? "target" : "source"));
    }
  });

  const sessionCell = document.createElement("td");
  sessionCell.className = "session-cell";
  sessionCell.dataset.label = "Session";
  const title = document.createElement("strong");
  title.className = "session-title";
  title.textContent = thread.display_title || thread.title || "(untitled)";
  title.title = thread.display_title || thread.title || thread.id;
  if (thread.thread_name && thread.thread_name !== thread.title) {
    const renamed = document.createElement("span");
    renamed.className = "renamed-note";
    renamed.textContent = "renamed";
    sessionCell.append(renamed);
  }
  if (options.target && state.lastCopiedTargetIds.has(thread.id)) {
    const copied = document.createElement("span");
    copied.className = "new-note";
    copied.textContent = "new";
    sessionCell.append(copied);
  }
  const preview = document.createElement("p");
  preview.textContent = thread.preview || thread.id;
  preview.title = thread.preview || thread.id;
  sessionCell.append(title, preview);

  row.append(
    sessionCell,
    textCell(thread.source, { className: "origin-cell", title: thread.source, label: "Origin" }),
    textCell(shortPath(thread.cwd), { className: "folder-cell", title: thread.cwd, label: "Folder" }),
    stateCell(thread),
    textCell(String(thread.child_count || 0), {
      className: "count-cell",
      title: `${thread.child_count || 0} children`,
      label: "Kids",
    }),
  );
  return row;
}

async function selectThreadForRender(thread, side) {
  const sameThread = state.activeSession?.id === thread.id;
  const origin = side === "source" ? currentSourceOrigin() : "local";
  state.activeSession = { id: thread.id, side, thread, origin };
  if (!sameThread) state.activeThreadDetail = null;
  state.activeThreadLoading = false;
  state.activeThreadLoadingMore = false;
  syncActiveThreadRows();
  renderSessionDetail();
  if (state.sessionRenderCollapsed || (sameThread && state.activeThreadDetail)) return;
  await loadThreadDetailPage({ append: false });
}

async function loadThreadDetailPage({ append = false } = {}) {
  if (!state.activeSession || state.sessionRenderCollapsed) return;
  const activeId = state.activeSession.id;
  const offset = append ? state.activeThreadDetail?.items?.length || 0 : 0;
  if (append) state.activeThreadLoadingMore = true;
  else state.activeThreadLoading = true;
  renderSessionDetail();
  try {
    const endpoint = state.activeSession.origin === "package" ? "/api/package-thread-detail" : "/api/thread-detail";
    const detail = await api(
      `${endpoint}?id=${encodeURIComponent(activeId)}&offset=${offset}&limit=${SESSION_RENDER_PAGE_SIZE}`
    );
    if (!state.activeSession || state.activeSession.id !== activeId) return;
    if (append && state.activeThreadDetail) {
      state.activeThreadDetail = {
        ...detail,
        items: [...(state.activeThreadDetail.items || []), ...(detail.items || [])],
        item_offset: 0,
      };
    } else {
      state.activeThreadDetail = detail;
    }
  } catch (error) {
    if (!state.activeSession || state.activeSession.id !== activeId) return;
    state.activeThreadDetail = {
      ok: false,
      errors: [error.message],
      items: [],
      thread: state.activeSession.thread,
      item_total: 0,
      has_more: false,
    };
  } finally {
    if (state.activeSession && state.activeSession.id === activeId) {
      state.activeThreadLoading = false;
      state.activeThreadLoadingMore = false;
      renderSessionDetail();
    }
  }
}

function reconcileActiveSession() {
  if (!state.activeSession) {
    renderSessionDetail();
    return;
  }
  const sourceHasActive = state.sourceThreads.some((thread) => thread.id === state.activeSession.id);
  const targetHasActive = state.targetThreads.some((thread) => thread.id === state.activeSession.id);
  const activeThread =
    state.sourceThreads.find((thread) => thread.id === state.activeSession.id) ||
    state.targetThreads.find((thread) => thread.id === state.activeSession.id);
  if (!sourceHasActive && !targetHasActive) {
    state.activeSession = null;
    state.activeThreadDetail = null;
    state.activeThreadLoading = false;
    state.activeThreadLoadingMore = false;
  } else if (activeThread) {
    state.activeSession.thread = activeThread;
  }
  renderSessionDetail();
}

function renderSessionDetail() {
  renderSessionChrome();
  const title = $("sessionRenderTitle");
  const side = $("sessionRenderSide");
  const meta = $("sessionRenderMeta");
  const messages = $("sessionRenderMessages");
  const footer = $("sessionRenderFooter");
  if (!title || !side || !meta || !messages || !footer) return;

  meta.replaceChildren();
  messages.replaceChildren();
  footer.replaceChildren();

  if (state.sessionRenderCollapsed) return;

  if (!state.activeSession) {
    title.textContent = "Session";
    side.textContent = "Select a row";
    messages.append(emptySessionNotice("Click any Source or Target session row to render its rollout transcript here."));
    return;
  }

  side.textContent = state.activeSession.side === "target" ? "Target" : "Source";
  if (state.activeThreadLoading) {
    title.textContent = "Loading session...";
    messages.append(emptySessionNotice("Reading rollout JSONL from disk."));
    return;
  }

  const detail = state.activeThreadDetail;
  const thread = detail?.thread || {};
  title.textContent =
    thread.display_title || thread.title || state.activeSession.thread?.display_title || state.activeSession.id;
  const shown = detail?.items?.length || 0;
  const total = detail?.item_total ?? shown;
  meta.append(
    sessionMetaField("Provider", thread.model_provider || detail?.meta?.model_provider || state.activeSession.thread?.model_provider),
    sessionMetaField("Model", thread.model || state.activeSession.thread?.model),
    sessionMetaField("Shown", total ? `${shown}/${total}` : "(none)"),
    sessionMetaField("Rollout", detail?.rollout?.exists ? `${detail.rollout.line_count || 0} lines` : "Missing"),
  );

  for (const error of detail?.errors || []) {
    messages.append(message("warn", error));
  }
  if (!detail?.items?.length) {
    messages.append(emptySessionNotice("No renderable messages were found in this rollout."));
    return;
  }

  for (const item of detail.items) {
    messages.append(sessionRenderItem(item));
  }
  renderSessionFooter(footer, detail);
}

function renderSessionChrome() {
  const rail = $("sessionRail");
  const toggle = $("sessionRenderToggleButton");
  const expand = $("sessionRenderExpandButton");
  if (rail) rail.classList.toggle("session-collapsed", state.sessionRenderCollapsed);
  if (toggle) {
    toggle.setAttribute("aria-label", "Hide session preview");
    toggle.title = "Hide session preview";
  }
  if (expand) {
    const selectedTitle =
      state.activeSession?.thread?.display_title || state.activeSession?.thread?.title || state.activeSession?.id || "Session";
    expand.title = `Show session preview${state.activeSession ? `: ${selectedTitle}` : ""}`;
  }
}

function renderSessionFooter(footer, detail) {
  if (!detail?.has_more) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "load-more-button";
  button.disabled = state.activeThreadLoadingMore;
  button.innerHTML = `
    <svg class="button-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 5v14"></path>
      <path d="M5 12h14"></path>
    </svg>
    <span>${state.activeThreadLoadingMore ? "Loading..." : "Load more"}</span>
  `;
  button.addEventListener("click", () => loadThreadDetailPage({ append: true }));
  const hint = document.createElement("span");
  hint.textContent = `${detail.items?.length || 0}/${detail.item_total || 0}`;
  footer.append(button, hint);
}

function setSessionRenderCollapsed(collapsed) {
  state.sessionRenderCollapsed = collapsed;
  renderSessionDetail();
  if (!collapsed && state.activeSession && !state.activeThreadDetail && !state.activeThreadLoading) {
    loadThreadDetailPage({ append: false }).catch(() => {});
  }
}

function sessionMetaField(label, value) {
  const node = document.createElement("div");
  const span = document.createElement("span");
  span.textContent = label;
  const code = document.createElement("code");
  code.textContent = value || "(none)";
  code.title = value || "";
  node.append(span, code);
  return node;
}

function emptySessionNotice(text) {
  const node = document.createElement("div");
  node.className = "session-empty";
  node.textContent = text;
  return node;
}

function sessionRenderItem(item) {
  const node = document.createElement("article");
  node.className = `render-item ${item.kind || "event"} ${item.role || "event"}`;

  const head = document.createElement("div");
  head.className = "render-item-head";
  const role = document.createElement("strong");
  role.textContent = renderItemLabel(item);
  const meta = document.createElement("span");
  meta.textContent = renderItemMeta(item);
  head.append(role, meta);

  const body = document.createElement("div");
  body.className = "render-item-body";
  if (item.kind === "tool_call") {
    const name = document.createElement("p");
    name.textContent = item.text || item.title || "Tool call";
    body.append(name);
    if (item.data?.arguments !== undefined && item.data?.arguments !== null) {
      body.append(codeBlock(formatStructuredValue(item.data.arguments)));
    }
  } else if (item.kind === "tool_result" || item.kind === "warning") {
    body.append(codeBlock(item.text || ""));
  } else {
    appendFormattedText(body, item.text || "");
  }

  node.append(head, body);
  return node;
}

function renderItemLabel(item) {
  if (item.kind === "tool_call") return item.title || "Tool call";
  if (item.kind === "tool_result") return "Tool result";
  if (item.kind === "warning") return "Warning";
  if (item.role) return item.role;
  return item.title || "event";
}

function renderItemMeta(item) {
  const bits = [];
  if (item.kind) bits.push(item.kind);
  if (item.line) bits.push(`line ${item.line}`);
  if (item.timestamp) bits.push(formatTime(item.timestamp));
  return bits.join(" / ");
}

function appendFormattedText(container, text) {
  if (!text) {
    container.append(document.createTextNode(""));
    return;
  }
  const fencePattern = /```([\w-]*)\n?([\s\S]*?)```/g;
  let lastIndex = 0;
  let match = fencePattern.exec(text);
  while (match) {
    appendTextBlock(container, text.slice(lastIndex, match.index));
    container.append(codeBlock(match[2], match[1]));
    lastIndex = fencePattern.lastIndex;
    match = fencePattern.exec(text);
  }
  appendTextBlock(container, text.slice(lastIndex));
}

function appendTextBlock(container, text) {
  if (!text) return;
  const block = document.createElement("p");
  block.textContent = text.trim() ? text.trim() : text;
  container.append(block);
}

function codeBlock(text, language = "") {
  const pre = document.createElement("pre");
  if (language) pre.dataset.language = language;
  const code = document.createElement("code");
  code.textContent = text;
  pre.append(code);
  return pre;
}

function formatStructuredValue(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderListCount(id, count) {
  const node = $(id);
  if (node) node.textContent = `${count} shown`;
}

function textCell(value, options = {}) {
  const cell = document.createElement("td");
  if (options.className) cell.className = options.className;
  if (options.label) cell.dataset.label = options.label;
  cell.textContent = value || "";
  if (options.title) cell.title = options.title;
  return cell;
}

function stateCell(thread) {
  const cell = document.createElement("td");
  cell.className = "state-cell";
  cell.dataset.label = "State";
  const flags = [];
  if (thread.archived) flags.push("Archived");
  if (!thread.rollout_exists) flags.push("Missing rollout");
  if (thread.hidden_empty_preview) flags.push("Empty preview");
  if (thread.parent_thread_id) flags.push("Child");
  if (!flags.length) flags.push("Ready");
  for (const flag of flags) {
    const badge = document.createElement("span");
    badge.className = flag === "Ready" ? "badge ok" : "badge";
    badge.textContent = flag;
    cell.append(badge);
  }
  return cell;
}

function renderSelectionState() {
  $("selectedCount").textContent = `${state.selected.size} selected`;
  $("selectAll").checked =
    state.sourceThreads.length > 0 && state.sourceThreads.every((thread) => state.selected.has(thread.id));
  updateCopyButton();
  updateRebindButton();
  renderPackagePanel();
}

function renderSkillSelectionState() {
  $("skillsSelectedCount").textContent = `${state.selectedSkills.size} selected`;
  $("skillsSelectAll").checked =
    state.sourceSkills.length > 0 && state.sourceSkills.every((skill) => state.selectedSkills.has(skill.id));
  updateSkillsButtons();
  renderSkillsPackagePanel();
}

async function previewCopy() {
  const endpoint = usingPackageSource() ? "/api/preview-package-copy" : "/api/preview-copy";
  const plan = await api(endpoint, {
    method: "POST",
    body: JSON.stringify(previewRequest(0)),
  });
  state.preview = plan;
  state.previewLoadingMore = false;
  renderPreview(plan);
  const loaded = plan.items?.length || 0;
  const total = plan.item_total ?? loaded;
  setCopyResult(
    plan.can_execute
      ? `Preview ready: ${loaded}${total !== loaded ? ` / ${total}` : ""} session(s) can be ${usingPackageSource() ? "imported" : "copied"}.`
      : "Preview is not executable. Fix the messages above.",
    plan.can_execute ? "info" : "error",
  );
}

function previewRequest(offset) {
  return {
    ...copyRequest(),
    preview_offset: offset,
    preview_limit: PREVIEW_RENDER_PAGE_SIZE,
  };
}

function renderPreview(plan) {
  const messages = $("previewMessages");
  const items = $("previewItems");
  messages.replaceChildren();
  items.replaceChildren();
  items.onscroll = null;
  if (!plan) {
    updateCopyButton();
    return;
  }
  for (const error of plan.errors || []) {
    messages.append(message("error", error));
  }
  for (const warning of plan.warnings || []) {
    messages.append(message("warn", warning));
  }
  for (const mapping of plan.workspace_mappings || []) {
    const node = document.createElement("div");
    node.className = "preview-item workspace-preview-item";
    node.innerHTML = `
      <strong>${escapeHtml(mapping.project_label || "Workspace")}</strong>
      <span>${escapeHtml(`${mapping.session_count || 0} session(s)`)}</span>
      <code>${escapeHtml(shortPath(mapping.source_cwd))} -> ${escapeHtml(shortPath(mapping.target_cwd))}</code>
    `;
    items.append(node);
  }
  renderPreviewItems(plan);
  updateCopyButton();
}

function renderPreviewItems(plan) {
  const items = $("previewItems");
  for (const node of items.querySelectorAll(".preview-session-item, .preview-lazy-status")) {
    node.remove();
  }
  const loadedItems = plan?.items || [];
  for (const item of loadedItems) {
    const node = document.createElement("div");
    node.className = "preview-item preview-session-item";
    node.innerHTML = `
      <strong>${escapeHtml(item.display_title || item.title || item.source_id)}</strong>
      <span>${escapeHtml(item.source_provider)} -> ${escapeHtml(item.target_provider)}</span>
      <code>${escapeHtml(item.source_id)} -> ${escapeHtml(item.target_id)}</code>
      ${item.overwritten ? `<span class="badge warn">Overwrite existing</span>` : ""}
      ${item.overwrite_match ? `<span>Matched by ${escapeHtml(item.overwrite_match)}</span>` : ""}
      ${item.cwd_rewritten ? `<code>${escapeHtml(shortPath(item.source_cwd))} -> ${escapeHtml(shortPath(item.target_cwd))}</code>` : ""}
    `;
    items.append(node);
  }
  const total = Number(plan?.item_total ?? loadedItems.length);
  if (plan?.has_more) {
    const status = document.createElement("span");
    status.className = "preview-lazy-status";
    status.textContent = state.previewLoadingMore
      ? `${loadedItems.length} / ${total} - loading next page...`
      : `${loadedItems.length} / ${total} - scroll for more`;
    items.append(status);
    items.onscroll = () => {
      if (items.scrollTop + items.clientHeight >= items.scrollHeight - PREVIEW_SCROLL_THRESHOLD) {
        loadMorePreview();
      }
    };
  } else {
    items.onscroll = null;
  }
}

async function loadMorePreview() {
  const basePlan = state.preview;
  if (!basePlan?.has_more || state.previewLoadingMore) return;

  const endpoint = usingPackageSource() ? "/api/preview-package-copy" : "/api/preview-copy";
  const nextOffset = Number(basePlan.next_preview_offset ?? basePlan.items?.length ?? 0);
  state.previewLoadingMore = true;
  renderPreviewItems(basePlan);
  try {
    const page = await api(endpoint, {
      method: "POST",
      body: JSON.stringify(previewRequest(nextOffset)),
    });
    if (state.preview !== basePlan) return;
    state.preview = {
      ...basePlan,
      ...page,
      items: [...(basePlan.items || []), ...(page.items || [])],
    };
  } catch (error) {
    setCopyResult(`Preview could not load the next page: ${error.message}`, "error");
  } finally {
    state.previewLoadingMore = false;
    if (state.preview === basePlan || state.preview) renderPreviewItems(state.preview);
  }
}

function invalidatePreview({ clearResult = true } = {}) {
  state.preview = null;
  state.previewLoadingMore = false;
  renderPreview(null);
  if (clearResult) setCopyResult("");
}

function setCopyResult(text, kind = "") {
  state.copyResult = text;
  const node = $("copyResult");
  node.textContent = text;
  node.className = `result${kind ? ` ${kind}` : ""}`;
}

function message(kind, text) {
  const node = document.createElement("div");
  node.className = `message ${kind}`;
  node.textContent = text;
  return node;
}

function updateCopyProgress(event = null) {
  const panel = $("copyProgress");
  if (!panel) return;
  if (event?.type === "complete") return;
  if (!event) {
    panel.hidden = true;
    panel.classList.remove("active", "complete", "error");
    return;
  }

  const current = Number(event.current || 0);
  const total = Number(event.total || 0);
  const percent = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
  const phaseLabels = {
    checking: "Checking",
    planning: "Planning",
    ready: "Ready",
    copying: "Copying",
    rebinding: "Rebinding",
    committing: "Committing",
    blocked: "Blocked",
    error: "Stopped",
    done: "Complete",
  };
  panel.hidden = false;
  panel.classList.toggle("active", !["done", "error", "blocked"].includes(event.phase));
  panel.classList.toggle("complete", event.phase === "done");
  panel.classList.toggle("error", event.phase === "error" || event.phase === "blocked");
  $("copyProgressPhase").textContent = phaseLabels[event.phase] || event.phase || "Working";
  $("copyProgressCount").textContent = total ? `${current} / ${total}` : "Working";
  $("copyProgressBar").style.width = `${percent}%`;
  $("copyProgressTrack").setAttribute("aria-valuenow", String(percent));
  $("copyProgressPercent").textContent = `${percent}%`;
  $("copyProgressItem").textContent = event.item_title || event.message || "Preparing session transfer";
}

async function executeCopy() {
  const blocked = (state.status && state.status.blocking_processes || []).length > 0;
  const reasons = copyDisabledReasons(blocked);
  if (reasons.length) return;
  const count = state.selected.size;
  const target = targetProviderValue();
  const action = usingPackageSource() ? "Import" : "Copy";
  const overwriteNotice = $("overwriteSessions")?.checked
    ? " Matching destination conversations will be overwritten."
    : "";
  const descendantsNotice = $("includeDescendants")?.checked ? " Child sessions included." : "";
  const confirmed = window.confirm(`${action} ${count} selected session(s) to ${target}?${descendantsNotice}${overwriteNotice}`);
  if (!confirmed) return;

  state.copyInProgress = true;
  updateCopyButton();
  updateCopyProgress({ phase: "checking", current: 0, total: count, message: `${action}ing sessions` });
  setCopyResult(`${action}ing ${count} selected session(s)...`, "info");
  const endpoint = usingPackageSource() ? "/api/copy-package-progress" : "/api/copy-progress";
  try {
    const result = await streamApi(
      endpoint,
      {
        method: "POST",
        body: JSON.stringify(copyRequest()),
      },
      updateCopyProgress,
    );
    state.preview = null;
    renderPreview(null);
    const copiedCount = Number(result.item_total ?? result.items?.length ?? 0);
    const overwrittenCount = Number(
      result.overwritten_count ?? (result.items || []).filter((item) => item.overwritten).length,
    );
    const resultText = result.ok
      ? `${usingPackageSource() ? "Imported" : "Copied"} ${copiedCount} session(s).${result.overwrite ? ` Overwrote ${overwrittenCount} matching session(s).` : ""} Session index entries: ${result.session_index_entries || 0}. Manifest: ${result.manifest_path}`
      : `Not ${usingPackageSource() ? "imported" : "copied"}. ${(result.errors || []).join("; ")}`;
    await loadStatus();
    renderAllShell();
    state.lastCopiedTargetIds = new Set((result.items || []).map((item) => item.target_id));
    await loadThreadLists({ preserveResult: true });
    setCopyResult(resultText, result.ok ? "success" : "error");
  } catch (error) {
    updateCopyProgress({ phase: "error", current: 0, total: count, message: error.message });
    setCopyResult(error.message, "error");
  } finally {
    state.copyInProgress = false;
    updateCopyButton();
  }
}

async function executeRebind() {
  const reasons = rebindDisabledReasons();
  if (reasons.length) return;
  const count = state.selected.size;
  const source = $("sourceProvider").value;
  const target = targetProviderValue();
  const descendantsNotice = $("includeDescendants")?.checked ? " Child sessions included." : "";
  const archivedNotice = $("includeArchived")?.checked ? " Archived sessions included." : "";
  const confirmed = window.confirm(
    `Rebind ${count} selected session(s) from ${source} to ${target}?${descendantsNotice}${archivedNotice}\n\nSession IDs, titles, names, and transcript files will be preserved.`
  );
  if (!confirmed) return;

  state.copyInProgress = true;
  updateCopyButton();
  updateRebindButton();
  updateCopyProgress({
    phase: "checking",
    current: 0,
    total: count,
    message: "Rebinding sessions",
  });
  setCopyResult(`Rebinding ${count} selected session(s)...`, "info");
  try {
    const result = await streamApi(
      "/api/rebind-progress",
      {
        method: "POST",
        body: JSON.stringify(rebindRequest()),
      },
      updateCopyProgress,
    );
    const reboundCount = Number(result.rebound_count ?? result.item_total ?? result.items?.length ?? 0);
    const resultText = result.ok
      ? `Rebound ${reboundCount} session(s) to ${result.target_provider || target}. IDs and transcript files were preserved. Backup: ${result.backup_path}`
      : `Not rebound. ${(result.errors || []).join("; ")}`;
    await loadStatus();
    state.selected.clear();
    state.lastCopiedTargetIds.clear();
    renderAllShell();
    if (result.ok && result.target_provider) {
      const sourceSelect = $("sourceProvider");
      if (Array.from(sourceSelect.options).some((option) => option.value === result.target_provider)) {
        sourceSelect.value = result.target_provider;
      }
    }
    await loadThreadLists({ preserveResult: true });
    setCopyResult(resultText, result.ok ? "success" : "error");
  } catch (error) {
    updateCopyProgress({ phase: "error", current: 0, total: count, message: error.message });
    setCopyResult(error.message, "error");
  } finally {
    state.copyInProgress = false;
    updateCopyButton();
    updateRebindButton();
  }
}

async function killBlockingProcesses() {
  const blocking = state.status?.blocking_processes || [];
  if (!blocking.length) return;
  const processLines = blocking
    .map((process) => `${process.name || "process"} PID ${process.pid}`)
    .join("\n");
  const confirmed = window.confirm(
    `Terminate these blocking process(es)?\n\n${processLines}\n\nIf this page is running inside Codex, Codex will close. Use an external browser for the final Copy step.`
  );
  if (!confirmed) return;

  const button = $("killBlockingButton");
  button.disabled = true;
  button.textContent = "Killing...";
  const result = await api("/api/kill-blocking-processes", {
    method: "POST",
    headers: {
      "X-Codex-Session-Transfer-Action": "kill-blocking-processes",
    },
  });
  await loadStatus();
  renderAllShell();
  await loadThreadLists();
  const remaining = result.remaining_blocking_processes?.length || 0;
  setCopyResult(
    remaining
      ? `Killed ${result.killed_count || 0}; ${remaining} blocker(s) remain. Refresh after they exit.`
      : `Killed ${result.killed_count || 0} blocker(s). Copy is ready when sessions and a target are selected.`,
    remaining ? "error" : "info",
  );
}

async function repairSessionIndexNames() {
  const confirmed = window.confirm(
    "Repair copied session names from successful manifests? This writes session_index.jsonl only."
  );
  if (!confirmed) return;

  const button = $("repairNamesButton");
  button.disabled = true;
  button.textContent = "Repairing...";
  const result = await api("/api/repair-session-index", {
    method: "POST",
    headers: {
      "X-Codex-Session-Transfer-Action": "repair-session-index",
    },
  });
  await loadStatus();
  renderAllShell();
  await loadThreadLists({ preserveResult: true });
  button.textContent = "Repair names";
  setCopyResult(
    result.ok
      ? `Repaired ${result.repaired_count || 0} copied session name(s) from ${result.scanned_manifests || 0} manifest(s).`
      : `Repair failed. ${(result.errors || []).join("; ")}`,
    result.ok ? "success" : "error",
  );
}

async function exportPackage() {
  if (usingPackageSource()) return;
  const count = state.selected.size;
  if (!count) return;
  const confirmed = window.confirm(`Export ${count} selected session(s) into a transfer package?`);
  if (!confirmed) return;

  const button = $("exportPackageButton");
  button.disabled = true;
  setCopyResult("Exporting package...", "info");
  const result = await api("/api/export-package", {
    method: "POST",
    body: JSON.stringify({
      source_provider: $("sourceProvider").value,
      thread_ids: Array.from(state.selected),
      include_descendants: $("includeDescendants").checked,
      include_archived: $("includeArchived").checked,
      output_path: $("exportDirInput")?.value.trim() || inferredExportDir(),
    }),
  });
  let openWarning = "";
  if (result.ok && result.package_path) {
    $("packagePathInput").value = result.package_path;
    try {
      await openPath(result.package_path);
    } catch (error) {
      openWarning = ` Folder was not opened: ${error.message}`;
    }
  }
  renderPackagePanel();
  setCopyResult(
    result.ok
      ? `Exported ${result.thread_count || 0} session(s) from ${result.project_count || 0} project(s). Package: ${result.package_path}${openWarning}`
      : `Export failed. ${(result.errors || []).join("; ")}`,
    result.ok && !openWarning ? "success" : "error",
  );
}

function chooseSessionPackageFile() {
  const input = $("packageFileInput");
  input.value = "";
  input.click();
}

async function loadPackageFromPath() {
  const path = $("packagePathInput").value.trim();
  if (!path) {
    $("packagePathInput").focus();
    setCopyResult("Choose a session package zip, or paste a package path and press Enter.", "error");
    return;
  }
  setCopyResult("Loading package...", "info");
  const result = await api("/api/load-package", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  if (!result.loaded) {
    setCopyResult(`Package not loaded. ${(result.errors || []).join("; ")}`, "error");
    return;
  }
  state.packageSource = result;
  state.selected.clear();
  state.lastCopiedTargetIds.clear();
  state.activeSession = null;
  state.activeThreadDetail = null;
  resetWorkspaceMappingState();
  ensureProviderSelection();
  renderAllShell();
  await loadThreadLists();
  setCopyResult(`Loaded package source: ${result.package_path}`, "success");
}

async function loadPackageFromFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    setCopyResult("Choose a .zip session package file.", "error");
    return;
  }
  $("packagePathInput").value = file.name;
  setCopyResult(`Loading selected package: ${file.name}`, "info");
  const result = await uploadPackageFile("/api/upload-package", file);
  if (!result.loaded) {
    setCopyResult(`Package not loaded. ${(result.errors || []).join("; ")}`, "error");
    return;
  }
  $("packagePathInput").value = result.package_path || file.name;
  state.packageSource = result;
  state.selected.clear();
  state.lastCopiedTargetIds.clear();
  state.activeSession = null;
  state.activeThreadDetail = null;
  resetWorkspaceMappingState();
  ensureProviderSelection();
  renderAllShell();
  await loadThreadLists();
  setCopyResult(`Loaded package source: ${result.package_path}`, "success");
}

async function unloadPackage() {
  const result = await api("/api/unload-package", { method: "POST", body: "{}" });
  state.packageSource = result;
  state.selected.clear();
  resetWorkspaceMappingState();
  state.activeSession = null;
  state.activeThreadDetail = null;
  ensureProviderSelection();
  renderAllShell();
  await loadThreadLists();
  setCopyResult("Package source unloaded.", "info");
}

function skillImportRequest() {
  return {
    skill_ids: Array.from(state.selectedSkills),
    overwrite: $("overwriteSkills").checked,
  };
}

async function exportSkillsPackage() {
  if (usingSkillPackageSource()) return;
  const count = state.selectedSkills.size;
  if (!count) return;
  const confirmed = window.confirm(`Export ${count} selected skill(s) into a transfer package?`);
  if (!confirmed) return;
  setSkillsResult("Exporting skills package...", "info");
  const result = await api("/api/export-skills-package", {
    method: "POST",
    body: JSON.stringify({ skill_ids: Array.from(state.selectedSkills) }),
  });
  if (result.ok && result.package_path) {
    $("skillsPackagePathInput").value = result.package_path;
  }
  renderSkillsPackagePanel();
  setSkillsResult(
    result.ok
      ? `Exported ${result.skill_count || 0} skill(s). Package: ${result.package_path}`
      : `Export failed. ${(result.errors || []).join("; ")}`,
    result.ok ? "success" : "error",
  );
}

function chooseSkillsPackageFile() {
  const input = $("skillsPackageFileInput");
  input.value = "";
  input.click();
}

async function loadSkillsPackageFromPath() {
  const path = $("skillsPackagePathInput").value.trim();
  if (!path) {
    $("skillsPackagePathInput").focus();
    setSkillsResult("Choose a skills package zip, or paste a package path and press Enter.", "error");
    return;
  }
  setSkillsResult("Loading skills package...", "info");
  const result = await api("/api/load-skills-package", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  if (!result.loaded) {
    setSkillsResult(`Skills package not loaded. ${(result.errors || []).join("; ")}`, "error");
    return;
  }
  state.skillPackageSource = result;
  state.selectedSkills.clear();
  state.skillPreview = null;
  renderAllShell();
  await loadSkillLists();
  setSkillsResult(`Loaded skills package: ${result.package_path}`, "success");
}

async function loadSkillsPackageFromFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    setSkillsResult("Choose a .zip skills package file.", "error");
    return;
  }
  $("skillsPackagePathInput").value = file.name;
  setSkillsResult(`Loading selected skills package: ${file.name}`, "info");
  const result = await uploadPackageFile("/api/upload-skills-package", file);
  if (!result.loaded) {
    setSkillsResult(`Skills package not loaded. ${(result.errors || []).join("; ")}`, "error");
    return;
  }
  $("skillsPackagePathInput").value = result.package_path || file.name;
  state.skillPackageSource = result;
  state.selectedSkills.clear();
  state.skillPreview = null;
  renderAllShell();
  await loadSkillLists();
  setSkillsResult(`Loaded skills package: ${result.package_path}`, "success");
}

async function unloadSkillsPackage() {
  const result = await api("/api/unload-skills-package", { method: "POST", body: "{}" });
  state.skillPackageSource = result;
  state.selectedSkills.clear();
  state.skillPreview = null;
  renderAllShell();
  await loadSkillLists();
  setSkillsResult("Skills package unloaded.", "info");
}

async function previewSkillsImport() {
  const plan = await api("/api/preview-skill-import", {
    method: "POST",
    body: JSON.stringify(skillImportRequest()),
  });
  state.skillPreview = plan;
  renderSkillsPreview(plan);
  setSkillsResult(
    plan.can_execute
      ? `Preview ready: ${plan.items.length} skill(s) can be imported.`
      : "Skills import preview is not executable.",
    plan.can_execute ? "info" : "error",
  );
}

async function importSkills() {
  if (!state.skillPreview?.can_execute) return;
  const count = state.skillPreview.items?.length || 0;
  const confirmed = window.confirm(`Import ${count} skill(s) into this machine?`);
  if (!confirmed) return;
  setSkillsResult("Importing skills...", "info");
  const result = await api("/api/import-skills", {
    method: "POST",
    body: JSON.stringify(skillImportRequest()),
  });
  state.skillPreview = result;
  renderSkillsPreview(result);
  await loadStatus();
  renderAllShell();
  await loadSkillLists({ preserveResult: true });
  setSkillsResult(
    result.ok
      ? `Imported ${result.imported_count || 0} skill(s).`
      : `Skills not imported. ${(result.errors || []).join("; ")}`,
    result.ok ? "success" : "error",
  );
}

function renderSkillsPreview(plan) {
  const items = $("skillsPreviewItems");
  if (!items) return;
  items.replaceChildren();
  if (!plan) {
    updateSkillsButtons();
    return;
  }
  for (const error of plan.errors || []) {
    items.append(message("error", error));
  }
  for (const warning of plan.warnings || []) {
    items.append(message("warn", warning));
  }
  for (const item of plan.items || []) {
    const node = document.createElement("div");
    node.className = "preview-item";
    node.innerHTML = `
      <strong>${escapeHtml(item.name || item.id)}</strong>
      <span>${escapeHtml(item.action || "import")} / ${escapeHtml(item.installed ? "installed" : "new")}</span>
      <code>${escapeHtml(item.id)}</code>
    `;
    items.append(node);
  }
  updateSkillsButtons();
}

function invalidateSkillPreview({ clearResult = true } = {}) {
  state.skillPreview = null;
  renderSkillsPreview(null);
  if (clearResult) setSkillsResult("");
}

function setSkillsResult(text, kind = "") {
  state.skillsResult = text;
  const node = $("skillsResult");
  if (!node) return;
  node.textContent = text;
  node.className = `result${kind ? ` ${kind}` : ""}`;
}

function updateSkillsButtons() {
  const reasons = skillDisabledReasons();
  const importButton = $("importSkillsButton");
  if (importButton) {
    importButton.disabled = reasons.length > 0;
    importButton.title = reasons.join(" ");
  }
  const reasonNode = $("skillsDisabledReason");
  if (reasonNode) reasonNode.textContent = reasons.length ? reasons.join(" ") : "Ready to import skills.";
}

function skillDisabledReasons() {
  const reasons = [];
  if (!usingSkillPackageSource()) {
    reasons.push("Load a skills package before importing.");
  }
  if (state.selectedSkills.size === 0) {
    reasons.push("Select at least one skill.");
  }
  if (!state.skillPreview) {
    reasons.push("Run Preview import first.");
  } else if (state.skillPreview.errors?.length) {
    reasons.push(`Preview has errors: ${state.skillPreview.errors[0]}`);
  } else if (!state.skillPreview.can_execute) {
    reasons.push("Preview plan is not executable.");
  }
  return reasons;
}

function updateCopyButton() {
  const blocked = (state.status && state.status.blocking_processes || []).length > 0;
  const reasons = copyDisabledReasons(blocked);
  const copyButton = $("copyButton");
  copyButton.disabled = reasons.length > 0;
  copyButton.title = reasons.join(" ");
  $("copyDisabledReason").textContent = reasons.length ? reasons.join(" ") : "Ready to copy. Preview is optional.";
}

function updateRebindButton() {
  const button = $("rebindButton");
  const hint = $("rebindDisabledReason");
  if (!button) return;
  const reasons = rebindDisabledReasons();
  button.disabled = reasons.length > 0;
  button.title = reasons.join(" ");
  if (hint) {
    hint.textContent = reasons.length
      ? reasons.join(" ")
      : "Rebind keeps the original session ID, transcript, title, and session index entry.";
  }
}

function rebindDisabledReasons() {
  const reasons = [];
  const selectedCount = state.selected.size;
  const source = $("sourceProvider")?.value || "";
  const target = targetProviderValue();
  const blockingCount = (state.status && state.status.blocking_processes || []).length;

  if (usingPackageSource()) {
    reasons.push("Rebind is available for local sessions only.");
  }
  if (selectedCount === 0) {
    reasons.push("Select at least one session.");
  }
  if (!source) {
    reasons.push("Choose a source provider.");
  }
  if (!target) {
    reasons.push("Choose a target provider.");
  }
  if (source && target && source === target) {
    reasons.push("Source and target providers must be different.");
  }
  if (target && targetProviderIsConfigured({ value: target }) === false) {
    reasons.push(`Target provider '${target}' is not defined in config.toml.`);
  }
  if (state.copyInProgress) {
    reasons.push("A session operation is already running.");
  }
  if (blockingCount) {
    reasons.push(`Close Codex/Codex++ before rebinding (${blockingCount} blocking process${blockingCount === 1 ? "" : "es"} detected).`);
  }
  return reasons;
}

function copyDisabledReasons(blocked) {
  const reasons = [];
  const selectedCount = state.selected.size;
  const target = targetProviderValue();
  const blockingCount = (state.status && state.status.blocking_processes || []).length;

  if (selectedCount === 0) {
    reasons.push("Select at least one session.");
  }
  if (!target) {
    reasons.push("Choose a target provider.");
  }
  if (target && targetProviderIsConfigured({ value: target }) === false) {
    reasons.push(`Target provider '${target}' is not defined in config.toml.`);
  }
  if (state.copyInProgress) {
    reasons.push("A session operation is already running.");
  }
  if (blocked) {
    reasons.push(`Close Codex/Codex++ before copying (${blockingCount} blocking process${blockingCount === 1 ? "" : "es"} detected).`);
  }
  return reasons;
}

function updateCustomTargetVisibility() {
  const custom = $("targetProviderCustom");
  const isCustom = $("targetProviderSelect").value === CUSTOM_TARGET;
  custom.hidden = !isCustom;
  if (!isCustom) custom.value = "";
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.selected.clear();
  state.lastCopiedTargetIds.clear();
  state.activeSession = null;
  state.activeThreadDetail = null;
  invalidatePreview();
  syncSourceCheckboxes();
  renderSelectionState();
  ensureProviderSelection();
  renderAllShell();
  loadThreadLists().catch((error) => setCopyResult(error.message, "error"));
}

function setPage(page) {
  if (state.page === page) return;
  state.page = page;
  renderAllShell();
  if (page === "skills") {
    loadSkillLists().catch((error) => setSkillsResult(error.message, "error"));
  } else {
    loadThreadLists().catch((error) => setCopyResult(error.message, "error"));
  }
}

function shortPath(value) {
  if (!value) return "";
  const text = String(value);
  const separator = text.includes("\\") ? "\\" : "/";
  const parts = text.split(/[\\/]/);
  if (parts.length <= 3) return value;
  const tail = parts.slice(-2).join(separator);
  if (separator === "/" && text.startsWith("/")) {
    return `/${parts[1]}/.../${tail}`;
  }
  if (separator === "\\" && text.startsWith("\\\\")) {
    const compact = parts.filter(Boolean);
    if (compact.length <= 3) return value;
    return `\\\\${compact[0]}\\...\\${compact.slice(-2).join("\\")}`;
  }
  return `${parts[0]}${separator}...${separator}${tail}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bindEvents() {
  $("refreshButton").addEventListener("click", loadAll);
  $("sessionPageButton").addEventListener("click", () => setPage("sessions"));
  $("skillsPageButton").addEventListener("click", () => setPage("skills"));
  $("localModeButton").addEventListener("click", () => setMode("local"));
  $("packageModeButton").addEventListener("click", () => setMode("package"));
  $("exportPackageButton").addEventListener("click", () => {
    exportPackage().catch((error) => {
      setCopyResult(error.message, "error");
      renderPackagePanel();
    });
  });
  $("loadPackageButton").addEventListener("click", chooseSessionPackageFile);
  $("packageFileInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    loadPackageFromFile(file).catch((error) => setCopyResult(error.message, "error"));
  });
  $("unloadPackageButton").addEventListener("click", () => {
    unloadPackage().catch((error) => setCopyResult(error.message, "error"));
  });
  $("packagePathInput").addEventListener("input", renderPackagePanel);
  $("packagePathInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadPackageFromPath().catch((error) => setCopyResult(error.message, "error"));
    }
  });
  $("exportDirInput").addEventListener("input", (event) => {
    storageSet(EXPORT_DIR_STORAGE_KEY, event.target.value.trim());
    event.target.title = event.target.value.trim();
  });
  $("exportDirInput").addEventListener("blur", syncExportDirInput);
  $("exportSkillsPackageButton").addEventListener("click", () => {
    exportSkillsPackage().catch((error) => setSkillsResult(error.message, "error"));
  });
  $("loadSkillsPackageButton").addEventListener("click", chooseSkillsPackageFile);
  $("skillsPackageFileInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    loadSkillsPackageFromFile(file).catch((error) => setSkillsResult(error.message, "error"));
  });
  $("unloadSkillsPackageButton").addEventListener("click", () => {
    unloadSkillsPackage().catch((error) => setSkillsResult(error.message, "error"));
  });
  $("skillsPackagePathInput").addEventListener("input", renderSkillsPackagePanel);
  $("skillsPackagePathInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadSkillsPackageFromPath().catch((error) => setSkillsResult(error.message, "error"));
    }
  });
  $("skillsSearchInput").addEventListener("input", debounce(loadSkillLists, 250));
  $("overwriteSkills").addEventListener("change", () => invalidateSkillPreview());
  $("previewSkillsButton").addEventListener("click", () => {
    previewSkillsImport().catch((error) => setSkillsResult(error.message, "error"));
  });
  $("importSkillsButton").addEventListener("click", () => {
    importSkills().catch((error) => setSkillsResult(error.message, "error"));
  });
  $("killBlockingButton").addEventListener("click", () => {
    killBlockingProcesses().catch((error) => {
      setCopyResult(error.message, "error");
      loadStatus().then(renderAllShell).catch(() => {});
    });
  });
  $("repairNamesButton").addEventListener("click", () => {
    repairSessionIndexNames().catch((error) => {
      $("repairNamesButton").textContent = "Repair names";
      setCopyResult(error.message, "error");
      loadStatus().then(renderAllShell).catch(() => {});
    });
  });
  $("sourceProvider").addEventListener("change", () => {
    state.selected.clear();
    syncSourceCheckboxes();
    renderSelectionState();
    renderTargetProviders();
    renderLiveTargetPanel();
    invalidatePreview();
    loadThreadLists();
  });
  $("targetProviderSelect").addEventListener("change", () => {
    updateCustomTargetVisibility();
    renderLiveTargetPanel();
    invalidatePreview();
    updateRebindButton();
    loadTargetThreads();
  });
  $("targetProviderCustom").addEventListener("input", () => {
    renderLiveTargetPanel();
    invalidatePreview();
    updateRebindButton();
    loadTargetThreads();
  });
  $("overwriteSessions").addEventListener("change", () => invalidatePreview());
  $("searchInput").addEventListener("input", debounce(loadThreadLists, 250));
  $("dateFromFilter").addEventListener("change", loadThreadLists);
  $("dateToFilter").addEventListener("change", loadThreadLists);
  $("recentLimitFilter").addEventListener("input", debounce(loadThreadLists, 250));
  $("includeArchived").addEventListener("change", loadThreadLists);
  $("includeDescendants").addEventListener("change", () => invalidatePreview());
  $("sourceFilter").addEventListener("change", loadThreadLists);
  $("projectFilter").addEventListener("change", () => {
    renderPackagePanel();
    invalidatePreview();
    loadThreadLists();
  });
  $("workspaceModeSelect").addEventListener("change", (event) => {
    state.workspaceMode = event.target.value;
    renderWorkspaceMappingList();
    invalidatePreview();
  });
  $("workspaceTargetRoot").addEventListener("input", (event) => {
    state.workspaceTargetRoot = event.target.value;
    renderWorkspaceMappingList();
    invalidatePreview();
  });
  $("chooseWorkspaceDirectoryButton").addEventListener("click", () => {
    chooseWorkspaceDirectory().catch((error) => setCopyResult(error.message, "error"));
  });
  $("previewButton").addEventListener("click", previewCopy);
  $("rebindButton").addEventListener("click", () => {
    executeRebind().catch((error) => setCopyResult(error.message, "error"));
  });
  $("copyButton").addEventListener("click", executeCopy);
  $("sessionRenderToggleButton").addEventListener("click", () => setSessionRenderCollapsed(true));
  $("sessionRenderExpandButton").addEventListener("click", () => setSessionRenderCollapsed(false));
  $("selectAll").addEventListener("change", (event) => {
    if (event.target.checked) {
      for (const thread of state.sourceThreads) state.selected.add(thread.id);
    } else {
      state.selected.clear();
    }
    syncSourceCheckboxes();
    renderSelectionState();
    renderPackagePanel();
    invalidatePreview();
  });
  $("skillsSelectAll").addEventListener("change", (event) => {
    if (event.target.checked) {
      for (const skill of state.sourceSkills) state.selectedSkills.add(skill.id);
    } else {
      state.selectedSkills.clear();
    }
    renderSourceSkills();
    invalidateSkillPreview();
  });
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), delay);
  };
}

function renderFileRuntimeNotice() {
  $("dbPath").textContent = "This screen needs the local HTTP API.";

  for (const id of [
    "killBlockingButton",
    "repairNamesButton",
    "sessionPageButton",
    "skillsPageButton",
    "localModeButton",
    "packageModeButton",
    "exportPackageButton",
    "packagePathInput",
    "loadPackageButton",
    "unloadPackageButton",
    "sourceProvider",
    "targetProviderSelect",
    "targetProviderCustom",
    "overwriteSessions",
    "searchInput",
    "sourceFilter",
    "projectFilter",
    "includeArchived",
    "includeDescendants",
    "selectAll",
    "previewButton",
    "rebindButton",
    "copyButton",
    "exportSkillsPackageButton",
    "skillsPackagePathInput",
    "loadSkillsPackageButton",
    "unloadSkillsPackageButton",
    "skillsSearchInput",
    "overwriteSkills",
    "skillsSelectAll",
    "previewSkillsButton",
    "importSkillsButton",
    "sessionRenderToggleButton",
  ]) {
    const node = $(id);
    if (node) node.disabled = true;
  }

  const refresh = $("refreshButton");
  refresh.textContent = "Open local app";
  refresh.disabled = false;
  refresh.addEventListener("click", () => {
    window.location.href = "http://127.0.0.1:8765/";
  });

  const notice = document.createElement("div");
  notice.className = "message error file-runtime-notice";
  const link = document.createElement("a");
  link.href = "http://127.0.0.1:8765/";
  link.textContent = "http://127.0.0.1:8765/";
  notice.append(
    "This file:// preview cannot load providers or sessions. Start the local server or Electron app, then open ",
    link,
    ".",
  );
  $("previewMessages").replaceChildren(notice);
  renderSessionDetail();
  setCopyResult("The static file is read-only without the local API.", "error");
}

if (window.location.protocol === "file:") {
  renderFileRuntimeNotice();
} else {
  bindEvents();
  renderSessionDetail();
  loadAll().catch((error) => {
    $("previewMessages").replaceChildren(message("error", error.message));
  });
}
