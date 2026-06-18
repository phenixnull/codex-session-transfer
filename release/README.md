# Releases

Packaged desktop builds are collected here after local builds:

- Windows: `npm run release:win`
- macOS: `npm run release:mac` on a macOS machine or the GitHub Actions release workflow

The app does not hardcode a Windows username. The packaged server resolves Codex data from the current user at runtime using `CODEX_HOME`, `CODEX_SQLITE_HOME`, `%USERPROFILE%\.codex`, and `%APPDATA%\codex-provider-switch`.
