# Codex 会话与 Session 存储机制调研说明

日期：2026-06-13

范围：Codex app、Codex CLI、本地 session/transcript、SQLite 会话索引、三方供应商切换后左侧会话列表变化的原因。

安全边界：本次调研除创建本文档和临时拉取官方源码到 `%TEMP%` 外，均为只读。没有写入 Codex 配置、auth、session 或 SQLite。所有 token、key、password、session、cookie、auth 字段只记录字段名，不记录值。

## 结论先行

Codex 的“会话正文”和“会话列表索引”是两层东西：

```text
会话正文：   C:\Users\Administrator\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl
会话索引：   C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

你这台机器当前有：

```text
CODEX_SQLITE_HOME=C:\Users\Administrator\.codex\sqlite
```

所以当前活跃的 SQLite 状态库是：

```text
C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

而不是旧的：

```text
C:\Users\Administrator\.codex\state_5.sqlite
```

`state_5.sqlite` 的核心表是 `threads`。这张表里有 `model_provider` 字段，并且官方源码给它建了专门索引 `idx_threads_provider`。也就是说，Provider 是 Codex 会话索引里的一级过滤维度。

最关键的源码证据是 app-server 的 `thread/list`。当前官方源码中，如果客户端没有传 `modelProviders`，服务端会默认过滤为当前配置里的 `model_provider_id`。因此，当你从供应商 A 切到供应商 B 后，左侧列表很可能只列 B 的 `threads.model_provider`，A 的会话文件仍在磁盘上，但被默认 provider filter 排除了。

这就是“切换供应商后 A 会话不见”的最短因果链：

```text
切换 provider -> 当前 config.model_provider_id 变成 B
左侧调用 thread/list -> 未显式要求 all providers
服务端默认 modelProviders = [B]
SQLite 查询加上 threads.model_provider IN (B)
A 的行被过滤掉
rollout JSONL 文件仍然存在
```

## 官方依据

使用过的官方来源：

- Codex authentication：<https://developers.openai.com/codex/auth>
- Config and state locations：<https://developers.openai.com/codex/config-advanced#config-and-state-locations>
- Environment variables：<https://developers.openai.com/codex/environment-variables>
- CLI resume / local transcripts：<https://developers.openai.com/codex/cli/features#resuming-conversations>
- Codex app features：<https://developers.openai.com/codex/app/features>
- Codex app-server：<https://developers.openai.com/codex/app-server>
- 官方源码 `openai/codex`，commit `f297b9f07de10c7d8b9ed284b674d06cc5ff7723`
  - `STATE_DB_FILENAME = state_5.sqlite`，`SQLITE_HOME_ENV = CODEX_SQLITE_HOME`：<https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/src/lib.rs>
  - `threads` 表结构和 `idx_threads_provider`：<https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/migrations/0001_threads.sql>
  - SQLite list filters：<https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/state/src/runtime/threads.rs>
  - app-server `thread/list` provider 默认过滤：<https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/app-server/src/request_processors/thread_processor.rs>
  - `session_index.jsonl` 追加式命名索引：<https://github.com/openai/codex/blob/f297b9f07de10c7d8b9ed284b674d06cc5ff7723/codex-rs/rollout/src/session_index.rs>

需要特别注意：app-server README 里写 `modelProviders` unset/null/empty 会包含所有 provider，但当前源码显示 `None` 会默认当前 provider，只有显式空数组才是不做 provider 过滤。这是文档和源码之间的差异，后续做工具时必须实测当前桌面 app 发出的请求。

## 本机目录结构

### Codex Home

主目录：

```text
C:\Users\Administrator\.codex
```

关键内容：

```text
.codex\
  config.toml
  auth.json                         敏感，只记录存在
  history.jsonl
  session_index.jsonl
  sessions\
  archived_sessions\
  sqlite\
    state_5.sqlite                  当前活跃 thread 索引
    logs_2.sqlite
    goals_1.sqlite
    memories_1.sqlite
  state_5.sqlite                    旧 root-level thread 索引
  logs_2.sqlite
  goals_1.sqlite
  memories_1.sqlite
  backups_state\provider-sync\
```

官方文档说明 `CODEX_HOME` 默认是 `~/.codex`，用于存放 config、auth、logs、sessions、skills、standalone package metadata 等。Windows app 和 Windows native CLI 使用 `%USERPROFILE%\.codex`。如果在 WSL 里跑 CLI，则默认用 WSL 的 Linux home，除非显式设置 `CODEX_HOME` 指向 Windows 目录。

### Codex App / Chromium UserData

观察到的桌面壳和 WebView 数据：

```text
C:\Users\Administrator\AppData\Roaming\Codex
C:\Users\Administrator\AppData\Roaming\Codex\web\Codex
C:\Users\Administrator\AppData\Roaming\Codex\web\Codex (Beta)
C:\Users\Administrator\AppData\Local\OpenAI\Codex
C:\Users\Administrator\AppData\Local\Codex
```

这些目录主要是 Chromium/WebView 缓存、History、Cookies、Preferences、Local State、运行时二进制、Sentry、日志等。没有发现 Codex thread/session/provider 的业务表。真正的会话业务状态仍然在 `.codex`。

### Provider Switcher

观察到的 provider 切换工具目录：

```text
C:\Users\Administrator\AppData\Roaming\codex-provider-switch
```

重要文件：

```text
preset-overrides.json        provider presets，含 configText/authText 字段
gmn-session.json             account/accessToken/refreshToken/expiresAt 字段
gwen-usage-account.json      username/password/loginUrl/usageUrl 字段
```

这些不是 Codex 官方会话存储，但会影响当前生效 provider/auth/config，因此会间接影响 app-server 的 provider filter。

## 会话正文：rollout JSONL

活跃会话目录：

```text
C:\Users\Administrator\.codex\sessions
```

本机统计：

```text
500 个 rollout JSONL
2026/04: 157
2026/05: 222
2026/06: 121
总大小约 5.3 GB
```

归档会话目录：

```text
C:\Users\Administrator\.codex\archived_sessions
```

本机统计：

```text
77 个 rollout JSONL
总大小约 440 MB
```

每个 rollout 文件的第一条记录是 `session_meta`，典型结构：

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
    "model_provider": "CodexPlusPlus|OpenAI|custom"
  }
}
```

会话的真实历史、模型输出、工具调用、命令输出、事件等正文都在 rollout JSONL 里。SQLite 的 `threads` 表主要是索引和展示 metadata，不是完整 transcript。

## SQLite 状态库

官方源码常量：

```text
STATE_DB_FILENAME = state_5.sqlite
SQLITE_HOME_ENV = CODEX_SQLITE_HOME
```

官方文档说明：

```text
CODEX_SQLITE_HOME 默认等于 CODEX_HOME
sqlite_home config 选项优先于 CODEX_SQLITE_HOME
```

因此解析顺序应理解为：

```text
sqlite_home config > CODEX_SQLITE_HOME > CODEX_HOME
```

### 当前活跃库

路径：

```text
C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

统计：

```text
threads: 577
CodexPlusPlus: 485
OpenAI: 85
custom: 7
active: 500
archived: 77
rollout_path 存在: 577/577
```

当前这条 `/goal` 线程和三个子 agent 都在这个库里，`model_provider = custom`。

### 旧 root-level 库

路径：

```text
C:\Users\Administrator\.codex\state_5.sqlite
```

统计：

```text
threads: 578
CodexPlusPlus: 486
OpenAI: 89
custom: 3
active: 501
archived: 77
rollout_path 存在: 573/578
```

这个库没有当前调研线程，最后更新时间早于当前活跃库。它更像旧位置或旧运行时曾经使用的状态库。

### provider-sync 备份

路径形态：

```text
C:\Users\Administrator\.codex\backups_state\provider-sync\YYYYMMDDHHMMSS\db\state_5.sqlite
```

2026-06-02 的备份全部是同 schema 快照，且 provider 全是 `CodexPlusPlus`，数量从 474 增到 486。这说明 provider 切换/同步工具曾经围绕 state DB 做过备份或迁移。

## threads 表结构

`threads` 是会话列表最重要的表。当前列包括：

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

重要索引：

```text
idx_threads_provider on threads(model_provider)
idx_threads_source on threads(source)
idx_threads_archived on threads(archived)
idx_threads_created_at
idx_threads_updated_at
idx_threads_created_at_ms
idx_threads_updated_at_ms
idx_threads_archived_cwd_created_at_ms
idx_threads_archived_cwd_updated_at_ms
idx_thread_spawn_edges_parent_status
idx_thread_dynamic_tools_thread
```

结论：

```text
model_provider 是 indexed row metadata
cwd 是 project/workspace 过滤维度
archived 控制归档视图
source/thread_source 控制 CLI、VS Code、exec、subagent 等来源
preview 为空的线程会被当前 SQLite list filter 排除
rollout_path 指向真实 JSONL transcript
```

没有发现独立的 `providers`、`accounts`、`profiles`、`projects`、`conversations` 业务表。账号没有作为本地 SQLite 分区出现。`remote_control_enrollments.account_id` 存在但当前表为空。

## thread/list 机制

app-server 的 `thread/list` 支持：

```text
cursor
limit
sortKey
sortDirection
modelProviders
sourceKinds
archived
cwd
useStateDbOnly
searchTerm
```

当前源码里的 provider 过滤逻辑是：

```text
modelProviders omitted/null -> 默认当前 config.model_provider_id
modelProviders = []         -> 不做 provider filter，显示所有 provider
modelProviders = ["A"]      -> 只显示 provider A
```

SQLite 过滤还会加：

```text
archived = 0，除非 archived=true
threads.preview <> ''
threads.source IN (...)
threads.model_provider IN (...)
threads.cwd IN (...)
title/preview searchTerm
```

所以会话“看不见”不等于文件丢失。常见隐藏原因：

```text
provider filter 排除了它
cwd/project filter 排除了它
sourceKinds 排除了它
archived=1 但没打开 archived view
preview 为空
当前 app-server 正在读另一套 sqlite_home
```

## session_index.jsonl 的作用

路径：

```text
C:\Users\Administrator\.codex\session_index.jsonl
```

本机统计：

```text
453 行
433 个 unique thread id
字段：id, thread_name, updated_at
存在重复 id
```

官方源码说明它是 append-only 的 thread name index，最新记录胜出。它不保存 provider、cwd、archived、rollout_path，也不是完整会话列表。不能把它当 sidebar 的权威数据源。

## history.jsonl 的作用

本机 `history.jsonl` 有 prompt 历史，字段形态类似：

```text
session_id
ts
text
```

它更像 composer prompt history 或 CLI prompt 历史，不是完整 transcript，也不是 sidebar 权威索引。

## 为什么切换供应商后 A 会话不显示

综合官方源码和本机证据，最可能原因是：

```text
当前 config.toml:
model_provider = "custom"

当前 state_5.sqlite:
CodexPlusPlus: 485
OpenAI: 85
custom: 7

如果左侧 thread/list 默认只列当前 provider:
只显示 custom 的 7 条相关线程
CodexPlusPlus/OpenAI 的线程仍在库里和 JSONL 里，但被过滤掉
```

这和你的现象高度吻合：

```text
上一次用 A -> 当前 provider=A -> 侧栏显示 A 相关会话
这一次换 B -> 当前 provider=B -> 侧栏默认只显示 B 相关会话
A 的会话没有同步加载，但不是一定丢失
```

另外，这台机器还存在两个 state DB：

```text
旧: C:\Users\Administrator\.codex\state_5.sqlite
新: C:\Users\Administrator\.codex\sqlite\state_5.sqlite
```

如果某个工具、旧 CLI、provider switcher 或 app-server 进程读的是不同 sqlite_home，也会出现“同一批 rollout 文件，侧边栏索引不一致”的情况。

## 后续工具设计建议

你后续要做“可选择不同供应商预览，任意会话迁移/复制”，建议拆成三个层级。

### 1. 跨 provider 预览

优先不改文件。

如果走 app-server：

```text
modelProviders=[]                         显示全部 provider
modelProviders=["CodexPlusPlus"]          只看 CodexPlusPlus
modelProviders=["OpenAI", "custom"]       多 provider 预览
archived=false/true                       活跃/归档切换
cwd=...                                   项目过滤
```

如果直读 SQLite：

```text
先解析 active sqlite_home
读 threads 表
按 provider/cwd/source/archived/updated_at_ms 聚合
校验 rollout_path 是否存在
需要时读取 rollout 第一条 session_meta 做二次校验
```

不要用 `session_index.jsonl` 当主索引。

### 2. 单向复制

复制时不要改原始线程。建议复制单位：

```text
rollout JSONL
threads row metadata
session_index name entry，可选
thread_spawn_edges，如果复制 parent/subagent 树
thread_dynamic_tools，可选
goals/memories/logs，默认不复制
```

如果要让 A 的某条会话作为 B provider 下的一条新会话出现，建议：

```text
生成新 thread id
生成新 rollout 文件名
修改复制品第一条 session_meta.id
修改复制品第一条 session_meta.model_provider = B
插入新的 threads.id
插入新的 threads.model_provider = B
threads.rollout_path 指向新 rollout
保持原始 A 会话不动
```

不建议同一个 `threads.id` 同时属于两个 provider，因为 `threads.id` 是主键。强行复用 id 会牵连 session_index、goals、logs、unread state、spawn edges、thread-workspace-root hints。

### 3. 双向同步

双向同步比复制复杂很多，需要明确：

```text
同名不同 id 怎么处理
同 id 不同历史怎么处理
provider metadata 以哪边为准
archived 状态以哪边为准
cwd 路径 D:\... 和 \\?\D:\... 如何归一化
子 agent 树如何 remap
是否同步 goals/memories/logs
app 正在运行时如何避免 WAL/锁竞争
```

建议第一版只做：

```text
只读预览
单向复制
生成 manifest
不删除、不覆盖、不迁移 auth
```

manifest 至少记录：

```text
source_thread_id
dest_thread_id
source_provider
dest_provider
source_rollout_path
dest_rollout_path
copied_at
source_sqlite_home
dest_sqlite_home
```

## 操作风险

1. SQLite 有 WAL/SHM。Codex app 运行中直接复制 DB 文件可能漏掉 WAL 里的最新数据。
2. 修改 DB 前必须关闭 Codex app/app-server，或者优先通过 app-server API。
3. auth/config/provider preset 不应和 session migration 混在一起迁移。
4. `logs_2.sqlite` 可能很大，只是诊断日志，不是会话可见性的必要条件。
5. `memories_1.sqlite` 可能包含生成摘要或敏感上下文，默认不要同步。
6. `preview = ''` 的线程可能在当前 list query 中被排除。
7. 官方 README 与当前源码对 `modelProviders` 默认行为不完全一致，必须以实际桌面版本请求为准。

## 排查清单

当切换 provider 后会话消失，按这个顺序查：

```sql
SELECT model_provider, archived, COUNT(*)
FROM threads
GROUP BY model_provider, archived
ORDER BY model_provider, archived;
```

确认当前 active sqlite home：

```text
sqlite_home config
CODEX_SQLITE_HOME
CODEX_HOME
```

确认会话文件是否存在：

```sql
SELECT id, model_provider, archived, cwd, rollout_path
FROM threads
WHERE model_provider <> '<current provider>'
ORDER BY updated_at_ms DESC;
```

确认是否只是 archived：

```sql
SELECT archived, COUNT(*)
FROM threads
GROUP BY archived;
```

确认 provider filter：

```text
thread/list omitted modelProviders -> 当前源码倾向过滤当前 provider
thread/list modelProviders=[]      -> 当前源码倾向显示全部 provider
```

## 本机最终判断

当前机器上，A/B provider 会话没有同步出现在左侧，最可能不是 session 文件丢失，而是 sidebar/list 层的 provider filter 和 sqlite_home 分裂共同导致。

最值得优先验证的点：

```text
桌面 app 当前调用 thread/list 时到底传没传 modelProviders
如果没传，当前源码会默认只列 config.model_provider_id
如果显式传 []，理论上应能列所有 provider
```

如果后续做工具，第一步建议先做只读 provider selector，通过同一 active `state_5.sqlite` 按 `model_provider` 聚合并预览，再考虑复制。

