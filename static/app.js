const state = {
  status: null,
  sessionProviders: [],
  targetProviders: [],
  stats: null,
  sourceThreads: [],
  targetThreads: [],
  selected: new Set(),
  lastCopiedTargetIds: new Set(),
  preview: null,
  copyResult: "",
};

const $ = (id) => document.getElementById(id);
const CUSTOM_TARGET = "__custom__";

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

function targetProviderValue() {
  const selected = $("targetProviderSelect").value;
  return selected === CUSTOM_TARGET
    ? $("targetProviderCustom").value.trim()
    : selected;
}

function copyRequest() {
  return {
    source_provider: $("sourceProvider").value,
    target_provider: targetProviderValue(),
    thread_ids: Array.from(state.selected),
    include_descendants: $("includeDescendants").checked,
    include_archived: $("includeArchived").checked,
  };
}

async function loadAll() {
  await loadStatus();
  ensureProviderSelection();
  renderAllShell();
  await loadThreadLists();
}

async function loadStatus() {
  state.status = await api("/api/status");
  state.sessionProviders = state.status.providers || [];
  state.targetProviders = state.status.target_providers || [];
  state.stats = state.status.session_stats || null;
}

function threadQueryParams(provider) {
  return new URLSearchParams({
    source_provider: provider || "",
    include_archived: $("includeArchived").checked ? "true" : "false",
    search: $("searchInput").value.trim(),
    source: $("sourceFilter").value,
    cwd: $("projectFilter").value,
  });
}

async function loadThreadLists(options = {}) {
  await loadSourceThreads(options);
  await loadTargetThreads();
}

async function loadSourceThreads(options = {}) {
  const params = new URLSearchParams({
    source_provider: $("sourceProvider").value,
    include_archived: $("includeArchived").checked ? "true" : "false",
    search: $("searchInput").value.trim(),
    source: $("sourceFilter").value,
    cwd: $("projectFilter").value,
  });
  state.sourceThreads = await api(`/api/threads?${params.toString()}`);
  for (const id of Array.from(state.selected)) {
    if (!state.sourceThreads.some((thread) => thread.id === id)) {
      state.selected.delete(id);
    }
  }
  renderSourceFilter();
  renderSourceThreads();
  invalidatePreview({ clearResult: !options.preserveResult });
}

async function loadTargetThreads() {
  const target = targetProviderValue();
  if (!target) {
    state.targetThreads = [];
  } else {
    state.targetThreads = await api(`/api/threads?${threadQueryParams(target).toString()}`);
  }
  renderTargetThreads();
}

function ensureProviderSelection() {
  const sourceSelect = $("sourceProvider");
  const sourceProvider = preferredSourceProvider(sourceSelect.value);
  if (sourceProvider) sourceSelect.value = sourceProvider;

  const targetSelect = $("targetProviderSelect");
  const targetProvider = preferredTargetProvider(targetSelect.value, sourceProvider || sourceSelect.value);
  if (targetProvider) targetSelect.value = targetProvider;
}

function preferredSourceProvider(currentValue) {
  const current = state.sessionProviders.find((provider) => provider.model_provider === currentValue);
  if (current && current.active > 0) return current.model_provider;

  const activeProvider = state.sessionProviders.find((provider) => provider.active > 0);
  if (activeProvider) return activeProvider.model_provider;

  return current?.model_provider || state.sessionProviders[0]?.model_provider || "";
}

function preferredTargetProvider(currentValue, sourceValue) {
  const options = state.targetProviders;
  const current = options.find((provider) => provider.value === currentValue);
  if (current && current.value !== sourceValue) return current.value;

  const liveTarget = options.find((provider) => provider.current && provider.value !== sourceValue);
  if (liveTarget) return liveTarget.value;

  const nonSourceTarget = options.find((provider) => provider.value !== sourceValue);
  if (nonSourceTarget) return nonSourceTarget.value;

  return current?.value || options.find((provider) => provider.current)?.value || options[0]?.value || "";
}

function renderAllShell() {
  renderStatus();
  renderStats();
  renderProviders();
  renderTargetProviders();
  renderLiveTargetPanel();
  renderProjectFilter();
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
  const select = $("sourceProvider");
  const current = select.value;
  select.replaceChildren();
  const grid = $("providerGrid");
  grid.replaceChildren();

  for (const provider of state.sessionProviders) {
    const option = document.createElement("option");
    option.value = provider.model_provider;
    option.textContent = `${provider.model_provider} (${provider.total})`;
    option.title = providerTooltip(provider);
    select.append(option);

    const card = document.createElement("div");
    card.className = "provider-tile";
    card.title = providerTooltip(provider);
    const providerStats = state.stats?.by_provider?.[provider.model_provider];
    card.innerHTML = `
      <strong>${escapeHtml(provider.model_provider)}</strong>
      <span>${provider.active} active / ${provider.archived} archived</span>
      <span>${providerStats?.projects ?? 0} projects</span>
      <span>session-db provider</span>
    `;
    grid.append(card);
  }
  const preferred = preferredSourceProvider(current);
  if (preferred) select.value = preferred;
}

function providerTooltip(provider) {
  const details = [];
  if (provider.model_provider) details.push(`Provider: ${provider.model_provider}`);
  details.push(`Active: ${provider.active ?? 0}`);
  details.push(`Archived: ${provider.archived ?? 0}`);
  details.push(`Total: ${provider.total ?? 0}`);
  const providerStats = state.stats?.by_provider?.[provider.model_provider];
  if (providerStats) details.push(`Projects: ${providerStats.projects ?? 0}`);
  return details.join(" | ");
}

function renderTargetProviders() {
  const select = $("targetProviderSelect");
  const current = select.value;
  select.replaceChildren();

  for (const provider of state.targetProviders) {
    const option = new Option(targetProviderLabel(provider), provider.value);
    option.title = targetProviderTooltip(provider);
    select.append(option);
  }
  select.append(new Option("Custom provider id...", CUSTOM_TARGET));
  const preferred = preferredTargetProvider(current, $("sourceProvider").value);
  if (preferred) {
    select.value = preferred;
  }
  updateCustomTargetVisibility();
}

function targetProviderLabel(provider) {
  const bits = [provider.label || provider.value];
  if (provider.current) bits.push("live");
  if (provider.session_total) bits.push(`${provider.session_total}`);
  return bits.join(" / ");
}

function targetProviderTooltip(provider) {
  const details = [];
  if (provider.value) details.push(`Provider: ${provider.value}`);
  if (provider.model) details.push(`Model: ${provider.model}`);
  if (provider.base_url) details.push(`Base URL: ${provider.base_url}`);
  if (provider.wire_api) details.push(`Wire API: ${provider.wire_api}`);
  if (provider.current) details.push("Current live config");
  if (provider.matches_current_base_url) details.push("Same base URL as current config");
  if (provider.sources?.includes("codex_plus_preset")) details.push("From Codex++ preset");
  if (provider.sources?.includes("codex_plus_override")) details.push("From Codex++ override");
  if (provider.sources?.includes("session_db")) details.push("Present in session DB");
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
  for (const project of state.stats?.by_project || []) {
    const label = `${project.label} (${project.total})`;
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
    const row = threadRow(thread);
    const selectCell = document.createElement("td");
    selectCell.className = "select-cell";
    selectCell.dataset.label = "Select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(thread.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(thread.id);
      else state.selected.delete(thread.id);
      renderSelectionState();
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
    const row = threadRow(thread, { target: true });
    body.append(row);
  }
  renderListCount("targetListCount", state.targetThreads.length);
}

function threadRow(thread, options = {}) {
  const row = document.createElement("tr");
  if (!thread.rollout_exists) row.classList.add("warn-row");
  if (options.target && state.lastCopiedTargetIds.has(thread.id)) row.classList.add("new-row");

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
}

async function previewCopy() {
  const plan = await api("/api/preview-copy", {
    method: "POST",
    body: JSON.stringify(copyRequest()),
  });
  state.preview = plan;
  renderPreview(plan);
  setCopyResult(
    plan.can_execute
      ? `Preview ready: ${plan.items.length} session(s) can be copied.`
      : "Preview is not executable. Fix the messages above.",
    plan.can_execute ? "info" : "error",
  );
}

function renderPreview(plan) {
  const messages = $("previewMessages");
  const items = $("previewItems");
  messages.replaceChildren();
  items.replaceChildren();
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
  for (const item of plan.items || []) {
    const node = document.createElement("div");
    node.className = "preview-item";
    node.innerHTML = `
      <strong>${escapeHtml(item.display_title || item.title || item.source_id)}</strong>
      <span>${escapeHtml(item.source_provider)} -> ${escapeHtml(item.target_provider)}</span>
      <code>${escapeHtml(item.source_id)} -> ${escapeHtml(item.target_id)}</code>
    `;
    items.append(node);
  }
  updateCopyButton();
}

function invalidatePreview({ clearResult = true } = {}) {
  state.preview = null;
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

async function executeCopy() {
  const blocked = (state.status && state.status.blocking_processes || []).length > 0;
  if (blocked) return;
  const count = state.preview ? state.preview.items.length : 0;
  if (!count) return;
  const target = targetProviderValue();
  const confirmed = window.confirm(`Copy ${count} session(s) to ${target}?`);
  if (!confirmed) return;
  $("copyButton").disabled = true;
  setCopyResult("Copying...", "info");
  const result = await api("/api/copy", {
    method: "POST",
    body: JSON.stringify(copyRequest()),
  });
  state.preview = result;
  renderPreview(result);
  const resultText = result.ok
    ? `Copied ${result.items?.length || 0} session(s). Session index entries: ${result.session_index_entries || 0}. Manifest: ${result.manifest_path}`
    : `Not copied. ${(result.errors || []).join("; ")}`;
  await loadStatus();
  renderAllShell();
  state.lastCopiedTargetIds = new Set((result.items || []).map((item) => item.target_id));
  await loadThreadLists({ preserveResult: true });
  setCopyResult(resultText, result.ok ? "success" : "error");
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
      : `Killed ${result.killed_count || 0} blocker(s). Copy can run after Preview succeeds.`,
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

function updateCopyButton() {
  const blocked = (state.status && state.status.blocking_processes || []).length > 0;
  const reasons = copyDisabledReasons(blocked);
  const copyButton = $("copyButton");
  copyButton.disabled = reasons.length > 0;
  copyButton.title = reasons.join(" ");
  $("copyDisabledReason").textContent = reasons.length ? reasons.join(" ") : "Ready to copy.";
}

function copyDisabledReasons(blocked) {
  const reasons = [];
  const selectedCount = state.selected.size;
  const source = $("sourceProvider").value;
  const target = targetProviderValue();
  const blockingCount = (state.status && state.status.blocking_processes || []).length;

  if (selectedCount === 0) {
    reasons.push("Select at least one session.");
  }
  if (!target) {
    reasons.push("Choose a target provider.");
  }
  if (source && target && source === target) {
    reasons.push("Source and target must be different.");
  }
  if (!state.preview) {
    reasons.push("Run Preview first.");
  } else if (state.preview.errors?.length) {
    reasons.push(`Preview has errors: ${state.preview.errors[0]}`);
  } else if (!state.preview.can_execute) {
    reasons.push("Preview plan is not executable.");
  }
  if (blocked) {
    reasons.push(`Close Codex/Codex++ before copying (${blockingCount} blocking process${blockingCount === 1 ? "" : "es"} detected). Preview is still read-only and safe.`);
  }
  return reasons;
}

function updateCustomTargetVisibility() {
  const custom = $("targetProviderCustom");
  const isCustom = $("targetProviderSelect").value === CUSTOM_TARGET;
  custom.hidden = !isCustom;
  if (!isCustom) custom.value = "";
}

function shortPath(value) {
  if (!value) return "";
  const parts = value.split(/[\\/]/);
  if (parts.length <= 3) return value;
  return `${parts[0]}\\...\\${parts.slice(-2).join("\\")}`;
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
    renderTargetProviders();
    renderLiveTargetPanel();
    invalidatePreview();
    loadThreadLists();
  });
  $("targetProviderSelect").addEventListener("change", () => {
    updateCustomTargetVisibility();
    renderLiveTargetPanel();
    invalidatePreview();
    loadTargetThreads();
  });
  $("targetProviderCustom").addEventListener("input", () => {
    renderLiveTargetPanel();
    invalidatePreview();
    loadTargetThreads();
  });
  $("searchInput").addEventListener("input", debounce(loadThreadLists, 250));
  $("includeArchived").addEventListener("change", loadThreadLists);
  $("includeDescendants").addEventListener("change", () => invalidatePreview());
  $("sourceFilter").addEventListener("change", loadThreadLists);
  $("projectFilter").addEventListener("change", loadThreadLists);
  $("previewButton").addEventListener("click", previewCopy);
  $("copyButton").addEventListener("click", executeCopy);
  $("selectAll").addEventListener("change", (event) => {
    if (event.target.checked) {
      for (const thread of state.sourceThreads) state.selected.add(thread.id);
    } else {
      state.selected.clear();
    }
    renderSourceThreads();
    invalidatePreview();
  });
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), delay);
  };
}

bindEvents();
loadAll().catch((error) => {
  $("previewMessages").replaceChildren(message("error", error.message));
});
