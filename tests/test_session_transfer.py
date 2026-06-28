import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from server import (
    CodexSessionTransfer,
    CopyRequest,
    ExportPackageRequest,
    SkillImportRequest,
    SkillPackageRequest,
)


THREAD_COLUMNS = [
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "created_at_ms",
    "updated_at_ms",
    "source",
    "thread_source",
    "agent_nickname",
    "agent_role",
    "agent_path",
    "model_provider",
    "model",
    "reasoning_effort",
    "cwd",
    "cli_version",
    "title",
    "preview",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "first_user_message",
    "archived",
    "archived_at",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "memory_mode",
]


def create_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER,
                git_sha TEXT,
                git_branch TEXT,
                git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT,
                agent_role TEXT,
                memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT,
                reasoning_effort TEXT,
                agent_path TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                preview TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_threads_provider ON threads(model_provider);
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE thread_dynamic_tools (
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                input_schema TEXT NOT NULL,
                defer_loading INTEGER NOT NULL DEFAULT 0,
                namespace TEXT,
                PRIMARY KEY(thread_id, position)
            );
            """
        )
        conn.commit()


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_rollout(
    codex_home: Path,
    thread_id: str,
    provider: str,
    *,
    archived: bool = False,
    source: object = "cli",
    parent_thread_id: str | None = None,
    stamp: str = "2026-06-13T10-00-00",
    entries: list[dict[str, object]] | None = None,
) -> Path:
    root = codex_home / ("archived_sessions" if archived else "sessions")
    path = root / "2026" / "06" / "13" / f"rollout-{stamp}-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": thread_id,
        "timestamp": "2026-06-13T10:00:00Z",
        "cwd": str(codex_home / "work"),
        "originator": "test",
        "cli_version": "test",
        "source": source,
        "model_provider": provider,
    }
    if parent_thread_id:
        payload["parent_thread_id"] = parent_thread_id
    line = {
        "timestamp": "2026-06-13T10:00:00Z",
        "item": {"type": "session_meta", "payload": payload},
    }
    rollout_entries = [line, {"item": {"type": "event_msg", "payload": {}}}]
    if entries:
        rollout_entries.extend(entries)
    path.write_text(
        "".join(compact_json(entry) + "\n" for entry in rollout_entries),
        encoding="utf-8",
    )
    return path


def insert_thread(
    db_path: Path,
    *,
    thread_id: str,
    rollout_path: Path,
    provider: str,
    title: str,
    preview: str = "preview",
    archived: bool = False,
    source: object = "cli",
    cwd: Path | None = None,
    updated_at_ms: int = 1_781_000_100_000,
) -> None:
    updated_at = updated_at_ms // 1000
    values = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "created_at": 1_781_000_000,
        "updated_at": updated_at,
        "created_at_ms": 1_781_000_000_000,
        "updated_at_ms": updated_at_ms,
        "source": source if isinstance(source, str) else compact_json(source),
        "thread_source": None,
        "agent_nickname": None,
        "agent_role": None,
        "agent_path": None,
        "model_provider": provider,
        "model": "gpt-test",
        "reasoning_effort": None,
        "cwd": str(cwd or db_path.parent.parent / "work"),
        "cli_version": "test",
        "title": title,
        "preview": preview,
        "sandbox_policy": "read-only",
        "approval_mode": "on-request",
        "tokens_used": 12,
        "first_user_message": "hello",
        "archived": 1 if archived else 0,
        "archived_at": 1_781_000_200 if archived else None,
        "git_sha": None,
        "git_branch": "main",
        "git_origin_url": None,
        "memory_mode": "enabled",
    }
    placeholders = ",".join("?" for _ in THREAD_COLUMNS)
    columns = ",".join(THREAD_COLUMNS)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            f"INSERT INTO threads ({columns}) VALUES ({placeholders})",
            [values[column] for column in THREAD_COLUMNS],
        )
        conn.commit()


class SessionTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temp.name) / ".codex"
        self.sqlite_home = self.codex_home / "sqlite"
        self.db_path = self.sqlite_home / "state_5.sqlite"
        self.switch_home = Path(self.temp.name) / "codex-provider-switch"
        create_schema(self.db_path)
        self.transfer = CodexSessionTransfer(
            codex_home=self.codex_home,
            sqlite_home=self.sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_thread(
        self,
        thread_id: str,
        provider: str = "ProviderA",
        *,
        title: str = "Thread",
        preview: str = "preview",
        archived: bool = False,
        source: object = "cli",
        parent_thread_id: str | None = None,
        cwd: Path | None = None,
        updated_at_ms: int = 1_781_000_100_000,
    ) -> Path:
        rollout_path = write_rollout(
            self.codex_home,
            thread_id,
            provider,
            archived=archived,
            source=source,
            parent_thread_id=parent_thread_id,
        )
        insert_thread(
            self.db_path,
            thread_id=thread_id,
            rollout_path=rollout_path,
            provider=provider,
            title=title,
            preview=preview,
            archived=archived,
            source=source,
            cwd=cwd,
            updated_at_ms=updated_at_ms,
        )
        return rollout_path

    def updated_ms(self, year: int, month: int, day: int, hour: int = 12) -> int:
        return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)

    def write_session_index(self, thread_id: str, thread_name: str) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        with (self.codex_home / "session_index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                compact_json(
                    {
                        "id": thread_id,
                        "thread_name": thread_name,
                        "updated_at": "2026-06-13T10:30:00Z",
                    }
                )
                + "\n"
            )

    def write_skill(self, skill_id: str, *, description: str = "Test skill", body: str = "") -> Path:
        skill_dir = self.codex_home / "skills" / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: {description}\n---\n\n# {skill_id}\n{body}\n",
            encoding="utf-8",
        )
        (skill_dir / "notes.txt").write_text(f"{skill_id} notes\n", encoding="utf-8")
        return skill_dir

    def test_lists_providers_and_thread_health_flags(self) -> None:
        existing_id = "11111111-1111-4111-8111-111111111111"
        empty_preview_id = "22222222-2222-4222-8222-222222222222"
        missing_id = "33333333-3333-4333-8333-333333333333"
        self.add_thread(existing_id, title="Existing")
        self.add_thread(empty_preview_id, title="Empty", preview="")
        missing_path = self.add_thread(missing_id, title="Missing")
        missing_path.unlink()
        self.add_thread(
            "44444444-4444-4444-8444-444444444444",
            provider="ProviderB",
            title="Other provider",
        )

        providers = self.transfer.list_providers()
        self.assertEqual(
            {item["model_provider"]: item["total"] for item in providers},
            {"ProviderA": 3, "ProviderB": 1},
        )

        threads = self.transfer.list_threads(source_provider="ProviderA", include_archived=True)
        by_id = {thread["id"]: thread for thread in threads}
        self.assertTrue(by_id[existing_id]["rollout_exists"])
        self.assertTrue(by_id[empty_preview_id]["hidden_empty_preview"])
        self.assertFalse(by_id[missing_id]["rollout_exists"])

    def test_list_threads_filters_by_project_and_updated_date(self) -> None:
        project = Path(self.temp.name) / "project-a"
        other_project = Path(self.temp.name) / "project-b"
        old_id = "11111111-1111-4111-8111-111111111111"
        match_id = "22222222-2222-4222-8222-222222222222"
        other_project_id = "33333333-3333-4333-8333-333333333333"
        self.add_thread(old_id, title="Old", cwd=project, updated_at_ms=self.updated_ms(2026, 6, 13))
        self.add_thread(match_id, title="Match", cwd=project, updated_at_ms=self.updated_ms(2026, 6, 14))
        self.add_thread(
            other_project_id,
            title="Other project",
            cwd=other_project,
            updated_at_ms=self.updated_ms(2026, 6, 14),
        )

        threads = self.transfer.list_threads(
            source_provider="ProviderA",
            include_archived=True,
            cwd=str(project),
            date_from="2026-06-14",
            date_to="2026-06-14",
        )

        self.assertEqual([thread["id"] for thread in threads], [match_id])

    def test_list_threads_applies_recent_limit_after_project_filter(self) -> None:
        project = Path(self.temp.name) / "project-a"
        other_project = Path(self.temp.name) / "project-b"
        older_id = "11111111-1111-4111-8111-111111111111"
        newest_id = "22222222-2222-4222-8222-222222222222"
        middle_id = "33333333-3333-4333-8333-333333333333"
        other_project_id = "44444444-4444-4444-8444-444444444444"
        self.add_thread(older_id, title="Older", cwd=project, updated_at_ms=self.updated_ms(2026, 6, 11))
        self.add_thread(newest_id, title="Newest", cwd=project, updated_at_ms=self.updated_ms(2026, 6, 13))
        self.add_thread(middle_id, title="Middle", cwd=project, updated_at_ms=self.updated_ms(2026, 6, 12))
        self.add_thread(
            other_project_id,
            title="Other project",
            cwd=other_project,
            updated_at_ms=self.updated_ms(2026, 6, 15),
        )

        threads = self.transfer.list_threads(
            source_provider="ProviderA",
            include_archived=True,
            cwd=str(project),
            recent_limit=2,
        )

        self.assertEqual([thread["id"] for thread in threads], [newest_id, middle_id])

    def test_thread_detail_reads_rollout_as_renderable_session_items(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        rollout_path = write_rollout(
            self.codex_home,
            thread_id,
            "ProviderA",
            entries=[
                {
                    "timestamp": "2026-06-13T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Render this session"}],
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell_command",
                        "arguments": {"command": "echo ok"},
                        "call_id": "call_1",
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "ok\n",
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Session rendered."}],
                    },
                },
            ],
        )
        insert_thread(
            self.db_path,
            thread_id=thread_id,
            rollout_path=rollout_path,
            provider="ProviderA",
            title="Renderable",
        )

        detail = self.transfer.thread_detail(thread_id)

        self.assertTrue(detail["ok"])
        self.assertEqual(detail["thread"]["id"], thread_id)
        self.assertEqual(detail["thread"]["display_title"], "Renderable")
        self.assertEqual(detail["meta"]["model_provider"], "ProviderA")
        self.assertTrue(detail["rollout"]["exists"])
        self.assertEqual(detail["rollout"]["line_count"], 6)
        self.assertEqual(
            [(item["kind"], item["role"], item["text"]) for item in detail["items"]],
            [
                ("message", "user", "Render this session"),
                ("tool_call", "tool", "shell_command"),
                ("tool_result", "tool", "ok\n"),
                ("message", "assistant", "Session rendered."),
            ],
        )

    def test_thread_detail_hides_internal_messages_and_redacts_secrets(self) -> None:
        thread_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        secret = "ghp_" + ("1" * 36)
        visible_text = f"please keep this token private: {secret}"
        rollout_path = write_rollout(
            self.codex_home,
            thread_id,
            "ProviderA",
            entries=[
                {
                    "timestamp": "2026-06-13T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": f"internal {secret}"}],
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": visible_text}],
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": visible_text,
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {
                    "timestamp": "2026-06-13T10:01:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "assistant_message",
                        "message": "done",
                    },
                },
            ],
        )
        insert_thread(
            self.db_path,
            thread_id=thread_id,
            rollout_path=rollout_path,
            provider="ProviderA",
            title="Private",
        )

        detail = self.transfer.thread_detail(thread_id)

        self.assertEqual([item["role"] for item in detail["items"]], ["user", "assistant"])
        rendered_text = json.dumps(detail["items"], ensure_ascii=False)
        self.assertNotIn(secret, rendered_text)
        self.assertNotIn("ghp_", rendered_text)
        self.assertIn("[redacted]", rendered_text)

    def test_thread_detail_paginates_renderable_items(self) -> None:
        thread_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        rollout_path = write_rollout(
            self.codex_home,
            thread_id,
            "ProviderA",
            entries=[
                {
                    "timestamp": "2026-06-13T10:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"message {index:03d}"}],
                    },
                }
                for index in range(125)
            ],
        )
        insert_thread(
            self.db_path,
            thread_id=thread_id,
            rollout_path=rollout_path,
            provider="ProviderA",
            title="Paged",
        )

        detail = self.transfer.thread_detail(thread_id, item_offset=20, item_limit=10)

        self.assertEqual(detail["item_offset"], 20)
        self.assertEqual(detail["item_limit"], 10)
        self.assertEqual(detail["item_total"], 125)
        self.assertTrue(detail["has_more"])
        self.assertEqual(len(detail["items"]), 10)
        self.assertEqual(detail["items"][0]["text"], "message 020")
        self.assertEqual(detail["items"][-1]["text"], "message 029")

    def test_preview_rejects_detached_child_thread(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                    "agent_path": None,
                    "agent_nickname": "Ada",
                    "agent_role": "worker",
                }
            }
        }
        self.add_thread(parent_id, title="Parent")
        self.add_thread(child_id, title="Child", source=source, parent_thread_id=parent_id)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent_id, child_id, "open"),
            )
            conn.commit()

        plan = self.transfer.preview_copy(
            CopyRequest("ProviderA", "ProviderB", [child_id], False, True)
        )

        self.assertFalse(plan["can_execute"])
        self.assertIn("parent", " ".join(plan["errors"]).lower())

    def test_copy_single_thread_rewrites_rollout_and_inserts_db_row(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, title="Source thread")

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        item = result["items"][0]
        self.assertNotEqual(item["source_id"], item["target_id"])
        self.assertTrue(Path(item["dest_rollout_path"]).exists())
        with closing(sqlite3.connect(self.db_path)) as conn:
            original_provider = conn.execute(
                "SELECT model_provider FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()[0]
            copied = conn.execute(
                "SELECT model_provider, rollout_path FROM threads WHERE id = ?",
                (item["target_id"],),
            ).fetchone()
        self.assertEqual(original_provider, "ProviderA")
        self.assertEqual(copied[0], "ProviderB")
        self.assertEqual(copied[1], item["dest_rollout_path"])

        first_line = Path(item["dest_rollout_path"]).read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(first_line)["item"]["payload"]
        self.assertEqual(payload["id"], item["target_id"])
        self.assertEqual(payload["model_provider"], "ProviderB")

    def test_copy_preserves_session_index_renamed_thread_name(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, title="Database title")
        self.write_session_index(thread_id, "Renamed title")

        before = self.transfer.list_threads(source_provider="ProviderA", include_archived=True)
        self.assertEqual(before[0]["display_title"], "Renamed title")

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        index_entries = {}
        for line in (self.codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            index_entries[item["id"]] = item

        self.assertEqual(index_entries[target_id]["thread_name"], "Renamed title")
        self.assertEqual(index_entries[target_id]["updated_at"], "2026-06-13T10:30:00Z")

    def test_copy_descendant_tree_remaps_edges_and_source_metadata(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "depth": 1,
                    "agent_path": None,
                    "agent_nickname": "Ada",
                    "agent_role": "worker",
                }
            }
        }
        self.add_thread(parent_id, title="Parent")
        self.add_thread(child_id, title="Child", source=source, parent_thread_id=parent_id)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent_id, child_id, "open"),
            )
            conn.execute(
                """
                INSERT INTO thread_dynamic_tools
                    (thread_id, position, name, description, input_schema)
                VALUES (?, 0, 'tool', 'desc', '{}')
                """,
                (child_id,),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [parent_id], True, True)
        )

        self.assertTrue(result["ok"], result)
        id_map = {item["source_id"]: item["target_id"] for item in result["items"]}
        with closing(sqlite3.connect(self.db_path)) as conn:
            edge = conn.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?",
                (id_map[child_id],),
            ).fetchone()
            child_source = conn.execute(
                "SELECT source FROM threads WHERE id = ?", (id_map[child_id],)
            ).fetchone()[0]
            tool_count = conn.execute(
                "SELECT COUNT(*) FROM thread_dynamic_tools WHERE thread_id = ?",
                (id_map[child_id],),
            ).fetchone()[0]

        self.assertEqual(edge, (id_map[parent_id], id_map[child_id]))
        self.assertEqual(
            json.loads(child_source)["subagent"]["thread_spawn"]["parent_thread_id"],
            id_map[parent_id],
        )
        self.assertEqual(tool_count, 1)

        child_copy = next(item for item in result["items"] if item["source_id"] == child_id)
        first_line = Path(child_copy["dest_rollout_path"]).read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(first_line)["item"]["payload"]
        self.assertEqual(payload["parent_thread_id"], id_map[parent_id])
        self.assertEqual(
            payload["source"]["subagent"]["thread_spawn"]["parent_thread_id"],
            id_map[parent_id],
        )

    def test_copy_blocks_when_codex_process_is_running(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, title="Source thread")
        transfer = CodexSessionTransfer(
            codex_home=self.codex_home,
            sqlite_home=self.sqlite_home,
            process_checker=lambda: [{"name": "Codex", "pid": 123}],
        )

        result = transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider = 'ProviderB'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_kill_blocking_processes_only_targets_detected_blockers(self) -> None:
        processes = [
            {"name": "codex.exe", "pid": 101},
            {"name": "codex-plus-plus-manager.exe", "pid": 102},
            {"name": "python.exe", "pid": 103},
        ]
        killed: list[int] = []

        def terminate(pid: int) -> dict[str, object]:
            killed.append(pid)
            processes[:] = [process for process in processes if process["pid"] != pid]
            return {"ok": True, "pid": pid}

        transfer = CodexSessionTransfer(
            codex_home=self.codex_home,
            sqlite_home=self.sqlite_home,
            process_checker=lambda: processes,
            process_terminator=terminate,
        )

        result = transfer.kill_blocking_processes()

        self.assertTrue(result["ok"])
        self.assertEqual(killed, [101, 102])
        self.assertEqual(result["killed_count"], 2)
        self.assertEqual(result["remaining_blocking_processes"], [])
        self.assertEqual(processes, [{"name": "python.exe", "pid": 103}])

    def test_repair_session_index_from_manifests_backfills_target_names(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.write_session_index(source_id, "Renamed title")
        self.transfer.manifest_dir.mkdir(parents=True, exist_ok=True)
        (self.transfer.manifest_dir / "copy-old.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "items": [
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "source_rollout_path": "source.jsonl",
                            "dest_rollout_path": "target.jsonl",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = self.transfer.repair_session_index_from_manifests()

        self.assertTrue(result["ok"])
        self.assertEqual(result["repaired_count"], 1)
        entries = {}
        for line in (self.codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            entries[item["id"]] = item
        self.assertEqual(entries[target_id]["thread_name"], "Renamed title")

    def test_target_providers_include_live_config_and_codex_plus_presets(self) -> None:
        self.add_thread("11111111-1111-4111-8111-111111111111", "custom", title="Live")
        self.add_thread("22222222-2222-4222-8222-222222222222", "OpenAI", title="OpenAI")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "custom"
model = "gpt-5.5"

[model_providers.custom]
name = "custom"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
requires_openai_auth = true
""",
            encoding="utf-8",
        )
        self.switch_home.mkdir(parents=True, exist_ok=True)
        (self.switch_home / "preset-overrides.json").write_text(
            json.dumps(
                {
                    "customPresets": [
                        {
                            "id": "fucheers-newapi",
                            "name": "Fucheers (NewAPI)",
                            "configText": """
model_provider = "Fucheers"
model = "gpt-5.5"

[model_providers.Fucheers]
name = "Fucheers"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
requires_openai_auth = true
""",
                            "authText": '{"OPENAI_API_KEY":"secret-not-returned"}',
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        providers = self.transfer.list_target_providers()
        by_value = {provider["value"]: provider for provider in providers}

        self.assertIn("custom", by_value)
        self.assertTrue(by_value["custom"]["current"])
        self.assertEqual(by_value["custom"]["model"], "gpt-5.5")
        self.assertEqual(by_value["custom"]["provider_name"], "custom")
        self.assertIn("Fucheers", by_value)
        self.assertEqual(by_value["Fucheers"]["preset_id"], "fucheers-newapi")
        self.assertNotIn("authText", json.dumps(providers))
        self.assertNotIn("secret-not-returned", json.dumps(providers))

    def test_session_stats_group_by_project_and_provider(self) -> None:
        first = self.add_thread(
            "11111111-1111-4111-8111-111111111111",
            "ProviderA",
            title="A",
        )
        second = self.add_thread(
            "22222222-2222-4222-8222-222222222222",
            "ProviderB",
            title="B",
            archived=True,
        )
        second.unlink()

        stats = self.transfer.session_stats()

        self.assertEqual(stats["totals"]["total"], 2)
        self.assertEqual(stats["totals"]["archived"], 1)
        self.assertEqual(stats["totals"]["missing_rollouts"], 1)
        self.assertEqual(stats["by_provider"]["ProviderA"]["active"], 1)
        self.assertEqual(stats["by_provider"]["ProviderB"]["archived"], 1)
        self.assertEqual(len(stats["by_project"]), 1)
        self.assertEqual(stats["by_project"][0]["total"], 2)

    def test_export_package_preserves_selected_sessions_grouped_by_project(self) -> None:
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        first_project = Path(self.temp.name) / "projectA"
        second_project = Path(self.temp.name) / "projectB"
        self.add_thread(first_id, title="First", cwd=first_project)
        self.add_thread(second_id, title="Second", cwd=second_project)
        self.write_session_index(first_id, "Renamed first")

        result = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [first_id, second_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        package_path = Path(result["package_path"])
        self.assertTrue(package_path.exists())
        self.assertEqual(result["thread_count"], 2)
        with zipfile.ZipFile(package_path) as package:
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
            names = set(package.namelist())

        self.assertEqual(manifest["source_provider"], "ProviderA")
        grouped = {
            project["cwd"]: {thread["id"] for thread in project["threads"]}
            for project in manifest["projects"]
        }
        self.assertEqual(grouped[str(first_project)], {first_id})
        self.assertEqual(grouped[str(second_project)], {second_id})
        self.assertIn("sqlite/state_5.sqlite", names)
        self.assertIn("session_index.jsonl", names)
        self.assertTrue(any(name.startswith("sessions/") and first_id in name for name in names))
        self.assertTrue(any(name.startswith("sessions/") and second_id in name for name in names))

    def test_imported_package_can_copy_into_target_machine_with_same_provider(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, provider="ProviderA", title="Portable")
        self.write_session_index(source_id, "Portable renamed")
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )

        target_codex_home = Path(self.temp.name) / "target" / ".codex"
        target_sqlite_home = target_codex_home / "sqlite"
        create_schema(target_sqlite_home / "state_5.sqlite")
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )

        loaded = target.load_transfer_package(Path(export["package_path"]))
        self.assertTrue(loaded["ok"], loaded)
        providers = target.package_status()["providers"]
        self.assertEqual([provider["model_provider"] for provider in providers], ["ProviderA"])

        preview = target.preview_imported_package_copy(
            CopyRequest("ProviderA", "ProviderA", [source_id], False, True)
        )
        self.assertTrue(preview["can_execute"], preview)
        result = target.copy_imported_package_threads(
            CopyRequest("ProviderA", "ProviderA", [source_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        self.assertNotEqual(target_id, source_id)
        self.assertTrue(Path(result["items"][0]["dest_rollout_path"]).is_relative_to(target_codex_home))
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            copied = conn.execute(
                "SELECT model_provider, rollout_path FROM threads WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(copied[0], "ProviderA")
        self.assertTrue(Path(copied[1]).exists())

        index_entries = {}
        for line in (target_codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            index_entries[item["id"]] = item
        self.assertEqual(index_entries[target_id]["thread_name"], "Portable renamed")

    def test_imported_package_copy_rewrites_cwd_for_target_project(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_cwd = Path("D:/Users/Administrator/Desktop/2025-CommercialOrder/OnGoingOrders/VideoCaption")
        target_cwd = "D:\\Users\\hd\\Desktop\\2026-CommercialOrder\\OnGoingOrders\\VideoCaption"
        self.add_thread(source_id, provider="ProviderA", title="Portable", cwd=source_cwd)
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )

        target_codex_home = Path(self.temp.name) / "target-cwd" / ".codex"
        target_sqlite_home = target_codex_home / "sqlite"
        create_schema(target_sqlite_home / "state_5.sqlite")
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )
        loaded = target.load_transfer_package(Path(export["package_path"]))
        self.assertTrue(loaded["ok"], loaded)

        request = CopyRequest(
            "ProviderA",
            "ProviderA",
            [source_id],
            False,
            True,
            {str(source_cwd): target_cwd},
        )
        preview = target.preview_imported_package_copy(request)
        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["items"][0]["source_cwd"], str(source_cwd))
        self.assertEqual(preview["items"][0]["target_cwd"], target_cwd)
        self.assertTrue(preview["items"][0]["cwd_rewritten"])

        result = target.copy_imported_package_threads(request)
        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            copied = conn.execute(
                "SELECT cwd, rollout_path FROM threads WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(copied[0], target_cwd)
        first_rollout_line = Path(copied[1]).read_text(encoding="utf-8").splitlines()[0]
        rollout_payload = json.loads(first_rollout_line)["item"]["payload"]
        self.assertEqual(rollout_payload["cwd"], target_cwd)

    def test_uploaded_package_file_is_saved_and_loaded_as_source(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, provider="ProviderA", title="Portable")
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )

        target_codex_home = Path(self.temp.name) / "uploaded-target" / ".codex"
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_codex_home / "sqlite",
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )

        uploaded = target.load_uploaded_transfer_package(
            "portable-session.zip",
            Path(export["package_path"]).read_bytes(),
        )

        self.assertTrue(uploaded["ok"], uploaded)
        self.assertTrue(uploaded["loaded"])
        saved_path = Path(uploaded["package_path"])
        self.assertTrue(saved_path.exists())
        self.assertTrue(saved_path.is_relative_to(target_codex_home / "session-transfer" / "packages"))
        providers = target.package_status()["providers"]
        self.assertEqual([provider["model_provider"] for provider in providers], ["ProviderA"])

    def test_export_skills_package_contains_only_selected_skills(self) -> None:
        self.write_skill("alpha-skill", description="Alpha")
        self.write_skill("beta-skill", description="Beta")

        result = self.transfer.export_skills_package(
            SkillPackageRequest(["alpha-skill"])
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["skill_count"], 1)
        package_path = Path(result["package_path"])
        self.assertTrue(package_path.exists())
        with zipfile.ZipFile(package_path) as package:
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
            names = set(package.namelist())

        self.assertEqual(manifest["format"], "codex-skill-transfer-package")
        self.assertEqual([skill["id"] for skill in manifest["skills"]], ["alpha-skill"])
        self.assertIn("skills/alpha-skill/SKILL.md", names)
        self.assertIn("skills/alpha-skill/notes.txt", names)
        self.assertFalse(any(name.startswith("skills/beta-skill/") for name in names))

    def test_loaded_skills_package_imports_selected_skills_and_respects_overwrite(self) -> None:
        self.write_skill("alpha-skill", description="Alpha")
        self.write_skill("beta-skill", description="Beta")
        export = self.transfer.export_skills_package(
            SkillPackageRequest(["alpha-skill", "beta-skill"])
        )

        target_codex_home = Path(self.temp.name) / "target-skills" / ".codex"
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_codex_home / "sqlite",
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )
        existing = target_codex_home / "skills" / "alpha-skill"
        existing.mkdir(parents=True, exist_ok=True)
        (existing / "SKILL.md").write_text("existing alpha\n", encoding="utf-8")

        loaded = target.load_skills_package(Path(export["package_path"]))
        self.assertTrue(loaded["ok"], loaded)
        package_skills = target.list_package_skills()
        self.assertEqual({skill["id"] for skill in package_skills}, {"alpha-skill", "beta-skill"})
        self.assertTrue(next(skill for skill in package_skills if skill["id"] == "alpha-skill")["installed"])

        preview = target.preview_import_skills(SkillImportRequest(["alpha-skill", "beta-skill"], False))
        self.assertFalse(preview["can_execute"])
        self.assertIn("already exists", " ".join(preview["errors"]))

        result = target.import_skills(SkillImportRequest(["beta-skill"], False))
        self.assertTrue(result["ok"], result)
        self.assertTrue((target_codex_home / "skills" / "beta-skill" / "SKILL.md").exists())
        self.assertEqual((existing / "SKILL.md").read_text(encoding="utf-8"), "existing alpha\n")

        overwrite = target.import_skills(SkillImportRequest(["alpha-skill"], True))
        self.assertTrue(overwrite["ok"], overwrite)
        self.assertIn("description: Alpha", (existing / "SKILL.md").read_text(encoding="utf-8"))

    def test_uploaded_skills_package_file_is_saved_and_loaded_as_source(self) -> None:
        self.write_skill("alpha-skill", description="Alpha")
        export = self.transfer.export_skills_package(
            SkillPackageRequest(["alpha-skill"])
        )

        target_codex_home = Path(self.temp.name) / "uploaded-skills-target" / ".codex"
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_codex_home / "sqlite",
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )

        uploaded = target.load_uploaded_skills_package(
            "portable-skills.zip",
            Path(export["package_path"]).read_bytes(),
        )

        self.assertTrue(uploaded["ok"], uploaded)
        self.assertTrue(uploaded["loaded"])
        saved_path = Path(uploaded["package_path"])
        self.assertTrue(saved_path.exists())
        self.assertTrue(saved_path.is_relative_to(target_codex_home / "session-transfer" / "skill-packages"))
        package_skills = target.list_package_skills()
        self.assertEqual([skill["id"] for skill in package_skills], ["alpha-skill"])


if __name__ == "__main__":
    unittest.main()
