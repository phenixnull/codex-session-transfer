const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");

const HOST = "127.0.0.1";
const PORT = process.env.CODEX_SESSION_TRANSFER_PORT || "8765";
const APP_URL = `http://${HOST}:${PORT}`;

let serverProcess = null;

function ignoreBrokenPipe(stream) {
  stream.on("error", (error) => {
    if (error && error.code === "EPIPE") return;
    throw error;
  });
}

ignoreBrokenPipe(process.stdout);
ignoreBrokenPipe(process.stderr);

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 980,
    minHeight: 760,
    title: "Codex Session Transfer",
    backgroundColor: "#171520",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : process.platform === "win32" ? "hidden" : "default",
    titleBarOverlay: process.platform === "win32"
      ? {
          color: "#171520",
          symbolColor: "#d7d9ff",
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

function startServer() {
  const repoRoot = path.join(__dirname, "..");
  const candidates = process.platform === "win32"
    ? [
        ["py", ["-3", "server.py", "--host", HOST, "--port", PORT]],
        ["python", ["server.py", "--host", HOST, "--port", PORT]],
      ]
    : [
        ["python3", ["server.py", "--host", HOST, "--port", PORT]],
        ["python", ["server.py", "--host", HOST, "--port", PORT]],
      ];

  for (const [command, args] of candidates) {
    try {
      serverProcess = spawn(command, args, {
        cwd: repoRoot,
        windowsHide: true,
        stdio: "ignore",
      });
      serverProcess.on("exit", () => {
        serverProcess = null;
      });
      return;
    } catch {
      serverProcess = null;
    }
  }
}

app.whenReady().then(async () => {
  if (!(await isServerReady())) {
    startServer();
  }

  if (!(await waitForServer())) {
    dialog.showErrorBox(
      "Codex Session Transfer",
      `Could not start or connect to the local server at ${APP_URL}.`
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
