# Codex Session Transfer

<p align="center">
  <strong>A local-first desktop workbench for moving Codex sessions and skills across providers and machines.</strong>
</p>

<p align="center">
  <a href="https://github.com/phenixnull/codex-session-transfer/actions/workflows/release-build.yml"><img alt="Release Build" src="https://github.com/phenixnull/codex-session-transfer/actions/workflows/release-build.yml/badge.svg"></a>
  <img alt="Version" src="https://img.shields.io/badge/version-0.3.0-blue">
  <img alt="Electron" src="https://img.shields.io/badge/Electron-39-47848f">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-3776ab">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-24292f">
</p>

![Codex Session Transfer main interface](assets/main-interface.png)

Codex Session Transfer gives you a focused UI for inspecting local Codex provider sessions, previewing copy plans, exporting portable transfer packages, and importing sessions or skills into another Codex environment. The app runs against local files only: the Electron shell launches a local Python server, and the browser UI talks to `127.0.0.1`.

## Highlights

- **Provider-aware session migration** - copy sessions between detected model providers without hand-editing the SQLite store.
- **Cross-machine transfer packages** - export selected sessions into a zip package, load the package on another machine, preview, then import.
- **Skills migration** - export, load, preview, and import Codex skills with overwrite control.
- **Preview-first workflow** - inspect planned writes before copying sessions or importing skills.
- **Local safety checks** - show database integrity, WAL files, blocking Codex processes, and session index status before write operations.
- **Desktop packaging** - build Windows and macOS desktop artifacts with Electron Builder and the included release workflow.

## Quick Start

```bash
git clone https://github.com/phenixnull/codex-session-transfer.git
cd codex-session-transfer
npm ci
npm run dev
```

`npm run dev` starts Electron. In development mode, Electron starts `server.py` automatically and opens the UI at `http://127.0.0.1:8765`.

To run only the local web server:

```bash
npm run server
```

Then open `http://127.0.0.1:8765` in a browser.

## Data Locations

The server resolves Codex data from the current user at runtime:

| Data | Default |
| --- | --- |
| Codex home | `CODEX_HOME` or `~/.codex` |
| SQLite home | `CODEX_SQLITE_HOME` or `~/.codex/sqlite` |
| Session database | `state_5.sqlite` |
| Session index | `~/.codex/session_index.jsonl` |
| Transfer packages | `~/.codex/session-transfer/packages` |
| Transfer manifests | `~/.codex/session-transfer/manifests` |
| Skill packages | `~/.codex/session-transfer/skill-packages` |

## Build

Generate icons and package the desktop app:

```bash
npm run icons
npm run release:win
```

macOS packages are built on macOS:

```bash
npm run release:mac
```

Release assets are collected under `release/v*/`. The GitHub Actions workflow packages Windows and macOS builds and publishes assets when a `v*` tag is pushed.

## Test

```bash
python -m unittest tests.test_session_transfer
```

## Project Layout

```text
electron/   Electron main process and desktop shell
static/     Browser UI served by the local Python server
server.py   Local API for sessions, packages, skills, and safety checks
scripts/    Build, icon, and release collection helpers
tests/      Unit tests for transfer, package, and skills behavior
release/    Locally collected release artifacts
```

## Safety Model

Codex Session Transfer is designed as a local maintenance tool. It does not upload session data to an external service. Write operations are explicit, previewable, and scoped to the current user's Codex directories. When the app detects running Codex processes that may hold database locks, it surfaces them before allowing repair or copy actions.
