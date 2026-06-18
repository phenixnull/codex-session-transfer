window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.shell = "electron";
  document.documentElement.dataset.platform =
    typeof process === "undefined" ? "unknown" : process.platform;
});
