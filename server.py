from __future__ import annotations

import argparse
import csv
import json
import ntpath
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse


STATE_DB_FILENAME = "state_5.sqlite"
SESSION_INDEX_FILENAME = "session_index.jsonl"
BLOCKING_PROCESS_NAMES = {"chatgpt", "codex", "codex-plus-plus", "codex-plus-plus-manager"}
MAX_COPY_WORKERS = 8
DEFAULT_THREAD_DETAIL_LIMIT = 80
MAX_THREAD_DETAIL_LIMIT = 200
DEFAULT_PREVIEW_PAGE_SIZE = 48
MAX_PREVIEW_PAGE_SIZE = 200
PREVIEW_TITLE_LIMIT = 320
PACKAGE_FORMAT = "codex-session-transfer-package"
PACKAGE_VERSION = 1
PACKAGE_MANIFEST_NAME = "manifest.json"
PACKAGE_DB_PATH = f"sqlite/{STATE_DB_FILENAME}"
PACKAGE_SCHEMA_TABLES = ("threads", "thread_spawn_edges", "thread_dynamic_tools")
RUNTIME_DATABASE_FILENAMES = (
    STATE_DB_FILENAME,
    "logs_2.sqlite",
    "goals_1.sqlite",
    "memories_1.sqlite",
    "queue_1.sqlite",
    "thread_history_1.sqlite",
)
MIRROR_SIDECAR_ALIASES = {
    "logs_2.sqlite": "mirror_logs",
    "goals_1.sqlite": "mirror_goals",
    "memories_1.sqlite": "mirror_memories",
    "queue_1.sqlite": "mirror_queue",
    "thread_history_1.sqlite": "mirror_history",
}
ROLLOUT_THREAD_ID_FIELDS = {
    "child_thread_id",
    "conversation_id",
    "forked_from_id",
    "id",
    "parent_thread_id",
    "session_id",
    "source_thread_id",
    "target_thread_id",
    "thread_id",
}
ROLLOUT_THREAD_ID_LIST_FIELDS = {"thread_ids"}
SKILL_PACKAGE_FORMAT = "codex-skill-transfer-package"
SKILL_PACKAGE_VERSION = 1
SKILL_PACKAGE_DIRNAME = "skills"
ROLLOUT_NAME_RE = re.compile(
    r"^(?P<prefix>rollout-.+(?:-|_))"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?P<suffix>\.jsonl)$"
)
SECRET_PATTERNS = (
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
PREVIEW_ITEM_FIELDS = (
    "source_id",
    "target_id",
    "title",
    "thread_name",
    "display_title",
    "session_index_present",
    "source_provider",
    "target_provider",
    "source_cwd",
    "target_cwd",
    "cwd_rewritten",
    "archived",
    "parent_source_id",
    "parent_target_id",
    "child_count",
    "overwritten",
    "overwrite_match",
)


def _clip_preview_text(value: Any) -> str:
    text = "" if value is None else str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) <= PREVIEW_TITLE_LIMIT:
        return text
    return text[: PREVIEW_TITLE_LIMIT - 3] + "..."
CODEX_DEFAULT_SOURCE_KINDS = {"cli", "vscode"}
DEFAULT_IMPORTED_SOURCE_KIND = "vscode"
DEFAULT_IMPORTED_THREAD_SOURCE = "user"
WORKSPACE_MAPPING_MODES = {"preserve_projects", "single_workspace"}

JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
ProcessChecker = Callable[[], list[dict[str, Any]]]
ProcessTerminator = Callable[[int], dict[str, Any]]
PathOpener = Callable[[Path], None]


@dataclass(frozen=True)
class WorkspaceMapping:
    mode: str
    target_root: str
    overrides: dict[str, str]

    @classmethod
    def from_json(cls, data: Any) -> "WorkspaceMapping":
        if not isinstance(data, dict):
            raise ValueError("workspace_mapping must be an object")
        mode = str(data.get("mode", "")).strip()
        if mode not in WORKSPACE_MAPPING_MODES:
            raise ValueError("workspace_mapping.mode is invalid")
        target_root = str(data.get("target_root", "")).strip()
        if not target_root or not Path(target_root).is_absolute():
            raise ValueError("workspace_mapping.target_root must be an absolute path")
        overrides_raw = data.get("overrides", {})
        if not isinstance(overrides_raw, dict):
            raise ValueError("workspace_mapping.overrides must be an object")
        overrides = {
            str(source).strip(): str(target).strip()
            for source, target in overrides_raw.items()
            if str(source).strip() and str(target).strip()
        }
        return cls(mode=mode, target_root=target_root, overrides=overrides)


@dataclass(frozen=True)
class CopyRequest:
    source_provider: str
    target_provider: str
    thread_ids: list[str]
    include_descendants: bool
    include_archived: bool
    cwd_map: dict[str, str] | None = None
    workspace_mapping: WorkspaceMapping | None = None
    overwrite: bool = False
    preview_offset: int = 0
    preview_limit: int = DEFAULT_PREVIEW_PAGE_SIZE
    overwrite_selections: dict[str, str | None] | None = None
    mirror_target: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CopyRequest":
        thread_ids = data.get("thread_ids", [])
        if not isinstance(thread_ids, list):
            raise ValueError("thread_ids must be a list")
        cwd_map_raw = data.get("cwd_map", {})
        if cwd_map_raw is None:
            cwd_map_raw = {}
        if not isinstance(cwd_map_raw, dict):
            raise ValueError("cwd_map must be an object")
        cwd_map = {
            str(source).strip(): str(target).strip()
            for source, target in cwd_map_raw.items()
            if str(source).strip() and str(target).strip()
        }
        workspace_mapping_raw = data.get("workspace_mapping")
        workspace_mapping = (
            WorkspaceMapping.from_json(workspace_mapping_raw)
            if workspace_mapping_raw is not None
            else None
        )
        if cwd_map and workspace_mapping is not None:
            raise ValueError("cwd_map and workspace_mapping cannot be used together")
        overwrite_selections_raw = data.get("overwrite_selections", {})
        if overwrite_selections_raw is None:
            overwrite_selections_raw = {}
        if not isinstance(overwrite_selections_raw, dict):
            raise ValueError("overwrite_selections must be an object")
        overwrite_selections: dict[str, str | None] = {}
        for source_id, target_id in overwrite_selections_raw.items():
            source_key = str(source_id).strip()
            if not source_key:
                continue
            if target_id is None:
                overwrite_selections[source_key] = None
                continue
            if not isinstance(target_id, str):
                raise ValueError("overwrite_selections values must be target ids or null")
            target_key = target_id.strip()
            overwrite_selections[source_key] = target_key or None
        try:
            preview_offset = int(data.get("preview_offset", 0))
            preview_limit = int(data.get("preview_limit", DEFAULT_PREVIEW_PAGE_SIZE))
        except (TypeError, ValueError) as exc:
            raise ValueError("preview_offset and preview_limit must be integers") from exc
        if preview_offset < 0:
            raise ValueError("preview_offset must be non-negative")
        if not 1 <= preview_limit <= MAX_PREVIEW_PAGE_SIZE:
            raise ValueError(
                f"preview_limit must be between 1 and {MAX_PREVIEW_PAGE_SIZE}"
            )
        return cls(
            source_provider=str(data.get("source_provider", "")).strip(),
            target_provider=str(data.get("target_provider", "")).strip(),
            thread_ids=[str(thread_id).strip() for thread_id in thread_ids if str(thread_id).strip()],
            include_descendants=bool(data.get("include_descendants", False)),
            include_archived=bool(data.get("include_archived", False)),
            cwd_map=cwd_map,
            workspace_mapping=workspace_mapping,
            overwrite=bool(data.get("overwrite", False)),
            overwrite_selections=overwrite_selections,
            preview_offset=preview_offset,
            preview_limit=preview_limit,
            mirror_target=bool(data.get("mirror_target", False)),
        )


@dataclass(frozen=True)
class RolloutWriteResult:
    item: dict[str, Any]
    destination_path: Path
    offset_map: dict[int, int]


@dataclass(frozen=True)
class RolloutBackup:
    original_path: Path
    backup_path: Path


@dataclass(frozen=True)
class RebindRequest:
    source_provider: str
    target_provider: str
    thread_ids: list[str]
    include_descendants: bool
    include_archived: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RebindRequest":
        thread_ids = data.get("thread_ids", [])
        if not isinstance(thread_ids, list):
            raise ValueError("thread_ids must be a list")
        return cls(
            source_provider=str(data.get("source_provider", "")).strip(),
            target_provider=str(data.get("target_provider", "")).strip(),
            thread_ids=[str(thread_id).strip() for thread_id in thread_ids if str(thread_id).strip()],
            include_descendants=bool(data.get("include_descendants", False)),
            include_archived=bool(data.get("include_archived", False)),
        )


@dataclass(frozen=True)
class ExportPackageRequest:
    source_provider: str
    thread_ids: list[str]
    include_descendants: bool
    include_archived: bool
    output_path: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ExportPackageRequest":
        thread_ids = data.get("thread_ids", [])
        if not isinstance(thread_ids, list):
            raise ValueError("thread_ids must be a list")
        return cls(
            source_provider=str(data.get("source_provider", "")).strip(),
            thread_ids=[str(thread_id).strip() for thread_id in thread_ids if str(thread_id).strip()],
            include_descendants=bool(data.get("include_descendants", False)),
            include_archived=bool(data.get("include_archived", False)),
            output_path=str(data.get("output_path", "")).strip(),
        )


@dataclass(frozen=True)
class LoadedTransferPackage:
    package_path: Path
    extract_dir: Path
    db_path: Path
    session_index_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class SkillPackageRequest:
    skill_ids: list[str]
    output_path: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SkillPackageRequest":
        skill_ids = data.get("skill_ids", [])
        if not isinstance(skill_ids, list):
            raise ValueError("skill_ids must be a list")
        return cls(
            skill_ids=[str(skill_id).strip() for skill_id in skill_ids if str(skill_id).strip()],
            output_path=str(data.get("output_path", "")).strip(),
        )


@dataclass(frozen=True)
class SkillImportRequest:
    skill_ids: list[str]
    overwrite: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SkillImportRequest":
        skill_ids = data.get("skill_ids", [])
        if not isinstance(skill_ids, list):
            raise ValueError("skill_ids must be a list")
        return cls(
            skill_ids=[str(skill_id).strip() for skill_id in skill_ids if str(skill_id).strip()],
            overwrite=bool(data.get("overwrite", False)),
        )


@dataclass(frozen=True)
class LoadedSkillPackage:
    package_path: Path
    extract_dir: Path
    manifest: dict[str, Any]


def compact_json(value: JSONValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_process_name(name: str) -> str:
    lower = Path(name).name.lower()
    return lower[:-4] if lower.endswith(".exe") else lower


def default_process_checker() -> list[dict[str, Any]]:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        processes: list[dict[str, Any]] = []
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) >= 2:
                try:
                    pid: int | str = int(row[1])
                except ValueError:
                    pid = row[1]
                processes.append({"name": row[0], "pid": pid})
        return processes

    completed = subprocess.run(
        ["ps", "-eo", "pid=,comm="],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processes = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            processes.append({"pid": parts[0], "name": parts[1]})
    return processes


def default_process_terminator(pid: int) -> dict[str, Any]:
    if pid == os.getpid():
        return {"ok": False, "pid": pid, "error": "Refusing to terminate the transfer server process"}

    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
        return {
            "ok": completed.returncode == 0,
            "pid": pid,
            "returncode": completed.returncode,
            "message": output,
        }

    try:
        os.kill(pid, 15)
    except OSError as exc:
        return {"ok": False, "pid": pid, "error": str(exc)}
    return {"ok": True, "pid": pid, "message": "SIGTERM sent"}


def default_path_opener(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


class CodexSessionTransfer:
    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        sqlite_home: Path | None = None,
        provider_switch_home: Path | None = None,
        process_checker: ProcessChecker | None = None,
        process_terminator: ProcessTerminator | None = None,
        path_opener: PathOpener | None = None,
    ) -> None:
        default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.codex_home = Path(codex_home or default_home)
        env_sqlite_home = os.environ.get("CODEX_SQLITE_HOME")
        if sqlite_home is not None:
            self.sqlite_home = Path(sqlite_home)
        elif env_sqlite_home:
            self.sqlite_home = Path(env_sqlite_home)
        else:
            self.sqlite_home = self._detect_sqlite_home(self.codex_home)
        self.db_path = self.sqlite_home / STATE_DB_FILENAME
        default_switch_home = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        self.provider_switch_home = Path(
            provider_switch_home or default_switch_home / "codex-provider-switch"
        )
        self.manifest_dir = self.codex_home / "session-transfer" / "manifests"
        self.package_dir = self.codex_home / "session-transfer" / "packages"
        self.package_import_dir = self.codex_home / "session-transfer" / "imports"
        self.skills_root = self.codex_home / "skills"
        self.skill_package_dir = self.codex_home / "session-transfer" / "skill-packages"
        self.skill_package_import_dir = self.codex_home / "session-transfer" / "skill-imports"
        self.session_index_path = self.codex_home / SESSION_INDEX_FILENAME
        self.process_checker = process_checker or default_process_checker
        self.process_terminator = process_terminator or default_process_terminator
        self.path_opener = path_opener or default_path_opener
        self.loaded_package: LoadedTransferPackage | None = None
        self.loaded_skill_package: LoadedSkillPackage | None = None

    @classmethod
    def _detect_sqlite_home(cls, codex_home: Path) -> Path:
        legacy_home = codex_home / "sqlite"
        candidates = [codex_home, legacy_home]
        existing = [home for home in candidates if (home / STATE_DB_FILENAME).exists()]
        if not existing:
            return legacy_home
        return max(
            existing,
            key=lambda home: (
                cls._state_db_activity_score(home / STATE_DB_FILENAME),
                1 if home == codex_home else 0,
            ),
        )

    @staticmethod
    def _state_db_activity_score(db_path: Path) -> float:
        if not db_path.exists():
            return -1.0
        score = db_path.stat().st_mtime
        wal_path = Path(str(db_path) + "-wal")
        if wal_path.exists() and wal_path.stat().st_size > 0:
            score = max(score, wal_path.stat().st_mtime)
        return score

    def status(self) -> dict[str, Any]:
        integrity = "missing"
        if self.db_path.exists():
            try:
                with closing(self._connect(read_only=True)) as conn:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:
                integrity = f"error: {exc}"

        return {
            "codex_home": str(self.codex_home),
            "sqlite_home": str(self.sqlite_home),
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
            "integrity_check": integrity,
            "blocking_processes": self.blocking_processes(),
            "wal_files": self._wal_files(),
            "session_index": self.session_index_status(),
            "current_config": self.current_config(),
            "session_stats": self.session_stats() if self.db_path.exists() else {},
            "providers": self.list_providers() if self.db_path.exists() else [],
            "target_providers": self.list_target_providers() if self.db_path.exists() else [],
            "package_source": self.package_status(),
            "skills": self.skills_status(),
        }

    def session_index_status(self) -> dict[str, Any]:
        entries = self._load_session_index()
        return {
            "path": str(self.session_index_path),
            "exists": self.session_index_path.exists(),
            "entries": len(entries),
        }

    def list_providers(self) -> list[dict[str, Any]]:
        with closing(self._connect(read_only=True)) as conn:
            return self._list_providers_from_connection(conn)

    def _list_providers_from_connection(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT model_provider, archived, COUNT(*) AS count
            FROM threads
            GROUP BY model_provider, archived
            ORDER BY model_provider, archived
            """
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            provider = row["model_provider"]
            item = grouped.setdefault(
                provider,
                {"model_provider": provider, "active": 0, "archived": 0, "total": 0},
            )
            count = int(row["count"])
            if int(row["archived"]):
                item["archived"] += count
            else:
                item["active"] += count
            item["total"] += count
        return list(grouped.values())

    def current_config(self) -> dict[str, Any]:
        config_path = self.codex_home / "config.toml"
        result: dict[str, Any] = {
            "config_path": str(config_path),
            "exists": config_path.exists(),
            "model_provider": None,
            "model": None,
            "base_url": None,
            "provider_name": None,
            "wire_api": None,
            "configured_provider_ids": [],
        }
        if not config_path.exists():
            return result
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            result["error"] = str(exc)
            return result
        provider_id = data.get("model_provider")
        providers = data.get("model_providers") or {}
        provider_info = providers.get(provider_id) if isinstance(providers, dict) else None
        result.update(
            {
                "model_provider": provider_id,
                "model": data.get("model"),
                "configured_provider_ids": sorted(providers.keys()) if isinstance(providers, dict) else [],
            }
        )
        if isinstance(provider_info, dict):
            result["provider_name"] = provider_info.get("name")
            result["base_url"] = provider_info.get("base_url")
            result["wire_api"] = provider_info.get("wire_api")
        elif provider_id:
            result["error"] = (
                f"Model provider '{provider_id}' is not defined in config.toml. "
                "Add a matching [model_providers.<id>] section or select a configured provider."
            )
        return result

    def _configured_provider_details(self) -> dict[str, dict[str, Any]]:
        """Read every provider section so source and target use one catalog."""
        config_path = self.codex_home / "config.toml"
        if not config_path.exists():
            return {}
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        providers = data.get("model_providers") or {}
        if not isinstance(providers, dict):
            return {}

        details: dict[str, dict[str, Any]] = {}
        for provider_id, provider_info in providers.items():
            if not isinstance(provider_info, dict):
                provider_info = {}
            details[str(provider_id)] = {
                "provider_name": provider_info.get("name"),
                "base_url": provider_info.get("base_url"),
                "wire_api": provider_info.get("wire_api"),
                "model": data.get("model"),
            }
        return details

    def list_target_providers(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        def upsert(value: str, **fields: Any) -> None:
            if not value:
                return
            item = merged.setdefault(
                value,
                {
                    "value": value,
                    "label": value,
                    "sources": [],
                    "session_total": 0,
                    "current": False,
                },
            )
            source = fields.pop("source", None)
            if source and source not in item["sources"]:
                item["sources"].append(source)
            for key, field_value in fields.items():
                if field_value not in (None, "", []):
                    item[key] = field_value

        for provider in self.list_providers():
            upsert(
                provider["model_provider"],
                source="session_db",
                label=provider["model_provider"],
                session_total=provider["total"],
                session_active=provider["active"],
                session_archived=provider["archived"],
            )

        current = self.current_config()
        current_provider = current.get("model_provider")
        if current_provider:
            upsert(
                str(current_provider),
                source="live_config",
                current=True,
                provider_name=current.get("provider_name"),
                model=current.get("model"),
                base_url=current.get("base_url"),
                wire_api=current.get("wire_api"),
            )

        for provider_id, details in self._configured_provider_details().items():
            upsert(
                provider_id,
                source="live_config" if provider_id == current_provider else "config",
                current=provider_id == current_provider,
                provider_name=details.get("provider_name"),
                model=details.get("model"),
                base_url=details.get("base_url"),
                wire_api=details.get("wire_api"),
            )

        for value, item in merged.items():
            if value == current_provider:
                item["current"] = True

        def sort_key(item: dict[str, Any]) -> tuple[int, str]:
            priority = 0 if item.get("current") else 1
            return priority, str(item.get("label") or item["value"]).lower()

        return sorted(merged.values(), key=sort_key)

    def session_stats(self) -> dict[str, Any]:
        with closing(self._connect(read_only=True)) as conn:
            return self._session_stats_from_connection(conn, include_current_config=True)

    def _session_stats_from_connection(
        self,
        conn: sqlite3.Connection,
        *,
        include_current_config: bool,
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT id, cwd, model_provider, archived, rollout_path, preview
            FROM threads
            ORDER BY cwd, model_provider
            """
        ).fetchall()
        totals = {
            "total": 0,
            "active": 0,
            "archived": 0,
            "missing_rollouts": 0,
            "hidden_empty_preview": 0,
            "projects": 0,
        }
        by_provider: dict[str, dict[str, Any]] = {}
        by_project: dict[str, dict[str, Any]] = {}

        for row in rows:
            provider = str(row["model_provider"])
            cwd_raw = str(row["cwd"])
            cwd = self._normalize_windows_path(cwd_raw)
            archived = bool(row["archived"])
            missing = not Path(row["rollout_path"]).exists()
            empty_preview = row["preview"] == ""

            totals["total"] += 1
            totals["archived" if archived else "active"] += 1
            totals["missing_rollouts"] += 1 if missing else 0
            totals["hidden_empty_preview"] += 1 if empty_preview else 0

            provider_stats = by_provider.setdefault(
                provider,
                {
                    "total": 0,
                    "active": 0,
                    "archived": 0,
                    "missing_rollouts": 0,
                    "hidden_empty_preview": 0,
                    "projects": set(),
                },
            )
            provider_stats["total"] += 1
            provider_stats["archived" if archived else "active"] += 1
            provider_stats["missing_rollouts"] += 1 if missing else 0
            provider_stats["hidden_empty_preview"] += 1 if empty_preview else 0
            provider_stats["projects"].add(cwd)

            project = by_project.setdefault(
                cwd,
                {
                    "cwd": cwd_raw,
                    "normalized_cwd": cwd,
                    "label": self._project_label(cwd),
                    "total": 0,
                    "active": 0,
                    "archived": 0,
                    "missing_rollouts": 0,
                    "hidden_empty_preview": 0,
                    "providers": {},
                },
            )
            project["total"] += 1
            project["archived" if archived else "active"] += 1
            project["missing_rollouts"] += 1 if missing else 0
            project["hidden_empty_preview"] += 1 if empty_preview else 0
            project["providers"][provider] = project["providers"].get(provider, 0) + 1

        totals["projects"] = len(by_project)
        provider_output = {}
        for provider, item in by_provider.items():
            provider_output[provider] = {
                **{key: value for key, value in item.items() if key != "projects"},
                "projects": len(item["projects"]),
            }

        project_output = sorted(
            by_project.values(),
            key=lambda item: (-int(item["total"]), str(item["normalized_cwd"]).lower()),
        )
        return {
            "totals": totals,
            "by_provider": provider_output,
            "by_project": project_output,
            "current_config": self.current_config() if include_current_config else {},
        }

    def list_threads(
        self,
        *,
        source_provider: str | None = None,
        include_archived: bool = False,
        search: str = "",
        cwd: str = "",
        source: str = "",
        date_from: str = "",
        date_to: str = "",
        recent_limit: int = 0,
    ) -> list[dict[str, Any]]:
        with closing(self._connect(read_only=True)) as conn:
            return self._list_threads_from_connection(
                conn,
                session_index=self._load_session_index(),
                source_provider=source_provider,
                include_archived=include_archived,
                search=search,
                cwd=cwd,
                source=source,
                date_from=date_from,
                date_to=date_to,
                recent_limit=recent_limit,
            )

    def _list_threads_from_connection(
        self,
        conn: sqlite3.Connection,
        *,
        session_index: dict[str, dict[str, Any]],
        source_provider: str | None = None,
        include_archived: bool = False,
        search: str = "",
        cwd: str = "",
        source: str = "",
        date_from: str = "",
        date_to: str = "",
        recent_limit: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        updated_expr = "COALESCE(updated_at_ms, updated_at * 1000)"
        if source_provider:
            clauses.append("model_provider = ?")
            params.append(source_provider)
        if not include_archived:
            clauses.append("archived = 0")
        if search:
            clauses.append(
                "(title LIKE ? OR preview LIKE ? OR first_user_message LIKE ? OR id LIKE ?)"
            )
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern, pattern])
        if cwd:
            clauses.append(f"{self._cwd_match_sql()} = ?")
            params.append(self._path_match_key(cwd))
        if source:
            clauses.append("source = ?")
            params.append(source)
        from_ms = self._date_start_ms(date_from)
        if from_ms is not None:
            clauses.append(f"{updated_expr} >= ?")
            params.append(from_ms)
        to_ms = self._date_start_ms(date_to)
        if to_ms is not None:
            clauses.append(f"{updated_expr} < ?")
            params.append(to_ms + 24 * 60 * 60 * 1000)

        query = f"""
            SELECT *
            FROM threads
            WHERE {' AND '.join(clauses)}
            ORDER BY {updated_expr} DESC, id DESC
        """
        if recent_limit > 0:
            query += "\nLIMIT ?"
            params.append(recent_limit)
        rows = conn.execute(query, params).fetchall()
        return [
            self._thread_summary(conn, row, session_index, compact=True)
            for row in rows
        ]

    def thread_detail(
        self,
        thread_id: str,
        *,
        item_offset: int = 0,
        item_limit: int = DEFAULT_THREAD_DETAIL_LIMIT,
    ) -> dict[str, Any]:
        with closing(self._connect(read_only=True)) as conn:
            return self._thread_detail_from_connection(
                conn,
                thread_id,
                session_index=self._load_session_index(),
                item_offset=item_offset,
                item_limit=item_limit,
            )

    def _thread_detail_from_connection(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        *,
        session_index: dict[str, dict[str, Any]],
        item_offset: int = 0,
        item_limit: int = DEFAULT_THREAD_DETAIL_LIMIT,
    ) -> dict[str, Any]:
        clean_id = thread_id.strip()
        if not clean_id:
            raise ValueError("thread id is required")
        item_offset = max(0, int(item_offset))
        item_limit = min(MAX_THREAD_DETAIL_LIMIT, max(1, int(item_limit)))

        row = conn.execute("SELECT * FROM threads WHERE id = ?", (clean_id,)).fetchone()
        if row is None:
            return {"ok": False, "errors": [f"Thread not found: {clean_id}"]}
        thread = self._thread_summary(conn, row, session_index)

        rollout_path = Path(row["rollout_path"])
        rollout = {
            "path": str(rollout_path),
            "exists": rollout_path.exists(),
            "line_count": 0,
        }
        errors: list[str] = []
        meta: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        item_total = 0
        has_more = False

        if not rollout_path.exists():
            errors.append(f"Rollout file missing: {rollout_path}")
        elif rollout_path.suffix != ".jsonl":
            errors.append(f"Unsupported rollout format: {rollout_path.name}")
        else:
            parsed = self._parse_rollout_for_render(
                rollout_path,
                item_offset=item_offset,
                item_limit=item_limit,
            )
            rollout["line_count"] = parsed["line_count"]
            errors.extend(parsed["errors"])
            meta = parsed["meta"]
            items = parsed["items"]
            item_total = parsed["item_total"]
            has_more = parsed["has_more"]

        return {
            "ok": not errors or bool(thread),
            "errors": errors,
            "thread": thread,
            "meta": meta,
            "rollout": rollout,
            "items": items,
            "item_offset": item_offset,
            "item_limit": item_limit,
            "item_total": item_total,
            "has_more": has_more,
        }

    @staticmethod
    def _report_copy_progress(
        callback: Callable[[dict[str, Any]], None] | None,
        *,
        phase: str,
        current: int = 0,
        total: int = 0,
        message: str = "",
        item: dict[str, Any] | None = None,
    ) -> None:
        if callback is None:
            return
        event: dict[str, Any] = {
            "type": "progress",
            "phase": phase,
            "current": max(0, int(current)),
            "total": max(0, int(total)),
        }
        if message:
            event["message"] = message
        if item:
            item_title = item.get("display_title") or item.get("title") or item.get("source_id")
            event["item_title"] = _clip_preview_text(item_title)
            event["source_id"] = item.get("source_id")
        try:
            callback(event)
        except OSError:
            # A disconnected browser should not interrupt an already started copy.
            pass

    def preview_copy(self, request: CopyRequest) -> dict[str, Any]:
        with closing(self._connect(read_only=True)) as conn:
            plan = self._build_copy_plan(conn, request)
        return self._public_plan(plan, request=request)

    def copy_threads(
        self,
        request: CopyRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if request.mirror_target:
            return self._mirror_provider_threads(request, progress_callback)
        progress_total = max(len(request.thread_ids), 1)
        self._report_copy_progress(
            progress_callback,
            phase="checking",
            total=progress_total,
            message="Checking the destination and selected sessions",
        )
        blocking = self.blocking_processes()
        if blocking:
            self._report_copy_progress(
                progress_callback,
                phase="blocked",
                total=progress_total,
                message="Close Codex and the provider switcher before copying",
            )
            payload = {
                "ok": False,
                "blocked": True,
                "errors": ["Close Codex and provider switcher processes before copying."],
                "blocking_processes": blocking,
            }
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="planning",
            total=progress_total,
            message="Building the copy plan",
        )
        preflight = self.preview_copy(request)
        if not preflight["can_execute"]:
            self._report_copy_progress(
                progress_callback,
                phase="error",
                total=progress_total,
                message=(preflight.get("errors") or ["Copy plan is not executable"])[0],
            )
            payload = {"ok": False, "blocked": False, **preflight}
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="ready",
            total=int(preflight.get("item_total") or len(preflight.get("items") or [])),
            message=(
                f"Ready to copy {int(preflight.get('item_total') or len(preflight.get('items') or []))} session(s)"
            ),
        )

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_database()
        copied_paths: list[Path] = []
        rollout_backups: list[tuple[Path, Path]] = []
        overwrite_paths: set[Path] = set()
        session_index_snapshot: tuple[bool, str] | None = None
        session_index_entries: list[dict[str, Any]] = []
        manifest_payload: dict[str, Any] | None = None

        with closing(self._connect(read_only=False)) as conn:
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                plan = self._build_copy_plan(conn, request)
                if plan["errors"]:
                    conn.rollback()
                    manifest_payload = {
                        "ok": False,
                        "blocked": False,
                        **self._public_plan(plan),
                        "backup_path": str(backup_path),
                    }
                else:
                    rollout_backups = self._prepare_import_overwrite(conn, plan)
                    overwrite_paths = {path for path, _ in rollout_backups}
                    if request.overwrite:
                        target_index_ids = set(self._load_session_index())
                        plan["_overwrite_session_index_ids"] = set(
                            plan.get("_overwrite_session_index_ids", set())
                        ) | target_index_ids.intersection(plan["_id_map"].values())
                    total_items = len(plan["items"])
                    self._write_rollout_plan_concurrently(
                        plan,
                        overwrite_paths=overwrite_paths,
                        copied_paths=copied_paths,
                        progress_callback=progress_callback,
                    )

                    self._insert_thread_rows(conn, plan, plan["target_provider"])
                    self._insert_spawn_edges(conn, plan)
                    self._insert_dynamic_tools(conn, plan)
                    self._report_copy_progress(
                        progress_callback,
                        phase="committing",
                        current=total_items,
                        total=total_items,
                        message="Committing the database and session index",
                    )
                    session_index_snapshot = self._snapshot_session_index()
                    session_index_entries = self._append_session_index_entries(plan)
                    conn.commit()
                    self._discard_rollout_backups(
                        rollout_backups,
                        {Path(item["dest_rollout_path"]) for item in plan["items"]},
                    )

                    manifest_payload = {
                        "ok": True,
                        "blocked": False,
                        **self._public_plan(plan),
                        "backup_path": str(backup_path),
                        "session_index_path": str(self.session_index_path),
                        "session_index_entries": len(session_index_entries),
                    }
            except Exception as exc:
                conn.rollback()
                if session_index_snapshot is not None:
                    self._restore_session_index(session_index_snapshot)
                for path in copied_paths:
                    if path in overwrite_paths:
                        continue
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                self._restore_rollout_backups(rollout_backups)
                for _, backup in rollout_backups:
                    try:
                        backup.unlink()
                    except FileNotFoundError:
                        pass
                manifest_payload = {
                    "ok": False,
                    "blocked": False,
                    "errors": [str(exc)],
                    "items": [],
                    "backup_path": str(backup_path),
                }

        manifest_path = self._write_manifest(manifest_payload, request)
        manifest_payload["manifest_path"] = str(manifest_path)
        self._report_copy_progress(
            progress_callback,
            phase="done" if manifest_payload.get("ok") else "error",
            current=len(manifest_payload.get("items") or []),
            total=len(manifest_payload.get("items") or []) or progress_total,
            message=(
                f"Copied {len(manifest_payload.get('items') or [])} session(s)"
                if manifest_payload.get("ok")
                else (manifest_payload.get("errors") or ["Copy failed"])[0]
            ),
        )
        return manifest_payload

    def _mirror_provider_threads(
        self,
        request: CopyRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self._report_copy_progress(
            progress_callback,
            phase="checking",
            message="Checking the source, target, and running Codex applications",
        )
        blocking = self.blocking_processes()
        if blocking:
            self._report_copy_progress(
                progress_callback,
                phase="blocked",
                message="Close ChatGPT, Codex, and provider switcher processes before mirroring",
            )
            payload = {
                "ok": False,
                "blocked": True,
                "errors": [
                    "Close ChatGPT, Codex, and provider switcher processes before mirroring."
                ],
                "blocking_processes": blocking,
                "items": [],
                "mirror_target": True,
            }
            manifest_path = self._write_manifest(payload, request, operation="mirror")
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="planning",
            message="Building the full provider mirror plan",
        )
        with closing(self._connect(read_only=True)) as conn:
            preflight_plan = self._build_copy_plan(conn, request)
        preflight = self._public_plan(preflight_plan, request=request)
        if not preflight["can_execute"]:
            self._report_copy_progress(
                progress_callback,
                phase="error",
                message=(preflight.get("errors") or ["Mirror plan is not executable"])[0],
            )
            payload = {"ok": False, "blocked": False, **preflight}
            manifest_path = self._write_manifest(payload, request, operation="mirror")
            payload["manifest_path"] = str(manifest_path)
            return payload

        total_items = len(preflight_plan["items"])
        source_ids_before_backup = set(preflight_plan.get("_ordered_ids", []))
        target_ids_before_backup = set(preflight_plan.get("_target_ids", set()))
        target_rollout_paths = self._mirror_target_rollout_paths(preflight_plan)
        source_rollout_paths = [
            Path(item["source_rollout_path"]) for item in preflight_plan["items"]
        ]
        backup_rollout_paths = list(
            dict.fromkeys([*source_rollout_paths, *target_rollout_paths])
        )
        self._report_copy_progress(
            progress_callback,
            phase="ready",
            total=total_items,
            message=(
                f"Ready to mirror {total_items} source session(s) over "
                f"{len(target_ids_before_backup)} target session(s)"
            ),
        )

        backup_dir = self._new_mirror_backup_directory()
        database_backups: dict[str, str] = {}
        rollout_backups: list[RolloutBackup] = []
        session_index_backup: str | None = None
        try:
            self._report_copy_progress(
                progress_callback,
                phase="backing_up",
                total=max(len(backup_rollout_paths), 1),
                message="Backing up Codex databases and all source/target sessions",
            )
            database_backups = self._backup_runtime_databases(backup_dir)
            session_index_backup = self._backup_session_index_for_mirror(backup_dir)
            rollout_backups = self._backup_mirror_rollouts(
                backup_rollout_paths,
                backup_dir,
                progress_callback=progress_callback,
            )
            self._write_mirror_backup_manifest(
                backup_dir,
                request,
                database_backups=database_backups,
                rollout_backups=rollout_backups,
                session_index_backup=session_index_backup,
                source_ids=source_ids_before_backup,
                target_ids=target_ids_before_backup,
            )
        except Exception as exc:
            self._report_copy_progress(
                progress_callback,
                phase="error",
                total=max(len(backup_rollout_paths), 1),
                message=f"Backup failed: {exc}",
            )
            payload = {
                "ok": False,
                "blocked": False,
                "errors": [f"Backup failed before any target data was changed: {exc}"],
                "items": [],
                "mirror_target": True,
                "backup_directory": str(backup_dir),
            }
            manifest_path = self._write_manifest(payload, request, operation="mirror")
            payload["manifest_path"] = str(manifest_path)
            return payload

        rewritten_paths: list[Path] = []
        session_index_snapshot: tuple[bool, str] | None = None
        session_index_entries: list[dict[str, Any]] = []
        manifest_payload: dict[str, Any] | None = None
        committed = False
        plan: dict[str, Any] | None = None
        with closing(self._connect(read_only=False)) as conn:
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                attached = self._attach_mirror_sidecars(conn)
                conn.execute("BEGIN IMMEDIATE")
                plan = self._build_copy_plan(conn, request)
                if plan["errors"]:
                    raise RuntimeError("; ".join(plan["errors"]))
                if set(plan.get("_target_ids", set())) != target_ids_before_backup:
                    raise RuntimeError(
                        "Target sessions changed after backup; close all Codex applications and retry"
                    )
                if set(plan.get("_ordered_ids", [])) != source_ids_before_backup:
                    raise RuntimeError(
                        "Source sessions changed after backup; close all Codex applications and retry"
                    )

                self._report_copy_progress(
                    progress_callback,
                    phase="clearing",
                    total=len(target_ids_before_backup),
                    message="Removing the old target-provider database records",
                )
                self._clear_mirror_target_records(conn, plan, attached)
                self._write_rollout_plan_concurrently(
                    plan,
                    overwrite_paths=set(source_rollout_paths),
                    copied_paths=rewritten_paths,
                    progress_callback=progress_callback,
                )
                updated = 0
                for chunk in self._id_chunks(sorted(source_ids_before_backup)):
                    placeholders = ",".join("?" for _ in chunk)
                    updated += conn.execute(
                        "UPDATE threads SET model_provider = ? "
                        f"WHERE id IN ({placeholders}) AND model_provider = ?",
                        [plan["target_provider"], *chunk, plan["source_provider"]],
                    ).rowcount
                if updated != total_items:
                    raise RuntimeError(
                        f"Expected to rebind {total_items} source session(s), but updated {updated}"
                    )

                self._report_copy_progress(
                    progress_callback,
                    phase="indexing",
                    current=total_items,
                    total=total_items,
                    message="Replacing target session names and indexes",
                )
                session_index_snapshot = self._snapshot_session_index()
                session_index_entries = self._replace_session_index_for_mirror(plan)

                self._report_copy_progress(
                    progress_callback,
                    phase="verifying",
                    current=total_items,
                    total=total_items,
                    message="Verifying provider routing, database rows, and copied files",
                )
                self._verify_mirror(conn, plan, attached)
                self._report_copy_progress(
                    progress_callback,
                    phase="committing",
                    current=total_items,
                    total=total_items,
                    message="Committing the provider mirror",
                )
                conn.commit()
                committed = True
                manifest_payload = {
                    "ok": True,
                    "blocked": False,
                    **self._public_plan(plan),
                    "backup_path": database_backups.get(STATE_DB_FILENAME),
                    "backup_directory": str(backup_dir),
                    "database_backups": database_backups,
                    "rollout_backup_count": len(rollout_backups),
                    "source_rollout_backup_count": len(source_rollout_paths),
                    "target_rollout_backup_count": len(target_rollout_paths),
                    "rebound_source_count": updated,
                    "session_index_backup": session_index_backup,
                    "session_index_path": str(self.session_index_path),
                    "session_index_entries": len(session_index_entries),
                    "history_projection_mode": "rebuild_on_resume",
                }
            except BaseException as exc:
                restore_errors: list[str] = []
                if not committed:
                    conn.rollback()
                    if session_index_snapshot is not None:
                        self._restore_session_index(session_index_snapshot)
                    restore_errors = self._restore_mirror_rollout_backups(rollout_backups)
                manifest_payload = {
                    "ok": False,
                    "blocked": False,
                    "errors": [str(exc)],
                    "items": [],
                    "mirror_target": True,
                    "backup_path": database_backups.get(STATE_DB_FILENAME),
                    "backup_directory": str(backup_dir),
                    "database_backups": database_backups,
                    "rollout_backup_count": len(rollout_backups),
                    "rolled_back": not committed,
                }
                if restore_errors:
                    manifest_payload["restore_errors"] = restore_errors

        if manifest_payload is None:
            manifest_payload = {
                "ok": False,
                "blocked": False,
                "errors": ["Provider mirror ended without a result"],
                "items": [],
                "mirror_target": True,
                "backup_directory": str(backup_dir),
            }

        if manifest_payload.get("ok") and plan is not None:
            protected_paths = set(source_rollout_paths) | set(rewritten_paths)
            cleanup_paths = [
                path for path in target_rollout_paths if path not in protected_paths
            ]
            cleanup_errors = self._delete_mirrored_target_rollouts(
                cleanup_paths,
                progress_callback=progress_callback,
            )
            if cleanup_errors:
                manifest_payload.setdefault("warnings", []).append(
                    f"{len(cleanup_errors)} obsolete target rollout file(s) could not be removed; "
                    "they are no longer referenced by the database."
                )
                manifest_payload["cleanup_errors"] = cleanup_errors
            manifest_payload["deleted_target_rollout_count"] = (
                len(cleanup_paths) - len(cleanup_errors)
            )

        manifest_path = self._write_manifest(manifest_payload, request, operation="mirror")
        manifest_payload["manifest_path"] = str(manifest_path)
        result_items = manifest_payload.get("items") or []
        self._report_copy_progress(
            progress_callback,
            phase="done" if manifest_payload.get("ok") else "error",
            current=len(result_items),
            total=len(result_items) or max(total_items, 1),
            message=(
                f"Mirrored {len(result_items)} session(s)"
                if manifest_payload.get("ok")
                else (manifest_payload.get("errors") or ["Provider mirror failed"])[0]
            ),
        )
        return manifest_payload

    def preview_rebind(self, request: RebindRequest) -> dict[str, Any]:
        with closing(self._connect(read_only=True)) as conn:
            plan = self._build_rebind_plan(conn, request)
        return self._public_rebind_plan(plan)

    def rebind_threads(
        self,
        request: RebindRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        progress_total = max(len(request.thread_ids), 1)
        self._report_copy_progress(
            progress_callback,
            phase="checking",
            total=progress_total,
            message="Checking the selected sessions and target provider",
        )
        blocking = self.blocking_processes()
        if blocking:
            self._report_copy_progress(
                progress_callback,
                phase="blocked",
                total=progress_total,
                message="Close Codex and the provider switcher before rebinding",
            )
            payload = {
                "ok": False,
                "blocked": True,
                "errors": ["Close Codex and provider switcher processes before rebinding."],
                "blocking_processes": blocking,
                "items": [],
            }
            manifest_path = self._write_manifest(payload, request, operation="rebind")
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="planning",
            total=progress_total,
            message="Building the provider rebind plan",
        )
        preflight = self.preview_rebind(request)
        if not preflight["can_execute"]:
            self._report_copy_progress(
                progress_callback,
                phase="error",
                total=progress_total,
                message=(preflight.get("errors") or ["Rebind plan is not executable"])[0],
            )
            payload = {"ok": False, "blocked": False, **preflight}
            manifest_path = self._write_manifest(payload, request, operation="rebind")
            payload["manifest_path"] = str(manifest_path)
            return payload

        item_total = int(preflight.get("item_total") or len(preflight.get("items") or []))
        self._report_copy_progress(
            progress_callback,
            phase="ready",
            total=item_total,
            message=f"Ready to rebind {item_total} session(s)",
        )

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_database(operation="rebind")
        rollout_backups: list[tuple[Path, Path]] = []
        rewritten_paths: set[Path] = set()
        manifest_payload: dict[str, Any] | None = None

        with closing(self._connect(read_only=False)) as conn:
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                plan = self._build_rebind_plan(conn, request)
                if plan["errors"]:
                    conn.rollback()
                    manifest_payload = {
                        "ok": False,
                        "blocked": False,
                        **self._public_rebind_plan(plan),
                        "backup_path": str(backup_path),
                    }
                else:
                    rollout_paths = list(
                        dict.fromkeys(Path(item["rollout_path"]) for item in plan["items"])
                    )
                    rollout_backups = self._backup_rollout_files(
                        rollout_paths,
                        operation="rebind",
                    )
                    total_items = len(plan["items"])
                    for index, item in enumerate(plan["items"], start=1):
                        rollout_path = Path(item["rollout_path"])
                        if rollout_path not in rewritten_paths:
                            self._write_rollout_provider(
                                rollout_path,
                                plan["target_provider"],
                            )
                            rewritten_paths.add(rollout_path)
                        self._report_copy_progress(
                            progress_callback,
                            phase="rebinding",
                            current=index,
                            total=total_items,
                            item=item,
                        )

                    ordered_ids = plan["_ordered_ids"]
                    placeholders = ",".join("?" for _ in ordered_ids)
                    updated = conn.execute(
                        "UPDATE threads SET model_provider = ? "
                        f"WHERE id IN ({placeholders}) AND model_provider = ?",
                        [plan["target_provider"], *ordered_ids, plan["source_provider"]],
                    ).rowcount
                    if updated != total_items:
                        raise RuntimeError(
                            f"Expected to update {total_items} session(s), but updated {updated}"
                        )

                    self._report_copy_progress(
                        progress_callback,
                        phase="committing",
                        current=total_items,
                        total=total_items,
                        message="Committing the provider rebind",
                    )
                    conn.commit()
                    manifest_payload = {
                        "ok": True,
                        "blocked": False,
                        **self._public_rebind_plan(plan),
                        "backup_path": str(backup_path),
                        "rollout_backup_paths": [str(backup) for _, backup in rollout_backups],
                        "session_index_path": str(self.session_index_path),
                        "session_index_entries": 0,
                    }
            except Exception as exc:
                conn.rollback()
                self._restore_rollout_backups(rollout_backups)
                manifest_payload = {
                    "ok": False,
                    "blocked": False,
                    "errors": [str(exc)],
                    "items": [],
                    "backup_path": str(backup_path),
                    "rollout_backup_paths": [str(backup) for _, backup in rollout_backups],
                    "rolled_back": True,
                }

        if manifest_payload is None:
            manifest_payload = {
                "ok": False,
                "blocked": False,
                "errors": ["Provider rebind ended without a result"],
                "items": [],
                "backup_path": str(backup_path),
            }
        manifest_path = self._write_manifest(manifest_payload, request, operation="rebind")
        manifest_payload["manifest_path"] = str(manifest_path)
        result_items = manifest_payload.get("items") or []
        self._report_copy_progress(
            progress_callback,
            phase="done" if manifest_payload.get("ok") else "error",
            current=len(result_items),
            total=len(result_items) or progress_total,
            message=(
                f"Rebound {len(result_items)} session(s)"
                if manifest_payload.get("ok")
                else (manifest_payload.get("errors") or ["Provider rebind failed"])[0]
            ),
        )
        return manifest_payload

    def blocking_processes(self) -> list[dict[str, Any]]:
        matches = []
        for process in self.process_checker():
            name = str(process.get("name", ""))
            if normalize_process_name(name) in BLOCKING_PROCESS_NAMES:
                matches.append(process)
        return matches

    def kill_blocking_processes(self) -> dict[str, Any]:
        targets = self.blocking_processes()
        results = []
        for process in targets:
            pid_value = process.get("pid")
            try:
                pid = int(pid_value)
            except (TypeError, ValueError):
                results.append(
                    {
                        "ok": False,
                        "process": process,
                        "error": f"Invalid PID: {pid_value}",
                    }
                )
                continue
            result = self.process_terminator(pid)
            results.append({"process": process, **result})

        remaining = self.blocking_processes()
        return {
            "ok": not any(not item.get("ok") for item in results),
            "targets": targets,
            "results": results,
            "killed_count": sum(1 for item in results if item.get("ok")),
            "remaining_blocking_processes": remaining,
        }

    def repair_session_index_from_manifests(self) -> dict[str, Any]:
        blocking = self.blocking_processes()
        if blocking:
            return {
                "ok": False,
                "blocked": True,
                "errors": ["Close Codex and provider switcher processes before repairing session names."],
                "blocking_processes": blocking,
            }

        source_index = self._load_session_index()
        existing_ids = set(source_index)
        entries: list[dict[str, Any]] = []
        scanned = 0
        for manifest_path in sorted(self.manifest_dir.glob("copy-*.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not manifest.get("ok"):
                continue
            scanned += 1
            for item in manifest.get("items") or []:
                if not isinstance(item, dict):
                    continue
                source_id = item.get("source_id")
                target_id = item.get("target_id")
                if not source_id or not target_id or target_id in existing_ids:
                    continue
                source_entry = source_index.get(str(source_id))
                if not source_entry:
                    continue
                thread_name = source_entry.get("thread_name")
                if not thread_name:
                    continue
                entries.append(
                    {
                        "id": str(target_id),
                        "thread_name": thread_name,
                        "updated_at": source_entry.get("updated_at") or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    }
                )
                existing_ids.add(str(target_id))

        snapshot = self._snapshot_session_index()
        try:
            self._write_session_index_entries(entries)
        except Exception:
            self._restore_session_index(snapshot)
            raise

        return {
            "ok": True,
            "blocked": False,
            "scanned_manifests": scanned,
            "repaired_count": len(entries),
            "session_index_path": str(self.session_index_path),
        }

    def skills_status(self) -> dict[str, Any]:
        skills = self.list_skills()
        return {
            "root": str(self.skills_root),
            "exists": self.skills_root.exists(),
            "total": len(skills),
            "package_source": self.skills_package_status(),
        }

    def list_skills(self, *, search: str = "") -> list[dict[str, Any]]:
        return self._list_skills_from_root(self.skills_root, search=search)

    def export_skills_package(self, request: SkillPackageRequest) -> dict[str, Any]:
        errors: list[str] = []
        selected_ids = self._dedupe(request.skill_ids)
        if not selected_ids:
            errors.append("Select at least one skill")

        local_skills = {skill["id"]: skill for skill in self.list_skills()}
        for skill_id in selected_ids:
            if not self._is_safe_skill_id(skill_id):
                errors.append(f"Invalid skill id: {skill_id}")
            elif skill_id not in local_skills:
                errors.append(f"Skill not found: {skill_id}")
        if errors:
            return {"ok": False, "can_execute": False, "errors": errors, "items": []}

        package_path = self._skill_package_output_path(request.output_path)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                staging_dir = Path(tmp)
                manifest = self._stage_skills_package(selected_ids, staging_dir)
                self._zip_directory(staging_dir, package_path)
        except Exception as exc:
            return {"ok": False, "can_execute": False, "errors": [str(exc)], "items": []}

        return {
            "ok": True,
            "can_execute": True,
            "package_path": str(package_path),
            "skill_count": len(selected_ids),
            "items": manifest["skills"],
        }

    def load_skills_package(self, package_path: Path) -> dict[str, Any]:
        path = Path(package_path).expanduser()
        if not path.exists():
            return {"ok": False, "loaded": False, "errors": [f"Package not found: {path}"]}
        if not path.is_file():
            return {"ok": False, "loaded": False, "errors": [f"Package path is not a file: {path}"]}

        extract_dir = self._new_skill_package_extract_dir(path)
        try:
            with zipfile.ZipFile(path) as package:
                manifest = self._read_skill_package_manifest(package)
                self._safe_extract_zip(package, extract_dir)
            for skill in manifest.get("skills") or []:
                skill_id = str(skill.get("id") or "")
                if not self._is_safe_skill_id(skill_id):
                    raise ValueError(f"Invalid skill id in package: {skill_id}")
                if not (extract_dir / SKILL_PACKAGE_DIRNAME / skill_id / "SKILL.md").exists():
                    raise ValueError(f"Package skill is missing SKILL.md: {skill_id}")
            self.loaded_skill_package = LoadedSkillPackage(
                package_path=path,
                extract_dir=extract_dir,
                manifest=manifest,
            )
        except Exception as exc:
            try:
                shutil.rmtree(extract_dir)
            except FileNotFoundError:
                pass
            return {"ok": False, "loaded": False, "errors": [str(exc)]}

        return {"ok": True, **self.skills_package_status()}

    def load_uploaded_skills_package(self, filename: str, content: bytes) -> dict[str, Any]:
        try:
            path = self._save_uploaded_package_file(
                filename,
                content,
                self.skill_package_dir,
                "codex-skills-package-upload",
            )
        except ValueError as exc:
            return {"ok": False, "loaded": False, "errors": [str(exc)]}
        return self.load_skills_package(path)

    def unload_skills_package(self) -> dict[str, Any]:
        self.loaded_skill_package = None
        return {"ok": True, "loaded": False}

    def skills_package_status(self) -> dict[str, Any]:
        package = self.loaded_skill_package
        if package is None:
            return {"loaded": False, "manifest": None, "skills": []}
        skills = self.list_package_skills()
        return {
            "loaded": True,
            "package_path": str(package.package_path),
            "extract_dir": str(package.extract_dir),
            "manifest": package.manifest,
            "skills": skills,
            "total": len(skills),
        }

    def list_package_skills(self, *, search: str = "") -> list[dict[str, Any]]:
        package = self._require_loaded_skill_package()
        return self._list_skills_from_root(
            package.extract_dir / SKILL_PACKAGE_DIRNAME,
            search=search,
            installed_root=self.skills_root,
        )

    def preview_import_skills(self, request: SkillImportRequest) -> dict[str, Any]:
        package = self._require_loaded_skill_package()
        errors: list[str] = []
        warnings: list[str] = []
        selected_ids = self._dedupe(request.skill_ids)
        if not selected_ids:
            errors.append("Select at least one skill")

        package_skills = {skill["id"]: skill for skill in self.list_package_skills()}
        items = []
        for skill_id in selected_ids:
            if not self._is_safe_skill_id(skill_id):
                errors.append(f"Invalid skill id: {skill_id}")
                continue
            skill = package_skills.get(skill_id)
            if skill is None:
                errors.append(f"Skill not found in package: {skill_id}")
                continue
            dest_path = self.skills_root / skill_id
            installed = dest_path.exists()
            if installed and not request.overwrite:
                errors.append(f"Skill already exists: {skill_id}")
            items.append(
                {
                    "id": skill_id,
                    "name": skill.get("name") or skill_id,
                    "description": skill.get("description") or "",
                    "source_path": str(package.extract_dir / SKILL_PACKAGE_DIRNAME / skill_id),
                    "dest_path": str(dest_path),
                    "installed": installed,
                    "action": "replace" if installed and request.overwrite else "create",
                }
            )

        return {
            "can_execute": not errors and bool(items),
            "errors": errors,
            "warnings": warnings,
            "items": items,
            "overwrite": request.overwrite,
            "package_path": str(package.package_path),
        }

    def import_skills(self, request: SkillImportRequest) -> dict[str, Any]:
        plan = self.preview_import_skills(request)
        if not plan["can_execute"]:
            return {"ok": False, "blocked": False, **plan}

        imported = []
        try:
            for item in plan["items"]:
                source_path = Path(item["source_path"])
                dest_path = Path(item["dest_path"])
                if dest_path.exists() and request.overwrite:
                    shutil.rmtree(dest_path)
                self._copy_tree_no_symlinks(source_path, dest_path)
                imported.append(item)
        except Exception as exc:
            return {"ok": False, "blocked": False, "errors": [str(exc)], "items": imported}

        return {"ok": True, "blocked": False, **plan, "imported_count": len(imported)}

    def export_package(self, request: ExportPackageRequest) -> dict[str, Any]:
        copy_request = CopyRequest(
            request.source_provider,
            f"{request.source_provider}__transfer_package_target__",
            request.thread_ids,
            request.include_descendants,
            request.include_archived,
        )
        with closing(self._connect(read_only=True)) as conn:
            plan = self._build_copy_plan(conn, copy_request)
            if plan["errors"]:
                return {
                    "ok": False,
                    "can_execute": False,
                    "errors": plan["errors"],
                    "warnings": plan["warnings"],
                    "items": plan["items"],
                }

            package_path = self._package_output_path(
                request.output_path,
                default_dir=self._default_export_dir(plan),
            )
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    staging_dir = Path(tmp)
                    manifest = self._stage_transfer_package(conn, plan, request, staging_dir)
                    self._zip_directory(staging_dir, package_path)
            except Exception as exc:
                return {
                    "ok": False,
                    "can_execute": False,
                    "errors": [str(exc)],
                    "warnings": [],
                    "items": [],
                }

        return {
            "ok": True,
            "can_execute": True,
            "package_path": str(package_path),
            "thread_count": len(plan["_ordered_ids"]),
            "project_count": len(manifest["projects"]),
            "source_provider": request.source_provider,
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "items": self._export_items(plan, manifest),
        }

    def open_path(self, path: Path) -> dict[str, Any]:
        if str(path).strip() in {"", "."}:
            return {"ok": False, "errors": ["Path is required"]}
        target = Path(path).expanduser()
        if not target.exists():
            return {"ok": False, "errors": [f"Path not found: {target}"]}
        opened_path = target.parent if target.is_file() else target
        try:
            self.path_opener(opened_path)
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)], "opened_path": str(opened_path)}
        return {"ok": True, "opened_path": str(opened_path)}

    def load_transfer_package(self, package_path: Path) -> dict[str, Any]:
        path = Path(package_path).expanduser()
        if not path.exists():
            return {"ok": False, "loaded": False, "errors": [f"Package not found: {path}"]}
        if not path.is_file():
            return {"ok": False, "loaded": False, "errors": [f"Package path is not a file: {path}"]}

        extract_dir = self._new_package_extract_dir(path)
        try:
            with zipfile.ZipFile(path) as package:
                manifest = self._read_package_manifest(package)
                self._safe_extract_zip(package, extract_dir)
            db_path = extract_dir / PACKAGE_DB_PATH
            session_index_path = extract_dir / SESSION_INDEX_FILENAME
            if not db_path.exists():
                raise ValueError(f"Package is missing {PACKAGE_DB_PATH}")
            self._materialize_package_rollout_paths(db_path, extract_dir)
            self.loaded_package = LoadedTransferPackage(
                package_path=path,
                extract_dir=extract_dir,
                db_path=db_path,
                session_index_path=session_index_path,
                manifest=manifest,
            )
        except Exception as exc:
            try:
                shutil.rmtree(extract_dir)
            except FileNotFoundError:
                pass
            return {"ok": False, "loaded": False, "errors": [str(exc)]}

        return {"ok": True, **self.package_status()}

    def load_uploaded_transfer_package(self, filename: str, content: bytes) -> dict[str, Any]:
        try:
            path = self._save_uploaded_package_file(
                filename,
                content,
                self.package_dir,
                "codex-session-package-upload",
            )
        except ValueError as exc:
            return {"ok": False, "loaded": False, "errors": [str(exc)]}
        return self.load_transfer_package(path)

    def unload_transfer_package(self) -> dict[str, Any]:
        self.loaded_package = None
        return {"ok": True, "loaded": False}

    def package_status(self) -> dict[str, Any]:
        package = self.loaded_package
        if package is None:
            return {"loaded": False, "providers": [], "session_stats": {}, "manifest": None}
        source_index = self._load_session_index_from_path(package.session_index_path)
        with closing(self._connect_path(package.db_path, read_only=True)) as conn:
            return {
                "loaded": True,
                "package_path": str(package.package_path),
                "extract_dir": str(package.extract_dir),
                "manifest": package.manifest,
                "providers": self._list_providers_from_connection(conn),
                "session_stats": self._session_stats_from_connection(
                    conn,
                    include_current_config=False,
                ),
                "session_index": {
                    "path": str(package.session_index_path),
                    "exists": package.session_index_path.exists(),
                    "entries": len(source_index),
                },
            }

    def list_package_threads(
        self,
        *,
        source_provider: str | None = None,
        include_archived: bool = False,
        search: str = "",
        cwd: str = "",
        source: str = "",
        date_from: str = "",
        date_to: str = "",
        recent_limit: int = 0,
    ) -> list[dict[str, Any]]:
        package = self._require_loaded_package()
        with closing(self._connect_path(package.db_path, read_only=True)) as conn:
            return self._list_threads_from_connection(
                conn,
                session_index=self._load_session_index_from_path(package.session_index_path),
                source_provider=source_provider,
                include_archived=include_archived,
                search=search,
                cwd=cwd,
                source=source,
                date_from=date_from,
                date_to=date_to,
                recent_limit=recent_limit,
            )

    def package_thread_detail(
        self,
        thread_id: str,
        *,
        item_offset: int = 0,
        item_limit: int = DEFAULT_THREAD_DETAIL_LIMIT,
    ) -> dict[str, Any]:
        package = self._require_loaded_package()
        with closing(self._connect_path(package.db_path, read_only=True)) as conn:
            return self._thread_detail_from_connection(
                conn,
                thread_id,
                session_index=self._load_session_index_from_path(package.session_index_path),
                item_offset=item_offset,
                item_limit=item_limit,
            )

    def preview_imported_package_copy(self, request: CopyRequest) -> dict[str, Any]:
        package = self._require_loaded_package()
        with closing(self._connect_path(package.db_path, read_only=True)) as source_conn:
            with closing(self._connect(read_only=True)) as target_conn:
                plan = self._build_copy_plan(
                    source_conn,
                    request,
                    target_conn=target_conn,
                    source_index=self._load_session_index_from_path(package.session_index_path),
                    dest_path_resolver=self._dest_rollout_path_for_import,
                )
        return self._public_plan(plan, request=request)

    def copy_imported_package_threads(
        self,
        request: CopyRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        package = self._require_loaded_package()
        progress_total = max(len(request.thread_ids), 1)
        self._report_copy_progress(
            progress_callback,
            phase="checking",
            total=progress_total,
            message="Checking the destination and selected sessions",
        )
        blocking = self.blocking_processes()
        if blocking:
            self._report_copy_progress(
                progress_callback,
                phase="blocked",
                total=progress_total,
                message="Close Codex and the provider switcher before importing",
            )
            payload = {
                "ok": False,
                "blocked": True,
                "errors": ["Close Codex and provider switcher processes before copying."],
                "blocking_processes": blocking,
                "package_path": str(package.package_path),
            }
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="planning",
            total=progress_total,
            message="Building the import plan",
        )
        preflight = self.preview_imported_package_copy(request)
        if not preflight["can_execute"]:
            self._report_copy_progress(
                progress_callback,
                phase="error",
                total=progress_total,
                message=(preflight.get("errors") or ["Import plan is not executable"])[0],
            )
            payload = {"ok": False, "blocked": False, **preflight, "package_path": str(package.package_path)}
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self._report_copy_progress(
            progress_callback,
            phase="ready",
            total=int(preflight.get("item_total") or len(preflight.get("items") or [])),
            message=(
                f"Ready to import {int(preflight.get('item_total') or len(preflight.get('items') or []))} session(s)"
            ),
        )

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_database()
        copied_paths: list[Path] = []
        rollout_backups: list[tuple[Path, Path]] = []
        overwrite_paths: set[Path] = set()
        session_index_snapshot: tuple[bool, str] | None = None
        session_index_entries: list[dict[str, Any]] = []
        manifest_payload: dict[str, Any] | None = None
        source_index = self._load_session_index_from_path(package.session_index_path)

        with closing(self._connect_path(package.db_path, read_only=True)) as source_conn:
            with closing(self._connect(read_only=False)) as target_conn:
                try:
                    target_conn.execute("PRAGMA foreign_keys = ON")
                    target_conn.execute("BEGIN IMMEDIATE")
                    plan = self._build_copy_plan(
                        source_conn,
                        request,
                        target_conn=target_conn,
                        source_index=source_index,
                        dest_path_resolver=self._dest_rollout_path_for_import,
                    )
                    self._prepare_import_visibility_plan(plan)
                    if plan["errors"]:
                        target_conn.rollback()
                        manifest_payload = {
                            "ok": False,
                            "blocked": False,
                            **self._public_plan(plan),
                            "backup_path": str(backup_path),
                            "package_path": str(package.package_path),
                        }
                    else:
                        rollout_backups = self._prepare_import_overwrite(target_conn, plan)
                        overwrite_paths = {path for path, _ in rollout_backups}
                        if request.overwrite:
                            target_index_ids = set(self._load_session_index())
                            plan["_overwrite_session_index_ids"] = set(
                                plan.get("_overwrite_session_index_ids", set())
                            ) | target_index_ids.intersection(plan["_id_map"].values())
                        total_items = len(plan["items"])
                        self._write_rollout_plan_concurrently(
                            plan,
                            overwrite_paths=overwrite_paths,
                            copied_paths=copied_paths,
                            progress_callback=progress_callback,
                        )

                        self._insert_thread_rows(target_conn, plan, plan["target_provider"])
                        self._insert_spawn_edges_from_source(source_conn, target_conn, plan)
                        self._insert_dynamic_tools_from_source(source_conn, target_conn, plan)
                        self._report_copy_progress(
                            progress_callback,
                            phase="committing",
                            current=total_items,
                            total=total_items,
                            message="Committing the database and session index",
                        )
                        session_index_snapshot = self._snapshot_session_index()
                        session_index_entries = self._append_session_index_entries(
                            plan,
                            source_index=source_index,
                            existing_index=self._load_session_index(),
                        )
                        target_conn.commit()
                        self._discard_rollout_backups(
                            rollout_backups,
                            {Path(item["dest_rollout_path"]) for item in plan["items"]},
                        )

                        manifest_payload = {
                            "ok": True,
                            "blocked": False,
                            **self._public_plan(plan),
                            "backup_path": str(backup_path),
                            "package_path": str(package.package_path),
                            "session_index_path": str(self.session_index_path),
                            "session_index_entries": len(session_index_entries),
                        }
                except Exception as exc:
                    target_conn.rollback()
                    if session_index_snapshot is not None:
                        self._restore_session_index(session_index_snapshot)
                    for path in copied_paths:
                        if path in overwrite_paths:
                            continue
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                    self._restore_rollout_backups(rollout_backups)
                    for _, backup in rollout_backups:
                        try:
                            backup.unlink()
                        except FileNotFoundError:
                            pass
                    manifest_payload = {
                        "ok": False,
                        "blocked": False,
                        "errors": [str(exc)],
                        "items": [],
                        "backup_path": str(backup_path),
                        "package_path": str(package.package_path),
                    }

        manifest_path = self._write_manifest(manifest_payload, request)
        manifest_payload["manifest_path"] = str(manifest_path)
        self._report_copy_progress(
            progress_callback,
            phase="done" if manifest_payload.get("ok") else "error",
            current=len(manifest_payload.get("items") or []),
            total=len(manifest_payload.get("items") or []) or progress_total,
            message=(
                f"Imported {len(manifest_payload.get('items') or [])} session(s)"
                if manifest_payload.get("ok")
                else (manifest_payload.get("errors") or ["Import failed"])[0]
            ),
        )
        return manifest_payload

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        return self._connect_path(self.db_path, read_only=read_only)

    def _connect_path(self, db_path: Path, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _require_loaded_package(self) -> LoadedTransferPackage:
        if self.loaded_package is None:
            raise ValueError("Load a transfer package first")
        return self.loaded_package

    def _require_loaded_skill_package(self) -> LoadedSkillPackage:
        if self.loaded_skill_package is None:
            raise ValueError("Load a skills package first")
        return self.loaded_skill_package

    def _is_safe_skill_id(self, skill_id: str) -> bool:
        if not skill_id or skill_id in {".", ".."}:
            return False
        return not any(part in skill_id for part in ("\\", "/", ":"))

    def _list_skills_from_root(
        self,
        root: Path,
        *,
        search: str = "",
        installed_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        if not root.exists():
            return []
        query = search.strip().lower()
        skills = []
        for skill_dir in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if not skill_dir.is_dir() or not self._is_safe_skill_id(skill_dir.name):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            summary = self._skill_summary(skill_dir, installed_root=installed_root)
            haystack = " ".join(
                str(summary.get(key) or "")
                for key in ("id", "name", "description", "path")
            ).lower()
            if query and query not in haystack:
                continue
            skills.append(summary)
        return skills

    def _skill_summary(
        self,
        skill_dir: Path,
        *,
        installed_root: Path | None = None,
    ) -> dict[str, Any]:
        skill_md = skill_dir / "SKILL.md"
        metadata = self._read_skill_metadata(skill_md)
        files = [path for path in skill_dir.rglob("*") if path.is_file()]
        size = 0
        latest_mtime = 0.0
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            size += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
        skill_id = skill_dir.name
        installed_path = installed_root / skill_id if installed_root else None
        return {
            "id": skill_id,
            "name": str(metadata.get("name") or skill_id),
            "description": str(metadata.get("description") or ""),
            "path": str(skill_dir),
            "skill_md_path": str(skill_md),
            "file_count": len(files),
            "size": size,
            "updated_at": datetime.fromtimestamp(latest_mtime, UTC).isoformat() if latest_mtime else None,
            "installed": bool(installed_path and installed_path.exists()),
        }

    def _read_skill_metadata(self, skill_md: Path) -> dict[str, str]:
        try:
            lines = skill_md.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        if not lines or lines[0].strip() != "---":
            return {}
        metadata: dict[str, str] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")
        return metadata

    def _skill_package_output_path(self, requested: str) -> Path:
        if requested:
            path = Path(requested).expanduser()
        else:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = self.skill_package_dir / f"codex-skills-package-{timestamp}.zip"
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        counter = 1
        candidate = path
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
            counter += 1
        return candidate

    def _save_uploaded_package_file(
        self,
        filename: str,
        content: bytes,
        destination_dir: Path,
        fallback_stem: str,
    ) -> Path:
        if not content:
            raise ValueError("Uploaded package is empty")
        original_name = Path(filename or f"{fallback_stem}.zip").name
        if Path(original_name).suffix.lower() != ".zip":
            raise ValueError("Uploaded package must be a .zip file")
        stem_source = Path(original_name).stem or fallback_stem
        stem = self._safe_package_segment(stem_source)
        destination_dir.mkdir(parents=True, exist_ok=True)
        path = destination_dir / f"{stem}.zip"
        counter = 1
        candidate = path
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
            counter += 1
        candidate.write_bytes(content)
        return candidate

    def _stage_skills_package(self, skill_ids: list[str], staging_dir: Path) -> dict[str, Any]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        skills_root = staging_dir / SKILL_PACKAGE_DIRNAME
        skills_root.mkdir(parents=True, exist_ok=True)
        skills = []
        for skill_id in skill_ids:
            source_path = self.skills_root / skill_id
            dest_path = skills_root / skill_id
            self._copy_tree_no_symlinks(source_path, dest_path)
            skill = self._skill_summary(source_path)
            skills.append(
                {
                    "id": skill_id,
                    "name": skill["name"],
                    "description": skill["description"],
                    "file_count": skill["file_count"],
                    "size": skill["size"],
                    "path": f"{SKILL_PACKAGE_DIRNAME}/{skill_id}",
                }
            )

        manifest = {
            "format": SKILL_PACKAGE_FORMAT,
            "version": SKILL_PACKAGE_VERSION,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_skills_root": str(self.skills_root),
            "skill_count": len(skills),
            "skills": skills,
        }
        (staging_dir / PACKAGE_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest

    def _read_skill_package_manifest(self, package: zipfile.ZipFile) -> dict[str, Any]:
        try:
            raw = package.read(PACKAGE_MANIFEST_NAME)
        except KeyError as exc:
            raise ValueError(f"Package is missing {PACKAGE_MANIFEST_NAME}") from exc
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Skills package manifest is not an object")
        if manifest.get("format") != SKILL_PACKAGE_FORMAT:
            raise ValueError("Unsupported skills package format")
        if int(manifest.get("version") or 0) > SKILL_PACKAGE_VERSION:
            raise ValueError("Skills package version is newer than this app supports")
        if not isinstance(manifest.get("skills"), list):
            raise ValueError("Skills package manifest is missing skills")
        return manifest

    def _new_skill_package_extract_dir(self, package_path: Path) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = self._safe_package_segment(package_path.stem)
        return self.skill_package_import_dir / f"{stem}-{timestamp}-{uuid.uuid4().hex[:8]}"

    def _copy_tree_no_symlinks(self, source_dir: Path, dest_dir: Path) -> None:
        if not source_dir.exists() or not source_dir.is_dir():
            raise ValueError(f"Source directory not found: {source_dir}")
        if dest_dir.exists():
            raise FileExistsError(f"Destination already exists: {dest_dir}")
        for path in source_dir.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Symlinks are not supported in skill packages: {path}")
        for path in source_dir.rglob("*"):
            relative = path.relative_to(source_dir)
            target = dest_dir / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

    def _package_output_path(self, requested: str, *, default_dir: Path | None = None) -> Path:
        if requested:
            path = Path(requested).expanduser()
            if path.suffix.lower() != ".zip":
                path = path / self._package_filename()
        else:
            path = (default_dir or self.package_dir) / self._package_filename()
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        counter = 1
        candidate = path
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
            counter += 1
        return candidate

    def _package_filename(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"codex-session-package-{timestamp}.zip"

    def _default_export_dir(self, plan: dict[str, Any]) -> Path | None:
        cwd_values = {
            str(item.get("source_cwd") or "").strip()
            for item in plan.get("items", [])
            if str(item.get("source_cwd") or "").strip()
        }
        if len(cwd_values) != 1:
            return None
        return Path(next(iter(cwd_values))).expanduser() / "exported"

    def _stage_transfer_package(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        request: ExportPackageRequest,
        staging_dir: Path,
    ) -> dict[str, Any]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        rel_rollouts = self._copy_package_rollouts(staging_dir, plan)
        self._write_package_database(conn, staging_dir / PACKAGE_DB_PATH, plan, rel_rollouts)
        manifest = self._package_manifest(plan, request, rel_rollouts)
        (staging_dir / PACKAGE_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_package_session_index(staging_dir / SESSION_INDEX_FILENAME, plan)
        return manifest

    def _copy_package_rollouts(self, staging_dir: Path, plan: dict[str, Any]) -> dict[str, str]:
        rel_rollouts: dict[str, str] = {}
        used: set[str] = set()
        for source_id in plan["_ordered_ids"]:
            row = plan["_rows"][source_id]
            source_path = Path(row["rollout_path"])
            rel_path = self._package_rollout_relative_path(source_path, row, used)
            dest_path = staging_dir / Path(*rel_path.parts)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, dest_path)
            rel_rollouts[source_id] = rel_path.as_posix()
        return rel_rollouts

    def _package_rollout_relative_path(
        self,
        source_path: Path,
        row: dict[str, Any],
        used: set[str],
    ) -> PurePosixPath:
        try:
            rel = source_path.resolve().relative_to(self.codex_home.resolve())
            candidate = PurePosixPath(rel.as_posix())
        except ValueError:
            project = self._safe_package_segment(self._project_label(str(row.get("cwd") or "project")))
            candidate = PurePosixPath("rollouts") / project / source_path.name
        candidate = self._dedupe_package_path(candidate, used)
        used.add(candidate.as_posix())
        return candidate

    def _dedupe_package_path(self, path: PurePosixPath, used: set[str]) -> PurePosixPath:
        if path.as_posix() not in used:
            return path
        suffix = "".join(path.suffixes)
        stem = path.name[: -len(suffix)] if suffix else path.name
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if candidate.as_posix() not in used:
                return candidate
            counter += 1

    def _safe_package_segment(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
        return cleaned[:80] or "project"

    def _write_package_database(
        self,
        source_conn: sqlite3.Connection,
        db_path: Path,
        plan: dict[str, Any],
        rel_rollouts: dict[str, str],
    ) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db_path)) as package_conn:
            package_conn.row_factory = sqlite3.Row
            self._create_package_schema(source_conn, package_conn)
            self._insert_package_threads(source_conn, package_conn, plan, rel_rollouts)
            self._insert_package_spawn_edges(source_conn, package_conn, plan)
            self._insert_package_dynamic_tools(source_conn, package_conn, plan)
            package_conn.commit()

    def _create_package_schema(
        self,
        source_conn: sqlite3.Connection,
        package_conn: sqlite3.Connection,
    ) -> None:
        for table_name in PACKAGE_SCHEMA_TABLES:
            row = source_conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if row and row["sql"]:
                package_conn.execute(row["sql"])

        index_rows = source_conn.execute(
            f"""
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND sql IS NOT NULL
              AND tbl_name IN ({",".join("?" for _ in PACKAGE_SCHEMA_TABLES)})
            ORDER BY name
            """,
            list(PACKAGE_SCHEMA_TABLES),
        ).fetchall()
        for row in index_rows:
            package_conn.execute(row["sql"])

    def _insert_package_threads(
        self,
        source_conn: sqlite3.Connection,
        package_conn: sqlite3.Connection,
        plan: dict[str, Any],
        rel_rollouts: dict[str, str],
    ) -> None:
        columns = self._table_columns(source_conn, "threads")
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        for source_id in plan["_ordered_ids"]:
            row = dict(plan["_rows"][source_id])
            row["rollout_path"] = rel_rollouts[source_id]
            package_conn.execute(
                f"INSERT INTO threads ({column_sql}) VALUES ({placeholders})",
                [row.get(column) for column in columns],
            )

    def _insert_package_spawn_edges(
        self,
        source_conn: sqlite3.Connection,
        package_conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> None:
        if not self._table_exists(source_conn, "thread_spawn_edges"):
            return
        selected = set(plan["_ordered_ids"])
        rows = source_conn.execute(
            """
            SELECT parent_thread_id, child_thread_id, status
            FROM thread_spawn_edges
            ORDER BY parent_thread_id, child_thread_id
            """
        ).fetchall()
        for row in rows:
            if row["parent_thread_id"] in selected and row["child_thread_id"] in selected:
                package_conn.execute(
                    """
                    INSERT INTO thread_spawn_edges
                        (parent_thread_id, child_thread_id, status)
                    VALUES (?, ?, ?)
                    """,
                    (row["parent_thread_id"], row["child_thread_id"], row["status"]),
                )

    def _insert_package_dynamic_tools(
        self,
        source_conn: sqlite3.Connection,
        package_conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> None:
        if not self._table_exists(source_conn, "thread_dynamic_tools"):
            return
        columns = self._table_columns(source_conn, "thread_dynamic_tools")
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        for source_id in plan["_ordered_ids"]:
            rows = source_conn.execute(
                "SELECT * FROM thread_dynamic_tools WHERE thread_id = ? ORDER BY position",
                (source_id,),
            ).fetchall()
            for source_row in rows:
                row = dict(source_row)
                package_conn.execute(
                    f"INSERT INTO thread_dynamic_tools ({column_sql}) VALUES ({placeholders})",
                    [row.get(column) for column in columns],
                )

    def _package_manifest(
        self,
        plan: dict[str, Any],
        request: ExportPackageRequest,
        rel_rollouts: dict[str, str],
    ) -> dict[str, Any]:
        source_index = self._load_session_index()
        projects: dict[str, dict[str, Any]] = {}
        providers: dict[str, int] = {}
        for source_id in plan["_ordered_ids"]:
            row = plan["_rows"][source_id]
            provider = str(row["model_provider"])
            providers[provider] = providers.get(provider, 0) + 1
            cwd = str(row["cwd"])
            normalized_cwd = self._normalize_windows_path(cwd)
            project = projects.setdefault(
                cwd,
                {
                    "cwd": cwd,
                    "normalized_cwd": normalized_cwd,
                    "label": self._project_label(normalized_cwd),
                    "threads": [],
                },
            )
            index_entry = source_index.get(source_id) or {}
            thread_name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
            project["threads"].append(
                {
                    "id": source_id,
                    "title": row.get("title") or "",
                    "thread_name": thread_name,
                    "display_title": thread_name or row.get("title") or row.get("preview") or source_id,
                    "model_provider": provider,
                    "source": row.get("source"),
                    "archived": bool(row.get("archived")),
                    "rollout_path": rel_rollouts[source_id],
                }
            )

        return {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_provider": request.source_provider,
            "source_codex_home": str(self.codex_home),
            "source_sqlite_home": str(self.sqlite_home),
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "thread_count": len(plan["_ordered_ids"]),
            "providers": providers,
            "projects": sorted(
                projects.values(),
                key=lambda item: (str(item["label"]).lower(), str(item["normalized_cwd"]).lower()),
            ),
        }

    def _export_items(self, plan: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
        project_by_thread = {
            thread["id"]: project
            for project in manifest["projects"]
            for thread in project["threads"]
        }
        thread_by_id = {
            thread["id"]: thread
            for project in manifest["projects"]
            for thread in project["threads"]
        }
        items = []
        for source_id in plan["_ordered_ids"]:
            row = plan["_rows"][source_id]
            project = project_by_thread.get(source_id, {})
            thread = thread_by_id.get(source_id, {})
            items.append(
                {
                    "source_id": source_id,
                    "display_title": thread.get("display_title") or row.get("title") or row.get("preview") or source_id,
                    "source_provider": row.get("model_provider"),
                    "cwd": row.get("cwd"),
                    "project_label": project.get("label"),
                }
            )
        return items

    def _write_package_session_index(self, path: Path, plan: dict[str, Any]) -> None:
        source_index = self._load_session_index()
        entries = []
        for source_id in plan["_ordered_ids"]:
            row = plan["_rows"][source_id]
            source_entry = source_index.get(source_id) or {}
            thread_name = source_entry.get("thread_name") if isinstance(source_entry, dict) else None
            updated_at = source_entry.get("updated_at") if isinstance(source_entry, dict) else None
            entries.append(
                {
                    "id": source_id,
                    "thread_name": thread_name or row.get("title") or row.get("preview") or source_id,
                    "updated_at": updated_at or self._thread_row_updated_at_iso(row),
                }
            )
        path.write_text(
            "".join(compact_json(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )

    def _zip_directory(self, source_dir: Path, package_path: Path) -> None:
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(source_dir).as_posix())

    def _read_package_manifest(self, package: zipfile.ZipFile) -> dict[str, Any]:
        try:
            raw = package.read(PACKAGE_MANIFEST_NAME)
        except KeyError as exc:
            raise ValueError(f"Package is missing {PACKAGE_MANIFEST_NAME}") from exc
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Package manifest is not an object")
        if manifest.get("format") != PACKAGE_FORMAT:
            raise ValueError("Unsupported package format")
        if int(manifest.get("version") or 0) > PACKAGE_VERSION:
            raise ValueError("Package version is newer than this app supports")
        return manifest

    def _safe_extract_zip(self, package: zipfile.ZipFile, extract_dir: Path) -> None:
        extract_root = extract_dir.resolve()
        extract_root.mkdir(parents=True, exist_ok=True)
        for member in package.infolist():
            name = member.filename
            pure = PurePosixPath(name)
            if (
                "\\" in name
                or pure.is_absolute()
                or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
            ):
                raise ValueError(f"Unsafe package path: {name}")
            target = (extract_root / Path(*pure.parts)).resolve()
            if not target.is_relative_to(extract_root):
                raise ValueError(f"Unsafe package path: {name}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)

    def _new_package_extract_dir(self, package_path: Path) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = self._safe_package_segment(package_path.stem)
        return self.package_import_dir / f"{stem}-{timestamp}-{uuid.uuid4().hex[:8]}"

    def _materialize_package_rollout_paths(self, db_path: Path, extract_dir: Path) -> None:
        root = extract_dir.resolve()
        with closing(self._connect_path(db_path, read_only=False)) as conn:
            rows = conn.execute("SELECT id, rollout_path FROM threads").fetchall()
            for row in rows:
                pure = PurePosixPath(str(row["rollout_path"]).replace("\\", "/"))
                if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                    raise ValueError(f"Unsafe rollout path in package DB: {row['rollout_path']}")
                absolute = (root / Path(*pure.parts)).resolve()
                if not absolute.is_relative_to(root):
                    raise ValueError(f"Unsafe rollout path in package DB: {row['rollout_path']}")
                if not absolute.exists():
                    raise ValueError(f"Package rollout file missing: {pure.as_posix()}")
                conn.execute(
                    "UPDATE threads SET rollout_path = ? WHERE id = ?",
                    (str(absolute), row["id"]),
                )
            conn.commit()

    def _dest_rollout_path_for_import(self, row: dict[str, Any], target_id: str) -> Path:
        source_path = Path(row["rollout_path"])
        match = ROLLOUT_NAME_RE.match(source_path.name)
        if not match:
            raise ValueError(f"Unsupported rollout file name: {source_path.name}")
        target_name = f"{match.group('prefix')}{target_id}{match.group('suffix')}"
        parts = list(source_path.parts)
        for root_name in ("archived_sessions", "sessions"):
            if root_name in parts:
                root_index = len(parts) - 1 - list(reversed(parts)).index(root_name)
                relative_parts = parts[root_index:-1]
                relative_parts[0] = "sessions"
                return self.codex_home.joinpath(*relative_parts, target_name)

        created_at = int(row.get("created_at") or datetime.now(UTC).timestamp())
        created = datetime.fromtimestamp(created_at, UTC)
        return self.codex_home / "sessions" / created.strftime("%Y") / created.strftime("%m") / created.strftime("%d") / target_name


    def _wal_files(self) -> list[dict[str, Any]]:
        files = []
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                files.append(
                    {
                        "path": str(path),
                        "size": path.stat().st_size,
                        "mtime": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                    }
                )
        return files

    def _codex_plus_preset_providers(self) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        path = self.provider_switch_home / "preset-overrides.json"
        if not path.exists():
            return providers
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return providers

        custom_presets = data.get("customPresets", [])
        if isinstance(custom_presets, list):
            for preset in custom_presets:
                if isinstance(preset, dict):
                    providers.extend(self._providers_from_preset(preset, "codex_plus_preset"))

        overrides = data.get("overrides", {})
        if isinstance(overrides, dict):
            for preset_id, override in overrides.items():
                if isinstance(override, dict):
                    preset = {"id": preset_id, "name": preset_id, **override}
                    providers.extend(self._providers_from_preset(preset, "codex_plus_override"))
        return providers

    def _providers_from_preset(self, preset: dict[str, Any], source: str) -> list[dict[str, Any]]:
        config_text = preset.get("configText")
        if not isinstance(config_text, str) or not config_text.strip():
            return []
        try:
            parsed = tomllib.loads(config_text)
        except tomllib.TOMLDecodeError:
            return []
        model_provider = parsed.get("model_provider")
        configured = parsed.get("model_providers") or {}
        results = []
        if isinstance(configured, dict) and configured:
            for provider_id, provider_info in configured.items():
                if not isinstance(provider_info, dict):
                    provider_info = {}
                value = str(model_provider or provider_id)
                if model_provider and provider_id != model_provider and provider_id not in configured:
                    value = str(provider_id)
                results.append(
                    {
                        "value": value,
                        "label": str(preset.get("name") or provider_info.get("name") or value),
                        "source": source,
                        "preset_id": preset.get("id"),
                        "preset_name": preset.get("name"),
                        "provider_name": provider_info.get("name"),
                        "base_url": provider_info.get("base_url"),
                        "wire_api": provider_info.get("wire_api"),
                        "model": parsed.get("model"),
                    }
                )
        elif model_provider:
            value = str(model_provider)
            results.append(
                {
                    "value": value,
                    "label": str(preset.get("name") or value),
                    "source": source,
                    "preset_id": preset.get("id"),
                    "preset_name": preset.get("name"),
                    "model": parsed.get("model"),
                }
            )
        return results

    def _configured_provider_aliases(self) -> dict[str, str]:
        """Return unambiguous provider-name -> provider-id aliases from config.toml."""
        config_path = self.codex_home / "config.toml"
        if not config_path.exists():
            return {}
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        providers = data.get("model_providers") or {}
        if not isinstance(providers, dict):
            return {}

        aliases: dict[str, str] = {}
        ambiguous: set[str] = set()
        for provider_id, provider_info in providers.items():
            if not isinstance(provider_info, dict):
                continue
            name = str(provider_info.get("name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            value = str(provider_id)
            previous = aliases.get(key)
            if previous is not None and previous != value:
                ambiguous.add(key)
            else:
                aliases[key] = value
        return {key: value for key, value in aliases.items() if key not in ambiguous}

    def _resolve_target_provider(self, requested: str) -> tuple[str, str | None]:
        """Resolve a target label to a provider ID without changing config/auth files."""
        clean = str(requested or "").strip()
        if not clean:
            return clean, None

        current = self.current_config()
        configured_ids = {
            str(value)
            for value in current.get("configured_provider_ids", [])
            if str(value).strip()
        }
        preset_ids = {
            str(provider.get("value") or "")
            for provider in self._codex_plus_preset_providers()
            if str(provider.get("value") or "").strip()
        }
        # An exact configured/preset ID is authoritative. Only fall back to a
        # display-name alias when no real provider ID matches the request.
        if clean in configured_ids or clean in preset_ids:
            return clean, None

        alias = self._configured_provider_aliases().get(clean.casefold())
        if alias and alias != clean:
            return (
                alias,
                f"Target provider '{clean}' matched the configured provider name; using provider id '{alias}'.",
            )
        return clean, None

    @staticmethod
    def _normalize_windows_path(path: str) -> str:
        extended_unc_prefix = "\\\\?\\UNC\\"
        if path.casefold().startswith(extended_unc_prefix.casefold()):
            return "\\\\" + path[len(extended_unc_prefix) :]
        return path[4:] if path.startswith("\\\\?\\") else path

    def _date_start_ms(self, value: str) -> int | None:
        clean = str(value or "").strip()
        if not clean:
            return None
        try:
            parsed = datetime.fromisoformat(clean).date()
        except ValueError:
            return None
        return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC).timestamp() * 1000)

    def _path_match_key(self, path: str) -> str:
        normalized = self._normalize_windows_path(path).replace("/", "\\").rstrip("\\")
        return normalized.casefold()

    def _source_path_match_key(self, path: str) -> tuple[str, str]:
        normalized = self._normalize_windows_path(str(path)).rstrip("\\/")
        if re.match(r"^[A-Za-z]:[\\/]", normalized) or normalized.startswith("\\\\"):
            return "windows", normalized.replace("/", "\\").casefold()
        return "posix", str(PurePosixPath(normalized.replace("\\", "/")))

    @staticmethod
    def _cwd_match_sql() -> str:
        prefix = "char(92) || char(92) || '?' || char(92)"
        normalized = (
            "CASE WHEN substr(cwd, 1, 4) = "
            f"{prefix} THEN substr(cwd, 5) ELSE cwd END"
        )
        return f"lower(rtrim(replace({normalized}, '/', char(92)), char(92)))"

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    @staticmethod
    def _ms_to_iso(value: int) -> str:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")

    def _imported_at_iso(self, plan: dict[str, Any]) -> str | None:
        imported_at_ms = plan.get("_imported_at_ms")
        if not imported_at_ms:
            return None
        return self._ms_to_iso(int(imported_at_ms))

    def _prepare_import_visibility_plan(self, plan: dict[str, Any]) -> None:
        if plan.get("_imported_at_ms") is None:
            plan["_imported_at_ms"] = self._now_ms()
        plan["_unarchive_imported_threads"] = True
        target_source_by_id: dict[str, str] = {}
        target_thread_source_by_id: dict[str, str] = {}
        for source_id in plan.get("_ordered_ids", []):
            row = plan.get("_rows", {}).get(source_id) or {}
            source_text = str(row.get("source") or "")
            if self._source_parent_id(source_text):
                continue
            target_source_by_id[source_id] = self._codex_default_visible_source(source_text)
            target_thread_source_by_id[source_id] = DEFAULT_IMPORTED_THREAD_SOURCE
        plan["_target_source_by_source_id"] = target_source_by_id
        plan["_target_thread_source_by_source_id"] = target_thread_source_by_id

    def _prepare_import_overwrite(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> list[tuple[Path, Path]]:
        overwritten_ids = sorted(plan.get("_overwritten_ids", set()))
        if not plan.get("overwrite") or not overwritten_ids:
            return []

        target_rows = self._threads_by_ids(conn, overwritten_ids)
        backups: list[tuple[Path, Path]] = []
        try:
            for row in target_rows.values():
                rollout_path = Path(str(row["rollout_path"]))
                if not rollout_path.exists():
                    continue
                backup_path = self.manifest_dir / (
                    f"overwrite-{uuid.uuid4().hex}-{rollout_path.name}"
                )
                shutil.copy2(rollout_path, backup_path)
                backups.append((rollout_path, backup_path))

            placeholders = ",".join("?" for _ in overwritten_ids)
            if self._table_exists(conn, "thread_spawn_edges"):
                conn.execute(
                    "DELETE FROM thread_spawn_edges "
                    f"WHERE parent_thread_id IN ({placeholders}) "
                    f"OR child_thread_id IN ({placeholders})",
                    overwritten_ids + overwritten_ids,
                )
            if self._table_exists(conn, "thread_dynamic_tools"):
                conn.execute(
                    "DELETE FROM thread_dynamic_tools "
                    f"WHERE thread_id IN ({placeholders})",
                    overwritten_ids,
                )
            conn.execute(
                f"DELETE FROM threads WHERE id IN ({placeholders})",
                overwritten_ids,
            )
        except Exception:
            self._restore_rollout_backups(backups)
            for _, backup_path in backups:
                try:
                    backup_path.unlink()
                except FileNotFoundError:
                    pass
            raise

        return backups

    def _restore_rollout_backups(self, backups: list[tuple[Path, Path]]) -> None:
        for original_path, backup_path in backups:
            try:
                if backup_path.exists():
                    original_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, original_path)
            except OSError:
                pass

    def _discard_rollout_backups(
        self,
        backups: list[tuple[Path, Path]],
        destination_paths: set[Path],
    ) -> None:
        for original_path, backup_path in backups:
            if original_path not in destination_paths:
                try:
                    original_path.unlink()
                except OSError:
                    pass
            try:
                backup_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _codex_default_visible_source(source: str) -> str:
        clean = str(source or "").strip()
        if clean in CODEX_DEFAULT_SOURCE_KINDS:
            return clean
        return DEFAULT_IMPORTED_SOURCE_KIND

    def _target_cwd_for_source(self, source_cwd: str, cwd_map: dict[str, str]) -> str:
        if not cwd_map:
            return source_cwd
        source_key = self._source_path_match_key(source_cwd)
        for mapped_source, mapped_target in cwd_map.items():
            if self._source_path_match_key(mapped_source) == source_key:
                return mapped_target
        return source_cwd

    def _project_label(self, cwd: str) -> str:
        normalized = self._normalize_windows_path(str(cwd)).rstrip("\\/")
        if not normalized:
            return cwd
        name = re.split(r"[\\/]", normalized)[-1]
        return name or normalized

    def _validate_target_directory(
        self,
        value: str,
        label: str,
        errors: list[str],
    ) -> Path | None:
        target = Path(value)
        if not target.is_absolute():
            errors.append(f"{label} must be an absolute directory path: {value}")
        elif not target.exists():
            errors.append(f"{label} directory does not exist: {value}")
        elif not target.is_dir():
            errors.append(f"{label} is not a directory: {value}")
        else:
            return target
        return None

    @staticmethod
    def _target_path_match_key(value: str) -> str:
        return os.path.normcase(os.path.normpath(value))

    def _resolve_workspace_cwds(
        self,
        rows: dict[str, dict[str, Any]],
        final_ids: list[str],
        mapping: WorkspaceMapping,
        errors: list[str],
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        if mapping.mode not in WORKSPACE_MAPPING_MODES:
            errors.append(f"Unsupported workspace mapping mode: {mapping.mode}")

        target_root = self._validate_target_directory(
            mapping.target_root,
            "Target workspace",
            errors,
        )
        root_path = target_root or Path(mapping.target_root)
        normalized_overrides = {
            self._source_path_match_key(source): target
            for source, target in mapping.overrides.items()
        }

        source_counts: dict[tuple[str, str], int] = {}
        source_values: dict[tuple[str, str], str] = {}
        source_order: list[tuple[str, str]] = []
        for thread_id in final_ids:
            source_cwd = str(rows[thread_id].get("cwd") or "").strip()
            if not source_cwd:
                errors.append(f"Thread {thread_id} has no source cwd")
                continue
            source_key = self._source_path_match_key(source_cwd)
            if source_key not in source_counts:
                source_counts[source_key] = 0
                source_values[source_key] = source_cwd
                source_order.append(source_key)
            source_counts[source_key] += 1

        targets_by_source: dict[tuple[str, str], str] = {}
        summaries: list[dict[str, Any]] = []
        target_sources: dict[str, list[str]] = {}
        for source_key in source_order:
            source_cwd = source_values[source_key]
            override = normalized_overrides.get(source_key)
            if override:
                target_cwd = override
            elif mapping.mode == "single_workspace":
                target_cwd = str(root_path)
            else:
                target_cwd = str(root_path / self._project_label(source_cwd))

            self._validate_target_directory(
                target_cwd,
                f"Target for {source_cwd}",
                errors,
            )
            targets_by_source[source_key] = target_cwd
            target_key = self._target_path_match_key(target_cwd)
            target_sources.setdefault(target_key, []).append(source_cwd)
            summaries.append(
                {
                    "source_cwd": source_cwd,
                    "target_cwd": target_cwd,
                    "project_label": self._project_label(source_cwd),
                    "session_count": source_counts[source_key],
                    "overridden": override is not None,
                }
            )

        if mapping.mode == "preserve_projects":
            for target_key, sources in target_sources.items():
                if len(sources) > 1:
                    errors.append(
                        "Multiple source projects resolve to the same target directory "
                        f"{target_key}: {', '.join(sources)}. Add a project override."
                    )

        cwd_by_source_id = {}
        for thread_id in final_ids:
            source_cwd = str(rows[thread_id].get("cwd") or "").strip()
            if source_cwd:
                cwd_by_source_id[thread_id] = targets_by_source.get(
                    self._source_path_match_key(source_cwd),
                    source_cwd,
                )
        return cwd_by_source_id, summaries

    def _thread_summary(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        session_index: dict[str, dict[str, Any]],
        *,
        compact: bool = False,
    ) -> dict[str, Any]:
        thread_id = row["id"]
        parent = self._parent_thread_id(conn, thread_id) or self._source_parent_id(row["source"])
        child_count = conn.execute(
            "SELECT COUNT(*) FROM thread_spawn_edges WHERE parent_thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
        rollout_path = Path(row["rollout_path"])
        index_entry = session_index.get(thread_id) or {}
        thread_name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
        title = row["title"]
        preview = row["preview"]
        if compact:
            title = _clip_preview_text(title)
            thread_name = _clip_preview_text(thread_name) if thread_name else None
            preview = _clip_preview_text(preview)
        display_title = thread_name or title or preview or thread_id
        return {
            "id": thread_id,
            "title": title,
            "thread_name": thread_name,
            "display_title": display_title,
            "preview": preview,
            "model_provider": row["model_provider"],
            "model": row["model"],
            "source": row["source"],
            "cwd": row["cwd"],
            "archived": bool(row["archived"]),
            "updated_at_ms": row["updated_at_ms"],
            "rollout_path": str(rollout_path),
            "rollout_exists": rollout_path.exists(),
            "hidden_empty_preview": row["preview"] == "",
            "parent_thread_id": parent,
            "child_count": int(child_count),
        }

    def _parse_rollout_for_render(
        self,
        rollout_path: Path,
        *,
        item_offset: int,
        item_limit: int,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        seen_messages: set[tuple[str, str]] = set()
        item_total = 0
        line_count = 0
        try:
            lines = rollout_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return {
                "meta": meta,
                "items": items,
                "errors": [str(exc)],
                "line_count": line_count,
                "item_total": item_total,
                "has_more": False,
            }

        for line_count, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"Line {line_count}: invalid JSON ({exc.msg})")
                warning_item = {
                    "kind": "warning",
                    "role": "event",
                    "title": "Invalid rollout JSON",
                    "text": self._clip_text(line, 1200),
                    "timestamp": None,
                    "line": line_count,
                    "data": {"error": exc.msg},
                }
                if item_total >= item_offset and len(items) < item_limit:
                    items.append(warning_item)
                item_total += 1
                continue
            if not isinstance(entry, dict):
                continue

            entry_type, payload = self._rollout_type_and_payload(entry)
            if entry_type == "session_meta":
                if isinstance(payload, dict):
                    meta = self._public_rollout_meta(payload)
                continue

            item = self._renderable_rollout_item(entry, entry_type, payload, line_count)
            if item:
                if item.get("kind") == "message":
                    message_key = (str(item.get("role") or ""), str(item.get("text") or ""))
                    if message_key in seen_messages:
                        continue
                    seen_messages.add(message_key)
                if item_total >= item_offset and len(items) < item_limit:
                    items.append(item)
                item_total += 1

        return {
            "meta": meta,
            "items": items,
            "errors": errors,
            "line_count": line_count,
            "item_total": item_total,
            "has_more": item_offset + len(items) < item_total,
        }

    def _rollout_type_and_payload(self, entry: dict[str, Any]) -> tuple[str | None, Any]:
        item = entry.get("item")
        if isinstance(item, dict):
            return item.get("type"), item.get("payload")
        return entry.get("type"), entry.get("payload")

    def _renderable_rollout_item(
        self,
        entry: dict[str, Any],
        entry_type: str | None,
        payload: Any,
        line: int,
    ) -> dict[str, Any] | None:
        timestamp = entry.get("timestamp")
        if not isinstance(payload, dict):
            return None

        payload_type = payload.get("type")
        item_type = str(payload_type or entry_type or "event")
        if item_type == "message":
            text = self._payload_text(payload)
            if not text:
                return None
            role = str(payload.get("role") or "assistant")
            if role in {"developer", "system"}:
                return None
            return {
                "kind": "message",
                "role": role,
                "title": role.title(),
                "text": self._clip_text(text),
                "timestamp": timestamp,
                "line": line,
                "data": {"content_types": self._content_types(payload.get("content"))},
            }

        if item_type == "function_call":
            name = str(payload.get("name") or payload.get("function") or payload.get("tool") or "tool call")
            return {
                "kind": "tool_call",
                "role": "tool",
                "title": name,
                "text": name,
                "timestamp": timestamp,
                "line": line,
                "data": {
                    "call_id": payload.get("call_id"),
                    "arguments": self._redact_sensitive_value(payload.get("arguments")),
                },
            }

        if item_type == "function_call_output":
            text = self._payload_text(payload)
            return {
                "kind": "tool_result",
                "role": "tool",
                "title": "Tool result",
                "text": self._clip_text(text),
                "timestamp": timestamp,
                "line": line,
                "data": {"call_id": payload.get("call_id")},
            }

        if item_type == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text.strip():
                return {
                    "kind": "message",
                    "role": "user",
                    "title": "User",
                    "text": self._clip_text(text),
                    "timestamp": timestamp,
                    "line": line,
                    "data": {"source": "event_msg"},
                }

        if item_type in {"task_started", "token_count", "reasoning"}:
            return None
        if entry_type == "event_msg":
            return None

        text = self._payload_text(payload)
        if not text:
            return None
        return {
            "kind": "event",
            "role": "event",
            "title": item_type,
            "text": self._clip_text(text),
            "timestamp": timestamp,
            "line": line,
            "data": {"entry_type": entry_type},
        }

    def _payload_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if content is not None:
            text = self._content_text(content)
            if text:
                return text
        for key in ("text", "message", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if value is not None and key == "output":
                return compact_json(value)
        return ""

    def _content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [self._content_text(item) for item in content]
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            for key in ("text", "content", "message", "output"):
                value = content.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, (list, dict)):
                    nested = self._content_text(value)
                    if nested:
                        return nested
        return ""

    def _content_types(self, content: Any) -> list[str]:
        if not isinstance(content, list):
            return []
        types = []
        for part in content:
            if isinstance(part, dict) and part.get("type"):
                types.append(str(part["type"]))
        return types

    def _public_rollout_meta(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"id", "timestamp", "cwd", "originator", "cli_version", "source", "model_provider"}
        return {
            key: self._redact_sensitive_value(value)
            for key, value in payload.items()
            if key in allowed
        }

    def _clip_text(self, text: str, limit: int = 12000) -> str:
        text = self._redact_sensitive_text(text)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"

    def _redact_sensitive_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_sensitive_text(value)
        if isinstance(value, list):
            return [self._redact_sensitive_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._redact_sensitive_value(item) for key, item in value.items()}
        return value

    def _redact_sensitive_text(self, text: str) -> str:
        redacted = text
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("[redacted]", redacted)
        return redacted

    def _build_rebind_plan(
        self,
        conn: sqlite3.Connection,
        request: RebindRequest,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        source_provider = request.source_provider.strip()
        target_provider = request.target_provider.strip()
        target_provider, target_provider_warning = self._resolve_target_provider(target_provider)
        ordered_ids = self._dedupe(request.thread_ids)

        if not source_provider:
            errors.append("source_provider is required")
        if not target_provider:
            errors.append("target_provider is required")
        if not ordered_ids:
            errors.append("Select at least one thread")
        if source_provider and target_provider == source_provider:
            errors.append("Source and target providers are already the same")

        config_path = self.codex_home / "config.toml"
        configured_provider_ids = {
            str(value).strip()
            for value in self.current_config().get("configured_provider_ids", [])
            if str(value).strip()
        }
        if target_provider and config_path.exists() and target_provider not in configured_provider_ids:
            errors.append(
                f"Target provider '{target_provider}' is not defined in {config_path}. "
                f"Add [model_providers.{target_provider}] or choose a configured provider."
            )
        if errors:
            return self._empty_rebind_plan(request, errors, warnings)
        if target_provider_warning:
            warnings.append(target_provider_warning)

        rows = self._threads_by_ids(conn, ordered_ids)
        for thread_id in ordered_ids:
            row = rows.get(thread_id)
            if row is None:
                errors.append(f"Thread not found: {thread_id}")
                continue
            if row["model_provider"] != source_provider:
                errors.append(f"Thread {thread_id} is not in provider {source_provider}")
            if int(row["archived"]) and not request.include_archived:
                errors.append(f"Thread {thread_id} is archived but include_archived is false")

        final_ids = [thread_id for thread_id in ordered_ids if thread_id in rows]
        if request.include_descendants:
            final_ids = self._append_descendants(conn, final_ids, source_provider, request, errors)
            rows = self._threads_by_ids(conn, final_ids)

        session_index = self._load_session_index()
        items: list[dict[str, Any]] = []
        for thread_id in final_ids:
            row = rows[thread_id]
            rollout_path = Path(row["rollout_path"])
            metadata_provider = self._validate_rebind_rollout(
                rollout_path,
                thread_id,
                errors,
            )
            index_entry = session_index.get(thread_id) or {}
            thread_name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
            display_title = thread_name or row["title"] or row["preview"] or thread_id
            parent = self._parent_thread_id(conn, thread_id) or self._source_parent_id(row["source"])
            items.append(
                {
                    "source_id": thread_id,
                    "target_id": thread_id,
                    "title": row["title"],
                    "thread_name": thread_name,
                    "display_title": display_title,
                    "session_index_present": bool(thread_name),
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                    "source_cwd": str(row["cwd"]),
                    "target_cwd": str(row["cwd"]),
                    "cwd_rewritten": False,
                    "archived": bool(row["archived"]),
                    "parent_source_id": parent,
                    "parent_target_id": parent,
                    "child_count": self._child_count(conn, thread_id),
                    "overwritten": False,
                    "overwrite_match": None,
                    "rollout_path": str(rollout_path),
                    "metadata_provider": metadata_provider,
                }
            )

        return {
            "can_execute": not errors and bool(items),
            "errors": errors,
            "warnings": warnings,
            "items": items,
            "source_provider": source_provider,
            "target_provider": target_provider,
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "_ordered_ids": final_ids,
            "_rows": rows,
        }

    def _empty_rebind_plan(
        self,
        request: RebindRequest,
        errors: list[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "can_execute": False,
            "errors": errors,
            "warnings": warnings,
            "items": [],
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "_ordered_ids": [],
            "_rows": {},
        }

    def _build_copy_plan(
        self,
        conn: sqlite3.Connection,
        request: CopyRequest,
        *,
        target_conn: sqlite3.Connection | None = None,
        source_index: dict[str, dict[str, Any]] | None = None,
        dest_path_resolver: Callable[[dict[str, Any], str], Path] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        session_index = source_index if source_index is not None else self._load_session_index()
        source_provider = request.source_provider.strip()
        target_provider = request.target_provider.strip()
        target_provider, target_provider_warning = self._resolve_target_provider(target_provider)
        mirror_target = bool(request.mirror_target)
        ordered_ids = self._dedupe(request.thread_ids)
        cwd_map = dict(request.cwd_map or {})

        if not source_provider:
            errors.append("source_provider is required")
        if not target_provider:
            errors.append("target_provider is required")
        if mirror_target and source_provider and target_provider and source_provider == target_provider:
            errors.append("Source and target providers must be different for a full mirror")
        if not mirror_target and not ordered_ids:
            errors.append("Select at least one thread")
        config_path = self.codex_home / "config.toml"
        configured_provider_ids = {
            str(value).strip()
            for value in self.current_config().get("configured_provider_ids", [])
            if str(value).strip()
        }
        if target_provider and config_path.exists() and target_provider not in configured_provider_ids:
            errors.append(
                f"Target provider '{target_provider}' is not defined in {config_path}. "
                f"Add [model_providers.{target_provider}] or choose a configured provider."
            )
        if errors:
            return self._empty_plan(request, errors, warnings)
        if target_provider_warning:
            warnings.append(target_provider_warning)

        if mirror_target:
            source_rows = conn.execute(
                "SELECT * FROM threads WHERE model_provider = ? ORDER BY created_at, id",
                (source_provider,),
            ).fetchall()
            rows = {str(row["id"]): dict(row) for row in source_rows}
            ordered_ids = list(rows)
            if not ordered_ids:
                errors.append(
                    f"Source provider '{source_provider}' has no sessions; refusing to clear the target"
                )
        else:
            rows = self._threads_by_ids(conn, ordered_ids)
        for thread_id in ordered_ids:
            row = rows.get(thread_id)
            if row is None:
                errors.append(f"Thread not found: {thread_id}")
                continue
            if row["model_provider"] != source_provider:
                errors.append(f"Thread {thread_id} is not in provider {source_provider}")
            if not mirror_target and int(row["archived"]) and not request.include_archived:
                errors.append(f"Thread {thread_id} is archived but include_archived is false")

        final_ids = [thread_id for thread_id in ordered_ids if thread_id in rows]
        if request.include_descendants and not mirror_target:
            final_ids = self._append_descendants(conn, final_ids, source_provider, request, errors)
            rows = self._threads_by_ids(conn, final_ids)

        id_set = set(final_ids)
        for thread_id in final_ids:
            row = rows[thread_id]
            parent = self._parent_thread_id(conn, thread_id) or self._source_parent_id(row["source"])
            if parent and parent not in id_set:
                errors.append(
                    f"Thread {thread_id} is a child of {parent}; include the parent or copy the tree."
                )

            rollout_path = Path(row["rollout_path"])
            if not rollout_path.exists():
                errors.append(f"Rollout file missing for thread {thread_id}: {rollout_path}")
            elif rollout_path.name.endswith(".jsonl.zst"):
                errors.append(f"Compressed rollout is not supported in v1: {rollout_path}")
            elif not rollout_path.name.endswith(".jsonl"):
                errors.append(f"Unsupported rollout extension for thread {thread_id}: {rollout_path}")
            elif not ROLLOUT_NAME_RE.match(rollout_path.name):
                errors.append(f"Unsupported rollout file name for thread {thread_id}: {rollout_path.name}")

        workspace_mappings: list[dict[str, Any]] = []
        if request.workspace_mapping is not None:
            cwd_by_source_id, workspace_mappings = self._resolve_workspace_cwds(
                rows,
                final_ids,
                request.workspace_mapping,
                errors,
            )
        else:
            cwd_by_source_id = {
                thread_id: self._target_cwd_for_source(str(rows[thread_id]["cwd"]), cwd_map)
                for thread_id in final_ids
            }

        target_database = target_conn or conn
        target_rows: dict[str, dict[str, Any]] = {}
        if mirror_target:
            target_rows = {
                str(row["id"]): dict(row)
                for row in target_database.execute(
                    "SELECT * FROM threads WHERE model_provider = ? ORDER BY created_at, id",
                    (target_provider,),
                ).fetchall()
            }
            warnings.append(
                f"All {len(target_rows)} existing target session(s) will be backed up and replaced."
            )
        overwritten_ids: set[str] = set()
        overwrite_matches: dict[str, dict[str, Any]] = {}
        overwrite_ambiguities: list[dict[str, Any]] = []
        if request.overwrite and not mirror_target:
            (
                overwrite_matches,
                match_warnings,
                overwrite_ambiguities,
                overwrite_selection_errors,
            ) = self._match_overwrite_targets(
                target_database,
                rows,
                final_ids,
                source_provider,
                target_provider,
                source_index=session_index,
                target_index=self._load_session_index(),
                overwrite_selections=request.overwrite_selections,
            )
            overwritten_ids = {item["id"] for item in overwrite_matches.values()}
            warnings.extend(match_warnings)
            errors.extend(overwrite_selection_errors)
            if overwrite_ambiguities:
                errors.append(
                    f"{len(overwrite_ambiguities)} ambiguous overwrite match(es) require a target selection or an explicit skip."
                )

        id_map: dict[str, str] = {}
        if mirror_target:
            # A provider mirror is a canonical takeover, not a duplicate import. Keeping the
            # source ids makes every existing sidebar entry resume through the new provider.
            id_map = {thread_id: thread_id for thread_id in final_ids}
        else:
            unmatched_ids: list[str] = []
            for thread_id in final_ids:
                target = overwrite_matches.get(thread_id)
                if target:
                    id_map[thread_id] = target["id"]
                else:
                    unmatched_ids.append(thread_id)
            id_map.update(self._new_id_map(target_database, unmatched_ids))
        items = []
        for thread_id in final_ids:
            row = rows[thread_id]
            parent = self._parent_thread_id(conn, thread_id) or self._source_parent_id(row["source"])
            source_cwd = str(row["cwd"])
            target_cwd = cwd_by_source_id.get(thread_id, source_cwd)
            if mirror_target:
                dest_path = Path(row["rollout_path"])
            else:
                dest_path = (
                    dest_path_resolver(row, id_map[thread_id])
                    if dest_path_resolver
                    else self._dest_rollout_path(Path(row["rollout_path"]), id_map[thread_id])
                )
            index_entry = session_index.get(thread_id) or {}
            thread_name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
            display_title = thread_name or row["title"] or row["preview"] or thread_id
            items.append(
                {
                    "source_id": thread_id,
                    "target_id": id_map[thread_id],
                    "title": row["title"],
                    "thread_name": thread_name,
                    "display_title": display_title,
                    "session_index_present": bool(thread_name),
                    "source_provider": source_provider,
                    "target_provider": target_provider,
                    "source_rollout_path": row["rollout_path"],
                    "dest_rollout_path": str(dest_path),
                    "source_cwd": source_cwd,
                    "target_cwd": target_cwd,
                    "cwd_rewritten": target_cwd != source_cwd,
                    "archived": bool(row["archived"]),
                    "parent_source_id": parent,
                    "parent_target_id": id_map.get(parent),
                    "child_count": self._child_count(conn, thread_id),
                    "overwritten": id_map[thread_id] in overwritten_ids,
                    "overwrite_match": (
                        overwrite_matches[thread_id].get("match")
                        if thread_id in overwrite_matches
                        else None
                    ),
                }
            )

        rewritten_count = sum(1 for item in items if item["cwd_rewritten"])
        if rewritten_count:
            warnings.append(f"{rewritten_count} session cwd path(s) will be rewritten for the target project.")

        return {
            "can_execute": not errors and bool(items),
            "errors": errors,
            "warnings": warnings,
            "items": items,
            "workspace_mappings": workspace_mappings,
            "source_provider": source_provider,
            "target_provider": target_provider,
            "overwrite": bool(request.overwrite and not mirror_target),
            "mirror_target": mirror_target,
            "replaced_target_count": len(target_rows),
            "include_descendants": request.include_descendants or mirror_target,
            "include_archived": request.include_archived or mirror_target,
            "_ordered_ids": final_ids,
            "_rows": rows,
            "_target_rows": target_rows,
            "_target_ids": set(target_rows),
            "_id_map": id_map,
            "_cwd_map": cwd_map,
            "_cwd_by_source_id": cwd_by_source_id,
            "_overwritten_ids": overwritten_ids,
            "_overwrite_session_index_ids": set(overwritten_ids) | set(target_rows),
            "_overwrite_ambiguities": overwrite_ambiguities,
        }

    def _empty_plan(
        self, request: CopyRequest, errors: list[str], warnings: list[str]
    ) -> dict[str, Any]:
        return {
            "can_execute": False,
            "errors": errors,
            "warnings": warnings,
            "items": [],
            "workspace_mappings": [],
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "overwrite": bool(request.overwrite),
            "mirror_target": bool(request.mirror_target),
            "replaced_target_count": 0,
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "_ordered_ids": [],
            "_rows": {},
            "_target_rows": {},
            "_target_ids": set(),
            "_id_map": {},
            "_cwd_map": dict(request.cwd_map or {}),
            "_cwd_by_source_id": {},
            "_overwritten_ids": set(),
            "_overwrite_session_index_ids": set(),
            "_overwrite_ambiguities": [],
        }

    def _append_descendants(
        self,
        conn: sqlite3.Connection,
        initial_ids: list[str],
        source_provider: str,
        request: CopyRequest,
        errors: list[str],
    ) -> list[str]:
        ordered = list(initial_ids)
        seen = set(ordered)
        frontier = list(initial_ids)
        while frontier:
            parent = frontier.pop(0)
            children = conn.execute(
                """
                SELECT child_thread_id
                FROM thread_spawn_edges
                WHERE parent_thread_id = ?
                ORDER BY child_thread_id
                """,
                (parent,),
            ).fetchall()
            for child_row in children:
                child_id = child_row["child_thread_id"]
                if child_id in seen:
                    continue
                child = self._threads_by_ids(conn, [child_id]).get(child_id)
                if child is None:
                    errors.append(f"Spawn edge points to missing child thread {child_id}")
                    continue
                if child["model_provider"] != source_provider:
                    errors.append(f"Descendant {child_id} is not in provider {source_provider}")
                    continue
                if int(child["archived"]) and not request.include_archived:
                    errors.append(f"Descendant {child_id} is archived but include_archived is false")
                    continue
                seen.add(child_id)
                ordered.append(child_id)
                frontier.append(child_id)
        return ordered

    def _threads_by_ids(
        self, conn: sqlite3.Connection, thread_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not thread_ids:
            return {}
        placeholders = ",".join("?" for _ in thread_ids)
        rows = conn.execute(
            f"SELECT * FROM threads WHERE id IN ({placeholders})",
            thread_ids,
        ).fetchall()
        return {row["id"]: dict(row) for row in rows}

    def _parent_thread_id(self, conn: sqlite3.Connection, thread_id: str) -> str | None:
        row = conn.execute(
            "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?",
            (thread_id,),
        ).fetchone()
        return str(row["parent_thread_id"]) if row else None

    def _child_count(self, conn: sqlite3.Connection, thread_id: str) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM thread_spawn_edges WHERE parent_thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )

    def _existing_thread_ids(self, conn: sqlite3.Connection, thread_ids: list[str]) -> set[str]:
        if not thread_ids:
            return set()
        placeholders = ",".join("?" for _ in thread_ids)
        rows = conn.execute(
            f"SELECT id FROM threads WHERE id IN ({placeholders})",
            thread_ids,
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def _new_id_map(
        self,
        conn: sqlite3.Connection,
        thread_ids: list[str],
        *,
        preserve_source_ids: bool = False,
    ) -> dict[str, str]:
        id_map: dict[str, str] = {}
        generated: set[str] = set()
        for thread_id in thread_ids:
            if preserve_source_ids:
                id_map[thread_id] = thread_id
                continue
            while True:
                candidate = str(uuid.uuid4())
                exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (candidate,)).fetchone()
                if not exists and candidate not in generated:
                    generated.add(candidate)
                    id_map[thread_id] = candidate
                    break
        return id_map

    def _match_overwrite_targets(
        self,
        conn: sqlite3.Connection,
        rows: dict[str, dict[str, Any]],
        source_ids: list[str],
        source_provider: str,
        target_provider: str,
        *,
        source_index: dict[str, dict[str, Any]],
        target_index: dict[str, dict[str, Any]],
        overwrite_selections: dict[str, str | None] | None = None,
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[str],
        list[dict[str, Any]],
        list[str],
    ]:
        """Find destination sessions that represent the same conversation.

        Provider switches can create a new thread id while retaining the same
        project and conversation identity. Exact ids therefore win, followed
        by increasingly conservative metadata matches. Ambiguous matches are
        returned as candidate lists so the caller can require an explicit
        target choice instead of risking an unrelated overwrite.
        """
        target_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM threads WHERE model_provider = ?",
                (target_provider,),
            ).fetchall()
        ]
        by_id = {str(row["id"]): row for row in target_rows}
        used_targets: set[str] = set()
        matches: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        ambiguities: list[dict[str, Any]] = []
        selection_errors: list[str] = []
        selections = dict(overwrite_selections or {})

        unknown_selection_ids = sorted(set(selections) - set(source_ids))
        for source_id in unknown_selection_ids:
            selection_errors.append(
                f"Overwrite selection references a source thread outside this copy: {source_id}."
            )

        def text_key(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

        def identity(row: dict[str, Any], index: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
            thread_id = str(row.get("id") or "")
            index_entry = index.get(thread_id) or {}
            name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
            title = text_key(name or row.get("title"))
            first_message = text_key(row.get("first_user_message"))
            cwd = self._path_match_key(str(row.get("cwd") or ""))
            return cwd, title, first_message

        source_identities = {
            source_id: identity(rows[source_id], source_index)
            for source_id in source_ids
            if source_id in rows
        }
        target_identities = {
            str(row["id"]): identity(row, target_index)
            for row in target_rows
        }
        historical_candidates = self._historical_overwrite_candidates(
            source_ids,
            source_provider,
            target_provider,
            set(by_id),
        )

        def display_title(row: dict[str, Any], index: dict[str, dict[str, Any]]) -> str:
            index_entry = index.get(str(row.get("id") or "")) or {}
            name = index_entry.get("thread_name") if isinstance(index_entry, dict) else None
            return str(name or row.get("title") or row.get("preview") or row.get("id") or "")

        def candidate_payload(target_id: str) -> dict[str, Any]:
            row = by_id[target_id]
            return {
                "id": target_id,
                "title": row.get("title") or "",
                "thread_name": (
                    (target_index.get(target_id) or {}).get("thread_name")
                    if isinstance(target_index.get(target_id) or {}, dict)
                    else None
                ),
                "display_title": display_title(row, target_index),
                "preview": row.get("preview") or "",
                "first_message": row.get("first_user_message") or "",
                "cwd": row.get("cwd") or "",
                "archived": bool(row.get("archived")),
                "created_at_ms": row.get("created_at_ms"),
                "updated_at_ms": row.get("updated_at_ms"),
            }

        def ambiguity_payload(
            source_id: str,
            label: str,
            candidate_ids: list[str],
        ) -> dict[str, Any]:
            row = rows[source_id]
            return {
                "source_id": source_id,
                "source_title": row.get("title") or "",
                "source_thread_name": (
                    (source_index.get(source_id) or {}).get("thread_name")
                    if isinstance(source_index.get(source_id) or {}, dict)
                    else None
                ),
                "source_display_title": display_title(row, source_index),
                "source_preview": row.get("preview") or "",
                "source_first_message": row.get("first_user_message") or "",
                "source_cwd": row.get("cwd") or "",
                "source_archived": bool(row.get("archived")),
                "reason": label,
                "candidates": [candidate_payload(target_id) for target_id in candidate_ids],
            }

        def candidate_pool(
            source_id: str,
            source_identity: tuple[str, str, str],
        ) -> tuple[str, list[str]]:
            exact = by_id.get(source_id)
            if exact is not None and str(exact["id"]) not in used_targets:
                return "session id", [source_id]

            history = sorted(historical_candidates.get(source_id, set()) - used_targets)
            if history:
                return (
                    "previous transfer" if len(history) == 1 else "previous transfers",
                    history,
                )

            cwd, title, first_message = source_identity
            predicates: list[tuple[str, Callable[[tuple[str, str, str]], bool]]] = []
            if cwd and title and first_message:
                predicates.append(
                    (
                        "project, title, first message",
                        lambda item: item[0] == cwd and item[1] == title and item[2] == first_message,
                    )
                )
            if cwd and first_message:
                predicates.append(
                    (
                        "project and first message",
                        lambda item: item[0] == cwd and item[2] == first_message,
                    )
                )
            if cwd and title:
                predicates.append(
                    (
                        "project and title",
                        lambda item: item[0] == cwd and item[1] == title,
                    )
                )

            for label, predicate in predicates:
                candidates = sorted(
                    target_id
                    for target_id, target_identity in target_identities.items()
                    if target_id not in used_targets and predicate(target_identity)
                )
                if candidates:
                    return label, candidates
            return "", []

        for source_id in source_ids:
            source_identity = source_identities.get(source_id)
            if source_identity is None:
                continue

            label, candidates = candidate_pool(source_id, source_identity)
            if source_id in selections:
                selected_target = selections[source_id]
                if selected_target is None:
                    continue
                if selected_target in used_targets:
                    selection_errors.append(
                        f"Overwrite selection for thread {source_id} reuses target session {selected_target}."
                    )
                    continue
                if selected_target not in candidates:
                    selection_errors.append(
                        f"Overwrite selection for thread {source_id} is not one of the current target candidates."
                    )
                    continue
                matches[source_id] = {"id": selected_target, "match": "manual selection"}
                used_targets.add(selected_target)
                continue

            if len(candidates) == 1:
                target_id = candidates[0]
                matches[source_id] = {"id": target_id, "match": label}
                used_targets.add(target_id)
                continue
            if len(candidates) > 1:
                if label == "previous transfers":
                    warnings.append(
                        f"Did not overwrite thread {source_id}: previous transfers identify multiple target sessions."
                    )
                else:
                    warnings.append(
                        f"Did not overwrite thread {source_id}: {label} matched multiple target sessions."
                    )
                ambiguities.append(ambiguity_payload(source_id, label, candidates))

        return matches, warnings, ambiguities, selection_errors

    def _historical_overwrite_candidates(
        self,
        source_ids: list[str],
        source_provider: str,
        target_provider: str,
        target_ids: set[str],
    ) -> dict[str, set[str]]:
        candidates = {source_id: set() for source_id in source_ids}
        source_id_set = set(source_ids)
        for manifest_path in self.manifest_dir.glob("copy-*.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not manifest.get("ok"):
                continue
            manifest_source = str(manifest.get("source_provider") or "")
            manifest_target = str(manifest.get("target_provider") or "")
            forward = manifest_source == source_provider and manifest_target == target_provider
            reverse = manifest_source == target_provider and manifest_target == source_provider
            if not forward and not reverse:
                continue
            for item in manifest.get("items") or []:
                if not isinstance(item, dict):
                    continue
                recorded_source = str(item.get("source_id") or "")
                recorded_target = str(item.get("target_id") or "")
                if forward and recorded_source in source_id_set and recorded_target in target_ids:
                    candidates[recorded_source].add(recorded_target)
                if reverse and recorded_target in source_id_set and recorded_source in target_ids:
                    candidates[recorded_target].add(recorded_source)
        return candidates

    def _dest_rollout_path(self, source_path: Path, target_id: str) -> Path:
        match = ROLLOUT_NAME_RE.match(source_path.name)
        if not match:
            raise ValueError(f"Unsupported rollout file name: {source_path.name}")
        return source_path.with_name(f"{match.group('prefix')}{target_id}{match.group('suffix')}")

    def _write_rollout_copy(
        self,
        source_path: Path,
        dest_path: Path,
        source_id: str,
        id_map: dict[str, str],
        target_provider: str,
        *,
        target_cwd: str | None = None,
        target_source: str | None = None,
        overwrite: bool = False,
        history_base_offsets: dict[tuple[str, int], int] | None = None,
        tracked_offsets: set[int] | None = None,
    ) -> dict[int, int]:
        if dest_path.exists() and not overwrite:
            raise FileExistsError(f"Destination rollout already exists: {dest_path}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        session_meta_count = 0
        old_offset = 0
        new_offset = 0
        offsets_to_track = set(tracked_offsets or set()) | {0}
        offset_map: dict[int, int] = {0: 0}
        try:
            with source_path.open("rb") as source, tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{dest_path.name}.",
                suffix=".tmp",
                dir=dest_path.parent,
                delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                line_count = 0
                for line_count, raw_line in enumerate(source, start=1):
                    if not raw_line.strip():
                        temp.write(raw_line)
                        continue
                    if raw_line.endswith(b"\r\n"):
                        content = raw_line[:-2]
                        line_ending = b"\r\n"
                    elif raw_line.endswith(b"\n"):
                        content = raw_line[:-1]
                        line_ending = b"\n"
                    else:
                        content = raw_line
                        line_ending = b""
                    try:
                        parsed = json.loads(content.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            f"Invalid rollout JSON at {source_path}:{line_count}: {exc}"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise ValueError(
                            f"Rollout line is not an object at {source_path}:{line_count}"
                        )
                    changed, is_session_meta = self._rewrite_rollout_record(
                        parsed,
                        source_id=source_id,
                        id_map=id_map,
                        target_provider=target_provider,
                        target_cwd=target_cwd,
                        target_source=target_source,
                        destination_path=dest_path,
                        history_base_offsets=history_base_offsets or {},
                    )
                    session_meta_count += int(is_session_meta)
                    if changed:
                        output_line = compact_json(parsed).encode("utf-8") + line_ending
                    else:
                        output_line = raw_line
                    temp.write(output_line)
                    old_offset += len(raw_line)
                    new_offset += len(output_line)
                    if old_offset in offsets_to_track:
                        offset_map[old_offset] = new_offset
                if line_count == 0:
                    raise ValueError(f"Rollout is empty: {source_path}")
                if session_meta_count == 0:
                    raise ValueError(f"Rollout has no session metadata: {source_path}")
                temp.flush()
                os.fsync(temp.fileno())
            if dest_path.exists() and not overwrite:
                raise FileExistsError(f"Destination rollout already exists: {dest_path}")
            missing_offsets = offsets_to_track.difference(offset_map)
            if missing_offsets:
                missing = min(missing_offsets)
                raise ValueError(
                    f"History base offset {missing} is not a rollout line boundary in {source_path}"
                )
            os.replace(temp_path, dest_path)
            temp_path = None
            return offset_map
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _rewrite_rollout_record(
        self,
        record: dict[str, Any],
        *,
        source_id: str,
        id_map: dict[str, str],
        target_provider: str,
        target_cwd: str | None,
        target_source: str | None,
        destination_path: Path,
        history_base_offsets: dict[tuple[str, int], int],
    ) -> tuple[bool, bool]:
        item_type, payload = self._rollout_record_item(record)
        if not isinstance(payload, dict):
            return False, False

        changed = False
        is_session_meta = item_type == "session_meta"
        if is_session_meta:
            metadata = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
            target_id = id_map.get(source_id)
            if target_id:
                changed |= self._set_json_value(metadata, "id", target_id)
                if "session_id" in metadata:
                    changed |= self._set_json_value(metadata, "session_id", target_id)
            changed |= self._set_json_value(metadata, "model_provider", target_provider)
            if target_cwd:
                changed |= self._set_json_value(metadata, "cwd", target_cwd)
            history_base = metadata.get("history_base")
            if isinstance(history_base, dict):
                base_thread_id = history_base.get("thread_id")
                base_offset = history_base.get("end_byte_offset")
                if (
                    isinstance(base_thread_id, str)
                    and base_thread_id in id_map
                    and isinstance(base_offset, int)
                ):
                    key = (base_thread_id, base_offset)
                    if key not in history_base_offsets:
                        raise ValueError(
                            f"Missing rewritten history offset for {base_thread_id} at {base_offset}"
                        )
                    changed |= self._set_json_value(
                        history_base,
                        "end_byte_offset",
                        history_base_offsets[key],
                    )
            changed |= self._remap_thread_identity_values(metadata, id_map)
            if target_source:
                changed |= self._set_json_value(metadata, "source", target_source)
            elif "source" in metadata:
                remapped_source = self._remap_source_value(metadata["source"], id_map)
                changed |= self._set_json_value(metadata, "source", remapped_source)
            return changed, True

        if item_type != "event_msg":
            return False, False

        event_type = str(payload.get("type") or "")
        if event_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                changed |= self._set_json_value(
                    settings,
                    "model_provider_id",
                    target_provider,
                )
                if target_cwd:
                    changed |= self._set_json_value(settings, "cwd", target_cwd)
                changed |= self._remap_thread_identity_values(settings, id_map)
        elif event_type == "session_configured":
            changed |= self._set_json_value(payload, "model_provider_id", target_provider)
            if target_cwd:
                changed |= self._set_json_value(payload, "cwd", target_cwd)
            if payload.get("rollout_path") is not None:
                changed |= self._set_json_value(
                    payload,
                    "rollout_path",
                    str(destination_path),
                )
            changed |= self._remap_thread_identity_values(payload, id_map)
        return changed, False

    def _write_rollout_plan_concurrently(
        self,
        plan: dict[str, Any],
        *,
        overwrite_paths: set[Path],
        copied_paths: list[Path],
        progress_callback: Callable[[dict[str, Any]], None] | None,
        phase: str = "copying",
    ) -> None:
        items = list(plan.get("items") or [])
        if not items:
            return

        target_sources = plan.get("_target_source_by_source_id", {})
        items_by_id = {str(item["source_id"]): item for item in items}
        dependencies = self._rollout_history_dependencies(items)
        required_offsets: dict[str, set[int]] = {}
        for source_id, dependency in dependencies.items():
            if dependency is None:
                continue
            base_id, base_offset = dependency
            if base_id not in plan["_id_map"]:
                continue
            if base_id not in items_by_id:
                raise ValueError(
                    f"History base {base_id} for {source_id} is not included in the copy plan"
                )
            required_offsets.setdefault(base_id, set()).add(base_offset)

        rewritten_history_offsets: dict[tuple[str, int], int] = {}

        def write_one(
            item: dict[str, Any],
            history_offsets: dict[tuple[str, int], int],
        ) -> RolloutWriteResult:
            destination_path = Path(item["dest_rollout_path"])
            offset_map = self._write_rollout_copy(
                Path(item["source_rollout_path"]),
                destination_path,
                item["source_id"],
                plan["_id_map"],
                plan["target_provider"],
                target_cwd=item.get("target_cwd") if item.get("cwd_rewritten") else None,
                target_source=target_sources.get(item["source_id"]),
                overwrite=destination_path in overwrite_paths,
                history_base_offsets=history_offsets,
                tracked_offsets=required_offsets.get(str(item["source_id"]), set()),
            )
            return RolloutWriteResult(
                item=item,
                destination_path=destination_path,
                offset_map=offset_map,
            )

        pending_ids = set(items_by_id)
        finished_ids: set[str] = set()
        completed = 0
        while pending_ids:
            ready_ids = [
                source_id
                for source_id in items_by_id
                if source_id in pending_ids
                and (
                    dependencies.get(source_id) is None
                    or dependencies[source_id][0] not in plan["_id_map"]
                    or dependencies[source_id][0] in finished_ids
                )
            ]
            if not ready_ids:
                raise ValueError(
                    "Rollout history dependencies contain a cycle: "
                    + ", ".join(sorted(pending_ids))
                )

            history_offsets = dict(rewritten_history_offsets)
            futures: dict[Future[RolloutWriteResult], dict[str, Any]] = {}
            failure: BaseException | None = None
            level_results: list[RolloutWriteResult] = []
            with ThreadPoolExecutor(
                max_workers=min(MAX_COPY_WORKERS, len(ready_ids)),
                thread_name_prefix="session-copy",
            ) as executor:
                for source_id in ready_ids:
                    item = items_by_id[source_id]
                    futures[executor.submit(write_one, item, history_offsets)] = item
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except BaseException as exc:
                        failure = exc
                        for waiting in futures:
                            if waiting is not future:
                                waiting.cancel()
                        break
                    level_results.append(result)
                    completed += 1
                    if result.destination_path not in copied_paths:
                        copied_paths.append(result.destination_path)
                    self._report_copy_progress(
                        progress_callback,
                        phase=phase,
                        current=completed,
                        total=len(items),
                        item=result.item,
                    )

            # Executor shutdown waits for already-running writers. Collect every successful
            # destination so rollback removes files that finished after a peer failed.
            known_results = {result.item["source_id"] for result in level_results}
            for future in futures:
                if future.cancelled() or not future.done() or future.exception() is not None:
                    continue
                result = future.result()
                if result.item["source_id"] not in known_results:
                    level_results.append(result)
                if result.destination_path not in copied_paths:
                    copied_paths.append(result.destination_path)
            if failure is not None:
                raise failure

            for result in level_results:
                source_id = str(result.item["source_id"])
                for old_offset, new_offset in result.offset_map.items():
                    rewritten_history_offsets[(source_id, old_offset)] = new_offset
                finished_ids.add(source_id)
                pending_ids.discard(source_id)

    def _rollout_history_dependencies(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, tuple[str, int] | None]:
        def read_one(item: dict[str, Any]) -> tuple[str, tuple[str, int] | None]:
            source_id = str(item["source_id"])
            dependency = self._rollout_history_dependency(
                Path(item["source_rollout_path"])
            )
            return source_id, dependency

        dependencies: dict[str, tuple[str, int] | None] = {}
        with ThreadPoolExecutor(
            max_workers=min(MAX_COPY_WORKERS, len(items)),
            thread_name_prefix="session-plan",
        ) as executor:
            futures = [executor.submit(read_one, item) for item in items]
            for future in as_completed(futures):
                source_id, dependency = future.result()
                dependencies[source_id] = dependency
        return dependencies

    def _rollout_history_dependency(
        self,
        rollout_path: Path,
    ) -> tuple[str, int] | None:
        with rollout_path.open("rb") as source:
            for line_number, raw_line in enumerate(source, start=1):
                if not raw_line.strip():
                    continue
                try:
                    parsed = json.loads(raw_line.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Invalid rollout JSON at {rollout_path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(parsed, dict):
                    continue
                item_type, _ = self._rollout_record_item(parsed)
                if item_type != "session_meta":
                    continue
                metadata = self._session_meta_payload(parsed)
                history_base = metadata.get("history_base")
                if not isinstance(history_base, dict):
                    return None
                base_id = history_base.get("thread_id")
                base_offset = history_base.get("end_byte_offset")
                if not isinstance(base_id, str) or not isinstance(base_offset, int):
                    raise ValueError(
                        f"Invalid history_base metadata in rollout: {rollout_path}"
                    )
                return base_id, base_offset
        return None

    @staticmethod
    def _rollout_record_item(record: dict[str, Any]) -> tuple[str, Any]:
        item = record.get("item")
        if isinstance(item, dict):
            return str(item.get("type") or ""), item.get("payload")
        return str(record.get("type") or ""), record.get("payload")

    @staticmethod
    def _set_json_value(target: dict[str, Any], key: str, value: Any) -> bool:
        if target.get(key) == value and key in target:
            return False
        target[key] = value
        return True

    def _remap_thread_identity_values(
        self,
        value: Any,
        id_map: dict[str, str],
    ) -> bool:
        changed = False
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ROLLOUT_THREAD_ID_FIELDS and isinstance(child, str) and child in id_map:
                    value[key] = id_map[child]
                    changed = True
                    continue
                if key in ROLLOUT_THREAD_ID_LIST_FIELDS and isinstance(child, list):
                    replacement = [id_map.get(item, item) if isinstance(item, str) else item for item in child]
                    if replacement != child:
                        value[key] = replacement
                        changed = True
                    continue
                changed |= self._remap_thread_identity_values(child, id_map)
        elif isinstance(value, list):
            for child in value:
                changed |= self._remap_thread_identity_values(child, id_map)
        return changed

    def _validate_rebind_rollout(
        self,
        rollout_path: Path,
        thread_id: str,
        errors: list[str],
    ) -> str | None:
        if not rollout_path.exists():
            errors.append(f"Rollout file missing for thread {thread_id}: {rollout_path}")
            return None
        if rollout_path.is_symlink():
            errors.append(f"Symlink rollout is not supported for thread {thread_id}: {rollout_path}")
            return None
        if rollout_path.suffix != ".jsonl" or not ROLLOUT_NAME_RE.match(rollout_path.name):
            errors.append(f"Unsupported rollout file for thread {thread_id}: {rollout_path}")
            return None
        try:
            with rollout_path.open("rb") as fh:
                first_line = fh.readline()
            if not first_line:
                raise ValueError("rollout is empty")
            line_text = first_line.decode("utf-8-sig")
            parsed = json.loads(line_text)
            if not isinstance(parsed, dict):
                raise ValueError("first rollout line is not an object")
            payload = self._session_meta_payload(parsed)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"Invalid session metadata for thread {thread_id}: {rollout_path} ({exc})")
            return None

        for key in ("id", "session_id"):
            value = payload.get(key)
            if value and str(value) != thread_id:
                errors.append(
                    f"Rollout metadata {key} does not match thread {thread_id}: {rollout_path}"
                )
                break
        value = payload.get("model_provider")
        return str(value) if value is not None else None

    def _backup_rollout_files(
        self,
        rollout_paths: list[Path],
        *,
        operation: str,
    ) -> list[tuple[Path, Path]]:
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backups: list[tuple[Path, Path]] = []
        try:
            for rollout_path in rollout_paths:
                backup_path = self.manifest_dir / (
                    f"{operation}-rollout-{uuid.uuid4().hex}-{rollout_path.name}"
                )
                try:
                    # A hard link keeps a full rollback copy without duplicating large transcripts
                    # on the usual same-volume Codex home. Fall back to a regular copy elsewhere.
                    os.link(rollout_path, backup_path)
                except OSError:
                    shutil.copy2(rollout_path, backup_path)
                backups.append((rollout_path, backup_path))
        except Exception:
            for _, backup_path in backups:
                try:
                    backup_path.unlink()
                except OSError:
                    pass
            raise
        return backups

    def _write_rollout_provider(self, rollout_path: Path, target_provider: str) -> None:
        self._write_rollout_copy(
            rollout_path,
            rollout_path,
            "",
            {},
            target_provider,
            overwrite=True,
        )

    def _session_meta_payload(self, rollout_line: dict[str, Any]) -> dict[str, Any]:
        item_type, payload = self._rollout_record_item(rollout_line)
        if item_type == "session_meta" and isinstance(payload, dict):
            metadata = payload.get("meta")
            return metadata if isinstance(metadata, dict) else payload
        raise ValueError("First rollout line is not session_meta")

    def _remap_optional_meta_id(
        self, payload: dict[str, Any], key: str, id_map: dict[str, str]
    ) -> None:
        value = payload.get(key)
        if value in id_map:
            payload[key] = id_map[value]
        elif key in payload:
            payload.pop(key, None)

    def _insert_thread_rows(
        self, conn: sqlite3.Connection, plan: dict[str, Any], target_provider: str
    ) -> None:
        columns = self._table_columns(conn, "threads")
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        imported_at_ms = plan.get("_imported_at_ms")
        imported_at = int(imported_at_ms) // 1000 if imported_at_ms else None
        target_source_by_id = plan.get("_target_source_by_source_id", {})
        target_thread_source_by_id = plan.get("_target_thread_source_by_source_id", {})
        for source_id in plan["_ordered_ids"]:
            row = dict(plan["_rows"][source_id])
            row["id"] = plan["_id_map"][source_id]
            row["rollout_path"] = next(
                item["dest_rollout_path"] for item in plan["items"] if item["source_id"] == source_id
            )
            row["model_provider"] = target_provider
            row["cwd"] = plan.get("_cwd_by_source_id", {}).get(source_id, row["cwd"])
            if plan.get("_unarchive_imported_threads"):
                row["archived"] = 0
                row["archived_at"] = None
            if source_id in target_source_by_id:
                row["source"] = target_source_by_id[source_id]
            else:
                row["source"] = self._remap_source_text(str(row["source"]), plan["_id_map"])
            if source_id in target_thread_source_by_id:
                row["thread_source"] = target_thread_source_by_id[source_id]
            if imported_at is not None:
                row["created_at"] = imported_at
                row["updated_at"] = imported_at
                row["created_at_ms"] = int(imported_at_ms)
                row["updated_at_ms"] = int(imported_at_ms)
            values = [row.get(column) for column in columns]
            conn.execute(
                f"INSERT INTO threads ({column_sql}) VALUES ({placeholders})",
                values,
            )

    def _insert_spawn_edges(self, conn: sqlite3.Connection, plan: dict[str, Any]) -> None:
        self._insert_spawn_edges_from_source(conn, conn, plan)

    def _insert_spawn_edges_from_source(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> None:
        id_map = plan["_id_map"]
        for source_id in plan["_ordered_ids"]:
            rows = source_conn.execute(
                """
                SELECT parent_thread_id, child_thread_id, status
                FROM thread_spawn_edges
                WHERE child_thread_id = ?
                """,
                (source_id,),
            ).fetchall()
            for row in rows:
                parent_id = row["parent_thread_id"]
                child_id = row["child_thread_id"]
                if parent_id in id_map and child_id in id_map:
                    target_conn.execute(
                        """
                        INSERT INTO thread_spawn_edges
                            (parent_thread_id, child_thread_id, status)
                        VALUES (?, ?, ?)
                        """,
                        (id_map[parent_id], id_map[child_id], row["status"]),
                    )

    def _insert_dynamic_tools(self, conn: sqlite3.Connection, plan: dict[str, Any]) -> None:
        self._insert_dynamic_tools_from_source(conn, conn, plan)

    def _insert_dynamic_tools_from_source(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> None:
        if not self._table_exists(source_conn, "thread_dynamic_tools"):
            return
        if not self._table_exists(target_conn, "thread_dynamic_tools"):
            return
        columns = self._table_columns(target_conn, "thread_dynamic_tools")
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        id_map = plan["_id_map"]
        for source_id in plan["_ordered_ids"]:
            rows = source_conn.execute(
                "SELECT * FROM thread_dynamic_tools WHERE thread_id = ? ORDER BY position",
                (source_id,),
            ).fetchall()
            for source_row in rows:
                row = dict(source_row)
                row["thread_id"] = id_map[source_id]
                target_conn.execute(
                    f"INSERT INTO thread_dynamic_tools ({column_sql}) VALUES ({placeholders})",
                    [row.get(column) for column in columns],
                )

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> list[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [row["name"] for row in rows]

    def _load_session_index(self) -> dict[str, dict[str, Any]]:
        return self._load_session_index_from_path(self.session_index_path)

    def _load_session_index_from_path(self, path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        entries: dict[str, dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return entries
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            thread_id = item.get("id")
            if thread_id:
                entries[str(thread_id)] = item
        return entries

    def _snapshot_session_index(self) -> tuple[bool, str]:
        if not self.session_index_path.exists():
            return False, ""
        return True, self.session_index_path.read_text(encoding="utf-8")

    def _restore_session_index(self, snapshot: tuple[bool, str]) -> None:
        existed, text = snapshot
        if existed:
            self.session_index_path.write_text(text, encoding="utf-8")
            return
        try:
            self.session_index_path.unlink()
        except FileNotFoundError:
            pass

    def _append_session_index_entries(
        self,
        plan: dict[str, Any],
        *,
        source_index: dict[str, dict[str, Any]] | None = None,
        existing_index: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        source_entries = source_index if source_index is not None else self._load_session_index()
        target_entries = existing_index if existing_index is not None else self._load_session_index()
        existing_ids = set(target_entries)
        overwrite_ids = set(plan.get("_overwrite_session_index_ids", set()))
        entries: list[dict[str, Any]] = []
        for source_id in plan["_ordered_ids"]:
            target_id = plan["_id_map"][source_id]
            if target_id in existing_ids and target_id not in overwrite_ids:
                continue
            row = plan["_rows"][source_id]
            source_entry = source_entries.get(source_id) or {}
            thread_name = source_entry.get("thread_name") if isinstance(source_entry, dict) else None
            updated_at = self._imported_at_iso(plan) or (
                source_entry.get("updated_at") if isinstance(source_entry, dict) else None
            )
            entries.append(
                {
                    "id": target_id,
                    "thread_name": thread_name or row.get("title") or row.get("preview") or target_id,
                    "updated_at": updated_at or self._thread_row_updated_at_iso(row),
                }
            )

        if not entries:
            return []

        if overwrite_ids:
            self._replace_session_index_entries(entries)
        else:
            self._write_session_index_entries(entries)
        return entries

    def _replace_session_index_entries(self, entries: list[dict[str, Any]]) -> None:
        replacements = {str(entry["id"]): entry for entry in entries}
        lines: list[str] = []
        replaced: set[str] = set()
        if self.session_index_path.exists():
            for line in self.session_index_path.read_text(encoding="utf-8").splitlines():
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = None
                thread_id = parsed.get("id") if isinstance(parsed, dict) else None
                if thread_id in replacements:
                    thread_id = str(thread_id)
                    if thread_id in replaced:
                        continue
                    lines.append(json.dumps(replacements[thread_id], ensure_ascii=False, separators=(",", ":")))
                    replaced.add(thread_id)
                    continue
                lines.append(line)

        for entry in entries:
            thread_id = str(entry["id"])
            if thread_id not in replaced:
                lines.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                replaced.add(thread_id)

        self.session_index_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_index_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )

    def _write_session_index_entries(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        self.session_index_path.parent.mkdir(parents=True, exist_ok=True)
        needs_newline = False
        if self.session_index_path.exists() and self.session_index_path.stat().st_size:
            with self.session_index_path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_newline = fh.read(1) != b"\n"

        with self.session_index_path.open("a", encoding="utf-8", newline="\n") as fh:
            if needs_newline:
                fh.write("\n")
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _thread_row_updated_at_iso(self, row: dict[str, Any]) -> str:
        updated_at_ms = row.get("updated_at_ms")
        if updated_at_ms:
            return datetime.fromtimestamp(int(updated_at_ms) / 1000, UTC).isoformat().replace("+00:00", "Z")
        updated_at = row.get("updated_at")
        if updated_at:
            return datetime.fromtimestamp(int(updated_at), UTC).isoformat().replace("+00:00", "Z")
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _mirror_target_rollout_paths(self, plan: dict[str, Any]) -> list[Path]:
        paths: list[Path] = []
        seen: set[tuple[str, str]] = set()
        safe_roots = [
            (self.codex_home / "sessions").resolve(),
            (self.codex_home / "archived_sessions").resolve(),
        ]
        for row in plan.get("_target_rows", {}).values():
            path = Path(str(row.get("rollout_path") or ""))
            if not path.exists():
                continue
            resolved = path.resolve()
            if not any(self._path_is_within(resolved, root) for root in safe_roots):
                raise ValueError(
                    f"Refusing to replace target rollout outside the Codex session folders: {path}"
                )
            plain_name = resolved.name[:-4] if resolved.name.endswith(".zst") else resolved.name
            if not ROLLOUT_NAME_RE.match(plain_name):
                raise ValueError(f"Unsupported target rollout file name: {path}")
            path_key = self._source_path_match_key(str(resolved))
            if path_key not in seen:
                seen.add(path_key)
                paths.append(resolved)
        return paths

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        path_text = str(path)
        root_text = str(root)
        normalized_path = CodexSessionTransfer._normalize_windows_path(path_text)
        normalized_root = CodexSessionTransfer._normalize_windows_path(root_text)

        def is_windows_path(value: str) -> bool:
            return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")

        path_is_windows = is_windows_path(normalized_path)
        root_is_windows = is_windows_path(normalized_root)
        if path_is_windows or root_is_windows:
            if not (path_is_windows and root_is_windows):
                return False
            comparable_path = ntpath.normcase(ntpath.normpath(normalized_path))
            comparable_root = ntpath.normcase(ntpath.normpath(normalized_root))
            try:
                return ntpath.commonpath((comparable_path, comparable_root)) == comparable_root
            except ValueError:
                return False
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _new_mirror_backup_directory(self) -> Path:
        root = self.codex_home / "session-transfer" / "backups"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"provider-mirror-{timestamp}"
        counter = 1
        while path.exists():
            path = root / f"provider-mirror-{timestamp}-{counter}"
            counter += 1
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _backup_runtime_databases(self, backup_dir: Path) -> dict[str, str]:
        database_dir = backup_dir / "databases"
        database_dir.mkdir(parents=True, exist_ok=True)
        backups: dict[str, str] = {}
        for filename in RUNTIME_DATABASE_FILENAMES:
            source_path = self.sqlite_home / filename
            if not source_path.exists():
                continue
            destination_path = database_dir / filename
            self._backup_sqlite_file(source_path, destination_path)
            backups[filename] = str(destination_path)
        if STATE_DB_FILENAME not in backups:
            raise FileNotFoundError(f"Codex state database is missing: {self.db_path}")
        return backups

    @staticmethod
    def _backup_sqlite_file(source_path: Path, destination_path: Path) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(source_path)
        try:
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    def _backup_session_index_for_mirror(self, backup_dir: Path) -> str | None:
        if not self.session_index_path.exists():
            return None
        destination = backup_dir / SESSION_INDEX_FILENAME
        shutil.copy2(self.session_index_path, destination)
        return str(destination)

    def _backup_mirror_rollouts(
        self,
        rollout_paths: list[Path],
        backup_dir: Path,
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> list[RolloutBackup]:
        if not rollout_paths:
            self._report_copy_progress(
                progress_callback,
                phase="backing_up",
                current=0,
                total=1,
                message="Target provider has no rollout files to back up",
            )
            return []

        backup_root = backup_dir / "rollouts"
        codex_root = self.codex_home.resolve()

        def backup_one(index: int, original_path: Path) -> RolloutBackup:
            try:
                relative_source = Path(
                    self._normalize_windows_path(str(original_path))
                ).resolve()
                relative = relative_source.relative_to(codex_root)
            except ValueError:
                relative = Path("external") / f"{index:06d}-{original_path.name}"
            backup_path = backup_root / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(original_path, backup_path)
            except OSError:
                shutil.copy2(original_path, backup_path)
            return RolloutBackup(original_path=original_path, backup_path=backup_path)

        futures: dict[Future[RolloutBackup], Path] = {}
        backups: list[RolloutBackup] = []
        failure: BaseException | None = None
        with ThreadPoolExecutor(
            max_workers=min(MAX_COPY_WORKERS, len(rollout_paths)),
            thread_name_prefix="session-backup",
        ) as executor:
            for index, path in enumerate(rollout_paths, start=1):
                futures[executor.submit(backup_one, index, path)] = path
            for future in as_completed(futures):
                try:
                    backup = future.result()
                except BaseException as exc:
                    failure = exc
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    break
                backups.append(backup)
                self._report_copy_progress(
                    progress_callback,
                    phase="backing_up",
                    current=len(backups),
                    total=len(rollout_paths),
                    message=backup.original_path.name,
                )
        if failure is not None:
            raise failure
        return backups

    def _write_mirror_backup_manifest(
        self,
        backup_dir: Path,
        request: CopyRequest,
        *,
        database_backups: dict[str, str],
        rollout_backups: list[RolloutBackup],
        session_index_backup: str | None,
        source_ids: set[str],
        target_ids: set[str],
    ) -> None:
        payload = {
            "created_at": datetime.now(UTC).isoformat(),
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "source_session_ids": sorted(source_ids),
            "target_session_ids": sorted(target_ids),
            "database_backups": database_backups,
            "session_index_backup": session_index_backup,
            "rollout_backups": [
                {
                    "original_path": str(item.original_path),
                    "backup_path": str(item.backup_path),
                }
                for item in rollout_backups
            ],
        }
        (backup_dir / "backup-manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _attach_mirror_sidecars(self, conn: sqlite3.Connection) -> dict[str, str]:
        attached: dict[str, str] = {}
        for filename, alias in MIRROR_SIDECAR_ALIASES.items():
            path = self.sqlite_home / filename
            if not path.exists():
                continue
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
            attached[filename] = alias
        return attached

    @staticmethod
    def _checked_identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"Unsafe SQLite identifier: {value}")
        return value

    def _qualified_table(self, schema: str, table: str) -> str:
        return f"{self._checked_identifier(schema)}.{self._checked_identifier(table)}"

    def _schema_table_exists(
        self,
        conn: sqlite3.Connection,
        schema: str,
        table: str,
    ) -> bool:
        schema = self._checked_identifier(schema)
        row = conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _schema_table_columns(
        self,
        conn: sqlite3.Connection,
        schema: str,
        table: str,
    ) -> list[str]:
        schema = self._checked_identifier(schema)
        table = self._checked_identifier(table)
        rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        return [str(row["name"]) for row in rows]

    def _thread_scoped_tables(
        self,
        conn: sqlite3.Connection,
        schema: str,
    ) -> list[str]:
        schema = self._checked_identifier(schema)
        names = [
            str(row["name"])
            for row in conn.execute(
                f"SELECT name FROM {schema}.sqlite_master "
                "WHERE type = 'table' AND name LIKE 'thread_%' ORDER BY name"
            ).fetchall()
        ]
        candidates = [
            name
            for name in names
            if name not in {"threads", "thread_spawn_edges"}
            and "thread_id" in self._schema_table_columns(conn, schema, name)
        ]
        dependencies: dict[str, set[str]] = {name: set() for name in candidates}
        for name in candidates:
            foreign_keys = conn.execute(
                f"PRAGMA {schema}.foreign_key_list({self._checked_identifier(name)})"
            ).fetchall()
            for foreign_key in foreign_keys:
                dependency = str(foreign_key["table"])
                if dependency in dependencies:
                    dependencies[name].add(dependency)

        ordered: list[str] = []
        pending = set(candidates)
        while pending:
            ready = sorted(
                name for name in pending if not (dependencies[name] & pending)
            )
            if not ready:
                ordered.extend(sorted(pending))
                break
            ordered.extend(ready)
            pending.difference_update(ready)
        return ordered

    @staticmethod
    def _id_chunks(values: list[str], size: int = 400) -> list[list[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    def _delete_thread_keyed_rows(
        self,
        conn: sqlite3.Connection,
        schema: str,
        table: str,
        column: str,
        thread_ids: set[str],
    ) -> int:
        if not thread_ids or not self._schema_table_exists(conn, schema, table):
            return 0
        qualified = self._qualified_table(schema, table)
        column = self._checked_identifier(column)
        deleted = 0
        for chunk in self._id_chunks(sorted(thread_ids)):
            placeholders = ",".join("?" for _ in chunk)
            deleted += conn.execute(
                f"DELETE FROM {qualified} WHERE {column} IN ({placeholders})",
                chunk,
            ).rowcount
        return deleted

    def _clear_mirror_target_records(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        attached: dict[str, str],
    ) -> None:
        target_ids = set(plan.get("_target_ids", set()))
        source_ids = set(plan.get("_ordered_ids", []))

        logs = attached.get("logs_2.sqlite")
        if logs and target_ids:
            self._delete_thread_keyed_rows(conn, logs, "logs", "thread_id", target_ids)

        queue = attached.get("queue_1.sqlite")
        if queue and target_ids:
            self._delete_thread_keyed_rows(
                conn, queue, "queued_items", "thread_id", target_ids
            )
            self._delete_thread_keyed_rows(
                conn, queue, "queued_thread_revisions", "thread_id", target_ids
            )

        history = attached.get("thread_history_1.sqlite")
        if history:
            for table in reversed(self._thread_scoped_tables(conn, history)):
                self._delete_thread_keyed_rows(
                    conn, history, table, "thread_id", target_ids | source_ids
                )

        goals = attached.get("goals_1.sqlite")
        if goals and target_ids:
            for table in reversed(self._thread_scoped_tables(conn, goals)):
                self._delete_thread_keyed_rows(
                    conn, goals, table, "thread_id", target_ids
                )

        memories = attached.get("memories_1.sqlite")
        if memories and target_ids:
            self._delete_thread_keyed_rows(
                conn, memories, "stage1_outputs", "thread_id", target_ids
            )

        if target_ids:
            for table in reversed(self._thread_scoped_tables(conn, "main")):
                self._delete_thread_keyed_rows(
                    conn, "main", table, "thread_id", target_ids
                )
        self._delete_thread_keyed_rows(
            conn, "main", "thread_spawn_edges", "parent_thread_id", target_ids
        )
        self._delete_thread_keyed_rows(
            conn, "main", "thread_spawn_edges", "child_thread_id", target_ids
        )
        self._delete_thread_keyed_rows(conn, "main", "threads", "id", target_ids)

    def _copy_thread_keyed_rows(
        self,
        conn: sqlite3.Connection,
        schema: str,
        table: str,
        plan: dict[str, Any],
    ) -> int:
        if not self._schema_table_exists(conn, schema, table):
            return 0
        columns = self._schema_table_columns(conn, schema, table)
        if "thread_id" not in columns:
            return 0
        qualified = self._qualified_table(schema, table)
        column_sql = ",".join(self._checked_identifier(column) for column in columns)
        placeholders = ",".join("?" for _ in columns)
        copied = 0
        for chunk in self._id_chunks(list(plan["_ordered_ids"])):
            select_placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT * FROM {qualified} WHERE thread_id IN ({select_placeholders})",
                chunk,
            ).fetchall()
            for source_row in rows:
                row = dict(source_row)
                row["thread_id"] = plan["_id_map"][str(row["thread_id"])]
                conn.execute(
                    f"INSERT INTO {qualified} ({column_sql}) VALUES ({placeholders})",
                    [row.get(column) for column in columns],
                )
                copied += 1
        return copied

    def _copy_mirror_state_records(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
    ) -> None:
        for table in self._thread_scoped_tables(conn, "main"):
            self._copy_thread_keyed_rows(conn, "main", table, plan)

    def _copy_mirror_sidecar_records(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        attached: dict[str, str],
    ) -> None:
        goals = attached.get("goals_1.sqlite")
        if goals:
            for table in self._thread_scoped_tables(conn, goals):
                self._copy_thread_keyed_rows(conn, goals, table, plan)
        memories = attached.get("memories_1.sqlite")
        if memories:
            self._copy_thread_keyed_rows(conn, memories, "stage1_outputs", plan)

    def _replace_session_index_for_mirror(
        self,
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source_entries = self._load_session_index()
        remove_ids = set(plan.get("_target_ids", set())) | set(plan["_id_map"].values())
        preserved_lines: list[str] = []
        if self.session_index_path.exists():
            for line in self.session_index_path.read_text(encoding="utf-8").splitlines():
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = None
                thread_id = str(parsed.get("id")) if isinstance(parsed, dict) and parsed.get("id") else ""
                if thread_id in remove_ids:
                    continue
                preserved_lines.append(line)

        entries: list[dict[str, Any]] = []
        for source_id in plan["_ordered_ids"]:
            row = plan["_rows"][source_id]
            source_entry = source_entries.get(source_id) or {}
            thread_name = source_entry.get("thread_name") if isinstance(source_entry, dict) else None
            updated_at = source_entry.get("updated_at") if isinstance(source_entry, dict) else None
            entries.append(
                {
                    "id": plan["_id_map"][source_id],
                    "thread_name": thread_name
                    or row.get("title")
                    or row.get("preview")
                    or plan["_id_map"][source_id],
                    "updated_at": updated_at or self._thread_row_updated_at_iso(row),
                }
            )
        lines = preserved_lines + [compact_json(entry) for entry in entries]
        self._atomic_write_text(
            self.session_index_path,
            "\n".join(lines) + ("\n" if lines else ""),
        )
        return entries

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                temp.write(text)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _count_thread_keyed_rows(
        self,
        conn: sqlite3.Connection,
        schema: str,
        table: str,
        column: str,
        thread_ids: set[str],
    ) -> int:
        if not thread_ids or not self._schema_table_exists(conn, schema, table):
            return 0
        qualified = self._qualified_table(schema, table)
        column = self._checked_identifier(column)
        total = 0
        for chunk in self._id_chunks(sorted(thread_ids)):
            placeholders = ",".join("?" for _ in chunk)
            total += int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {qualified} WHERE {column} IN ({placeholders})",
                    chunk,
                ).fetchone()[0]
            )
        return total

    def _verify_mirror(
        self,
        conn: sqlite3.Connection,
        plan: dict[str, Any],
        attached: dict[str, str],
    ) -> None:
        source_ids = set(plan["_ordered_ids"])
        target_ids = set(plan["_id_map"].values())
        old_target_ids = set(plan.get("_target_ids", set()))
        source_count = self._count_thread_keyed_rows(
            conn, "main", "threads", "id", source_ids
        )
        target_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider = ?",
                (plan["target_provider"],),
            ).fetchone()[0]
        )
        stale_source_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM threads WHERE model_provider = ?",
                (plan["source_provider"],),
            ).fetchone()[0]
        )
        if source_count != len(source_ids) or target_count != len(source_ids):
            raise RuntimeError(
                f"Mirror verification expected {len(source_ids)} source and target sessions, "
                f"found {source_count} source and {target_count} target"
            )
        if stale_source_count:
            raise RuntimeError(
                f"Mirror verification found {stale_source_count} session(s) still routed through "
                f"source provider {plan['source_provider']}"
            )
        if self._count_thread_keyed_rows(
            conn, "main", "threads", "id", old_target_ids
        ):
            raise RuntimeError("Old target session rows remain after replacement")
        if self._count_thread_keyed_rows(
            conn, "main", "threads", "id", target_ids
        ) != len(target_ids):
            raise RuntimeError("One or more mirrored target session rows are missing")
        missing_rollouts = [
            item["dest_rollout_path"]
            for item in plan["items"]
            if not Path(item["dest_rollout_path"]).is_file()
        ]
        if missing_rollouts:
            raise RuntimeError(f"Mirrored rollout is missing: {missing_rollouts[0]}")

        for table in self._thread_scoped_tables(conn, "main"):
            source_rows = self._count_thread_keyed_rows(
                conn, "main", table, "thread_id", source_ids
            )
            target_rows = self._count_thread_keyed_rows(
                conn, "main", table, "thread_id", target_ids
            )
            if source_rows != target_rows:
                raise RuntimeError(
                    f"State table {table} copied {target_rows} row(s); expected {source_rows}"
                )

        for filename, table_names in (
            ("goals_1.sqlite", None),
            ("memories_1.sqlite", ["stage1_outputs"]),
        ):
            schema = attached.get(filename)
            if not schema:
                continue
            tables = table_names or self._thread_scoped_tables(conn, schema)
            for table in tables:
                source_rows = self._count_thread_keyed_rows(
                    conn, schema, table, "thread_id", source_ids
                )
                target_rows = self._count_thread_keyed_rows(
                    conn, schema, table, "thread_id", target_ids
                )
                if source_rows != target_rows:
                    raise RuntimeError(
                        f"Runtime table {filename}:{table} copied {target_rows} row(s); "
                        f"expected {source_rows}"
                    )

        history = attached.get("thread_history_1.sqlite")
        if history:
            for table in self._thread_scoped_tables(conn, history):
                if self._count_thread_keyed_rows(
                    conn, history, table, "thread_id", old_target_ids | target_ids
                ):
                    raise RuntimeError(
                        f"History projection table {table} was not cleared for rebuild"
                    )

        foreign_key_errors = conn.execute("PRAGMA main.foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(f"Foreign key verification failed: {foreign_key_errors[0]}")

        index_entries = self._load_session_index()
        if old_target_ids.intersection(index_entries):
            raise RuntimeError("Old target session names remain in session_index.jsonl")
        if not target_ids.issubset(index_entries):
            raise RuntimeError("Mirrored session names are missing from session_index.jsonl")

    @staticmethod
    def _remove_written_rollouts(paths: list[Path]) -> None:
        for path in set(paths):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _restore_mirror_rollout_backups(backups: list[RolloutBackup]) -> list[str]:
        errors: list[str] = []
        for backup in backups:
            temp_path = backup.original_path.with_name(
                f".{backup.original_path.name}.{uuid.uuid4().hex}.restore"
            )
            try:
                backup.original_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup.backup_path, temp_path)
                os.replace(temp_path, backup.original_path)
            except OSError as exc:
                errors.append(f"{backup.original_path}: {exc}")
            finally:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
        return errors

    def _delete_mirrored_target_rollouts(
        self,
        rollout_paths: list[Path],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> list[str]:
        if not rollout_paths:
            return []

        def delete_one(path: Path) -> None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        errors: list[str] = []
        completed = 0
        with ThreadPoolExecutor(
            max_workers=min(MAX_COPY_WORKERS, len(rollout_paths)),
            thread_name_prefix="session-cleanup",
        ) as executor:
            futures = {executor.submit(delete_one, path): path for path in rollout_paths}
            for future in as_completed(futures):
                path = futures[future]
                try:
                    future.result()
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
                completed += 1
                self._report_copy_progress(
                    progress_callback,
                    phase="cleaning",
                    current=completed,
                    total=len(rollout_paths),
                    message=path.name,
                )
        return errors

    def _backup_database(self, *, operation: str = "copy") -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.manifest_dir / f"state_5.before-{operation}-{timestamp}.sqlite"
        counter = 1
        while backup_path.exists():
            backup_path = self.manifest_dir / f"state_5.before-{operation}-{timestamp}-{counter}.sqlite"
            counter += 1
        source = sqlite3.connect(self.db_path)
        try:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return backup_path

    def _write_manifest(
        self,
        payload: dict[str, Any],
        request: CopyRequest | RebindRequest,
        *,
        operation: str = "copy",
    ) -> Path:
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = self.manifest_dir / f"{operation}-{timestamp}.json"
        counter = 1
        while manifest_path.exists():
            manifest_path = self.manifest_dir / f"{operation}-{timestamp}-{counter}.json"
            counter += 1
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "source_sqlite_home": str(self.sqlite_home),
            "state_db_path": str(self.db_path),
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "overwrite": bool(getattr(request, "overwrite", False)),
            **self._manifest_safe_payload(payload),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path

    def _public_plan(
        self,
        plan: dict[str, Any],
        *,
        request: CopyRequest | None = None,
    ) -> dict[str, Any]:
        payload = {key: value for key, value in plan.items() if not key.startswith("_")}
        ambiguities = plan.get("_overwrite_ambiguities") or []
        payload["overwrite_ambiguities"] = [
            self._public_overwrite_ambiguity(item)
            for item in ambiguities
            if isinstance(item, dict)
        ]
        payload["overwrite_ambiguity_count"] = len(payload["overwrite_ambiguities"])
        if request is None:
            return payload

        all_items = payload.get("items") or []
        offset = min(request.preview_offset, len(all_items))
        end = min(offset + request.preview_limit, len(all_items))
        payload["items"] = [self._public_plan_item(item) for item in all_items[offset:end]]
        payload["item_total"] = len(all_items)
        payload["preview_offset"] = offset
        payload["preview_limit"] = request.preview_limit
        payload["has_more"] = end < len(all_items)
        payload["next_preview_offset"] = end if end < len(all_items) else None
        return payload

    def _public_rebind_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in plan.items() if not key.startswith("_")}
        items = payload.get("items") or []
        payload["items"] = [
            self._public_plan_item(item)
            for item in items
            if isinstance(item, dict)
        ]
        payload["item_total"] = len(items)
        payload["rebound_count"] = len(items)
        return payload

    def _public_plan_item(self, item: dict[str, Any]) -> dict[str, Any]:
        public_item = {
            key: item[key]
            for key in PREVIEW_ITEM_FIELDS
            if key in item
        }
        for key in ("title", "thread_name", "display_title"):
            if key in public_item:
                public_item[key] = _clip_preview_text(public_item[key])
        return public_item

    def _public_overwrite_ambiguity(self, item: dict[str, Any]) -> dict[str, Any]:
        public_item = {
            key: item.get(key)
            for key in (
                "source_id",
                "source_title",
                "source_thread_name",
                "source_display_title",
                "source_preview",
                "source_first_message",
                "source_cwd",
                "source_archived",
                "reason",
            )
            if key in item
        }
        public_item["candidates"] = []
        for candidate in item.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            public_candidate = {
                key: candidate.get(key)
                for key in (
                    "id",
                    "title",
                    "thread_name",
                    "display_title",
                    "preview",
                    "first_message",
                    "cwd",
                    "archived",
                    "created_at_ms",
                    "updated_at_ms",
                )
                if key in candidate
            }
            for key in ("title", "thread_name", "display_title", "preview", "first_message", "cwd"):
                if key in public_candidate:
                    public_candidate[key] = _clip_preview_text(public_candidate[key])
            public_item["candidates"].append(public_candidate)
        for key in (
            "source_title",
            "source_thread_name",
            "source_display_title",
            "source_preview",
            "source_first_message",
            "source_cwd",
        ):
            if key in public_item:
                public_item[key] = _clip_preview_text(public_item[key])
        return public_item

    def public_copy_result(self, result: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in result.items() if not key.startswith("_")}
        items = result.get("items") or []
        payload["items"] = [
            self._public_plan_item(item)
            for item in items
            if isinstance(item, dict)
        ]
        payload["item_total"] = int(result.get("item_total") or len(items))
        payload["overwritten_count"] = sum(
            1
            for item in items
            if isinstance(item, dict) and item.get("overwritten")
        )
        return payload

    def public_rebind_result(self, result: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in result.items() if not key.startswith("_")}
        items = result.get("items") or []
        payload["items"] = [
            self._public_plan_item(item)
            for item in items
            if isinstance(item, dict)
        ]
        payload["item_total"] = int(result.get("item_total") or len(items))
        payload["rebound_count"] = int(result.get("rebound_count") or len(items))
        return payload

    def _manifest_safe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cloned = json.loads(json.dumps(payload, ensure_ascii=False))
        for item in cloned.get("items") or []:
            if isinstance(item, dict):
                item.pop("title", None)
                item.pop("display_title", None)
                item.pop("thread_name", None)
        return cloned

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _source_parent_id(self, source: Any) -> str | None:
        parsed = self._parse_source(source)
        if not isinstance(parsed, dict):
            return None
        subagent = parsed.get("subagent")
        if not isinstance(subagent, dict):
            return None
        thread_spawn = subagent.get("thread_spawn")
        if isinstance(thread_spawn, dict):
            parent = thread_spawn.get("parent_thread_id")
            return str(parent) if parent else None
        return None

    def _remap_source_text(self, source: str, id_map: dict[str, str]) -> str:
        parsed = self._parse_source(source)
        if not isinstance(parsed, dict):
            return source
        changed = self._remap_source_object(parsed, id_map)
        return compact_json(parsed) if changed else source

    def _remap_source_value(self, source: Any, id_map: dict[str, str]) -> Any:
        if isinstance(source, str):
            parsed = self._parse_source(source)
            if not isinstance(parsed, dict):
                return source
            changed = self._remap_source_object(parsed, id_map)
            return compact_json(parsed) if changed else source
        if isinstance(source, dict):
            cloned = json.loads(compact_json(source))
            self._remap_source_object(cloned, id_map)
            return cloned
        return source

    def _remap_source_object(self, source: dict[str, Any], id_map: dict[str, str]) -> bool:
        subagent = source.get("subagent")
        if not isinstance(subagent, dict):
            return False
        thread_spawn = subagent.get("thread_spawn")
        if not isinstance(thread_spawn, dict):
            return False
        parent_id = thread_spawn.get("parent_thread_id")
        if parent_id in id_map:
            thread_spawn["parent_thread_id"] = id_map[parent_id]
            return True
        return False

    def _parse_source(self, source: Any) -> Any:
        if isinstance(source, dict):
            return json.loads(compact_json(source))
        if not isinstance(source, str):
            return source
        try:
            return json.loads(source)
        except json.JSONDecodeError:
            return source


def parse_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def app_base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def parent_process_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False
    if parent_pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == wait_failed:
                return True
            return result == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watch_parent_process(
    parent_pid: int,
    stop_callback: Callable[[], None],
    *,
    poll_interval: float = 1.0,
) -> None:
    while parent_process_is_alive(parent_pid):
        time.sleep(poll_interval)
    stop_callback()


def make_handler(
    transfer: CodexSessionTransfer,
    static_dir: Path,
    *,
    instance_token: str = "",
) -> type[SimpleHTTPRequestHandler]:
    class TransferRequestHandler(SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "instance_token": instance_token})
                return
            if parsed.path == "/api/status":
                self._send_json(transfer.status())
                return
            if parsed.path == "/api/providers":
                self._send_json(transfer.list_providers())
                return
            if parsed.path == "/api/target-providers":
                self._send_json(transfer.list_target_providers())
                return
            if parsed.path == "/api/session-stats":
                self._send_json(transfer.session_stats())
                return
            if parsed.path == "/api/package-source":
                self._send_json(transfer.package_status())
                return
            if parsed.path == "/api/skills":
                query = parse_qs(parsed.query)
                self._send_json(transfer.list_skills(search=query.get("search", [""])[0]))
                return
            if parsed.path == "/api/skills-package-source":
                self._send_json(transfer.skills_package_status())
                return
            if parsed.path == "/api/package-skills":
                query = parse_qs(parsed.query)
                self._send_json(transfer.list_package_skills(search=query.get("search", [""])[0]))
                return
            if parsed.path == "/api/threads":
                query = parse_qs(parsed.query)
                self._send_json(
                    transfer.list_threads(
                        source_provider=query.get("source_provider", [""])[0] or None,
                        include_archived=parse_bool(query.get("include_archived", [""])[0]),
                        search=query.get("search", [""])[0],
                        cwd=query.get("cwd", [""])[0],
                        source=query.get("source", [""])[0],
                        date_from=query.get("date_from", [""])[0],
                        date_to=query.get("date_to", [""])[0],
                        recent_limit=max(0, parse_int(query.get("recent_limit", [""])[0], 0)),
                    )
                )
                return
            if parsed.path == "/api/package-threads":
                query = parse_qs(parsed.query)
                self._send_json(
                    transfer.list_package_threads(
                        source_provider=query.get("source_provider", [""])[0] or None,
                        include_archived=parse_bool(query.get("include_archived", [""])[0]),
                        search=query.get("search", [""])[0],
                        cwd=query.get("cwd", [""])[0],
                        source=query.get("source", [""])[0],
                        date_from=query.get("date_from", [""])[0],
                        date_to=query.get("date_to", [""])[0],
                        recent_limit=max(0, parse_int(query.get("recent_limit", [""])[0], 0)),
                    )
                )
                return
            if parsed.path == "/api/thread-detail":
                query = parse_qs(parsed.query)
                self._send_json(
                    transfer.thread_detail(
                        query.get("id", [""])[0],
                        item_offset=parse_int(query.get("offset", [""])[0], 0),
                        item_limit=parse_int(
                            query.get("limit", [""])[0],
                            DEFAULT_THREAD_DETAIL_LIMIT,
                        ),
                    )
                )
                return
            if parsed.path == "/api/package-thread-detail":
                query = parse_qs(parsed.query)
                self._send_json(
                    transfer.package_thread_detail(
                        query.get("id", [""])[0],
                        item_offset=parse_int(query.get("offset", [""])[0], 0),
                        item_limit=parse_int(
                            query.get("limit", [""])[0],
                            DEFAULT_THREAD_DETAIL_LIMIT,
                        ),
                    )
                )
                return
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path == "/api/kill-blocking-processes":
                    if not self._has_action_header("kill-blocking-processes"):
                        self._send_json(
                            {"ok": False, "errors": ["Missing action confirmation header"]},
                            HTTPStatus.FORBIDDEN,
                        )
                        return
                    self._send_json(transfer.kill_blocking_processes())
                    return
                if self.path == "/api/repair-session-index":
                    if not self._has_action_header("repair-session-index"):
                        self._send_json(
                            {"ok": False, "errors": ["Missing action confirmation header"]},
                            HTTPStatus.FORBIDDEN,
                        )
                        return
                    self._send_json(transfer.repair_session_index_from_manifests())
                    return
                if self.path == "/api/export-package":
                    request = ExportPackageRequest.from_json(self._read_json())
                    self._send_json(transfer.export_package(request))
                    return
                if self.path == "/api/open-path":
                    data = self._read_json()
                    self._send_json(transfer.open_path(Path(str(data.get("path", "")).strip())))
                    return
                if self.path == "/api/load-package":
                    data = self._read_json()
                    self._send_json(transfer.load_transfer_package(Path(str(data.get("path", "")).strip())))
                    return
                if self.path == "/api/upload-package":
                    self._send_json(
                        transfer.load_uploaded_transfer_package(
                            self._upload_filename("codex-session-package.zip"),
                            self._read_binary(),
                        )
                    )
                    return
                if self.path == "/api/unload-package":
                    self._send_json(transfer.unload_transfer_package())
                    return
                if self.path == "/api/export-skills-package":
                    request = SkillPackageRequest.from_json(self._read_json())
                    self._send_json(transfer.export_skills_package(request))
                    return
                if self.path == "/api/load-skills-package":
                    data = self._read_json()
                    self._send_json(transfer.load_skills_package(Path(str(data.get("path", "")).strip())))
                    return
                if self.path == "/api/upload-skills-package":
                    self._send_json(
                        transfer.load_uploaded_skills_package(
                            self._upload_filename("codex-skills-package.zip"),
                            self._read_binary(),
                        )
                    )
                    return
                if self.path == "/api/unload-skills-package":
                    self._send_json(transfer.unload_skills_package())
                    return
                if self.path == "/api/preview-skill-import":
                    request = SkillImportRequest.from_json(self._read_json())
                    self._send_json(transfer.preview_import_skills(request))
                    return
                if self.path == "/api/import-skills":
                    request = SkillImportRequest.from_json(self._read_json())
                    self._send_json(transfer.import_skills(request))
                    return

                if self.path == "/api/rebind-progress":
                    request = RebindRequest.from_json(self._read_json())
                    self._stream_rebind(request)
                    return
                if self.path == "/api/preview-rebind":
                    request = RebindRequest.from_json(self._read_json())
                    self._send_json(transfer.preview_rebind(request))
                    return
                if self.path == "/api/rebind":
                    request = RebindRequest.from_json(self._read_json())
                    self._send_json(
                        transfer.public_rebind_result(transfer.rebind_threads(request))
                    )
                    return

                if self.path in {"/api/copy-progress", "/api/copy-package-progress"}:
                    request = CopyRequest.from_json(self._read_json())
                    self._stream_copy(
                        request,
                        package=self.path == "/api/copy-package-progress",
                    )
                    return

                request = CopyRequest.from_json(self._read_json())
                if self.path == "/api/preview-copy":
                    self._send_json(transfer.preview_copy(request))
                    return
                if self.path == "/api/copy":
                    self._send_json(transfer.public_copy_result(transfer.copy_threads(request)))
                    return
                if self.path == "/api/preview-package-copy":
                    self._send_json(transfer.preview_imported_package_copy(request))
                    return
                if self.path == "/api/copy-package":
                    self._send_json(
                        transfer.public_copy_result(transfer.copy_imported_package_threads(request))
                    )
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            except ValueError as exc:
                self._send_json({"ok": False, "errors": [str(exc)]}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json(
                    {"ok": False, "errors": [str(exc)]},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _stream_copy(self, request: CopyRequest, *, package: bool) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send_event(payload: dict[str, Any]) -> None:
                body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                chunk = f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    # A disconnected browser should not interrupt an already started copy.
                    pass

            try:
                result = (
                    transfer.copy_imported_package_threads(request, progress_callback=send_event)
                    if package
                    else transfer.copy_threads(request, progress_callback=send_event)
                )
            except Exception as exc:
                send_event(
                    {
                        "type": "progress",
                        "phase": "error",
                        "current": 0,
                        "total": max(len(request.thread_ids), 1),
                        "message": str(exc),
                    }
                )
                result = {"ok": False, "blocked": False, "errors": [str(exc)], "items": []}
            send_event({"type": "complete", "result": transfer.public_copy_result(result)})
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

        def _stream_rebind(self, request: RebindRequest) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def send_event(payload: dict[str, Any]) -> None:
                body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                chunk = f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    pass

            try:
                result = transfer.rebind_threads(request, progress_callback=send_event)
            except Exception as exc:
                send_event(
                    {
                        "type": "progress",
                        "phase": "error",
                        "current": 0,
                        "total": max(len(request.thread_ids), 1),
                        "message": str(exc),
                    }
                )
                result = {"ok": False, "blocked": False, "errors": [str(exc)], "items": []}
            send_event({"type": "complete", "result": transfer.public_rebind_result(result)})
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length)
            if not data:
                return {}
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def _read_binary(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)

        def _upload_filename(self, fallback: str) -> str:
            value = self.headers.get("X-Package-Filename") or fallback
            return unquote(value)

        def _has_action_header(self, action: str) -> bool:
            return self.headers.get("X-Codex-Session-Transfer-Action") == action

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TransferRequestHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex provider session transfer tool")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--instance-token", default="")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--sqlite-home", type=Path)
    args = parser.parse_args(argv)

    transfer = CodexSessionTransfer(codex_home=args.codex_home, sqlite_home=args.sqlite_home)
    static_dir = app_base_dir() / "static"
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(transfer, static_dir, instance_token=args.instance_token),
    )
    if args.parent_pid:
        threading.Thread(
            target=watch_parent_process,
            args=(args.parent_pid, server.shutdown),
            name="electron-parent-watchdog",
            daemon=True,
        ).start()
    url = f"http://{args.host}:{args.port}"
    print(f"Codex session transfer tool listening on {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
