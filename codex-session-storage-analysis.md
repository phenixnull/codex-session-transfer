# Codex Session Storage Mechanism Analysis

Date: 2026-06-13

Scope: Codex app, Codex CLI, local sessions, SQLite-backed thread index, and the observed issue where switching third-party model providers changes the visible thread list.

Safety boundary: this investigation was read-only except for creating this note and a temporary official source checkout under `%TEMP%`. Secret values were not read into this document. Files named like `auth.json`, `gmn-session.json`, `gwen-usage-account.json`, `webui-auth-sessions.json`, `cap_sid`, and `codexui-password` were treated as sensitive.

## Short Answer

The thread body is stored as JSONL rollout files under `CODEX_HOME`, normally:

```text
C:\Users\Administrator\.codex\sessions\YYYY\MM\DD\rollout-<timestamp>-<thread-id>.jsonl
```

The desktop app and app-server do not build the sidebar by scanning only those JSONL files every time. They use a SQLite-backed state index, especially:

```text
C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

on this machine, because the current environment sets:

```text
CODEX_SQLITE_HOME=C:\Users\Administrator\.codex\sqlite
```

The main table is `threads`. Each row has a `model_provider` field, plus `cwd`, `source`, `thread_source`, `archived`, `title`, `preview`, and `rollout_path`. The current app-server source has provider filtering in `thread/list`: when `modelProviders` is omitted, it defaults to the active configured provider. If the client does not explicitly request all providers, changing provider A to B naturally makes A's threads disappear from the default list even though the JSONL rollout files still exist.

So the likely core cause is not lost sessions. It is a combination of:

1. `threads.model_provider` is row-level metadata and is indexed.
2. `thread/list` supports provider filtering.
3. Current source code defaults omitted `modelProviders` to the active `config.model_provider_id`.
4. This machine has two SQLite state locations, old root-level and current `sqlite` subdir, which can further split visibility.

## Official Sources Used

- Codex authentication and login cache: <https://developers.openai.com/codex/auth>
- Config and state locations: <https://developers.openai.com/codex/config-advanced#config-and-state-locations>
- Environment variables, including `CODEX_HOME` and `CODEX_SQLITE_HOME`: <https://developers.openai.com/codex/environment-variables>
- CLI resume and local transcript behavior: <https://developers.openai.com/codex/cli/features#resuming-conversations>
- Codex app features and thread search: <https://developers.openai.com/codex/app/features>
- Codex app-server concepts and `thread/list`: <https://developers.openai.com/codex/app-server>
- Official source checkout used for implementation evidence: `openai/codex` at commit `f297b9f07de10c7d8b9ed284b674d06cc5ff7723`
  - `STATE_DB_FILENAME = state_5.sqlite`, `SQLITE_HOME_ENV = CODEX_SQLITE_HOME`: <https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/src/lib.rs>
  - `threads` schema and `idx_threads_provider`: <https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/migrations/0001_threads.sql>
  - SQLite list filters: <https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/src/runtime/threads.rs>
  - app-server `thread/list` provider default: <https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/app-server/src/request_processors/thread_processor.rs>
  - `session_index.jsonl` append-only name index: <https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/rollout/src/session_index.rs>

## Local Storage Surfaces

### Shared Codex Home

Observed main home:

```text
C:\Users\Administrator\.codex
```

Important contents:

```text
.codex\
  config.toml
  auth.json                      sensitive
  history.jsonl
  session_index.jsonl
  sessions\
  archived_sessions\
  sqlite\
    state_5.sqlite
    logs_2.sqlite
    goals_1.sqlite
    memories_1.sqlite
  state_5.sqlite                 older root-level state DB
  logs_2.sqlite                  older root-level logs DB
  goals_1.sqlite
  memories_1.sqlite
  backups_state\provider-sync\
```

Official documentation says `CODEX_HOME` defaults to `~/.codex` and stores config, auth, logs, sessions, skills, and standalone package metadata. On Windows app, native Windows Codex uses `%USERPROFILE%\.codex`. WSL CLI uses the Linux home unless `CODEX_HOME` is explicitly pointed at the Windows path.

### Desktop App User Data

Observed desktop/Electron/WebView areas:

```text
C:\Users\Administrator\AppData\Roaming\Codex
C:\Users\Administrator\AppData\Roaming\Codex\web\Codex
C:\Users\Administrator\AppData\Roaming\Codex\web\Codex (Beta)
C:\Users\Administrator\AppData\Local\OpenAI\Codex
C:\Users\Administrator\AppData\Local\Codex
```

These mainly look like Chromium/WebView user data, runtime binaries, caches, history, cookies, Sentry, and logs. I did not find Codex thread/session business tables there. The business state is in `.codex`.

### Provider Switcher State

Observed provider-switch helper area:

```text
C:\Users\Administrator\AppData\Roaming\codex-provider-switch
```

Important files:

```text
preset-overrides.json       provider presets, configText, authText
gmn-session.json            account/accessToken/refreshToken/expiresAt fields
gwen-usage-account.json     username/password/loginUrl/usageUrl fields
```

Values were not recorded. This folder is not the Codex session store, but it can change the active provider config/auth surface, which can change what app-server considers the active provider.

## Local Counts

### Rollout Files

Active sessions:

```text
C:\Users\Administrator\.codex\sessions
```

Observed:

```text
500 rollout JSONL files
2026/04: 157
2026/05: 222
2026/06: 121
total size: about 5.3 GB
```

Archived sessions:

```text
C:\Users\Administrator\.codex\archived_sessions
```

Observed:

```text
77 rollout JSONL files
total size: about 440 MB
```

Every inspected rollout file starts with a `session_meta` record shaped like:

```json
{
  "timestamp": "...",
  "type": "session_meta",
  "payload": {
    "id": "...",
    "timestamp": "...",
    "cwd": "...",
    "originator": "...",
    "cli_version": "...",
    "source": "cli|vscode|exec|...",
    "model_provider": "CodexPlusPlus|OpenAI|custom",
    "base_instructions": "...",
    "dynamic_tools": "..."
  }
}
```

The session body and turns are in these JSONL files, not in `threads` table rows.

### Current Active SQLite State

Current active SQLite home:

```text
C:\Users\Administrator\.codex\sqlite
```

Current `state_5.sqlite`:

```text
threads: 577
CodexPlusPlus: 485
OpenAI: 85
custom: 7
active: 500
archived: 77
rollout_path exists: 577/577
```

The current `/goal` thread and the three subagents from this investigation are present only in this active DB and have `model_provider = custom`.

### Older Root-Level SQLite State

Older root-level DB:

```text
C:\Users\Administrator\.codex\state_5.sqlite
```

Observed:

```text
threads: 578
CodexPlusPlus: 486
OpenAI: 89
custom: 3
active: 501
archived: 77
rollout_path exists: 573/578
```

This DB was not updated with the current investigation threads. It appears to be an older state store used before `CODEX_SQLITE_HOME` pointed to `.codex\sqlite`.

### Provider-Sync Backups

Observed provider-sync backup DBs:

```text
C:\Users\Administrator\.codex\backups_state\provider-sync\YYYYMMDDHHMMSS\db\state_5.sqlite
```

The 2026-06-02 backups are all same-schema snapshots and all rows had `model_provider = CodexPlusPlus`, growing from 474 to 486 threads. This is strong local evidence that provider switching or provider-sync tooling has historically backed up/rebuilt the state index.

## SQLite Schema

Main DB filename is official source constant:

```text
state_5.sqlite
```

Key tables observed:

```text
threads
thread_spawn_edges
thread_dynamic_tools
agent_jobs
agent_job_items
backfill_state
remote_control_enrollments
_sqlx_migrations
```

`threads` is the key sidebar/index table. Current columns include:

```text
id
rollout_path
created_at
updated_at
source
model_provider
cwd
title
sandbox_policy
approval_mode
tokens_used
has_user_event
archived
archived_at
git_sha
git_branch
git_origin_url
cli_version
first_user_message
agent_nickname
agent_role
memory_mode
model
reasoning_effort
agent_path
created_at_ms
updated_at_ms
thread_source
preview
```

Important indexes:

```text
idx_threads_provider on threads(model_provider)
idx_threads_source on threads(source)
idx_threads_archived on threads(archived)
idx_threads_created_at / updated_at
idx_threads_created_at_ms / updated_at_ms
idx_threads_archived_cwd_created_at_ms
idx_threads_archived_cwd_updated_at_ms
idx_thread_spawn_edges_parent_status
idx_thread_dynamic_tools_thread
```

Important point: `model_provider` is a first-class indexed column, but there is no observed separate `providers`, `accounts`, `profiles`, or `projects` business table. Provider is per-thread metadata, not a separate database partition by itself.

## Listing Mechanics

The app-server API has a `thread/list` method. Official app-server docs say it supports pagination plus filters:

```text
modelProviders
sourceKinds
archived
cwd
searchTerm
useStateDbOnly
```

Current source implementation adds a critical behavior:

```rust
let model_provider_filter = match model_providers {
    Some(providers) => {
        if providers.is_empty() {
            None
        } else {
            Some(providers)
        }
    }
    None => Some(vec![self.config.model_provider_id.clone()]),
};
```

Meaning, in this source snapshot:

```text
modelProviders omitted/null -> filter to current active provider
modelProviders = []         -> no provider filter, show all providers
modelProviders = ["A"]      -> show only provider A
```

This is slightly different from the app-server README wording, which says unset/null/empty includes all providers. Treat that as a documentation/source mismatch that should be verified against the exact desktop app build. The current source code matches your symptom very closely.

SQLite filtering also applies:

```text
archived=false by default
threads.preview <> ''
threads.model_provider IN (...)
threads.source IN (...)
threads.cwd IN (...)
title/preview searchTerm
```

Therefore a thread can be invisible in the left sidebar while still fully present on disk when any of these are true:

```text
provider filter excludes it
cwd/project filter excludes it
sourceKinds excludes it
archived = 1 and archived view is not open
preview is empty
the app is reading a different sqlite_home
```

## Role of `session_index.jsonl`

Observed:

```text
C:\Users\Administrator\.codex\session_index.jsonl
lines: 453
unique ids: 433
fields: id, thread_name, updated_at
duplicates: expected, append-only latest-name-wins design
```

Official source confirms it is an append-only thread-name index. It is not a complete authoritative thread list and does not store provider/cwd/archive metadata. It is useful for name lookup and rename history, not for reconstructing full sidebar state.

## Why Provider A Threads Disappear After Switching to Provider B

Most likely chain:

1. Provider switch updates active `model_provider` in `config.toml` or the app-server effective config.
2. The desktop sidebar calls `thread/list`.
3. If the request omits `modelProviders`, current source defaults to the active provider id.
4. The SQL query includes `threads.model_provider IN (current_provider)`.
5. Provider A rows are still in `state_5.sqlite` and their rollout files still exist, but they are filtered out.

Secondary factors on this machine:

1. The active SQLite store is `.codex\sqlite\state_5.sqlite`, not the older `.codex\state_5.sqlite`.
2. The older root DB and active DB have different rows.
3. Provider-sync backups show prior provider-specific state snapshots.
4. App global state has project/sidebar group state, pinned thread ids, projectless thread ids, active workspace roots, and collapsed sidebar groups.
5. AppData userData contains Chromium profile data but not the thread index.

## Migration and Preview Design Notes

For the future tool you described, separate three goals:

### 1. Cross-provider Preview

Best target: no file mutation.

Use app-server `thread/list` with explicit provider choices:

```text
modelProviders = []                         all providers
modelProviders = ["CodexPlusPlus"]          one provider
modelProviders = ["OpenAI", "custom"]       selected providers
archived = false/true                       active or archived
cwd = ...                                   project filter
```

If app-server is not used, query the resolved active SQLite home:

```text
sqlite_home config > CODEX_SQLITE_HOME > CODEX_HOME
```

Then read `threads` and join only by `rollout_path` existence. Do not treat `session_index.jsonl` as authoritative.

### 2. Safe Copy to Another Provider

Do not mutate originals. The safer conceptual copy unit is:

```text
rollout JSONL file
threads row metadata
optional session_index name entry
optional thread_spawn_edges if copying a tree
optional thread_dynamic_tools if needed
optional goals/memories/log links only if intentionally preserving those surfaces
```

To make a copied thread appear under provider B while preserving A:

```text
new thread id recommended
new rollout filename recommended
session_meta.id must match new id
session_meta.model_provider should become provider B
threads.id must match new id
threads.model_provider should become provider B
threads.rollout_path must point to the new rollout
thread_spawn_edges must be remapped if copying parent/child trees
```

Keeping the same thread id in two providers is risky because `threads.id` is the primary key. It also confuses `session_index.jsonl`, unread ids, goals, logs, and spawned edges.

### 3. Bidirectional Sync

Bidirectional sync is harder than copy:

```text
need conflict strategy for same title, same id, divergent histories
need path normalization for D:\... vs \\?\D:\...
need archive state reconciliation
need provider metadata reconciliation
need app shutdown or app-server API writes to avoid WAL/locking races
```

Recommended first version: one-way copy with a manifest recording source id, destination id, source provider, destination provider, source rollout path, destination rollout path, and copy time.

## Operational Risks

1. SQLite uses WAL files. Copying or editing live DB files while Codex is running can miss uncheckpointed changes or hit locks.
2. `auth.json` and provider-switch auth files are unrelated to session migration and must not be copied as part of thread migration.
3. `logs_2.sqlite` can be huge and is diagnostic, not required for thread visibility.
4. `memories_1.sqlite` contains generated memory material and may include sensitive summaries. Keep it out of migration unless explicitly required.
5. App-server README and current source disagree on omitted `modelProviders`; verify the actual desktop request before relying on docs alone.
6. Some rows can have empty `preview`; current SQLite filter excludes `preview = ''` from list results.

## Practical Verification Checklist

When a provider switch makes threads disappear:

1. Check active provider:

```text
read ~/.codex/config.toml model_provider
```

2. Check active SQLite home:

```text
sqlite_home config key
CODEX_SQLITE_HOME env
fallback CODEX_HOME
```

3. Count threads by provider:

```sql
SELECT model_provider, archived, COUNT(*)
FROM threads
GROUP BY model_provider, archived
ORDER BY model_provider, archived;
```

4. Confirm rollout files still exist:

```sql
SELECT id, model_provider, cwd, archived, rollout_path
FROM threads
WHERE model_provider <> '<current provider>';
```

5. Check whether app-server/sidebar is listing with a provider filter:

```text
modelProviders omitted/null means current source likely filters to active provider
modelProviders=[] should mean all providers in current source
```

6. Check archive view:

```text
archived=false/null shows active only
archived=true shows archived only
```

## Current Machine Specific Conclusions

Current provider in `config.toml`:

```text
model_provider = "custom"
```

Current provider definition:

```text
[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = true
base_url = redacted here
```

Current active state DB:

```text
C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

Current active DB contains:

```text
CodexPlusPlus: 485
OpenAI: 85
custom: 7
```

Since the active provider is `custom`, a default provider-filtered sidebar can show only `custom` threads. Provider A threads are not necessarily gone; they remain as rows with `model_provider = CodexPlusPlus` or `OpenAI` and their rollout paths exist.

