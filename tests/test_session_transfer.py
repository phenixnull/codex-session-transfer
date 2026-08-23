import json
import os
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import (
    CodexSessionTransfer,
    CopyRequest,
    ExportPackageRequest,
    MAX_PREVIEW_PAGE_SIZE,
    PREVIEW_TITLE_LIMIT,
    RebindRequest,
    SkillImportRequest,
    SkillPackageRequest,
    WorkspaceMapping,
    make_handler,
    parent_process_is_alive,
    watch_parent_process,
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
    created_at_ms: int = 1_781_000_000_000,
    updated_at_ms: int = 1_781_000_100_000,
    thread_source: str | None = None,
    first_user_message: str = "hello",
) -> None:
    created_at = created_at_ms // 1000
    updated_at = updated_at_ms // 1000
    values = {
        "id": thread_id,
        "rollout_path": str(rollout_path),
        "created_at": created_at,
        "updated_at": updated_at,
        "created_at_ms": created_at_ms,
        "updated_at_ms": updated_at_ms,
        "source": source if isinstance(source, str) else compact_json(source),
        "thread_source": thread_source,
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
        "first_user_message": first_user_message,
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

    def test_default_sqlite_home_prefers_newer_root_state_database(self) -> None:
        codex_home = Path(self.temp.name) / "detected-root" / ".codex"
        legacy_db = codex_home / "sqlite" / "state_5.sqlite"
        root_db = codex_home / "state_5.sqlite"
        create_schema(legacy_db)
        create_schema(root_db)
        os.utime(legacy_db, (1_000, 1_000))
        os.utime(root_db, (2_000, 2_000))

        with patch.dict(os.environ, {"CODEX_SQLITE_HOME": ""}):
            transfer = CodexSessionTransfer(
                codex_home=codex_home,
                provider_switch_home=self.switch_home,
                process_checker=lambda: [],
            )

        self.assertEqual(transfer.db_path, root_db)

    def test_default_sqlite_home_keeps_legacy_state_database_when_it_is_the_only_one(self) -> None:
        codex_home = Path(self.temp.name) / "detected-legacy" / ".codex"
        legacy_db = codex_home / "sqlite" / "state_5.sqlite"
        create_schema(legacy_db)

        with patch.dict(os.environ, {"CODEX_SQLITE_HOME": ""}):
            transfer = CodexSessionTransfer(
                codex_home=codex_home,
                provider_switch_home=self.switch_home,
                process_checker=lambda: [],
            )

        self.assertEqual(transfer.db_path, legacy_db)

    def test_copy_request_parses_workspace_mapping(self) -> None:
        target_root = Path(self.temp.name) / "target-root"
        source_cwd = "C:\\old\\ProjectA"
        target_project = target_root / "ProjectA-renamed"

        request = CopyRequest.from_json(
            {
                "source_provider": "ProviderA",
                "target_provider": "ProviderB",
                "thread_ids": ["thread-a"],
                "workspace_mapping": {
                    "mode": "preserve_projects",
                    "target_root": str(target_root),
                    "overrides": {source_cwd: str(target_project)},
                },
            }
        )

        self.assertIsNotNone(request.workspace_mapping)
        self.assertEqual(request.workspace_mapping.mode, "preserve_projects")
        self.assertEqual(request.workspace_mapping.target_root, str(target_root))
        self.assertEqual(
            request.workspace_mapping.overrides,
            {source_cwd: str(target_project)},
        )

    def test_copy_request_parses_overwrite_selections_and_explicit_skip(self) -> None:
        request = CopyRequest.from_json(
            {
                "source_provider": "ProviderA",
                "target_provider": "ProviderB",
                "thread_ids": ["source-a", "source-b"],
                "overwrite": True,
                "overwrite_selections": {
                    "source-a": "target-a",
                    "source-b": None,
                },
            }
        )

        self.assertEqual(
            request.overwrite_selections,
            {"source-a": "target-a", "source-b": None},
        )

        with self.assertRaises(ValueError):
            CopyRequest.from_json(
                {
                    "source_provider": "ProviderA",
                    "target_provider": "ProviderB",
                    "thread_ids": ["source-a"],
                    "overwrite_selections": {"source-a": 42},
                }
            )

    def test_copy_request_rejects_invalid_workspace_mapping_shapes(self) -> None:
        base = {
            "source_provider": "ProviderA",
            "target_provider": "ProviderB",
            "thread_ids": ["thread-a"],
        }

        invalid_mappings = [
            "not-an-object",
            {"mode": "unknown", "target_root": str(Path(self.temp.name))},
            {
                "mode": "preserve_projects",
                "target_root": str(Path(self.temp.name)),
                "overrides": [],
            },
            {"mode": "single_workspace", "target_root": "relative/path"},
        ]
        for workspace_mapping in invalid_mappings:
            with self.subTest(workspace_mapping=workspace_mapping):
                with self.assertRaises(ValueError):
                    CopyRequest.from_json({**base, "workspace_mapping": workspace_mapping})

    def test_copy_request_rejects_legacy_and_workspace_mapping_together(self) -> None:
        target_root = Path(self.temp.name) / "target-root"

        with self.assertRaises(ValueError):
            CopyRequest.from_json(
                {
                    "source_provider": "ProviderA",
                    "target_provider": "ProviderB",
                    "thread_ids": ["thread-a"],
                    "cwd_map": {"C:\\old\\ProjectA": str(target_root / "ProjectA")},
                    "workspace_mapping": {
                        "mode": "preserve_projects",
                        "target_root": str(target_root),
                    },
                }
            )

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
        created_at_ms: int = 1_781_000_000_000,
        updated_at_ms: int = 1_781_000_100_000,
        thread_source: str | None = None,
        first_user_message: str = "hello",
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
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            thread_source=thread_source,
            first_user_message=first_user_message,
        )
        return rollout_path

    def loaded_package_target(
        self,
        package_path: Path,
        name: str,
    ) -> tuple[CodexSessionTransfer, Path]:
        target_codex_home = Path(self.temp.name) / name / ".codex"
        target_sqlite_home = target_codex_home / "sqlite"
        create_schema(target_sqlite_home / "state_5.sqlite")
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )
        loaded = target.load_transfer_package(package_path)
        self.assertTrue(loaded["ok"], loaded)
        return target, target_sqlite_home

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

    def test_thread_list_compacts_display_text_without_compacting_detail(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        long_title = "Title: " + "t" * 2000
        long_preview = "Preview: " + "p" * 2000
        long_name = "Name: " + "n" * 2000
        self.add_thread(
            thread_id,
            title=long_title,
            preview=long_preview,
        )
        self.write_session_index(thread_id, long_name)

        listed = self.transfer.list_threads(
            source_provider="ProviderA",
            include_archived=True,
        )
        self.assertEqual(len(listed), 1)
        self.assertLessEqual(len(listed[0]["title"]), PREVIEW_TITLE_LIMIT)
        self.assertLessEqual(len(listed[0]["preview"]), PREVIEW_TITLE_LIMIT)
        self.assertLessEqual(len(listed[0]["thread_name"]), PREVIEW_TITLE_LIMIT)
        self.assertLessEqual(len(listed[0]["display_title"]), PREVIEW_TITLE_LIMIT)

        detail = self.transfer.thread_detail(thread_id)
        self.assertEqual(detail["thread"]["title"], long_title)
        self.assertEqual(detail["thread"]["preview"], long_preview)
        self.assertEqual(detail["thread"]["thread_name"], long_name)

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

    def test_rebind_preserves_session_id_rollout_tail_and_session_index(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        rollout_path = self.add_thread(thread_id, provider="ProviderA", title="Rebind me")
        self.write_session_index(thread_id, "Renamed rebind session")
        original_rollout = rollout_path.read_bytes()
        original_index = (self.codex_home / "session_index.jsonl").read_text(encoding="utf-8")

        request = RebindRequest("ProviderA", "ProviderB", [thread_id], False, True)
        preview = self.transfer.preview_rebind(request)
        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["items"][0]["source_id"], thread_id)
        self.assertEqual(preview["items"][0]["target_id"], thread_id)

        progress: list[dict[str, object]] = []
        result = self.transfer.rebind_threads(request, progress.append)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rebound_count"], 1)
        self.assertEqual(result["session_index_entries"], 0)
        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertTrue(Path(result["rollout_backup_paths"][0]).exists())
        self.assertIn("rebinding", [event.get("phase") for event in progress])

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT id, model_provider, rollout_path FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        self.assertEqual(row[0], thread_id)
        self.assertEqual(row[1], "ProviderB")
        self.assertEqual(row[2], str(rollout_path))

        updated_lines = rollout_path.read_bytes().splitlines(keepends=True)
        original_lines = original_rollout.splitlines(keepends=True)
        self.assertEqual(updated_lines[1:], original_lines[1:])
        self.assertEqual(
            json.loads(updated_lines[0].decode("utf-8"))["item"]["payload"]["model_provider"],
            "ProviderB",
        )
        self.assertEqual(
            (self.codex_home / "session_index.jsonl").read_text(encoding="utf-8"),
            original_index,
        )

    def test_rebind_rolls_back_database_and_rollouts_after_partial_failure(self) -> None:
        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        first_rollout = self.add_thread(first_id, provider="ProviderA", title="First")
        second_rollout = self.add_thread(second_id, provider="ProviderA", title="Second")
        original_first = first_rollout.read_bytes()
        original_second = second_rollout.read_bytes()
        original_writer = self.transfer._write_rollout_provider
        calls = 0

        def write_first_then_fail(path: Path, target_provider: str) -> None:
            nonlocal calls
            if calls == 0:
                calls += 1
                original_writer(path, target_provider)
                return
            raise OSError("simulated rollout write failure")

        with patch.object(self.transfer, "_write_rollout_provider", side_effect=write_first_then_fail):
            result = self.transfer.rebind_threads(
                RebindRequest("ProviderA", "ProviderB", [first_id, second_id], False, True)
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertIn("simulated rollout write failure", result["errors"][0])
        self.assertEqual(first_rollout.read_bytes(), original_first)
        self.assertEqual(second_rollout.read_bytes(), original_second)
        with closing(sqlite3.connect(self.db_path)) as conn:
            providers = conn.execute(
                "SELECT id, model_provider FROM threads ORDER BY id"
            ).fetchall()
        self.assertEqual(providers, [(first_id, "ProviderA"), (second_id, "ProviderA")])

    def test_rebind_is_blocked_while_codex_is_running(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, provider="ProviderA")
        self.transfer.process_checker = lambda: [{"name": "codex.exe", "pid": 1234}]

        result = self.transfer.rebind_threads(
            RebindRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertIn("provider switcher", result["errors"][0])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT model_provider FROM threads WHERE id = ?", (thread_id,)
                ).fetchone()[0],
                "ProviderA",
            )

    def test_copy_accepts_current_rollout_names_with_composite_session_ids(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_path = self.add_thread(thread_id, title="Current rollout name")
        composite_path = source_path.with_name(
            "rollout-2026-08-22T16-53-01-01a02832-bc7a-7163-a988-11b4d47af310_"
            "01a028ac-64bb-7380-adfc-61dd04ab421f.jsonl"
        )
        source_path.rename(composite_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (str(composite_path), thread_id),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        destination = Path(result["items"][0]["dest_rollout_path"])
        self.assertTrue(destination.exists())
        self.assertEqual(
            destination.name,
            f"rollout-2026-08-22T16-53-01-{result['items'][0]['target_id']}.jsonl",
        )

    def test_copy_single_thread_allows_same_provider(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, title="Same provider source")

        preview = self.transfer.preview_copy(
            CopyRequest("ProviderA", "ProviderA", [thread_id], False, True)
        )
        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["items"][0]["source_provider"], "ProviderA")
        self.assertEqual(preview["items"][0]["target_provider"], "ProviderA")

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderA", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["items"][0]["source_id"], result["items"][0]["target_id"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            provider_count = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider = 'ProviderA'"
            ).fetchone()[0]
        self.assertEqual(provider_count, 2)

    def test_path_is_within_accepts_windows_extended_path_prefix(self) -> None:
        sessions_root = Path(r"C:\Users\hd\.codex\sessions")
        rollout = Path(
            r"\\?\C:\Users\hd\.codex\sessions\2026\06\04\rollout-"
            r"2026-06-04T21-35-39-c431cb07-1fcf-4919-9414-0b35ef850f11.jsonl"
        )
        lookalike = Path(
            r"\\?\C:\Users\hd\.codex\sessions-evil\rollout-"
            r"2026-06-04T21-35-39-c431cb07-1fcf-4919-9414-0b35ef850f11.jsonl"
        )
        other_drive = Path(
            r"\\?\D:\Users\hd\.codex\sessions\rollout-"
            r"2026-06-04T21-35-39-c431cb07-1fcf-4919-9414-0b35ef850f11.jsonl"
        )
        unc_root = Path(r"\\server\share\.codex\sessions")
        extended_unc_rollout = Path(
            r"\\?\UNC\server\share\.codex\sessions\rollout-"
            r"2026-06-04T21-35-39-c431cb07-1fcf-4919-9414-0b35ef850f11.jsonl"
        )

        self.assertTrue(self.transfer._path_is_within(rollout, sessions_root))
        self.assertFalse(self.transfer._path_is_within(lookalike, sessions_root))
        self.assertFalse(self.transfer._path_is_within(other_drive, sessions_root))
        self.assertTrue(self.transfer._path_is_within(extended_unc_rollout, unc_root))

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are Windows-only")
    def test_mirror_accepts_existing_target_rollout_with_extended_path(self) -> None:
        target_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        target_path = self.add_thread(
            target_id,
            provider="ProviderB",
            title="Extended target rollout",
        ).resolve()
        extended_target_path = Path(f"\\\\?\\{target_path}")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (str(extended_target_path), target_id),
            )
            conn.commit()

        paths = self.transfer._mirror_target_rollout_paths(
            {
                "_target_rows": {
                    target_id: {"rollout_path": str(extended_target_path)},
                }
            }
        )

        self.assertEqual(paths, [extended_target_path.resolve()])

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are Windows-only")
    def test_full_mirror_supports_extended_source_and_target_rollout_paths(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        source_path = self.add_thread(
            source_id,
            provider="ProviderA",
            title="Extended source rollout",
        ).resolve()
        target_path = self.add_thread(
            target_id,
            provider="ProviderB",
            title="Extended target rollout",
        ).resolve()
        extended_source_path = Path(f"\\\\?\\{source_path}")
        extended_target_path = Path(f"\\\\?\\{target_path}")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (
                    (str(extended_source_path), source_id),
                    (str(extended_target_path), target_id),
                ),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                False,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(extended_target_path.exists())
        self.assertTrue(extended_source_path.exists())
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT model_provider, rollout_path FROM threads WHERE id = ?",
                (source_id,),
            ).fetchone()
        self.assertEqual(row, ("ProviderB", str(extended_source_path)))

    def test_full_mirror_replaces_every_target_session_without_matching(self) -> None:
        source_active = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_archived = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        old_target = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        target_only = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        unrelated = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        source_paths = {
            source_active: self.add_thread(
                source_active,
                provider="ProviderA",
                title="Latest active",
            ),
            source_archived: self.add_thread(
                source_archived,
                provider="ProviderA",
                title="Latest archived",
                archived=True,
            ),
        }
        target_paths = {
            old_target: self.add_thread(
                old_target,
                provider="ProviderB",
                title="Stale matching-looking session",
            ),
            target_only: self.add_thread(
                target_only,
                provider="ProviderB",
                title="Target only",
            ),
        }
        self.add_thread(unrelated, provider="ProviderC", title="Leave untouched")
        self.write_session_index(source_active, "Newest active name")
        self.write_session_index(source_archived, "Newest archived name")
        self.write_session_index(old_target, "Old target name")
        source_bytes = {thread_id: path.read_bytes() for thread_id, path in source_paths.items()}

        preview = self.transfer.preview_copy(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                False,
                mirror_target=True,
                preview_limit=1,
            )
        )

        self.assertTrue(preview["can_execute"], preview)
        self.assertTrue(preview["mirror_target"])
        self.assertEqual(preview["item_total"], 2)
        self.assertEqual(preview["replaced_target_count"], 2)
        self.assertEqual(len(preview["items"]), 1)
        self.assertTrue(preview["has_more"])
        self.assertEqual(preview["overwrite_ambiguity_count"], 0)

        events: list[dict[str, object]] = []
        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                False,
                mirror_target=True,
            ),
            progress_callback=events.append,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["mirror_target"])
        self.assertEqual(result["replaced_target_count"], 2)
        self.assertEqual(result["deleted_target_rollout_count"], 2)
        self.assertEqual(result["rollout_backup_count"], 4)
        self.assertEqual(result["source_rollout_backup_count"], 2)
        self.assertEqual(result["target_rollout_backup_count"], 2)
        self.assertEqual(result["rebound_source_count"], 2)
        self.assertTrue(Path(result["backup_directory"]).is_dir())
        self.assertTrue((Path(result["backup_directory"]) / "backup-manifest.json").is_file())
        for path in target_paths.values():
            self.assertFalse(path.exists())
        for thread_id, path in source_paths.items():
            self.assertNotEqual(path.read_bytes(), source_bytes[thread_id])
            self.assertIn(b'"model_provider":"ProviderB"', path.read_bytes())

        backup_manifest = json.loads(
            (Path(result["backup_directory"]) / "backup-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(backup_manifest["source_session_ids"]), set(source_paths))
        backed_up_by_original = {
            item["original_path"]: Path(item["backup_path"])
            for item in backup_manifest["rollout_backups"]
        }
        for thread_id, path in source_paths.items():
            self.assertEqual(backed_up_by_original[str(path)].read_bytes(), source_bytes[thread_id])

        target_ids = {item["target_id"] for item in result["items"]}
        self.assertEqual(target_ids, {source_active, source_archived})
        self.assertTrue(
            all(item["source_id"] == item["target_id"] for item in result["items"])
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            source_rows = conn.execute(
                "SELECT id, title, archived FROM threads WHERE model_provider = 'ProviderA' ORDER BY title"
            ).fetchall()
            target_rows = conn.execute(
                "SELECT id, title, archived FROM threads WHERE model_provider = 'ProviderB' ORDER BY title"
            ).fetchall()
            unrelated_count = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE id = ? AND model_provider = 'ProviderC'",
                (unrelated,),
            ).fetchone()[0]
        self.assertEqual(source_rows, [])
        self.assertEqual(
            [(row[1], row[2]) for row in target_rows],
            [("Latest active", 0), ("Latest archived", 1)],
        )
        self.assertEqual(unrelated_count, 1)

        index_entries = self.transfer._load_session_index()
        self.assertFalse({old_target, target_only}.intersection(index_entries))
        self.assertTrue(target_ids.issubset(index_entries))
        self.assertEqual(
            {index_entries[thread_id]["thread_name"] for thread_id in target_ids},
            {"Newest active name", "Newest archived name"},
        )
        phases = [event.get("phase") for event in events]
        for phase in (
            "backing_up",
            "clearing",
            "copying",
            "indexing",
            "verifying",
            "committing",
            "cleaning",
            "done",
        ):
            self.assertIn(phase, phases)

    def test_full_mirror_rewrites_every_persisted_provider_setting_and_identity(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_path = self.add_thread(
            source_id,
            provider="ProviderA",
            title="Routing history",
        )
        records = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()]
        records.extend(
            [
                {
                    "item": {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {
                                "model": "gpt-test",
                                "model_provider_id": "ProviderA",
                                "cwd": str(self.codex_home / "work"),
                            },
                        },
                    }
                },
                {
                    "item": {
                        "type": "event_msg",
                        "payload": {
                            "type": "session_configured",
                            "session_id": source_id,
                            "thread_id": source_id,
                            "model_provider_id": "ProviderA",
                            "rollout_path": str(source_path),
                        },
                    }
                },
                {
                    "type": "session_meta",
                    "payload": {
                        "meta": {
                            "id": source_id,
                            "session_id": source_id,
                            "model_provider": "ProviderA",
                            "cwd": str(self.codex_home / "work"),
                        },
                        "git": None,
                    },
                },
            ]
        )
        source_path.write_text(
            "".join(compact_json(record) + "\n" for record in records),
            encoding="utf-8",
        )

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                True,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        self.assertEqual(target_id, source_id)
        with closing(sqlite3.connect(self.db_path)) as conn:
            target_path = Path(
                conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?", (target_id,)
                ).fetchone()[0]
            )
        copied = [json.loads(line) for line in target_path.read_text(encoding="utf-8").splitlines()]
        session_meta_count = 0
        settings_count = 0
        configured_count = 0
        for record in copied:
            item = record.get("item") if isinstance(record.get("item"), dict) else record
            item_type = item.get("type")
            payload = item.get("payload")
            if item_type == "session_meta":
                session_meta_count += 1
                metadata = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
                self.assertEqual(metadata["id"], target_id)
                if "session_id" in metadata:
                    self.assertEqual(metadata["session_id"], target_id)
                self.assertEqual(metadata["model_provider"], "ProviderB")
            elif item_type == "event_msg" and payload.get("type") == "thread_settings_applied":
                settings_count += 1
                self.assertEqual(
                    payload["thread_settings"]["model_provider_id"], "ProviderB"
                )
            elif item_type == "event_msg" and payload.get("type") == "session_configured":
                configured_count += 1
                self.assertEqual(payload["session_id"], target_id)
                self.assertEqual(payload["thread_id"], target_id)
                self.assertEqual(payload["model_provider_id"], "ProviderB")
                self.assertEqual(payload["rollout_path"], str(target_path))
        self.assertEqual(session_meta_count, 2)
        self.assertEqual(settings_count, 1)
        self.assertEqual(configured_count, 1)
        self.assertEqual(target_path, source_path)
        self.assertNotIn('"model_provider":"ProviderA"', source_path.read_text(encoding="utf-8"))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM threads WHERE model_provider = 'ProviderA'"
                ).fetchone()[0],
                0,
            )

    def test_full_mirror_cleans_stale_and_duplicate_global_sidebar_references(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.add_thread(source_id, provider="ProviderA", title="Current source")
        self.add_thread(target_id, provider="ProviderB", title="Old target")
        self.write_session_index(source_id, "Current source")
        self.write_session_index(source_id, "Current source (latest)")
        self.write_session_index(target_id, "Old target")

        global_state = {
            "projectless-thread-ids": [source_id, target_id, target_id],
            "pinned-thread-ids": [target_id],
            "thread-project-assignments": {
                source_id: {"projectKind": "local"},
                target_id: {"projectKind": "local"},
            },
            "thread-writable-roots": {target_id: ["C:/old"]},
            "sidebar-project-thread-orders": {
                "project-a": {"threadIds": [target_id, source_id, source_id]},
                "project-b": {"threadIds": [target_id]},
            },
            "electron-persisted-atom-state": {
                "thread-descriptions-v1": {
                    source_id: "Current source",
                    target_id: "Old target",
                },
                "thread-client-id-v1:local%3A" + target_id: "client-old",
                "thread-reference-capability:" + target_id: "cap-old",
            },
        }
        self.transfer.global_state_path.write_text(
            json.dumps(global_state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        original_global_state = self.transfer.global_state_path.read_text(encoding="utf-8")

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                True,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        cleanup = result["global_state_cleanup"]
        self.assertGreater(cleanup["removed_thread_references"], 0)
        self.assertGreater(cleanup["deduplicated_sidebar_ids"], 0)
        backup_path = Path(result["global_state_backup"])
        self.assertEqual(backup_path.read_text(encoding="utf-8"), original_global_state)

        cleaned = json.loads(self.transfer.global_state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cleaned["sidebar-project-thread-orders"],
            {"project-a": {"threadIds": [source_id]}, "project-b": {"threadIds": []}},
        )
        self.assertEqual(cleaned["projectless-thread-ids"], [source_id])
        self.assertNotIn(target_id, cleaned["thread-project-assignments"])
        self.assertNotIn(target_id, cleaned["electron-persisted-atom-state"]["thread-descriptions-v1"])
        self.assertNotIn(
            "thread-client-id-v1:local%3A" + target_id,
            cleaned["electron-persisted-atom-state"],
        )

        index_lines = (self.transfer.session_index_path.read_text(encoding="utf-8")).splitlines()
        index_ids = [json.loads(line)["id"] for line in index_lines]
        self.assertEqual(index_ids.count(source_id), 1)
        self.assertNotIn(target_id, index_ids)

    def test_repair_session_state_removes_stale_sidebar_entries_and_duplicate_index_lines(self) -> None:
        live_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        stale_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        missing_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.add_thread(live_id, provider="ProviderA", title="Live")
        missing_path = self.add_thread(missing_id, provider="ProviderA", title="Missing rollout")
        missing_path.unlink()
        self.write_session_index(live_id, "Live")
        self.write_session_index(live_id, "Live latest")
        self.write_session_index(stale_id, "Deleted")
        self.write_session_index(missing_id, "Missing rollout")
        self.transfer.global_state_path.write_text(
            json.dumps(
                {
                    "sidebar-project-thread-orders": {
                        "project": {"threadIds": [stale_id, missing_id, live_id, live_id]}
                    },
                    "electron-persisted-atom-state": {
                        "thread-descriptions-v1": {
                            stale_id: "Deleted",
                            missing_id: "Missing rollout",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = self.transfer.repair_session_index_from_manifests()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["session_index_deduplicated"], 3)
        self.assertGreater(result["global_state_cleanup"]["removed_thread_references"], 0)
        cleaned = json.loads(self.transfer.global_state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            cleaned["sidebar-project-thread-orders"]["project"]["threadIds"],
            [live_id],
        )
        index_ids = [
            json.loads(line)["id"]
            for line in self.transfer.session_index_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(index_ids, [live_id])

    def test_full_mirror_restores_global_state_when_verification_fails(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.add_thread(source_id, provider="ProviderA", title="Source")
        self.add_thread(target_id, provider="ProviderB", title="Target")
        original_global_state = json.dumps(
            {
                "sidebar-project-thread-orders": {
                    "project": {"threadIds": [target_id]}
                }
            },
            ensure_ascii=False,
        )
        self.transfer.global_state_path.write_text(original_global_state, encoding="utf-8")

        with patch.object(
            self.transfer,
            "_verify_mirror",
            side_effect=RuntimeError("forced global state rollback"),
        ):
            result = self.transfer.copy_threads(
                CopyRequest(
                    "ProviderA",
                    "ProviderB",
                    [],
                    False,
                    True,
                    mirror_target=True,
                )
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(
            self.transfer.global_state_path.read_text(encoding="utf-8"),
            original_global_state,
        )

    def test_full_mirror_remaps_paginated_history_base_id_and_byte_offset(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        parent_path = self.add_thread(parent_id, provider="A", title="History parent")
        child_path = self.add_thread(
            child_id,
            provider="A",
            title="History child",
            parent_thread_id=parent_id,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')",
                (parent_id, child_id),
            )
            conn.commit()
        child_records = [
            json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()
        ]
        child_records[0]["item"]["payload"]["history_base"] = {
            "thread_id": parent_id,
            "end_ordinal_exclusive": 2,
            "end_byte_offset": parent_path.stat().st_size,
        }
        child_path.write_text(
            "".join(compact_json(record) + "\n" for record in child_records),
            encoding="utf-8",
        )
        original_parent_size = parent_path.stat().st_size

        result = self.transfer.copy_threads(
            CopyRequest(
                "A",
                "ProviderWithLongIdentifier",
                [],
                False,
                True,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        id_map = {item["source_id"]: item["target_id"] for item in result["items"]}
        self.assertEqual(id_map, {parent_id: parent_id, child_id: child_id})
        with closing(sqlite3.connect(self.db_path)) as conn:
            copied_paths = {
                row[0]: Path(row[1])
                for row in conn.execute(
                    "SELECT id, rollout_path FROM threads WHERE model_provider = ?",
                    ("ProviderWithLongIdentifier",),
                )
            }
        copied_child = json.loads(
            copied_paths[id_map[child_id]].read_text(encoding="utf-8").splitlines()[0]
        )["item"]["payload"]
        history_base = copied_child["history_base"]
        self.assertEqual(history_base["thread_id"], id_map[parent_id])
        self.assertEqual(
            history_base["end_byte_offset"],
            copied_paths[id_map[parent_id]].stat().st_size,
        )
        self.assertNotEqual(history_base["end_byte_offset"], original_parent_size)

    def test_full_mirror_preserves_reverted_rollout_history_base(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        replacement_rollout_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        base_path = self.add_thread(thread_id, provider="ProviderA", title="Reverted thread")
        base_bytes = base_path.read_bytes()
        replacement_path = base_path.with_name(
            f"{base_path.stem}_{replacement_rollout_id}.jsonl"
        )
        replacement_records = [
            json.loads(line) for line in base_path.read_text(encoding="utf-8").splitlines()
        ]
        replacement_meta = replacement_records[0]["item"]["payload"]
        replacement_meta["history_mode"] = "paginated"
        replacement_meta["history_base"] = {
            "thread_id": thread_id,
            "end_ordinal_exclusive": 2,
            "end_byte_offset": len(base_bytes),
        }
        replacement_path.write_text(
            "".join(compact_json(record) + "\n" for record in replacement_records),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (str(replacement_path), thread_id),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                True,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        copied_meta = json.loads(
            replacement_path.read_text(encoding="utf-8").splitlines()[0]
        )["item"]["payload"]
        self.assertEqual(copied_meta["model_provider"], "ProviderB")
        self.assertEqual(copied_meta["history_base"]["thread_id"], thread_id)
        self.assertEqual(copied_meta["history_base"]["end_byte_offset"], len(base_bytes))
        self.assertEqual(base_path.read_bytes(), base_bytes)

    def test_copy_preserves_external_reverted_rollout_history_base(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        replacement_rollout_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        base_path = self.add_thread(thread_id, provider="ProviderA", title="Reverted thread")
        base_size = base_path.stat().st_size
        replacement_path = base_path.with_name(
            f"{base_path.stem}_{replacement_rollout_id}.jsonl"
        )
        replacement_records = [
            json.loads(line) for line in base_path.read_text(encoding="utf-8").splitlines()
        ]
        replacement_meta = replacement_records[0]["item"]["payload"]
        replacement_meta["history_mode"] = "paginated"
        replacement_meta["history_base"] = {
            "thread_id": thread_id,
            "end_ordinal_exclusive": 2,
            "end_byte_offset": base_size,
        }
        replacement_path.write_text(
            "".join(compact_json(record) + "\n" for record in replacement_records),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE threads SET rollout_path = ? WHERE id = ?",
                (str(replacement_path), thread_id),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        target_path = Path(result["items"][0]["dest_rollout_path"])
        copied_meta = json.loads(
            target_path.read_text(encoding="utf-8").splitlines()[0]
        )["item"]["payload"]
        self.assertNotEqual(result["items"][0]["target_id"], thread_id)
        self.assertEqual(copied_meta["id"], result["items"][0]["target_id"])
        self.assertEqual(copied_meta["history_base"]["thread_id"], thread_id)
        self.assertEqual(copied_meta["history_base"]["end_byte_offset"], base_size)

    def test_full_mirror_copies_durable_sidecars_and_clears_runtime_projections(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        old_target = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.add_thread(source_id, provider="ProviderA", title="Source")
        self.add_thread(old_target, provider="ProviderB", title="Target")

        sidecars = {
            "goals_1.sqlite": """
                CREATE TABLE thread_goals (
                    thread_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    objective TEXT NOT NULL
                );
                CREATE TABLE thread_goal_continuation_deferrals (
                    thread_id TEXT PRIMARY KEY REFERENCES thread_goals(thread_id) ON DELETE CASCADE
                );
            """,
            "memories_1.sqlite": """
                CREATE TABLE stage1_outputs (
                    thread_id TEXT PRIMARY KEY,
                    raw_memory TEXT NOT NULL
                );
            """,
            "thread_history_1.sqlite": """
                CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, PRIMARY KEY(thread_id, turn_id));
                CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, PRIMARY KEY(thread_id, item_id));
                CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER);
            """,
            "queue_1.sqlite": """
                CREATE TABLE queued_items (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
                CREATE TABLE queued_thread_revisions (revision INTEGER, thread_id TEXT UNIQUE);
            """,
            "logs_2.sqlite": """
                CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT);
            """,
        }
        for filename, schema in sidecars.items():
            with closing(sqlite3.connect(self.sqlite_home / filename)) as conn:
                conn.executescript(schema)
                conn.commit()

        with closing(sqlite3.connect(self.sqlite_home / "goals_1.sqlite")) as conn:
            conn.execute("INSERT INTO thread_goals VALUES (?, 'goal-source', 'latest goal')", (source_id,))
            conn.execute("INSERT INTO thread_goal_continuation_deferrals VALUES (?)", (source_id,))
            conn.execute("INSERT INTO thread_goals VALUES (?, 'goal-target', 'old goal')", (old_target,))
            conn.commit()
        with closing(sqlite3.connect(self.sqlite_home / "memories_1.sqlite")) as conn:
            conn.execute("INSERT INTO stage1_outputs VALUES (?, 'source memory')", (source_id,))
            conn.execute("INSERT INTO stage1_outputs VALUES (?, 'target memory')", (old_target,))
            conn.commit()
        with closing(sqlite3.connect(self.sqlite_home / "thread_history_1.sqlite")) as conn:
            for thread_id in (source_id, old_target):
                conn.execute("INSERT INTO thread_turns VALUES (?, 'turn')", (thread_id,))
                conn.execute("INSERT INTO thread_items VALUES (?, 'item')", (thread_id,))
                conn.execute(
                    "INSERT INTO thread_history_projection_state VALUES (?, 123)",
                    (thread_id,),
                )
            conn.commit()
        with closing(sqlite3.connect(self.sqlite_home / "queue_1.sqlite")) as conn:
            conn.execute("INSERT INTO queued_items VALUES ('source-item', ?)", (source_id,))
            conn.execute("INSERT INTO queued_items VALUES ('target-item', ?)", (old_target,))
            conn.execute("INSERT INTO queued_thread_revisions VALUES (1, ?)", (source_id,))
            conn.execute("INSERT INTO queued_thread_revisions VALUES (1, ?)", (old_target,))
            conn.commit()
        with closing(sqlite3.connect(self.sqlite_home / "logs_2.sqlite")) as conn:
            conn.execute("INSERT INTO logs VALUES (1, ?)", (source_id,))
            conn.execute("INSERT INTO logs VALUES (2, ?)", (old_target,))
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [],
                False,
                True,
                mirror_target=True,
            )
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        self.assertEqual(target_id, source_id)
        self.assertEqual(set(result["database_backups"]), set(sidecars) | {"state_5.sqlite"})
        with closing(sqlite3.connect(self.sqlite_home / "goals_1.sqlite")) as conn:
            goals = conn.execute(
                "SELECT thread_id, goal_id, objective FROM thread_goals ORDER BY thread_id"
            ).fetchall()
            deferrals = conn.execute(
                "SELECT thread_id FROM thread_goal_continuation_deferrals ORDER BY thread_id"
            ).fetchall()
        self.assertEqual(goals, [(source_id, "goal-source", "latest goal")])
        self.assertEqual(deferrals, [(source_id,)])
        with closing(sqlite3.connect(self.sqlite_home / "memories_1.sqlite")) as conn:
            memories = conn.execute(
                "SELECT thread_id, raw_memory FROM stage1_outputs ORDER BY thread_id"
            ).fetchall()
        self.assertEqual(
            set(memories),
            {(source_id, "source memory")},
        )
        with closing(sqlite3.connect(self.sqlite_home / "thread_history_1.sqlite")) as conn:
            for table in (
                "thread_turns",
                "thread_items",
                "thread_history_projection_state",
            ):
                ids = {row[0] for row in conn.execute(f"SELECT thread_id FROM {table}")}
                self.assertEqual(ids, set())
        with closing(sqlite3.connect(self.sqlite_home / "queue_1.sqlite")) as conn:
            self.assertEqual(
                {row[0] for row in conn.execute("SELECT thread_id FROM queued_items")},
                {source_id},
            )
            self.assertEqual(
                {row[0] for row in conn.execute("SELECT thread_id FROM queued_thread_revisions")},
                {source_id},
            )
        with closing(sqlite3.connect(self.sqlite_home / "logs_2.sqlite")) as conn:
            self.assertEqual(
                {row[0] for row in conn.execute("SELECT thread_id FROM logs")},
                {source_id},
            )

    def test_full_mirror_writes_rollouts_concurrently(self) -> None:
        source_ids = [
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ]
        for index, source_id in enumerate(source_ids):
            self.add_thread(source_id, provider="ProviderA", title=f"Concurrent {index}")
        barrier = threading.Barrier(len(source_ids))
        original_writer = self.transfer._write_rollout_copy

        def synchronized_writer(*args, **kwargs):
            barrier.wait(timeout=3)
            return original_writer(*args, **kwargs)

        with patch.object(
            self.transfer,
            "_write_rollout_copy",
            side_effect=synchronized_writer,
        ):
            result = self.transfer.copy_threads(
                CopyRequest(
                    "ProviderA",
                    "ProviderB",
                    [],
                    False,
                    True,
                    mirror_target=True,
                )
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["items"]), len(source_ids))

    def test_full_mirror_rolls_back_after_concurrent_writer_failure(self) -> None:
        first_source = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_source = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        old_target = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        first_path = self.add_thread(first_source, provider="ProviderA", title="First")
        second_path = self.add_thread(second_source, provider="ProviderA", title="Second")
        target_path = self.add_thread(old_target, provider="ProviderB", title="Old target")
        self.write_session_index(first_source, "First source")
        self.write_session_index(old_target, "Old target")
        original_index = self.transfer._snapshot_session_index()
        original_first = first_path.read_bytes()
        original_second = second_path.read_bytes()
        original_target = target_path.read_bytes()
        session_files_before = set((self.codex_home / "sessions").rglob("*.jsonl"))
        original_writer = self.transfer._write_rollout_copy
        first_finished = threading.Event()

        def write_one_then_fail(source_path: Path, *args, **kwargs):
            if source_path == second_path:
                first_finished.wait(timeout=3)
                raise OSError("simulated concurrent mirror failure")
            result = original_writer(source_path, *args, **kwargs)
            if source_path == first_path:
                first_finished.set()
            return result

        with patch.object(
            self.transfer,
            "_write_rollout_copy",
            side_effect=write_one_then_fail,
        ):
            result = self.transfer.copy_threads(
                CopyRequest(
                    "ProviderA",
                    "ProviderB",
                    [],
                    False,
                    True,
                    mirror_target=True,
                )
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertIn("simulated concurrent mirror failure", result["errors"][0])
        self.assertEqual(first_path.read_bytes(), original_first)
        self.assertEqual(second_path.read_bytes(), original_second)
        self.assertEqual(target_path.read_bytes(), original_target)
        self.assertEqual(self.transfer._snapshot_session_index(), original_index)
        self.assertEqual(
            set((self.codex_home / "sessions").rglob("*.jsonl")),
            session_files_before,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            providers = dict(
                conn.execute("SELECT id, model_provider FROM threads ORDER BY id").fetchall()
            )
        self.assertEqual(providers[first_source], "ProviderA")
        self.assertEqual(providers[second_source], "ProviderA")
        self.assertEqual(providers[old_target], "ProviderB")

    def test_local_copy_overwrite_replaces_matching_conversation_after_provider_switch(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        destination_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        project = Path(self.temp.name) / "shared-project"
        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Current conversation",
            preview="Newest transcript",
            cwd=project,
            first_user_message="Keep this thread in sync",
        )
        destination_rollout = write_rollout(
            self.codex_home,
            destination_id,
            "ProviderB",
            entries=[{"item": {"type": "old_event", "payload": {"old": True}}}],
        )
        insert_thread(
            self.db_path,
            thread_id=destination_id,
            rollout_path=destination_rollout,
            provider="ProviderB",
            title="Current conversation",
            cwd=project,
            first_user_message="Keep this thread in sync",
        )

        preview = self.transfer.preview_copy(
            CopyRequest("ProviderA", "ProviderB", [source_id], False, True, overwrite=True)
        )

        self.assertTrue(preview["can_execute"], preview)
        item = preview["items"][0]
        self.assertEqual(item["target_id"], destination_id)
        self.assertTrue(item["overwritten"])
        self.assertEqual(item["overwrite_match"], "project, title, first message")

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [source_id], False, True, overwrite=True)
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, title, model_provider, rollout_path FROM threads WHERE model_provider = 'ProviderB'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], destination_id)
        self.assertEqual(rows[0][1], "Current conversation")
        self.assertNotIn('"old":true', Path(rows[0][3]).read_text(encoding="utf-8"))

    def test_local_copy_overwrite_requires_manual_choice_for_ambiguous_targets(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_one = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        target_two = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        project = Path(self.temp.name) / "ambiguous-project"
        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Repeated conversation",
            preview="Source transcript",
            cwd=project,
            first_user_message="The same opening message",
        )
        for target_id, marker in ((target_one, "one"), (target_two, "two")):
            target_rollout = write_rollout(
                self.codex_home,
                target_id,
                "ProviderB",
                entries=[{"item": {"type": "old_event", "payload": {"marker": marker}}}],
            )
            insert_thread(
                self.db_path,
                thread_id=target_id,
                rollout_path=target_rollout,
                provider="ProviderB",
                title="Repeated conversation",
                preview=f"Target transcript {marker}",
                cwd=project,
                first_user_message="The same opening message",
            )

        request = CopyRequest(
            "ProviderA",
            "ProviderB",
            [source_id],
            False,
            True,
            overwrite=True,
        )
        preview = self.transfer.preview_copy(request)

        self.assertFalse(preview["can_execute"], preview)
        self.assertEqual(preview["overwrite_ambiguity_count"], 1)
        self.assertEqual(
            {candidate["id"] for candidate in preview["overwrite_ambiguities"][0]["candidates"]},
            {target_one, target_two},
        )
        self.assertIn("require a target selection", " ".join(preview["errors"]))

        selected = self.transfer.preview_copy(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [source_id],
                False,
                True,
                overwrite=True,
                overwrite_selections={source_id: target_two},
            )
        )
        self.assertTrue(selected["can_execute"], selected)
        self.assertEqual(selected["overwrite_ambiguity_count"], 0)
        self.assertEqual(selected["items"][0]["target_id"], target_two)
        self.assertEqual(selected["items"][0]["overwrite_match"], "manual selection")
        self.assertTrue(selected["items"][0]["overwritten"])

        copied = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [source_id],
                False,
                True,
                overwrite=True,
                overwrite_selections={source_id: target_two},
            )
        )
        self.assertTrue(copied["ok"], copied)
        self.assertEqual(copied["items"][0]["target_id"], target_two)
        with closing(sqlite3.connect(self.db_path)) as conn:
            target_rows = conn.execute(
                "SELECT id, rollout_path FROM threads WHERE model_provider = 'ProviderB' ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in target_rows], [target_one, target_two])
        self.assertNotIn('"marker":"two"', Path(target_rows[1][1]).read_text(encoding="utf-8"))

    def test_local_copy_overwrite_can_explicitly_skip_ambiguous_target(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        target_one = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        target_two = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        project = Path(self.temp.name) / "skip-ambiguous-project"
        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Repeated conversation",
            cwd=project,
            first_user_message="The same opening message",
        )
        for target_id in (target_one, target_two):
            target_rollout = write_rollout(self.codex_home, target_id, "ProviderB")
            insert_thread(
                self.db_path,
                thread_id=target_id,
                rollout_path=target_rollout,
                provider="ProviderB",
                title="Repeated conversation",
                cwd=project,
                first_user_message="The same opening message",
            )

        result = self.transfer.copy_threads(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [source_id],
                False,
                True,
                overwrite=True,
                overwrite_selections={source_id: None},
            )
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["items"][0]["overwritten"])
        self.assertNotIn(result["items"][0]["target_id"], {target_one, target_two})

    def test_copy_rejects_invalid_or_reused_overwrite_selection(self) -> None:
        source_one = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_two = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        target_one = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        target_two = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        project = Path(self.temp.name) / "selection-validation-project"
        for source_id in (source_one, source_two):
            self.add_thread(
                source_id,
                provider="ProviderA",
                title="Repeated conversation",
                cwd=project,
                first_user_message="The same opening message",
            )
        for target_id in (target_one, target_two):
            target_rollout = write_rollout(self.codex_home, target_id, "ProviderB")
            insert_thread(
                self.db_path,
                thread_id=target_id,
                rollout_path=target_rollout,
                provider="ProviderB",
                title="Repeated conversation",
                cwd=project,
                first_user_message="The same opening message",
            )

        invalid = self.transfer.preview_copy(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [source_one],
                False,
                True,
                overwrite=True,
                overwrite_selections={source_one: "not-a-target"},
            )
        )
        self.assertFalse(invalid["can_execute"], invalid)
        self.assertIn("not one of the current target candidates", " ".join(invalid["errors"]))

        reused = self.transfer.preview_copy(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                [source_one, source_two],
                False,
                True,
                overwrite=True,
                overwrite_selections={source_one: target_one, source_two: target_one},
            )
        )
        self.assertFalse(reused["can_execute"], reused)
        self.assertIn("reuses target session", " ".join(reused["errors"]))

    def test_local_copy_overwrite_preserves_thread_section_and_project(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        destination_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
        project = Path(self.temp.name) / "shared-project"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                ALTER TABLE threads ADD COLUMN thread_section_id TEXT;
                ALTER TABLE threads ADD COLUMN section_position INTEGER;
                ALTER TABLE threads ADD COLUMN section_entered_at_ms INTEGER;
                ALTER TABLE threads ADD COLUMN project_id TEXT;
                CREATE TABLE thread_sections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    appearance TEXT NOT NULL
                );
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                INSERT INTO thread_sections VALUES ('source-section', 'Source', 'blue');
                INSERT INTO thread_sections VALUES ('destination-section', 'Destination', 'red');
                INSERT INTO projects VALUES ('source-project', 'Source project');
                INSERT INTO projects VALUES ('destination-project', 'Destination project');
                """
            )
            conn.commit()

        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Current conversation",
            cwd=project,
            first_user_message="Keep this thread in sync",
        )
        self.add_thread(
            destination_id,
            provider="ProviderB",
            title="Current conversation",
            cwd=project,
            first_user_message="Keep this thread in sync",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                UPDATE threads
                SET thread_section_id = ?, section_position = ?, section_entered_at_ms = ?, project_id = ?
                WHERE id = ?
                """,
                ("source-section", 4, 123456, "source-project", source_id),
            )
            conn.execute(
                """
                UPDATE threads
                SET thread_section_id = ?, section_position = ?, section_entered_at_ms = ?, project_id = ?
                WHERE id = ?
                """,
                ("destination-section", 9, 789012, "destination-project", destination_id),
            )
            conn.commit()

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [source_id], False, True, overwrite=True)
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT thread_section_id, section_position, section_entered_at_ms, project_id
                FROM threads WHERE id = ?
                """,
                (destination_id,),
            ).fetchone()
        self.assertEqual(row, ("source-section", 4, 123456, "source-project"))

    def test_local_copy_overwrite_uses_previous_transfer_when_conversation_metadata_changes(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Original conversation",
            first_user_message="Original prompt",
        )
        first_copy = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [source_id], False, True)
        )
        self.assertTrue(first_copy["ok"], first_copy)
        switched_id = first_copy["items"][0]["target_id"]
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE threads SET title = ?, first_user_message = ? WHERE id = ?",
                ("Renamed after switching", "Changed metadata", switched_id),
            )
            conn.commit()

        preview = self.transfer.preview_copy(
            CopyRequest("ProviderB", "ProviderA", [switched_id], False, True, overwrite=True)
        )

        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["items"][0]["target_id"], source_id)
        self.assertTrue(preview["items"][0]["overwritten"])
        self.assertEqual(preview["items"][0]["overwrite_match"], "previous transfer")

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
            {"name": "ChatGPT.exe", "pid": 104},
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
        self.assertEqual(killed, [101, 102, 104])
        self.assertEqual(result["killed_count"], 3)
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

    def test_target_providers_match_session_db_and_include_live_config(self) -> None:
        self.add_thread("11111111-1111-4111-8111-111111111111", "custom", title="Live")
        self.add_thread("22222222-2222-4222-8222-222222222222", "OpenAI", title="OpenAI")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "custom"
model = "gpt-5.5"

[model_providers.custom]
name = "xixiapi"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
requires_openai_auth = true
""",
            encoding="utf-8",
        )
        providers = self.transfer.list_target_providers()
        by_value = {provider["value"]: provider for provider in providers}

        self.assertEqual(set(by_value), {"custom", "OpenAI"})
        self.assertEqual(by_value["custom"]["label"], "custom")
        self.assertTrue(by_value["custom"]["current"])
        self.assertEqual(by_value["custom"]["model"], "gpt-5.5")
        self.assertEqual(by_value["custom"]["provider_name"], "xixiapi")
        self.assertFalse(by_value["OpenAI"]["current"])

    def test_target_providers_include_non_current_configured_provider_details(self) -> None:
        self.add_thread("11111111-1111-4111-8111-111111111111", "custom", title="Old provider")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "Fucheers"
model = "gpt-5.6"

[model_providers.Fucheers]
name = "Fucheers"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"

[model_providers.custom]
name = "xixiapi"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
""",
            encoding="utf-8",
        )

        providers = {provider["value"]: provider for provider in self.transfer.list_target_providers()}

        self.assertEqual(providers["custom"]["provider_name"], "xixiapi")
        self.assertIn("config", providers["custom"]["sources"])
        self.assertFalse(providers["custom"]["current"])

    def test_copy_reports_progress_and_rejects_unconfigured_target(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, provider="ProviderA", title="Progress")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "ProviderB"

[model_providers.ProviderB]
name = "Provider B"
""",
            encoding="utf-8",
        )

        blocked_plan = self.transfer.preview_copy(
            CopyRequest("ProviderA", "ProviderA", [source_id], False, True)
        )
        self.assertFalse(blocked_plan["can_execute"])
        self.assertIn("not defined", blocked_plan["errors"][0])

        events: list[dict[str, object]] = []
        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "ProviderB", [source_id], False, True),
            progress_callback=events.append,
        )

        self.assertTrue(result["ok"], result)
        phases = [event["phase"] for event in events]
        self.assertIn("planning", phases)
        self.assertIn("copying", phases)
        self.assertIn("committing", phases)
        self.assertEqual(phases[-1], "done")

    def test_preview_returns_only_requested_page_and_total(self) -> None:
        thread_ids = [
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ]
        for index, thread_id in enumerate(thread_ids, start=1):
            self.add_thread(thread_id, provider="ProviderA", title=f"Session {index}")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "ProviderB"

[model_providers.ProviderB]
name = "Provider B"
""",
            encoding="utf-8",
        )

        plan = self.transfer.preview_copy(
            CopyRequest(
                "ProviderA",
                "ProviderB",
                thread_ids,
                False,
                True,
                preview_offset=1,
                preview_limit=1,
            )
        )

        self.assertTrue(plan["can_execute"], plan)
        self.assertEqual(plan["item_total"], 3)
        self.assertEqual(plan["preview_offset"], 1)
        self.assertEqual(plan["preview_limit"], 1)
        self.assertTrue(plan["has_more"])
        self.assertEqual(plan["next_preview_offset"], 2)
        self.assertEqual([item["title"] for item in plan["items"]], ["Session 2"])

    def test_preview_and_public_copy_results_bound_large_session_labels(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        long_title = "User prompt: " + "x" * 2000
        self.add_thread(thread_id, provider="ProviderA", title=long_title)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "ProviderB"

[model_providers.ProviderB]
name = "Provider B"
""",
            encoding="utf-8",
        )

        plan = self.transfer.preview_copy(
            CopyRequest("ProviderA", "ProviderB", [thread_id], False, True)
        )

        self.assertLessEqual(len(plan["items"][0]["title"]), PREVIEW_TITLE_LIMIT)
        self.assertLessEqual(len(plan["items"][0]["display_title"]), PREVIEW_TITLE_LIMIT)
        self.assertNotIn("source_rollout_path", plan["items"][0])
        self.assertNotIn("dest_rollout_path", plan["items"][0])
        self.assertLess(len(json.dumps(plan, ensure_ascii=False)), 20_000)

        events: list[dict[str, object]] = []
        self.transfer._report_copy_progress(
            events.append,
            phase="copying",
            current=1,
            total=1,
            item={"display_title": long_title, "source_id": thread_id},
        )
        self.assertLessEqual(len(str(events[0]["item_title"])), PREVIEW_TITLE_LIMIT)

        public_result = self.transfer.public_copy_result(
            {
                "ok": True,
                "items": [
                    {
                        "source_id": thread_id,
                        "target_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "display_title": long_title,
                        "source_rollout_path": "source.jsonl",
                        "dest_rollout_path": "target.jsonl",
                    }
                ],
            }
        )
        self.assertEqual(public_result["item_total"], 1)
        self.assertNotIn("source_rollout_path", public_result["items"][0])
        self.assertNotIn("dest_rollout_path", public_result["items"][0])
        self.assertLessEqual(
            len(public_result["items"][0]["display_title"]), PREVIEW_TITLE_LIMIT
        )

    def test_http_preview_and_copy_progress_use_compact_public_payloads(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        long_title = "HTTP session: " + "x" * 2000
        source_rollout = self.add_thread(thread_id, provider="ProviderA", title=long_title)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "ProviderB"

[model_providers.ProviderB]
name = "Provider B"
""",
            encoding="utf-8",
        )

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.transfer, Path(__file__).resolve().parents[1] / "static"),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def post(path: str) -> tuple[dict[str, str], bytes]:
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            try:
                connection.request(
                    "POST",
                    path,
                    body=json.dumps(
                        {
                            "source_provider": "ProviderA",
                            "target_provider": "ProviderB",
                            "thread_ids": [thread_id],
                            "include_archived": True,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                return dict(response.getheaders()), body
            finally:
                connection.close()

        try:
            preview_headers, preview_body = post("/api/preview-copy")
            preview = json.loads(preview_body)
            preview_item = preview["items"][0]
            self.assertIn("application/json", preview_headers["Content-Type"])
            self.assertLessEqual(len(preview_item["display_title"]), PREVIEW_TITLE_LIMIT)
            self.assertNotIn("source_rollout_path", preview_item)
            self.assertNotIn("dest_rollout_path", preview_item)
            self.assertLess(len(preview_body), 20_000)

            stream_headers, stream_body = post("/api/copy-progress")
            events = [json.loads(line) for line in stream_body.splitlines() if line.strip()]
            self.assertIn("application/x-ndjson", stream_headers["Content-Type"])
            phases = [event["phase"] for event in events if event.get("type") == "progress"]
            self.assertIn("copying", phases)
            self.assertEqual(phases[-1], "done")
            copying_event = next(
                event for event in events if event.get("phase") == "copying"
            )
            self.assertLessEqual(len(copying_event["item_title"]), PREVIEW_TITLE_LIMIT)
            complete = next(event for event in events if event.get("type") == "complete")
            result = complete["result"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["item_total"], 1)
            self.assertNotIn("source_rollout_path", result["items"][0])
            self.assertNotIn("dest_rollout_path", result["items"][0])
            self.assertTrue(Path(source_rollout).exists())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_http_health_returns_exact_instance_token(self) -> None:
        instance_token = "test-instance-token"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.transfer,
                Path(__file__).resolve().parents[1] / "static",
                instance_token=instance_token,
            ),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(
                payload,
                {"ok": True, "instance_token": instance_token},
            )
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_parent_watchdog_stops_after_parent_disappears(self) -> None:
        stopped = threading.Event()
        with patch("server.parent_process_is_alive", return_value=False):
            watch_parent_process(999_999, stopped.set, poll_interval=0.001)

        self.assertTrue(stopped.is_set())
        self.assertTrue(parent_process_is_alive(os.getpid()))

    def test_http_copy_progress_reports_unexpected_handler_errors(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.transfer, Path(__file__).resolve().parents[1] / "static"),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            with patch.object(
                self.transfer,
                "copy_threads",
                side_effect=RuntimeError("synthetic copy failure"),
            ):
                connection.request(
                    "POST",
                    "/api/copy-progress",
                    body=json.dumps(
                        {
                            "source_provider": "ProviderA",
                            "target_provider": "ProviderB",
                            "thread_ids": ["thread-a"],
                            "include_archived": True,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                events = [
                    json.loads(line)
                    for line in response.read().splitlines()
                    if line.strip()
                ]

            self.assertEqual(response.status, 200)
            phases = [event["phase"] for event in events if event.get("type") == "progress"]
            self.assertEqual(phases, ["error"])
            self.assertEqual(events[-1]["type"], "complete")
            self.assertFalse(events[-1]["result"]["ok"])
            self.assertIn("synthetic copy failure", events[-1]["result"]["errors"][0])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_http_copy_progress_reports_blocked_phase_before_complete(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(thread_id, provider="ProviderA", title="Blocked copy")
        self.transfer.process_checker = lambda: [{"name": "codex.exe", "pid": 123}]
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.transfer, Path(__file__).resolve().parents[1] / "static"),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request(
                "POST",
                "/api/copy-progress",
                body=json.dumps(
                    {
                        "source_provider": "ProviderA",
                        "target_provider": "ProviderB",
                        "thread_ids": [thread_id],
                        "include_archived": True,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            events = [
                json.loads(line)
                for line in response.read().splitlines()
                if line.strip()
            ]

            self.assertEqual(response.status, 200)
            progress = [event for event in events if event.get("type") == "progress"]
            self.assertEqual([event["phase"] for event in progress], ["checking", "blocked"])
            self.assertEqual(progress[-1]["current"], 0)
            self.assertEqual(events[-1]["type"], "complete")
            self.assertTrue(events[-1]["result"]["blocked"])
            self.assertFalse(events[-1]["result"]["ok"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_copy_request_rejects_invalid_preview_window(self) -> None:
        base = {
            "source_provider": "ProviderA",
            "target_provider": "ProviderB",
            "thread_ids": ["thread-a"],
        }
        for key, value, message in (
            ("preview_offset", -1, "preview_offset must be non-negative"),
            ("preview_limit", 0, "preview_limit must be between 1"),
            ("preview_limit", MAX_PREVIEW_PAGE_SIZE + 1, "preview_limit must be between 1"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    CopyRequest.from_json({**base, key: value})

    def test_copy_executes_full_plan_even_when_preview_page_is_small(self) -> None:
        thread_ids = [
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ]
        for index, thread_id in enumerate(thread_ids, start=1):
            self.add_thread(thread_id, provider="ProviderA", title=f"Session {index}")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "ProviderB"

[model_providers.ProviderB]
name = "Provider B"
""",
            encoding="utf-8",
        )
        request = CopyRequest(
            "ProviderA",
            "ProviderB",
            thread_ids,
            False,
            True,
            preview_limit=1,
        )
        events: list[dict[str, object]] = []

        result = self.transfer.copy_threads(request, progress_callback=events.append)

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["items"]), 3)
        ready = next(event for event in events if event["phase"] == "ready")
        self.assertEqual(ready["total"], 3)
        with closing(sqlite3.connect(self.db_path)) as conn:
            copied_count = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider = 'ProviderB'"
            ).fetchone()[0]
        self.assertEqual(copied_count, 3)

    def test_package_copy_reports_progress_for_full_plan_when_preview_page_is_small(self) -> None:
        thread_ids = [
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ]
        for index, thread_id in enumerate(thread_ids, start=1):
            self.add_thread(thread_id, provider="ProviderA", title=f"Package session {index}")
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", thread_ids, False, True)
        )
        target, target_sqlite_home = self.loaded_package_target(
            Path(export["package_path"]),
            "package-progress-target",
        )
        events: list[dict[str, object]] = []

        result = target.copy_imported_package_threads(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                thread_ids,
                False,
                True,
                preview_limit=1,
            ),
            progress_callback=events.append,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["items"]), 3)
        ready = next(event for event in events if event["phase"] == "ready")
        self.assertEqual(ready["total"], 3)
        copying = [event for event in events if event["phase"] == "copying"]
        self.assertEqual([event["current"] for event in copying], [1, 2, 3])
        self.assertEqual(events[-1]["phase"], "done")
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 3)

    def test_target_providers_include_live_config_without_sessions(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "live-only"
model = "gpt-5.5"

[model_providers.live-only]
name = "Remote provider"
base_url = "https://example.test/v1"
wire_api = "responses"
""",
            encoding="utf-8",
        )
        self.switch_home.mkdir(parents=True, exist_ok=True)
        (self.switch_home / "preset-overrides.json").write_text(
            json.dumps(
                {
                    "customPresets": [
                        {
                            "id": "unrelated-preset",
                            "name": "Unrelated preset",
                            "configText": 'model_provider = "preset-only"',
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        providers = self.transfer.list_target_providers()

        self.assertEqual(
            providers,
            [
                {
                    "value": "live-only",
                    "label": "live-only",
                    "sources": ["live_config"],
                    "session_total": 0,
                    "current": True,
                    "provider_name": "Remote provider",
                    "model": "gpt-5.5",
                    "base_url": "https://example.test/v1",
                    "wire_api": "responses",
                }
            ],
        )

    def test_current_config_reports_unknown_model_provider_id(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "Fucheers"

[model_providers.custom]
name = "Fucheers"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
""",
            encoding="utf-8",
        )

        current = self.transfer.current_config()

        self.assertEqual(current["model_provider"], "Fucheers")
        self.assertEqual(current["configured_provider_ids"], ["custom"])
        self.assertIn("not defined", current["error"])

    def test_copy_maps_provider_display_name_to_configured_provider_id(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, provider="ProviderA", title="Migrated")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "config.toml").write_text(
            """
model_provider = "custom"
model = "gpt-5.5"

[model_providers.custom]
name = "Fucheers"
base_url = "https://www.fucheers.top/v1"
wire_api = "responses"
""",
            encoding="utf-8",
        )

        preview = self.transfer.preview_copy(
            CopyRequest("ProviderA", "Fucheers", [source_id], False, True)
        )

        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["target_provider"], "custom")
        self.assertIn("using provider id 'custom'", preview["warnings"][0])

        result = self.transfer.copy_threads(
            CopyRequest("ProviderA", "Fucheers", [source_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        with closing(sqlite3.connect(self.db_path)) as conn:
            provider = conn.execute(
                "SELECT model_provider, rollout_path FROM threads WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(provider[0], "custom")
        copied_meta = json.loads(Path(provider[1]).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(copied_meta["item"]["payload"]["model_provider"], "custom")

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

    def test_export_package_defaults_to_selected_project_exported_directory(self) -> None:
        thread_id = "11111111-1111-4111-8111-111111111111"
        project = Path(self.temp.name) / "paper-project"
        self.add_thread(thread_id, title="Paper", cwd=project)

        result = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [thread_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        package_path = Path(result["package_path"])
        self.assertTrue(package_path.exists())
        self.assertEqual(package_path.parent, project / "exported")
        self.assertTrue(package_path.name.startswith("codex-session-package-"))

    def test_export_package_writes_inside_requested_export_directory(self) -> None:
        thread_id = "11111111-1111-4111-8111-111111111111"
        project = Path(self.temp.name) / "paper-project"
        export_dir = Path(self.temp.name) / "custom-export"
        self.add_thread(thread_id, title="Paper", cwd=project)

        result = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [thread_id], False, True, str(export_dir))
        )

        self.assertTrue(result["ok"], result)
        package_path = Path(result["package_path"])
        self.assertTrue(package_path.exists())
        self.assertEqual(package_path.parent, export_dir)
        self.assertTrue(package_path.name.startswith("codex-session-package-"))

    def test_open_path_reveals_parent_directory_for_file(self) -> None:
        opened: list[Path] = []
        transfer = CodexSessionTransfer(
            codex_home=self.codex_home,
            sqlite_home=self.sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
            path_opener=lambda path: opened.append(path),
        )
        directory = Path(self.temp.name) / "exports"
        package_path = directory / "package.zip"
        directory.mkdir()
        package_path.write_text("zip", encoding="utf-8")

        result = transfer.open_path(package_path)

        self.assertTrue(result["ok"], result)
        self.assertEqual(opened, [directory])
        self.assertEqual(Path(result["opened_path"]), directory)

    def test_open_path_rejects_missing_paths(self) -> None:
        result = self.transfer.open_path(Path(self.temp.name) / "missing.zip")

        self.assertFalse(result["ok"])
        self.assertIn("Path not found", result["errors"][0])

    def test_open_path_rejects_empty_path(self) -> None:
        result = self.transfer.open_path(Path(""))

        self.assertFalse(result["ok"])
        self.assertIn("Path is required", result["errors"][0])

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

    def test_imported_package_can_overwrite_matching_session_id(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, provider="ProviderA", title="Portable source")
        self.write_session_index(source_id, "Portable source name")
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )

        target_codex_home = Path(self.temp.name) / "overwrite-target" / ".codex"
        target_sqlite_home = target_codex_home / "sqlite"
        target_db = target_sqlite_home / "state_5.sqlite"
        create_schema(target_db)
        old_rollout = write_rollout(
            target_codex_home,
            source_id,
            "ProviderA",
            entries=[{"item": {"type": "old_event", "payload": {"old": True}}}],
        )
        insert_thread(
            target_db,
            thread_id=source_id,
            rollout_path=old_rollout,
            provider="ProviderA",
            title="Existing target",
        )
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )
        target.session_index_path.parent.mkdir(parents=True, exist_ok=True)
        target.session_index_path.write_text(
            compact_json(
                {
                    "id": source_id,
                    "thread_name": "Existing target name",
                    "updated_at": "2026-06-13T10:30:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertTrue(target.load_transfer_package(Path(export["package_path"]))["ok"])
        request = CopyRequest(
            "ProviderA",
            "ProviderA",
            [source_id],
            False,
            True,
            overwrite=True,
        )
        preview = target.preview_imported_package_copy(request)
        self.assertTrue(preview["can_execute"], preview)
        self.assertTrue(preview["overwrite"])
        self.assertEqual(preview["items"][0]["target_id"], source_id)
        self.assertTrue(preview["items"][0]["overwritten"])

        result = target.copy_imported_package_threads(request)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["items"][0]["overwritten"])
        self.assertEqual(result["items"][0]["target_id"], source_id)
        with closing(sqlite3.connect(target_db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)
            row = conn.execute(
                "SELECT title, model_provider, rollout_path FROM threads WHERE id = ?",
                (source_id,),
            ).fetchone()
        self.assertEqual(row[0], "Portable source")
        self.assertEqual(row[1], "ProviderA")
        copied_rollout = Path(row[2])
        self.assertTrue(copied_rollout.exists())
        self.assertNotIn('"old":true', copied_rollout.read_text(encoding="utf-8"))

        index_entries = [
            json.loads(line)
            for line in target.session_index_path.read_text(encoding="utf-8").splitlines()
        ]
        matching_entries = [entry for entry in index_entries if entry["id"] == source_id]
        self.assertEqual(len(matching_entries), 1)
        self.assertEqual(matching_entries[0]["thread_name"], "Portable source name")
        self.assertEqual(list(target.manifest_dir.glob("overwrite-*")), [])

    def test_project_filter_matches_normalized_windows_cwd_variants(self) -> None:
        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        canonical_cwd = Path("D:/Users/hd/Desktop/2026-CommercialOrder/OnGoingOrders/Papers")
        extended_cwd = Path("\\\\?\\D:\\Users\\hd\\Desktop\\2026-CommercialOrder\\OnGoingOrders\\Papers")
        self.add_thread(first_id, title="Canonical", cwd=canonical_cwd)
        self.add_thread(second_id, title="Extended", cwd=extended_cwd)

        threads = self.transfer.list_threads(
            source_provider="ProviderA",
            cwd=str(canonical_cwd),
        )

        self.assertEqual({thread["id"] for thread in threads}, {first_id, second_id})

    def test_imported_package_copy_refreshes_codex_sidebar_visibility_fields(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(
            source_id,
            provider="ProviderA",
            title="Portable",
            archived=True,
            source="exec",
            thread_source="exec",
            created_at_ms=1_700_000_000_000,
            updated_at_ms=1_700_000_100_000,
        )
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )

        target_codex_home = Path(self.temp.name) / "target-visible" / ".codex"
        target_sqlite_home = target_codex_home / "sqlite"
        create_schema(target_sqlite_home / "state_5.sqlite")
        target = CodexSessionTransfer(
            codex_home=target_codex_home,
            sqlite_home=target_sqlite_home,
            provider_switch_home=self.switch_home,
            process_checker=lambda: [],
        )
        self.assertTrue(target.load_transfer_package(Path(export["package_path"]))["ok"])

        before_copy_ms = int(datetime.now(UTC).timestamp() * 1000)
        result = target.copy_imported_package_threads(
            CopyRequest("ProviderA", "ProviderA", [source_id], False, True)
        )

        self.assertTrue(result["ok"], result)
        target_id = result["items"][0]["target_id"]
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            copied = conn.execute(
                """
                SELECT source, thread_source, created_at_ms, updated_at_ms, rollout_path, archived, archived_at
                FROM threads
                WHERE id = ?
                """,
                (target_id,),
            ).fetchone()
        self.assertIn(copied[0], {"cli", "vscode"})
        self.assertEqual(copied[1], "user")
        self.assertGreaterEqual(copied[2], before_copy_ms)
        self.assertGreaterEqual(copied[3], before_copy_ms)
        self.assertEqual(copied[5], 0)
        self.assertIsNone(copied[6])
        rollout_path = Path(copied[4])
        self.assertIn("sessions", rollout_path.parts)
        self.assertNotIn("archived_sessions", rollout_path.parts)
        visible_threads = target.list_threads(source_provider="ProviderA")
        self.assertIn(target_id, {thread["id"] for thread in visible_threads})
        first_rollout_line = Path(copied[4]).read_text(encoding="utf-8").splitlines()[0]
        rollout_payload = json.loads(first_rollout_line)["item"]["payload"]
        self.assertEqual(rollout_payload["source"], copied[0])
        index_entries = [
            json.loads(line)
            for line in (target_codex_home / "session_index.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        copied_index = next(entry for entry in index_entries if entry["id"] == target_id)
        copied_index_ms = int(
            datetime.fromisoformat(copied_index["updated_at"].replace("Z", "+00:00")).timestamp()
            * 1000
        )
        self.assertGreaterEqual(copied_index_ms, before_copy_ms)

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

    def test_imported_package_preview_preserves_projects_under_target_root(self) -> None:
        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        first_cwd = Path("C:/source/ProjectA")
        second_cwd = Path("D:/source/ProjectB")
        self.add_thread(first_id, cwd=first_cwd)
        self.add_thread(second_id, cwd=second_cwd)
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [first_id, second_id], False, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "preserve-target")
        target_root = Path(self.temp.name) / "workspaces"
        (target_root / "ProjectA").mkdir(parents=True)
        (target_root / "ProjectB").mkdir()

        preview = target.preview_imported_package_copy(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [first_id, second_id],
                False,
                True,
                workspace_mapping=WorkspaceMapping(
                    "preserve_projects",
                    str(target_root),
                    {},
                ),
            )
        )

        self.assertTrue(preview["can_execute"], preview)
        targets = {item["source_cwd"]: item["target_cwd"] for item in preview["items"]}
        self.assertEqual(targets[str(first_cwd)], str(target_root / "ProjectA"))
        self.assertEqual(targets[str(second_cwd)], str(target_root / "ProjectB"))
        self.assertEqual(len(preview["workspace_mappings"]), 2)

    def test_imported_package_preview_maps_projects_to_single_workspace(self) -> None:
        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.add_thread(first_id, cwd=Path("C:/source/ProjectA"))
        self.add_thread(second_id, cwd=Path("D:/source/ProjectB"))
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [first_id, second_id], False, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "single-target")
        target_workspace = Path(self.temp.name) / "single-workspace"
        target_workspace.mkdir()

        preview = target.preview_imported_package_copy(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [first_id, second_id],
                False,
                True,
                workspace_mapping=WorkspaceMapping(
                    "single_workspace",
                    str(target_workspace),
                    {},
                ),
            )
        )

        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(
            {item["target_cwd"] for item in preview["items"]},
            {str(target_workspace)},
        )

    def test_imported_package_preview_applies_project_override(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_cwd = Path("C:/source/ProjectA")
        self.add_thread(source_id, cwd=source_cwd)
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "override-target")
        target_root = Path(self.temp.name) / "override-root"
        override = Path(self.temp.name) / "renamed-project"
        target_root.mkdir()
        override.mkdir()

        preview = target.preview_imported_package_copy(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [source_id],
                False,
                True,
                workspace_mapping=WorkspaceMapping(
                    "preserve_projects",
                    str(target_root),
                    {str(source_cwd): str(override)},
                ),
            )
        )

        self.assertTrue(preview["can_execute"], preview)
        self.assertEqual(preview["items"][0]["target_cwd"], str(override))

    def test_imported_package_preview_rejects_duplicate_preserved_project_names(self) -> None:
        first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.add_thread(first_id, cwd=Path("C:/source-one/Same"))
        self.add_thread(second_id, cwd=Path("D:/source-two/Same"))
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [first_id, second_id], False, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "duplicate-target")
        target_root = Path(self.temp.name) / "duplicate-root"
        (target_root / "Same").mkdir(parents=True)

        preview = target.preview_imported_package_copy(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [first_id, second_id],
                False,
                True,
                workspace_mapping=WorkspaceMapping(
                    "preserve_projects",
                    str(target_root),
                    {},
                ),
            )
        )

        self.assertFalse(preview["can_execute"])
        self.assertIn("same target", " ".join(preview["errors"]).lower())

    def test_imported_package_preview_maps_descendant_with_different_cwd(self) -> None:
        parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        parent_cwd = Path("C:/source/ParentProject")
        child_cwd = Path("C:/source/ChildWorktree")
        self.add_thread(parent_id, cwd=parent_cwd)
        self.add_thread(
            child_id,
            cwd=child_cwd,
            parent_thread_id=parent_id,
            source={"subagent": {"thread_spawn": {"parent_thread_id": parent_id}}},
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
                (parent_id, child_id, "open"),
            )
            conn.commit()
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [parent_id], True, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "descendant-target")
        target_root = Path(self.temp.name) / "descendant-root"
        (target_root / "ParentProject").mkdir(parents=True)
        (target_root / "ChildWorktree").mkdir()

        preview = target.preview_imported_package_copy(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [parent_id],
                True,
                True,
                workspace_mapping=WorkspaceMapping(
                    "preserve_projects",
                    str(target_root),
                    {},
                ),
            )
        )

        self.assertTrue(preview["can_execute"], preview)
        targets = {item["source_id"]: item["target_cwd"] for item in preview["items"]}
        self.assertEqual(targets[parent_id], str(target_root / "ParentProject"))
        self.assertEqual(targets[child_id], str(target_root / "ChildWorktree"))

    def test_imported_package_preview_rejects_invalid_target_directories(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, cwd=Path("C:/source/ProjectA"))
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )
        target, _ = self.loaded_package_target(Path(export["package_path"]), "invalid-target")
        missing = Path(self.temp.name) / "missing-workspace"
        file_target = Path(self.temp.name) / "workspace-file"
        file_target.write_text("not a directory", encoding="utf-8")

        for invalid_target in (missing, file_target):
            with self.subTest(invalid_target=invalid_target):
                preview = target.preview_imported_package_copy(
                    CopyRequest(
                        "ProviderA",
                        "ProviderA",
                        [source_id],
                        False,
                        True,
                        workspace_mapping=WorkspaceMapping(
                            "single_workspace",
                            str(invalid_target),
                            {},
                        ),
                    )
                )
                self.assertFalse(preview["can_execute"])
                self.assertIn("directory", " ".join(preview["errors"]).lower())

    def test_source_path_matching_is_windows_aware_without_folding_posix_case(self) -> None:
        self.assertEqual(
            self.transfer._source_path_match_key("\\\\?\\C:\\Work\\Repo\\"),
            self.transfer._source_path_match_key("c:/work/repo"),
        )
        self.assertNotEqual(
            self.transfer._source_path_match_key("/Work/Repo"),
            self.transfer._source_path_match_key("/work/repo"),
        )

    def test_imported_package_copy_updates_modern_session_identity_and_current_cwd(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        source_cwd = Path("C:/source/ModernProject")
        source_rollout = self.add_thread(source_id, cwd=source_cwd)
        lines = source_rollout.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["item"]["payload"]["session_id"] = source_id
        historical_turn = {
            "timestamp": "2026-06-13T10:05:00Z",
            "item": {
                "type": "turn_context",
                "payload": {"cwd": str(source_cwd), "model": "gpt-test"},
            },
        }
        source_rollout.write_text(
            "\n".join([compact_json(first), *lines[1:], compact_json(historical_turn)]) + "\n",
            encoding="utf-8",
        )
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )
        target, target_sqlite_home = self.loaded_package_target(
            Path(export["package_path"]),
            "modern-target",
        )
        target_workspace = Path(self.temp.name) / "modern-workspace"
        target_workspace.mkdir()

        result = target.copy_imported_package_threads(
            CopyRequest(
                "ProviderA",
                "ProviderA",
                [source_id],
                False,
                True,
                workspace_mapping=WorkspaceMapping(
                    "single_workspace",
                    str(target_workspace),
                    {},
                ),
            )
        )

        self.assertTrue(result["ok"], result)
        item = result["items"][0]
        target_id = item["target_id"]
        copied_lines = Path(item["dest_rollout_path"]).read_text(encoding="utf-8").splitlines()
        copied_meta = json.loads(copied_lines[0])["item"]["payload"]
        self.assertEqual(copied_meta["id"], target_id)
        self.assertEqual(copied_meta["session_id"], target_id)
        self.assertEqual(copied_meta["cwd"], str(target_workspace))
        self.assertEqual(copied_meta["model_provider"], "ProviderA")
        self.assertEqual(json.loads(copied_lines[-1]), historical_turn)
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            row = conn.execute(
                "SELECT cwd, rollout_path FROM threads WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(row[0], str(target_workspace))
        self.assertEqual(row[1], item["dest_rollout_path"])
        index_entry = json.loads(
            (target.codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(set(index_entry), {"id", "thread_name", "updated_at"})

    def test_imported_package_copy_rolls_back_index_database_and_rollout_on_failure(self) -> None:
        source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.add_thread(source_id, cwd=Path("C:/source/RollbackProject"))
        self.write_session_index(source_id, "Rollback source")
        export = self.transfer.export_package(
            ExportPackageRequest("ProviderA", [source_id], False, True)
        )
        target, target_sqlite_home = self.loaded_package_target(
            Path(export["package_path"]),
            "rollback-target",
        )
        target_workspace = Path(self.temp.name) / "rollback-workspace"
        target_workspace.mkdir()
        target.session_index_path.parent.mkdir(parents=True, exist_ok=True)
        original_index = compact_json(
            {
                "id": "existing",
                "thread_name": "Existing",
                "updated_at": "2026-06-13T10:30:00Z",
            }
        ) + "\n"
        target.session_index_path.write_text(original_index, encoding="utf-8")
        append_entries = target._append_session_index_entries

        def append_then_fail(*args, **kwargs):
            append_entries(*args, **kwargs)
            raise RuntimeError("forced post-index failure")

        with patch.object(target, "_append_session_index_entries", side_effect=append_then_fail):
            result = target.copy_imported_package_threads(
                CopyRequest(
                    "ProviderA",
                    "ProviderA",
                    [source_id],
                    False,
                    True,
                    workspace_mapping=WorkspaceMapping(
                        "single_workspace",
                        str(target_workspace),
                        {},
                    ),
                )
            )

        self.assertFalse(result["ok"])
        self.assertIn("forced post-index failure", " ".join(result["errors"]))
        self.assertEqual(target.session_index_path.read_text(encoding="utf-8"), original_index)
        with closing(sqlite3.connect(target_sqlite_home / "state_5.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)
        self.assertEqual(list((target.codex_home / "sessions").rglob("*.jsonl")), [])

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
