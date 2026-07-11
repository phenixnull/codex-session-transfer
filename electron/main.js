const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HOST = "127.0.0.1";
const PORT = process.env.CODEX_SESSION_TRANSFER_PORT || "8765";
const APP_URL = `http://${HOST}:${PORT}`;
const APP_ID = "com.phenixnull.codex-session-transfer";

let serverProcess = null;

function ignoreBrokenPipe(stream) {
  stream.on("error", (error) => {
    if (error && error.code === "EPIPE") return;
    throw error;
  });
}

ignoreBrokenPipe(process.stdout);
ignoreBrokenPipe(process.stderr);

if (process.platform === "win32") {
  app.setAppUserModelId(APP_ID);
}

function appRoot() {
  return app.isPackaged ? app.getAppPath() : path.join(__dirname, "..");
}

function iconPath() {
  const name = process.platform === "win32" ? "icon.ico" : "icon.png";
  return path.join(appRoot(), "assets", name);
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 720,
    minHeight: 480,
    title: "Codex Session Transfer",
    backgroundColor: "#1c1917",
    icon: iconPath(),
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : process.platform === "win32" ? "hidden" : "default",
    titleBarOverlay: process.platform === "win32"
      ? {
          color: "#1c1917",
          symbolColor: "#fafaf9",
          height: 44,
        }
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });

  win.loadURL(APP_URL);
}

ipcMain.handle("codex-session-transfer:choose-directory", async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(parent, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0] || null;
});

async function waitForServer() {
  for (let i = 0; i < 60; i += 1) {
    try {
      const response = await fetch(`${APP_URL}/api/status`);
      if (response.ok) return true;
    } catch {
      // Keep polling while the local server starts.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function isServerReady() {
  try {
    const response = await fetch(`${APP_URL}/api/status`);
    return response.ok;
  } catch {
    return false;
  }
}

function packagedServerCandidate() {
  const platformDir = process.platform === "win32" ? "win" : process.platform === "darwin" ? "mac" : "linux";
  const binaryName = process.platform === "win32"
    ? "codex-session-transfer-server.exe"
    : "codex-session-transfer-server";
  const resourcePath = path.join(process.resourcesPath, "server", platformDir, binaryName);
  if (!fs.existsSync(resourcePath)) return null;
  return {
    command: resourcePath,
    args: ["--host", HOST, "--port", PORT],
    cwd: path.dirname(resourcePath),
    label: "packaged server",
  };
}

function devServerCandidates() {
  const repoRoot = path.join(__dirname, "..");
  const candidates = [];
  if (process.env.PYTHON) {
    candidates.push({
      command: process.env.PYTHON,
      args: ["server.py", "--host", HOST, "--port", PORT],
      cwd: repoRoot,
      label: "PYTHON",
    });
  }
  const bundledPython = path.join(
    os.homedir(),
    ".cache",
    "codex-runtimes",
    "codex-primary-runtime",
    "dependencies",
    "python",
    process.platform === "win32" ? "python.exe" : "bin/python"
  );
  if (fs.existsSync(bundledPython)) {
    candidates.push({
      command: bundledPython,
      args: ["server.py", "--host", HOST, "--port", PORT],
      cwd: repoRoot,
      label: "Codex bundled Python",
    });
  }
  candidates.push(
    ...(process.platform === "win32"
    ? [
        { command: "py", args: ["-3", "server.py", "--host", HOST, "--port", PORT], cwd: repoRoot, label: "py launcher" },
        { command: "python", args: ["server.py", "--host", HOST, "--port", PORT], cwd: repoRoot, label: "python" },
      ]
    : [
        { command: "python3", args: ["server.py", "--host", HOST, "--port", PORT], cwd: repoRoot, label: "python3" },
        { command: "python", args: ["server.py", "--host", HOST, "--port", PORT], cwd: repoRoot, label: "python" },
      ])
  );
  return candidates;
}

function serverCandidates() {
  const candidates = [];
  const packaged = packagedServerCandidate();
  if (packaged) candidates.push(packaged);
  if (!app.isPackaged) candidates.push(...devServerCandidates());
  return candidates;
}

function launchServer(candidate) {
  return new Promise((resolve) => {
    let child = null;
    try {
      child = spawn(candidate.command, candidate.args, {
        cwd: candidate.cwd,
        env: { ...process.env, PYTHONUTF8: "1" },
        windowsHide: true,
        stdio: "ignore",
      });

      child.on("exit", () => {
        if (serverProcess === child) {
          serverProcess = null;
        }
      });

      let settled = false;
      const settle = (result) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(result);
      };
      const timer = setTimeout(() => settle({ ok: true, child }), 500);
      child.once("error", (error) => settle({ ok: false, error }));
      child.once("exit", (code, signal) => {
        settle({ ok: false, error: new Error(`${candidate.label} exited early (${code ?? signal})`) });
      });
    } catch (error) {
      resolve({ ok: false, error });
    }
  });
}

async function startServer() {
  for (const candidate of serverCandidates()) {
    const result = await launchServer(candidate);
    if (!result.ok) {
      continue;
    }
    serverProcess = result.child;
    if (await waitForServer()) {
      return true;
    }
    if (serverProcess && !serverProcess.killed) {
      serverProcess.kill();
    }
  }
  return false;
}

app.whenReady().then(async () => {
  if (!(await isServerReady())) {
    const started = await startServer();
    if (!started) {
      dialog.showErrorBox(
        "Codex Session Transfer",
        `Could not start the packaged local server for ${APP_URL}.`
      );
      app.quit();
      return;
    }
  }

  if (!(await isServerReady())) {
    dialog.showErrorBox(
      "Codex Session Transfer",
      `Could not connect to the local server at ${APP_URL}.`
    );
    app.quit();
    return;
  }

  createWindow();
});

app.on("before-quit", () => {
  if (serverProcess && !serverProcess.killed) {
    serverProcess.kill();
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
