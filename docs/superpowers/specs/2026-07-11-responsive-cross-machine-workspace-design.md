# Responsive Layout and Cross-Machine Workspace Mapping Design

Date: 2026-07-11

## Scope

This release fixes two connected usability and correctness problems:

1. The desktop UI must keep all controls and session lists reachable on short or scaled displays.
2. A cross-machine package must be able to import sessions from any source project into user-selected Codex App working directories while updating every authoritative current-path field consistently.

The release target is `v0.4.0`. It includes a verified Windows build and a tag-triggered GitHub release with Windows and macOS artifacts.

## Official Codex Evidence

The design was checked against the current `openai/codex` source at commit `5c19155cbd93bfa099016e7487259f61669823ff` and the official documentation links already recorded in `codex-session-storage-analysis.md`.

- App-server `thread/list` treats `cwd` as an exact thread filter. A wrong imported cwd can hide a thread from the expected project group.
- `SessionMeta` stores both `session_id`, `id`, and `cwd` in the rollout.
- The state database stores `id`, `rollout_path`, `model_provider`, and `cwd` in `threads`.
- Rollout metadata extraction copies `session_meta.cwd` back into state metadata. Updating only SQLite is therefore not durable.
- `session_index.jsonl` contains only `id`, `thread_name`, and `updated_at`; it is not a cwd or rollout-path index.
- Historical `TurnContext` and command execution items contain the cwd that applied when those historical events occurred. They remain history and must not be rewritten as if the old commands ran elsewhere.

Authoritative references:

- <https://developers.openai.com/codex/config-advanced#config-and-state-locations>
- <https://developers.openai.com/codex/environment-variables>
- <https://developers.openai.com/codex/app-server>
- <https://github.com/openai/codex/blob/5c19155cbd93bfa099016e7487259f61669823ff/codex-rs/app-server/README.md#example-list-threads-with-pagination--filters>
- <https://github.com/openai/codex/blob/5c19155cbd93bfa099016e7487259f61669823ff/codex-rs/protocol/src/protocol.rs#L3023>
- <https://github.com/openai/codex/blob/5c19155cbd93bfa099016e7487259f61669823ff/codex-rs/state/migrations/0001_threads.sql>
- <https://github.com/openai/codex/blob/5c19155cbd93bfa099016e7487259f61669823ff/codex-rs/rollout/src/metadata.rs#L37>
- <https://github.com/openai/codex/blob/5c19155cbd93bfa099016e7487259f61669823ff/codex-rs/rollout/src/session_index.rs#L22>

## Responsive Layout

### Root cause

The desktop layout is selected only by viewport width. On a wide but short viewport such as `1366x600`, the desktop branch remains active while `body`, `.app-shell`, `.content-row`, and `.main-surface` all clip or hide vertical overflow. The session setup panel consumes nearly all available height and collapses `#sessionTransferGrid` to about 16 pixels. No ancestor can scroll to the clipped list.

Electron also enforces a `980x760` minimum window. That minimum can exceed the usable desktop area on a 768-pixel display after taskbar space or display scaling is applied.

### Behavior

- At widths above the existing stacked-layout breakpoint and heights at or below 760 CSS pixels, `.main-surface` becomes a contained vertical scroll region.
- In that compact-height desktop mode, the session transfer grid keeps a useful minimum height of at least 240 pixels instead of collapsing.
- The sidebar, session-render rail, preview rail, and session lists retain their existing local scroll behavior.
- At widths up to 1240 pixels, the existing stacked layout remains the page-level scrolling fallback.
- Electron minimum dimensions become `720x480`, which matches the responsive CSS range and fits common scaled displays.
- No fixed overlay or sticky action area may cover content. Keyboard focus and mouse-wheel scrolling must reach the same content.

## Workspace Mapping UX

The cross-machine package panel shows workspace mapping only after a package is loaded.

### Modes

1. `Preserve projects` is the default. The user selects a target root, and each source project maps to `<target root>/<source project name>`.
2. `Single workspace` maps every selected session, including selected descendants, to the exact same target directory.

The UI also renders a per-project mapping list. Each row shows the full source cwd and computed target cwd and allows a target override. This covers duplicate project names, renamed repositories, worktrees, and arbitrary destination layouts without forcing one global rule.

The Electron shell provides a native directory picker through a narrow preload IPC bridge. The browser-only UI keeps typed path inputs as a fallback.

### Selection behavior

- Mapping rows are derived from the loaded package manifest and refreshed as selected sessions change.
- Only projects represented by selected sessions are sent as explicit overrides.
- Descendants added by `Copy tree` are mapped server-side using the selected mode, so a hidden descendant cannot retain an old-machine cwd.
- Duplicate computed destinations are allowed only in `Single workspace` mode. In preserve mode, duplicate project names require an explicit override for at least one project.
- The preview shows every effective source-to-target cwd mapping before import.

## Request Contract

`CopyRequest` keeps the existing `cwd_map` for backward compatibility and adds an optional workspace mapping object:

```json
{
  "workspace_mapping": {
    "mode": "preserve_projects",
    "target_root": "D:\\CodexWork",
    "overrides": {
      "C:\\old\\ProjectA": "D:\\clients\\ProjectA-renamed"
    }
  }
}
```

Supported modes are `preserve_projects` and `single_workspace`. When `workspace_mapping` is present it owns target-cwd resolution; legacy `cwd_map` is accepted only when the new object is absent.

Target paths must be absolute, must exist, and must be directories. Preserve mode also requires the target root to exist. The importer never creates project directories implicitly because an empty directory can make a session appear valid while its code is absent.

Path comparison uses platform-aware normalization: extended Windows prefixes and separator variants are normalized, Windows comparison is case-insensitive, and POSIX comparison remains case-sensitive.

## Import Data Flow

1. Load and validate the package without mutating the target Codex home.
2. Expand selected descendants and collect every distinct source cwd in the final plan.
3. Resolve each source cwd from an explicit override, preserve-project derivation, or the single target workspace.
4. Reject missing, relative, non-directory, ambiguous, or unmapped targets in preview.
5. Generate new thread ids and destination rollout paths under the target Codex home.
6. For each copied rollout, update the first `session_meta` record:
   - `id` to the new thread id.
   - `session_id` to the new thread id when the field is present.
   - `model_provider` to the selected target provider.
   - `cwd` to the resolved target workspace.
   - Parent and fork ids to their remapped ids.
7. Insert the copied `threads` row with matching `id`, `rollout_path`, `model_provider`, and `cwd`, plus the existing import visibility timestamps and source normalization.
8. Copy and remap spawn edges and dynamic tools as today.
9. Append the target id and preserved name to `session_index.jsonl`; do not add invented path fields.
10. Commit SQLite only after rollout files and the session index update succeed. On failure, restore the session index snapshot, roll back SQLite, and remove newly written rollout files.
11. Record source and target cwd values in the operation manifest for audit and recovery.

Historical messages, command records, tool output, `TurnContext.cwd`, and embedded user text are deliberately unchanged.

## Error Handling

Preview blocks execution and identifies the exact source project when:

- a target root or override is relative;
- a target path does not exist or is not a directory;
- preserve mode produces duplicate destinations;
- a selected descendant has no resolvable source cwd;
- a target path uses a path syntax invalid for the target operating system;
- the package contains a malformed rollout or unsupported filename;
- Codex or a provider switcher still holds the database.

The UI keeps the user's mapping inputs after a failed preview. Loading or unloading a package resets package-specific mappings.

## Testing

### Backend tests

- Parse valid and invalid workspace mapping payloads.
- Preserve project names under a target root.
- Merge multiple source projects into one workspace.
- Apply per-project overrides.
- Map descendants whose cwd differs from the selected parent.
- Reject relative, missing, file, ambiguous, and unmapped targets.
- Normalize Windows extended paths without making POSIX paths case-insensitive.
- Verify the copied rollout updates `id`, optional `session_id`, provider, and cwd.
- Verify the target SQLite row has the same cwd and rollout path.
- Verify `session_index.jsonl` remains name-only.
- Verify rollback removes partially written files and restores the index.

### Frontend and browser checks

- At `1366x600` and `1280x720`, the main panel must have a real vertical scroll range and the transfer grid must remain at least 240 pixels high.
- At `1024x600` and `700x800`, page-level scrolling must reach the last action.
- Session and skills pages must have no incoherent overlap at desktop and compact widths.
- The mapping mode, directory picker fallback, computed mappings, overrides, preview messages, and retained error state must be exercised.
- Screenshots are captured for desktop, compact-height, and narrow layouts.

### Release checks

- Run the complete Python unit suite.
- Run JavaScript syntax checks and the responsive Playwright check.
- Build the Windows server and Electron Setup/Portable artifacts locally.
- Verify generated SHA-256 files and release manifests.
- Bump package and lockfile versions to `0.4.0` and update user-facing release documentation.
- Push commits and tag `v0.4.0` to `origin`.
- Monitor the GitHub Actions release workflow until both Windows and macOS jobs pass.
- Verify the GitHub release contains Setup, Portable, DMG, macOS ZIP, checksum, and manifest assets.

## Non-Goals

- The importer does not copy source repositories or project files.
- It does not rewrite historical transcript text or command output.
- It does not migrate authentication, provider credentials, memories, logs, unread state, goals, or app UI cache.
- It does not silently create missing target projects.
- It does not perform bidirectional session synchronization.
