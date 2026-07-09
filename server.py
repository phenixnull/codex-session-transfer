from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
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
BLOCKING_PROCESS_NAMES = {"codex", "codex-plus-plus", "codex-plus-plus-manager"}
DEFAULT_THREAD_DETAIL_LIMIT = 80
MAX_THREAD_DETAIL_LIMIT = 200
PACKAGE_FORMAT = "codex-session-transfer-package"
PACKAGE_VERSION = 1
PACKAGE_MANIFEST_NAME = "manifest.json"
PACKAGE_DB_PATH = f"sqlite/{STATE_DB_FILENAME}"
PACKAGE_SCHEMA_TABLES = ("threads", "thread_spawn_edges", "thread_dynamic_tools")
SKILL_PACKAGE_FORMAT = "codex-skill-transfer-package"
SKILL_PACKAGE_VERSION = 1
SKILL_PACKAGE_DIRNAME = "skills"
ROLLOUT_NAME_RE = re.compile(
    r"^(?P<prefix>rollout-.+-)"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?P<suffix>\.jsonl)$"
)
SECRET_PATTERNS = (
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
CODEX_DEFAULT_SOURCE_KINDS = {"cli", "vscode"}
DEFAULT_IMPORTED_SOURCE_KIND = "vscode"
DEFAULT_IMPORTED_THREAD_SOURCE = "user"

JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
ProcessChecker = Callable[[], list[dict[str, Any]]]
ProcessTerminator = Callable[[int], dict[str, Any]]
PathOpener = Callable[[Path], None]


@dataclass(frozen=True)
class CopyRequest:
    source_provider: str
    target_provider: str
    thread_ids: list[str]
    include_descendants: bool
    include_archived: bool
    cwd_map: dict[str, str] | None = None

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
        return cls(
            source_provider=str(data.get("source_provider", "")).strip(),
            target_provider=str(data.get("target_provider", "")).strip(),
            thread_ids=[str(thread_id).strip() for thread_id in thread_ids if str(thread_id).strip()],
            include_descendants=bool(data.get("include_descendants", False)),
            include_archived=bool(data.get("include_archived", False)),
            cwd_map=cwd_map,
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
        return result

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
                    "matches_current_base_url": False,
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
        current_base_url = current.get("base_url")
        if current_provider:
            upsert(
                str(current_provider),
                source="live_config",
                label=str(current.get("provider_name") or current_provider),
                current=True,
                provider_name=current.get("provider_name"),
                model=current.get("model"),
                base_url=current.get("base_url"),
                wire_api=current.get("wire_api"),
            )

        for provider in self._codex_plus_preset_providers():
            value = provider["value"]
            provider_fields = dict(provider)
            provider_fields.pop("value", None)
            upsert(value, **provider_fields)
            if current_base_url and provider.get("base_url") == current_base_url:
                merged[value]["matches_current_base_url"] = True

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
        return [self._thread_summary(conn, row, session_index) for row in rows]

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

    def preview_copy(self, request: CopyRequest) -> dict[str, Any]:
        with closing(self._connect(read_only=True)) as conn:
            plan = self._build_copy_plan(conn, request)
        return self._public_plan(plan)

    def copy_threads(self, request: CopyRequest) -> dict[str, Any]:
        blocking = self.blocking_processes()
        if blocking:
            payload = {
                "ok": False,
                "blocked": True,
                "errors": ["Close Codex and provider switcher processes before copying."],
                "blocking_processes": blocking,
            }
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        preflight = self.preview_copy(request)
        if not preflight["can_execute"]:
            payload = {"ok": False, "blocked": False, **preflight}
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_database()
        copied_paths: list[Path] = []
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
                    for item in plan["items"]:
                        self._write_rollout_copy(
                            Path(item["source_rollout_path"]),
                            Path(item["dest_rollout_path"]),
                            item["source_id"],
                            plan["_id_map"],
                            request.target_provider,
                            target_cwd=item.get("target_cwd") if item.get("cwd_rewritten") else None,
                        )
                        copied_paths.append(Path(item["dest_rollout_path"]))

                    self._insert_thread_rows(conn, plan, request.target_provider)
                    self._insert_spawn_edges(conn, plan)
                    self._insert_dynamic_tools(conn, plan)
                    session_index_snapshot = self._snapshot_session_index()
                    session_index_entries = self._append_session_index_entries(plan)
                    conn.commit()

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
                    try:
                        path.unlink()
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
                    allow_same_provider=True,
                )
        return self._public_plan(plan)

    def copy_imported_package_threads(self, request: CopyRequest) -> dict[str, Any]:
        package = self._require_loaded_package()
        blocking = self.blocking_processes()
        if blocking:
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

        preflight = self.preview_imported_package_copy(request)
        if not preflight["can_execute"]:
            payload = {"ok": False, "blocked": False, **preflight, "package_path": str(package.package_path)}
            manifest_path = self._write_manifest(payload, request)
            payload["manifest_path"] = str(manifest_path)
            return payload

        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = self._backup_database()
        copied_paths: list[Path] = []
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
                        allow_same_provider=True,
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
                        for item in plan["items"]:
                            self._write_rollout_copy(
                                Path(item["source_rollout_path"]),
                                Path(item["dest_rollout_path"]),
                                item["source_id"],
                                plan["_id_map"],
                                request.target_provider,
                                target_cwd=item.get("target_cwd") if item.get("cwd_rewritten") else None,
                                target_source=plan.get("_target_source_by_source_id", {}).get(
                                    item["source_id"]
                                ),
                            )
                            copied_paths.append(Path(item["dest_rollout_path"]))

                        self._insert_thread_rows(target_conn, plan, request.target_provider)
                        self._insert_spawn_edges_from_source(source_conn, target_conn, plan)
                        self._insert_dynamic_tools_from_source(source_conn, target_conn, plan)
                        session_index_snapshot = self._snapshot_session_index()
                        session_index_entries = self._append_session_index_entries(
                            plan,
                            source_index=source_index,
                            existing_index=self._load_session_index(),
                        )
                        target_conn.commit()

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
                        try:
                            path.unlink()
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

    def _normalize_windows_path(self, path: str) -> str:
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

    @staticmethod
    def _codex_default_visible_source(source: str) -> str:
        clean = str(source or "").strip()
        if clean in CODEX_DEFAULT_SOURCE_KINDS:
            return clean
        return DEFAULT_IMPORTED_SOURCE_KIND

    def _target_cwd_for_source(self, source_cwd: str, cwd_map: dict[str, str]) -> str:
        if not cwd_map:
            return source_cwd
        source_key = self._path_match_key(source_cwd)
        for mapped_source, mapped_target in cwd_map.items():
            if self._path_match_key(mapped_source) == source_key:
                return mapped_target
        return source_cwd

    def _project_label(self, cwd: str) -> str:
        normalized = cwd.rstrip("\\/")
        if not normalized:
            return cwd
        name = Path(normalized).name
        return name or normalized

    def _thread_summary(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        session_index: dict[str, dict[str, Any]],
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
        display_title = thread_name or row["title"] or row["preview"] or thread_id
        return {
            "id": thread_id,
            "title": row["title"],
            "thread_name": thread_name,
            "display_title": display_title,
            "preview": row["preview"],
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

    def _build_copy_plan(
        self,
        conn: sqlite3.Connection,
        request: CopyRequest,
        *,
        target_conn: sqlite3.Connection | None = None,
        source_index: dict[str, dict[str, Any]] | None = None,
        dest_path_resolver: Callable[[dict[str, Any], str], Path] | None = None,
        allow_same_provider: bool = False,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        session_index = source_index if source_index is not None else self._load_session_index()
        source_provider = request.source_provider.strip()
        target_provider = request.target_provider.strip()
        ordered_ids = self._dedupe(request.thread_ids)
        cwd_map = dict(request.cwd_map or {})

        if not source_provider:
            errors.append("source_provider is required")
        if not target_provider:
            errors.append("target_provider is required")
        if (
            source_provider
            and target_provider
            and source_provider == target_provider
            and not allow_same_provider
        ):
            errors.append("source_provider and target_provider must be different")
        if not ordered_ids:
            errors.append("Select at least one thread")
        if errors:
            return self._empty_plan(request, errors, warnings)

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

        id_map = self._new_id_map(target_conn or conn, final_ids)
        items = []
        cwd_by_source_id: dict[str, str] = {}
        for thread_id in final_ids:
            row = rows[thread_id]
            parent = self._parent_thread_id(conn, thread_id) or self._source_parent_id(row["source"])
            source_cwd = str(row["cwd"])
            target_cwd = self._target_cwd_for_source(source_cwd, cwd_map)
            cwd_by_source_id[thread_id] = target_cwd
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
            "source_provider": source_provider,
            "target_provider": target_provider,
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            "_ordered_ids": final_ids,
            "_rows": rows,
            "_id_map": id_map,
            "_cwd_map": cwd_map,
            "_cwd_by_source_id": cwd_by_source_id,
        }

    def _empty_plan(
        self, request: CopyRequest, errors: list[str], warnings: list[str]
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
            "_id_map": {},
            "_cwd_map": dict(request.cwd_map or {}),
            "_cwd_by_source_id": {},
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

    def _new_id_map(self, conn: sqlite3.Connection, thread_ids: list[str]) -> dict[str, str]:
        id_map: dict[str, str] = {}
        generated: set[str] = set()
        for thread_id in thread_ids:
            while True:
                candidate = str(uuid.uuid4())
                exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (candidate,)).fetchone()
                if not exists and candidate not in generated:
                    generated.add(candidate)
                    id_map[thread_id] = candidate
                    break
        return id_map

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
    ) -> None:
        if dest_path.exists():
            raise FileExistsError(f"Destination rollout already exists: {dest_path}")
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines:
            raise ValueError(f"Rollout is empty: {source_path}")
        first_line = json.loads(lines[0])
        payload = self._session_meta_payload(first_line)
        payload["id"] = id_map[source_id]
        payload["model_provider"] = target_provider
        if target_cwd:
            payload["cwd"] = target_cwd
        self._remap_optional_meta_id(payload, "parent_thread_id", id_map)
        self._remap_optional_meta_id(payload, "forked_from_id", id_map)
        if target_source:
            payload["source"] = target_source
        elif "source" in payload:
            payload["source"] = self._remap_source_value(payload["source"], id_map)

        lines[0] = compact_json(first_line) + "\n"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text("".join(lines), encoding="utf-8")

    def _session_meta_payload(self, rollout_line: dict[str, Any]) -> dict[str, Any]:
        item = rollout_line.get("item")
        if isinstance(item, dict) and item.get("type") == "session_meta":
            payload = item.get("payload")
            if isinstance(payload, dict):
                return payload
        if rollout_line.get("type") == "session_meta" and isinstance(
            rollout_line.get("payload"), dict
        ):
            return rollout_line["payload"]
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
        entries: list[dict[str, Any]] = []
        for source_id in plan["_ordered_ids"]:
            target_id = plan["_id_map"][source_id]
            if target_id in existing_ids:
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

        self._write_session_index_entries(entries)
        return entries

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

    def _backup_database(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.manifest_dir / f"state_5.before-copy-{timestamp}.sqlite"
        counter = 1
        while backup_path.exists():
            backup_path = self.manifest_dir / f"state_5.before-copy-{timestamp}-{counter}.sqlite"
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

    def _write_manifest(self, payload: dict[str, Any], request: CopyRequest) -> Path:
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = self.manifest_dir / f"copy-{timestamp}.json"
        counter = 1
        while manifest_path.exists():
            manifest_path = self.manifest_dir / f"copy-{timestamp}-{counter}.json"
            counter += 1
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "source_provider": request.source_provider,
            "target_provider": request.target_provider,
            "source_sqlite_home": str(self.sqlite_home),
            "state_db_path": str(self.db_path),
            "include_descendants": request.include_descendants,
            "include_archived": request.include_archived,
            **self._manifest_safe_payload(payload),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path

    def _public_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in plan.items() if not key.startswith("_")}

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


def make_handler(transfer: CodexSessionTransfer, static_dir: Path) -> type[SimpleHTTPRequestHandler]:
    class TransferRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(static_dir), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
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

                request = CopyRequest.from_json(self._read_json())
                if self.path == "/api/preview-copy":
                    self._send_json(transfer.preview_copy(request))
                    return
                if self.path == "/api/copy":
                    self._send_json(transfer.copy_threads(request))
                    return
                if self.path == "/api/preview-package-copy":
                    self._send_json(transfer.preview_imported_package_copy(request))
                    return
                if self.path == "/api/copy-package":
                    self._send_json(transfer.copy_imported_package_threads(request))
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            except ValueError as exc:
                self._send_json({"ok": False, "errors": [str(exc)]}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json(
                    {"ok": False, "errors": [str(exc)]},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

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
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--sqlite-home", type=Path)
    args = parser.parse_args(argv)

    transfer = CodexSessionTransfer(codex_home=args.codex_home, sqlite_home=args.sqlite_home)
    static_dir = app_base_dir() / "static"
    server = ThreadingHTTPServer((args.host, args.port), make_handler(transfer, static_dir))
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
