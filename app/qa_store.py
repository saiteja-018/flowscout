"""Persistence for the FlowScout QA console.

Two explicit backends are supported:

* ``postgres`` - the production store.  The schema is additive and lives
  beside the existing ``runs`` ledger.
* ``local`` - a file-backed development store.  It persists *real* execution
  data; it is never used to fabricate a run.  The UI labels the backend so an
  operator can tell the difference.

Selection is explicit through ``FLOWSCOUT_CONSOLE_STORE``.  ``auto`` tries
PostgreSQL first and falls back to the local store only when the database is
unreachable, recording the chosen backend for the Settings page.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.evidence_io import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCAL_PATH = PROJECT_ROOT / "reports" / "console" / "store.json"

RUN_COLUMNS = (
    "run_id",
    "project_id",
    "kind",
    "mode",
    "target_url",
    "status",
    "phase",
    "progress",
    "sandbox_name",
    "provider_id",
    "cleanup_ok",
    "cleanup_message",
    "error",
    "summary",
    "issues",
    "actions",
    "logs",
    "screenshots",
    "generated_test_ids",
    "started_at",
    "finished_at",
    "duration_s",
    "created_at",
    "updated_at",
)
JSON_RUN_FIELDS = {
    "summary",
    "issues",
    "actions",
    "logs",
    "screenshots",
    "generated_test_ids",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qa_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    target_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES qa_projects(project_id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('exploration','regression','demo')),
    mode TEXT NOT NULL CHECK (mode IN ('live','mock')),
    target_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    phase TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    sandbox_name TEXT,
    provider_id TEXT,
    cleanup_ok BOOLEAN,
    cleanup_message TEXT,
    error TEXT,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    issues JSONB NOT NULL DEFAULT '[]'::jsonb,
    actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    logs JSONB NOT NULL DEFAULT '[]'::jsonb,
    screenshots JSONB NOT NULL DEFAULT '[]'::jsonb,
    generated_test_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    duration_s DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_test_cases (
    test_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES qa_projects(project_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL,
    actions JSONB NOT NULL,
    expected_results JSONB NOT NULL DEFAULT '[]'::jsonb,
    assertions JSONB NOT NULL DEFAULT '[]'::jsonb,
    source TEXT NOT NULL DEFAULT 'exploration',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_test_results (
    result_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES qa_runs(run_id) ON DELETE CASCADE,
    test_id TEXT NOT NULL,
    test_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed','failed','skipped')),
    duration_s DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT,
    actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    assertions JSONB NOT NULL DEFAULT '[]'::jsonb,
    screenshots JSONB NOT NULL DEFAULT '[]'::jsonb,
    logs JSONB NOT NULL DEFAULT '[]'::jsonb,
    previous_status TEXT,
    newly_failing BOOLEAN NOT NULL DEFAULT FALSE,
    fixed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS qa_runs_project_idx ON qa_runs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS qa_test_cases_project_idx ON qa_test_cases (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS qa_test_results_run_idx ON qa_test_results (run_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _normalize_project(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("created_at", "updated_at"):
        result[key] = _iso(result.get(key))
    return result


def _normalize_run(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("created_at", "updated_at", "started_at", "finished_at"):
        result[key] = _iso(result.get(key))
    for key in JSON_RUN_FIELDS:
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = [] if key != "summary" else {}
        if value is None:
            result[key] = {} if key == "summary" else []
    if result.get("progress") is None:
        result["progress"] = 0
    return result


def _normalize_test(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["created_at"] = _iso(result.get("created_at"))
    for key in ("actions", "expected_results", "assertions", "tags"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = []
        if value is None:
            result[key] = []
    return result


def _normalize_result(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["created_at"] = _iso(result.get("created_at"))
    for key in ("actions", "assertions", "screenshots", "logs"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = []
        if value is None:
            result[key] = []
    return result


class QAStore(Protocol):
    backend: str

    def ensure_schema(self) -> None: ...
    def list_projects(self) -> list[dict[str, Any]]: ...
    def get_project(self, project_id: str) -> dict[str, Any] | None: ...
    def create_project(self, name: str, target_url: str) -> dict[str, Any]: ...
    def get_or_create_project(self, target_url: str, name: str | None = None) -> dict[str, Any]: ...
    def list_runs(self, *, limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def create_run(self, record: dict[str, Any]) -> dict[str, Any]: ...
    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None: ...
    def list_test_cases(self, *, project_id: str | None = None) -> list[dict[str, Any]]: ...
    def get_test_case(self, test_id: str) -> dict[str, Any] | None: ...
    def create_test_cases(self, tests: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def delete_test_case(self, test_id: str) -> bool: ...
    def add_test_results(self, results: list[dict[str, Any]]) -> None: ...
    def list_test_results(self, run_id: str) -> list[dict[str, Any]]: ...
    def previous_regression_results(self, project_id: str, before_run_id: str) -> dict[str, dict[str, Any]]: ...
    def stats(self) -> dict[str, Any]: ...


class BaseQAStore:
    backend = "base"

    def get_or_create_project(self, target_url: str, name: str | None = None) -> dict[str, Any]:
        for project in self.list_projects():
            if project["target_url"].rstrip("/") == target_url.rstrip("/"):
                return project
        return self.create_project(name or target_url, target_url)


class LocalQAStore(BaseQAStore):
    """A single-file, atomically-written development store."""

    backend = "local"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.getenv("FLOWSCOUT_CONSOLE_STORE_PATH", DEFAULT_LOCAL_PATH))
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "projects": [],
            "runs": [],
            "test_cases": [],
            "test_results": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for key in self._data:
            value = payload.get(key)
            if isinstance(value, list):
                self._data[key] = value

    def _save(self) -> None:
        atomic_write_json(self.path, self._data)

    def ensure_schema(self) -> None:
        with self._lock:
            self._save()

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                _normalize_project(row)
                for row in sorted(self._data["projects"], key=lambda row: row["created_at"])
            ]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._data["projects"]:
                if row["project_id"] == project_id:
                    return _normalize_project(row)
        return None

    def create_project(self, name: str, target_url: str) -> dict[str, Any]:
        now = utcnow()
        row = {
            "project_id": new_id("prj"),
            "name": name.strip()[:120] or target_url,
            "target_url": target_url,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._data["projects"].append(row)
            self._save()
        return _normalize_project(row)

    def list_runs(self, *, limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                _normalize_run(row)
                for row in self._data["runs"]
                if project_id is None or row.get("project_id") == project_id
            ]
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[: max(1, min(limit, 500))]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._data["runs"]:
                if row["run_id"] == run_id:
                    return _normalize_run(row)
        return None

    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        row = {
            "run_id": record.get("run_id") or new_id("run"),
            "project_id": record.get("project_id"),
            "kind": record.get("kind", "exploration"),
            "mode": record.get("mode", "mock"),
            "target_url": record.get("target_url", ""),
            "status": record.get("status", "queued"),
            "phase": record.get("phase", "queued"),
            "progress": int(record.get("progress", 0)),
            "sandbox_name": record.get("sandbox_name"),
            "provider_id": record.get("provider_id"),
            "cleanup_ok": record.get("cleanup_ok"),
            "cleanup_message": record.get("cleanup_message"),
            "error": record.get("error"),
            "summary": record.get("summary") or {},
            "issues": record.get("issues") or [],
            "actions": record.get("actions") or [],
            "logs": record.get("logs") or [],
            "screenshots": record.get("screenshots") or [],
            "generated_test_ids": record.get("generated_test_ids") or [],
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "duration_s": record.get("duration_s"),
            "created_at": record.get("created_at") or now,
            "updated_at": now,
        }
        with self._lock:
            if any(existing["run_id"] == row["run_id"] for existing in self._data["runs"]):
                raise ValueError(f"run {row['run_id']} already exists")
            self._data["runs"].append(row)
            self._save()
        return _normalize_run(row)

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {key: value for key, value in fields.items() if key in RUN_COLUMNS and key != "run_id"}
        allowed["updated_at"] = utcnow()
        with self._lock:
            for row in self._data["runs"]:
                if row["run_id"] == run_id:
                    row.update(allowed)
                    self._save()
                    return _normalize_run(row)
        return None

    def list_test_cases(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                _normalize_test(row)
                for row in self._data["test_cases"]
                if project_id is None or row.get("project_id") == project_id
            ]
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows

    def get_test_case(self, test_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._data["test_cases"]:
                if row["test_id"] == test_id:
                    return _normalize_test(row)
        return None

    def create_test_cases(self, tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        with self._lock:
            for test in tests:
                row = {
                    "test_id": test.get("test_id") or new_id("tc"),
                    "project_id": test.get("project_id"),
                    "name": test["name"],
                    "description": test.get("description", ""),
                    "target_url": test["target_url"],
                    "actions": test.get("actions") or [],
                    "expected_results": test.get("expected_results") or [],
                    "assertions": test.get("assertions") or [],
                    "source": test.get("source", "exploration"),
                    "tags": test.get("tags") or [],
                    "created_at": test.get("created_at") or utcnow(),
                }
                if any(existing["test_id"] == row["test_id"] for existing in self._data["test_cases"]):
                    continue
                self._data["test_cases"].append(row)
                created.append(_normalize_test(row))
            if created:
                self._save()
        return created

    def delete_test_case(self, test_id: str) -> bool:
        with self._lock:
            before = len(self._data["test_cases"])
            self._data["test_cases"] = [
                row for row in self._data["test_cases"] if row["test_id"] != test_id
            ]
            changed = len(self._data["test_cases"]) != before
            if changed:
                self._save()
            return changed

    def add_test_results(self, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        with self._lock:
            for result in results:
                row = {
                    "result_id": result.get("result_id") or new_id("tr"),
                    "run_id": result["run_id"],
                    "test_id": result["test_id"],
                    "test_name": result["test_name"],
                    "status": result["status"],
                    "duration_s": float(result.get("duration_s") or 0),
                    "error": result.get("error"),
                    "actions": result.get("actions") or [],
                    "assertions": result.get("assertions") or [],
                    "screenshots": result.get("screenshots") or [],
                    "logs": result.get("logs") or [],
                    "previous_status": result.get("previous_status"),
                    "newly_failing": bool(result.get("newly_failing")),
                    "fixed": bool(result.get("fixed")),
                    "created_at": result.get("created_at") or utcnow(),
                }
                self._data["test_results"].append(row)
            self._save()

    def list_test_results(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                _normalize_result(row)
                for row in self._data["test_results"]
                if row["run_id"] == run_id
            ]
        rows.sort(key=lambda row: row.get("created_at") or "")
        return rows

    def previous_regression_results(
        self, project_id: str, before_run_id: str
    ) -> dict[str, dict[str, Any]]:
        with self._lock:
            runs = [
                row
                for row in self._data["runs"]
                if row.get("project_id") == project_id
                and row.get("kind") == "regression"
                and row.get("status") == "succeeded"
                and row["run_id"] != before_run_id
            ]
        runs.sort(key=lambda row: row.get("finished_at") or row.get("created_at") or "", reverse=True)
        if not runs:
            return {}
        previous_run = runs[0]
        return {row["test_id"]: row for row in self.list_test_results(previous_run["run_id"])}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            runs = [_normalize_run(row) for row in self._data["runs"]]
            tests = [_normalize_test(row) for row in self._data["test_cases"]]
            results = [_normalize_result(row) for row in self._data["test_results"]]
        completed = [run for run in runs if run["status"] in {"succeeded", "failed"}]
        passed_runs = [run for run in completed if run["status"] == "succeeded"]
        issue_keys = {
            (
                str(issue.get("category") or ""),
                str(issue.get("title") or ""),
                str(issue.get("url") or ""),
                str(issue.get("description") or ""),
            )
            for run in runs
            for issue in (run.get("issues") or [])
        }
        return {
            "backend": self.backend,
            "projects": len(self._data["projects"]),
            "runs": len(runs),
            "completed_runs": len(completed),
            "successful_runs": len(passed_runs),
            "running_runs": len([run for run in runs if run["status"] in {"queued", "running"}]),
            "test_cases": len(tests),
            "test_results": len(results),
            "passed_results": len([row for row in results if row["status"] == "passed"]),
            "failed_results": len([row for row in results if row["status"] == "failed"]),
            "issues": len(issue_keys),
            "newly_failing": len([row for row in results if row.get("newly_failing")]),
        }


class MemoryQAStore(LocalQAStore):
    backend = "memory"

    def __init__(self) -> None:
        self.path = Path(os.devnull)
        self._lock = threading.RLock()
        self._data = {"projects": [], "runs": [], "test_cases": [], "test_results": []}

    def _load(self) -> None:
        return

    def _save(self) -> None:
        return


class PostgresQAStore(BaseQAStore):
    backend = "postgres"

    def __init__(self, database_url: str, *, connect_timeout: int = 3) -> None:
        self.database_url = database_url
        self.connect_timeout = connect_timeout
        self._schema_checked = False

    def _connect(self):
        import psycopg2

        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        conn = psycopg2.connect(self.database_url, connect_timeout=self.connect_timeout)
        conn.autocommit = False
        return conn

    def ensure_schema(self) -> None:
        if self._schema_checked:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
            self._schema_checked = True
        finally:
            conn.close()

    def list_projects(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM qa_projects ORDER BY created_at")
                return [_normalize_project(dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM qa_projects WHERE project_id = %s", (project_id,))
                row = cur.fetchone()
                return _normalize_project(dict(row)) if row else None
        finally:
            conn.close()

    def create_project(self, name: str, target_url: str) -> dict[str, Any]:
        self.ensure_schema()
        now = utcnow()
        row = {
            "project_id": new_id("prj"),
            "name": name.strip()[:120] or target_url,
            "target_url": target_url,
            "created_at": now,
            "updated_at": now,
        }
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO qa_projects (project_id, name, target_url, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (row["project_id"], row["name"], row["target_url"], now, now),
                )
            conn.commit()
            return _normalize_project(row)
        finally:
            conn.close()

    def list_runs(self, *, limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if project_id:
                    cur.execute(
                        "SELECT * FROM qa_runs WHERE project_id = %s ORDER BY created_at DESC LIMIT %s",
                        (project_id, max(1, min(limit, 500))),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM qa_runs ORDER BY created_at DESC LIMIT %s",
                        (max(1, min(limit, 500)),),
                    )
                return [_normalize_run(dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM qa_runs WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                return _normalize_run(dict(row)) if row else None
        finally:
            conn.close()

    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        now = utcnow()
        row = {
            "run_id": record.get("run_id") or new_id("run"),
            "project_id": record.get("project_id"),
            "kind": record.get("kind", "exploration"),
            "mode": record.get("mode", "mock"),
            "target_url": record.get("target_url", ""),
            "status": record.get("status", "queued"),
            "phase": record.get("phase", "queued"),
            "progress": int(record.get("progress", 0)),
            "sandbox_name": record.get("sandbox_name"),
            "provider_id": record.get("provider_id"),
            "cleanup_ok": record.get("cleanup_ok"),
            "cleanup_message": record.get("cleanup_message"),
            "error": record.get("error"),
            "summary": record.get("summary") or {},
            "issues": record.get("issues") or [],
            "actions": record.get("actions") or [],
            "logs": record.get("logs") or [],
            "screenshots": record.get("screenshots") or [],
            "generated_test_ids": record.get("generated_test_ids") or [],
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "duration_s": record.get("duration_s"),
            "created_at": record.get("created_at") or now,
            "updated_at": now,
        }
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO qa_runs (
                        run_id, project_id, kind, mode, target_url, status, phase,
                        progress, sandbox_name, provider_id, cleanup_ok,
                        cleanup_message, error, summary, issues, actions, logs,
                        screenshots, generated_test_ids, started_at, finished_at,
                        duration_s, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        row["run_id"], row["project_id"], row["kind"], row["mode"],
                        row["target_url"], row["status"], row["phase"], row["progress"],
                        row["sandbox_name"], row["provider_id"], row["cleanup_ok"],
                        row["cleanup_message"], row["error"],
                        psycopg2.extras.Json(row["summary"]),
                        psycopg2.extras.Json(row["issues"]),
                        psycopg2.extras.Json(row["actions"]),
                        psycopg2.extras.Json(row["logs"]),
                        psycopg2.extras.Json(row["screenshots"]),
                        psycopg2.extras.Json(row["generated_test_ids"]),
                        row["started_at"], row["finished_at"], row["duration_s"],
                        row["created_at"], row["updated_at"],
                    ),
                )
            conn.commit()
            return _normalize_run(row)
        finally:
            conn.close()

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        self.ensure_schema()
        import psycopg2.extras

        allowed = {key: value for key, value in fields.items() if key in RUN_COLUMNS and key != "run_id"}
        allowed["updated_at"] = utcnow()
        if not allowed:
            return self.get_run(run_id)
        assignments = []
        values: list[Any] = []
        for key, value in allowed.items():
            assignments.append(f"{key} = %s")
            if key in JSON_RUN_FIELDS:
                values.append(psycopg2.extras.Json(value if value is not None else ({} if key == "summary" else [])))
            else:
                values.append(value)
        values.append(run_id)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE qa_runs SET {', '.join(assignments)} WHERE run_id = %s",
                    values,
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_run(run_id)

    def list_test_cases(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if project_id:
                    cur.execute(
                        "SELECT * FROM qa_test_cases WHERE project_id = %s ORDER BY created_at DESC",
                        (project_id,),
                    )
                else:
                    cur.execute("SELECT * FROM qa_test_cases ORDER BY created_at DESC")
                return [_normalize_test(dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_test_case(self, test_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM qa_test_cases WHERE test_id = %s", (test_id,))
                row = cur.fetchone()
                return _normalize_test(dict(row)) if row else None
        finally:
            conn.close()

    def create_test_cases(self, tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        created: list[dict[str, Any]] = []
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                for test in tests:
                    row = {
                        "test_id": test.get("test_id") or new_id("tc"),
                        "project_id": test.get("project_id"),
                        "name": test["name"],
                        "description": test.get("description", ""),
                        "target_url": test["target_url"],
                        "actions": test.get("actions") or [],
                        "expected_results": test.get("expected_results") or [],
                        "assertions": test.get("assertions") or [],
                        "source": test.get("source", "exploration"),
                        "tags": test.get("tags") or [],
                        "created_at": test.get("created_at") or utcnow(),
                    }
                    cur.execute(
                        """
                        INSERT INTO qa_test_cases (
                            test_id, project_id, name, description, target_url, actions,
                            expected_results, assertions, source, tags, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (test_id) DO NOTHING
                        """,
                        (
                            row["test_id"], row["project_id"], row["name"],
                            row["description"], row["target_url"],
                            psycopg2.extras.Json(row["actions"]),
                            psycopg2.extras.Json(row["expected_results"]),
                            psycopg2.extras.Json(row["assertions"]),
                            row["source"], psycopg2.extras.Json(row["tags"]),
                            row["created_at"],
                        ),
                    )
                    created.append(_normalize_test(row))
            conn.commit()
            return created
        finally:
            conn.close()

    def delete_test_case(self, test_id: str) -> bool:
        self.ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM qa_test_cases WHERE test_id = %s", (test_id,))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        finally:
            conn.close()

    def add_test_results(self, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                for result in results:
                    cur.execute(
                        """
                        INSERT INTO qa_test_results (
                            result_id, run_id, test_id, test_name, status, duration_s,
                            error, actions, assertions, screenshots, logs,
                            previous_status, newly_failing, fixed, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            result.get("result_id") or new_id("tr"),
                            result["run_id"], result["test_id"], result["test_name"],
                            result["status"], float(result.get("duration_s") or 0),
                            result.get("error"),
                            psycopg2.extras.Json(result.get("actions") or []),
                            psycopg2.extras.Json(result.get("assertions") or []),
                            psycopg2.extras.Json(result.get("screenshots") or []),
                            psycopg2.extras.Json(result.get("logs") or []),
                            result.get("previous_status"),
                            bool(result.get("newly_failing")),
                            bool(result.get("fixed")),
                            result.get("created_at") or utcnow(),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def list_test_results(self, run_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM qa_test_results WHERE run_id = %s ORDER BY created_at",
                    (run_id,),
                )
                return [_normalize_result(dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def previous_regression_results(
        self, project_id: str, before_run_id: str
    ) -> dict[str, dict[str, Any]]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT run_id FROM qa_runs WHERE project_id = %s AND kind = 'regression' "
                    "AND status = 'succeeded' AND run_id <> %s "
                    "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
                    (project_id, before_run_id),
                )
                row = cur.fetchone()
                if not row:
                    return {}
                cur.execute(
                    "SELECT * FROM qa_test_results WHERE run_id = %s",
                    (row["run_id"],),
                )
                return {
                    result["test_id"]: _normalize_result(dict(result))
                    for result in cur.fetchall()
                }
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        self.ensure_schema()
        import psycopg2.extras

        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS projects FROM qa_projects"
                )
                projects = int(cur.fetchone()["projects"])
                cur.execute(
                    "SELECT COUNT(*) AS runs, "
                    "COUNT(*) FILTER (WHERE status IN ('succeeded','failed')) AS completed_runs, "
                    "COUNT(*) FILTER (WHERE status = 'succeeded') AS successful_runs, "
                    "COUNT(*) FILTER (WHERE status IN ('queued','running')) AS running_runs "
                    "FROM qa_runs"
                )
                runs = dict(cur.fetchone())
                cur.execute("SELECT COUNT(*) AS test_cases FROM qa_test_cases")
                test_cases = int(cur.fetchone()["test_cases"])
                cur.execute(
                    "SELECT COUNT(*) AS test_results, "
                    "COUNT(*) FILTER (WHERE status = 'passed') AS passed_results, "
                    "COUNT(*) FILTER (WHERE status = 'failed') AS failed_results, "
                    "COUNT(*) FILTER (WHERE newly_failing) AS newly_failing "
                    "FROM qa_test_results"
                )
                results = dict(cur.fetchone())
                cur.execute(
                    "SELECT COUNT(DISTINCT ("
                    "COALESCE(issue->>'category', ''), "
                    "COALESCE(issue->>'title', ''), "
                    "COALESCE(issue->>'url', ''), "
                    "COALESCE(issue->>'description', '')"
                    ")) AS issues "
                    "FROM qa_runs, jsonb_array_elements(issues) AS issue"
                )
                issues = int(cur.fetchone()["issues"])
                return {
                    "backend": self.backend,
                    "projects": projects,
                    "runs": int(runs["runs"]),
                    "completed_runs": int(runs["completed_runs"]),
                    "successful_runs": int(runs["successful_runs"]),
                    "running_runs": int(runs["running_runs"]),
                    "test_cases": test_cases,
                    "test_results": int(results["test_results"]),
                    "passed_results": int(results["passed_results"]),
                    "failed_results": int(results["failed_results"]),
                    "issues": issues,
                    "newly_failing": int(results["newly_failing"]),
                }
        finally:
            conn.close()


def create_qa_store() -> QAStore:
    """Select a store without ever pretending an unavailable DB was used."""
    requested = os.getenv("FLOWSCOUT_CONSOLE_STORE", "auto").strip().lower()
    database_url = os.getenv("DATABASE_URL", "")
    connect_timeout = int(os.getenv("DATABASE_CONNECT_TIMEOUT", "3"))
    if requested in {"memory", "test"}:
        return MemoryQAStore()
    if requested == "local":
        store = LocalQAStore()
        store.ensure_schema()
        return store
    if requested == "postgres":
        store = PostgresQAStore(database_url, connect_timeout=connect_timeout)
        store.ensure_schema()
        return store
    if requested != "auto":
        raise ValueError(
            "FLOWSCOUT_CONSOLE_STORE must be auto, postgres, local, or memory"
        )
    if database_url:
        try:
            store = PostgresQAStore(database_url, connect_timeout=connect_timeout)
            store.ensure_schema()
            return store
        except Exception:
            pass
    store = LocalQAStore()
    store.ensure_schema()
    return store
