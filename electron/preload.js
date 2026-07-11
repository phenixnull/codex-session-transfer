const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("codexDesktop", {
  chooseDirectory: () => ipcRenderer.invoke("codex-session-transfer:choose-directory"),
});

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.shell = "electron";
  document.documentElement.dataset.platform =
    typeof process === "undefined" ? "unknown" : process.platform;
});
