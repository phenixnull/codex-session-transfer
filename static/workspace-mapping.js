(function initWorkspaceMapping(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WorkspaceMapping = api;
})(typeof globalThis === 'object' ? globalThis : this, function workspaceMappingFactory() {
  function cleanPath(value) {
    return String(value || '').replace(/^\\\\\?\\/, '').replace(/[\\/]+$/, '');
  }

  function isWindowsPath(value) {
    const clean = cleanPath(value);
    return /^[A-Za-z]:[\\/]/.test(clean) || clean.startsWith('\\\\');
  }

  function sourceKey(value) {
    const clean = cleanPath(value);
    if (isWindowsPath(clean)) return `windows:${clean.replaceAll('/', '\\').toLowerCase()}`;
    return `posix:${clean.replaceAll('\\', '/')}`;
  }

  function targetKey(value) {
    const clean = cleanPath(value);
    if (isWindowsPath(clean)) return `windows:${clean.replaceAll('/', '\\').toLowerCase()}`;
    return `posix:${clean.replaceAll('\\', '/')}`;
  }

  function projectLabel(value) {
    const clean = cleanPath(value);
    if (!clean) return '';
    const parts = clean.split(/[\\/]/);
    return parts[parts.length - 1] || clean;
  }

  function joinPath(base, child) {
    const cleanBase = cleanPath(base);
    const cleanChild = String(child || '').replace(/^[\\/]+/, '');
    if (!cleanBase) return cleanChild;
    const separator = isWindowsPath(cleanBase) ? '\\' : '/';
    return `${cleanBase}${separator}${cleanChild}`;
  }

  function selectedProjects(manifest, selectedIds) {
    const selected = selectedIds instanceof Set ? selectedIds : new Set(selectedIds || []);
    return (manifest?.projects || []).filter((project) => {
      return (project.threads || []).some((thread) => selected.has(thread.id));
    });
  }

  function computedTarget(targetRoot, sourceCwd, mode) {
    const rootPath = cleanPath(targetRoot);
    if (!rootPath || mode === 'single_workspace') return rootPath;
    return joinPath(rootPath, projectLabel(sourceCwd));
  }

  function effectiveMappings(projects, targetRoot, mode, overrides = {}) {
    const overrideBySource = new Map(
      Object.entries(overrides).map(([source, target]) => [sourceKey(source), target]),
    );
    return (projects || []).map((project) => {
      const sourceCwd = project.cwd || '';
      const override = overrideBySource.get(sourceKey(sourceCwd));
      return {
        project,
        sourceCwd,
        targetCwd: override || computedTarget(targetRoot, sourceCwd, mode),
        overridden: Boolean(override),
      };
    });
  }

  function duplicateTargets(mappings) {
    const firstByTarget = new Map();
    const duplicates = new Set();
    for (const entry of mappings || []) {
      const key = targetKey(entry.targetCwd);
      if (!key) continue;
      if (firstByTarget.has(key)) duplicates.add(firstByTarget.get(key));
      else firstByTarget.set(key, entry.targetCwd);
    }
    return Array.from(duplicates);
  }

  return {
    computedTarget,
    duplicateTargets,
    effectiveMappings,
    joinPath,
    projectLabel,
    selectedProjects,
  };
});
