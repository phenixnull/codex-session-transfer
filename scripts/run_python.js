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

function usable(candidate) {
  const result = spawnSync(candidate.command, [...candidate.prefix, "--version"], {
    stdio: "ignore",
    windowsHide: true,
  });
  return result.status === 0;
}

const args = process.argv.slice(2);
const candidate = candidates().find(usable);

if (!candidate) {
  console.error("Could not find a usable Python 3 interpreter. Set PYTHON to a Python executable.");
  process.exit(1);
}

const result = spawnSync(candidate.command, [...candidate.prefix, ...args], {
  stdio: "inherit",
  windowsHide: true,
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 1);
