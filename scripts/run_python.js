const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function bundledPythonPath() {
  return path.join(
    os.homedir(),
    ".cache",
    "codex-runtimes",
    "codex-primary-runtime",
    "dependencies",
    "python",
    process.platform === "win32" ? "python.exe" : "bin/python"
  );
}

function candidates() {
  const values = [];
  if (process.env.PYTHON) values.push({ command: process.env.PYTHON, prefix: [] });
  const bundled = bundledPythonPath();
  if (fs.existsSync(bundled)) values.push({ command: bundled, prefix: [] });
  values.push({ command: "python3", prefix: [] });
  values.push({ command: "python", prefix: [] });
  if (process.platform === "win32") values.push({ command: "py", prefix: ["-3"] });
  return values;
}

function requiredModules(args) {
  const scriptPath = args.find(
    (value) => typeof value === "string" && value.toLowerCase().endsWith(".py"),
  );
  if (scriptPath && path.basename(scriptPath).toLowerCase() === "build_server.py") {
    return ["PyInstaller"];
  }
  return [];
}

function moduleUsable(candidate, moduleName) {
  const result = spawnSync(candidate.command, [...candidate.prefix, "-c", `import ${moduleName}`], {
    stdio: "ignore",
    windowsHide: true,
  });
  return result.status === 0;
}

function usable(candidate, modules = []) {
  const result = spawnSync(candidate.command, [...candidate.prefix, "--version"], {
    stdio: "ignore",
    windowsHide: true,
  });
  return result.status === 0 && modules.every((moduleName) => moduleUsable(candidate, moduleName));
}

function selectCandidate(args) {
  const modules = requiredModules(args);
  return candidates().find((candidate) => usable(candidate, modules));
}

function main(args) {
  const modules = requiredModules(args);
  const candidate = candidates().find((item) => usable(item, modules));
  if (!candidate) {
    const requirement = modules.length ? ` with ${modules.join(", ")} installed` : "";
    console.error(
      `Could not find a usable Python 3 interpreter${requirement}. ` +
        "Set PYTHON to a Python executable.",
    );
    return 1;
  }

  const result = spawnSync(candidate.command, [...candidate.prefix, ...args], {
    stdio: "inherit",
    windowsHide: true,
  });

  if (result.error) {
    console.error(result.error.message);
    return 1;
  }

  return result.status ?? 1;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = {
  bundledPythonPath,
  candidates,
  main,
  moduleUsable,
  requiredModules,
  selectCandidate,
  usable,
};
