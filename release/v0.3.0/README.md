# Codex Session Transfer v0.3.0

Desktop packages are published as GitHub Release assets:

- Windows installer: `Codex Session Transfer-0.3.0-Setup-x64.exe`
- Windows portable app: `Codex Session Transfer-0.3.0-Portable-x64.exe`
- macOS packages: built by the release workflow from the `v0.3.0` tag

The local app resolves the current user's Codex data at runtime. It does not hardcode a Windows user name, so it works with standard and custom profile names by checking `CODEX_HOME`, `CODEX_SQLITE_HOME`, the current user's `.codex` folder, and Codex Provider Switch config under the current user's app data.

Recommended flow: close or kill running Codex/Codex++ blockers, choose or accept the detected source and live target providers, run Preview, then run Copy.
