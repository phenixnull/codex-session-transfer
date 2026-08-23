const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  LOOPBACK_HOST,
  buildServerArguments,
  createInstanceToken,
  reserveLoopbackPort,
  serverMatchesInstance,
} = require("./server-runtime");

const HOST = LOOPBACK_HOST;
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

function createWindow(appUrl) {
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

  win.loadURL(appUrl);
}

ipcMain.handle("codex-session-transfer:choose-directory", async (event) => {
  const parent = BrowserWindow.fromWebContents(event.sender);
  const result = await dialog.showOpenDialog(parent, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0] || null;
});

async function waitForServer(appUrl, instanceToken, child) {
  for (let i = 0; i < 60; i += 1) {
    if (child && child.exitCode !== null) return false;
    if (await serverMatchesInstance(appUrl, instanceToken)) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function packagedServerCandidate(runtimeArgs) {
  const platformDir = process.platform === "win32" ? "win" : process.platform === "darwin" ? "mac" : "linux";
  const binaryName = process.platform === "win32"
    ? "codex-session-transfer-server.exe"
    : "codex-session-transfer-server";
  const resourcePath = path.join(process.resourcesPath, "server", platformDir, binaryName);
  if (!fs.existsSync(resourcePath)) return null;
  return {
    command: resourcePath,
    args: runtimeArgs,
    cwd: path.dirname(resourcePath),
    label: "packaged server",
  };
}

function devServerCandidates(runtimeArgs) {
  const repoRoot = path.join(__dirname, "..");
  const candidates = [];
  if (process.env.PYTHON) {
    candidates.push({
      command: process.env.PYTHON,
      args: ["server.py", ...runtimeArgs],
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
      args: ["server.py", ...runtimeArgs],
      cwd: repoRoot,
      label: "Codex bundled Python",
    });
  }
  candidates.push(
    ...(process.platform === "win32"
    ? [
        { command: "py", args: ["-3", "server.py", ...runtimeArgs], cwd: repoRoot, label: "py launcher" },
        { command: "python", args: ["server.py", ...runtimeArgs], cwd: repoRoot, label: "python" },
      ]
    : [
        { command: "python3", args: ["server.py", ...runtimeArgs], cwd: repoRoot, label: "python3" },
        { command: "python", args: ["server.py", ...runtimeArgs], cwd: repoRoot, label: "python" },
      ])
  );
  return candidates;
}

function serverCandidates(runtimeArgs) {
  const candidates = [];
  const packaged = packagedServerCandidate(runtimeArgs);
  if (packaged) candidates.push(packaged);
  if (!app.isPackaged) candidates.push(...devServerCandidates(runtimeArgs));
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

function terminateServer(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, 1500);
    child.once("exit", finish);
    try {
      child.kill();
    } catch {
      finish();
    }
  });
}

async function startServer() {
  const instanceToken = createInstanceToken();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    let reservation;
    try {
      reservation = await reserveLoopbackPort(HOST);
    } catch {
      continue;
    }
    const appUrl = `http://${HOST}:${reservation.port}`;
    const runtimeArgs = buildServerArguments({
      host: HOST,
      port: reservation.port,
      instanceToken,
      parentPid: process.pid,
    });
    await reservation.release();

    for (const candidate of serverCandidates(runtimeArgs)) {
      const result = await launchServer(candidate);
      if (!result.ok) continue;
      serverProcess = result.child;
      if (await waitForServer(appUrl, instanceToken, result.child)) {
        return { ok: true, appUrl, instanceToken };
      }
      await terminateServer(result.child);
      if (serverProcess === result.child) serverProcess = null;
    }
  }
  return { ok: false };
}

app.whenReady().then(async () => {
  const runtime = await startServer();
  if (!runtime.ok || !(await serverMatchesInstance(runtime.appUrl, runtime.instanceToken))) {
    dialog.showErrorBox(
      "Codex Session Transfer",
      "Could not start this app's packaged local server."
    );
    app.quit();
    return;
  }

  createWindow(runtime.appUrl);
});

app.on("before-quit", () => {
  if (serverProcess && !serverProcess.killed) {
    serverProcess.kill();
  }
});

app.on("window-all-closed", () => {
  app.quit();
});
