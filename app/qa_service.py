"""Background orchestration for FlowScout console runs.

The service owns the Celesto sandbox lifecycle.  Every live run:

1. creates a fresh sandbox through the existing REST adapter;
2. launches the in-guest Playwright browser;
3. explores or executes saved tests;
4. always attempts cleanup in ``finally``;
5. records the provider-confirmed deletion on the run.

Live execution is fail-closed: a missing key, an explicit mock environment, or
a missing authorization flag never silently degrades to mock execution.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.evidence_io import atomic_write_json
from app.llm_planner import LLMPlanner, PlannerError
from app.qa_agent import generate_sandbox_name
from app.qa_store import QAStore, new_id, utcnow
from app.sandbox_runner import SandboxAdapter, create_adapter
from app.site_explorer import (
    GuestBrowserDriver,
    HostBrowserDriver,
    PublicSiteExplorer,
    RegressionRunner,
    validate_test_case,
)
from app.url_policy import allowlist_from_env, validate_public_url

logger = logging.getLogger("flowscout.service")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_ROOT = PROJECT_ROOT / "reports" / "console" / "runs"


class RunRequestError(ValueError):
    """The requested run is invalid or not allowed."""


class LiveRunBusy(RunRequestError):
    pass


class LiveNotConfigured(RunRequestError):
    pass


class LiveAuthorizationRequired(RunRequestError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _live_env_configured() -> bool:
    key = os.getenv("CELESTO_API_KEY", "").strip()
    mode = os.getenv("CELESTO_MODE", "mock").strip().lower()
    return bool(key and key != "replace-me" and mode == "live")


def _planner_env_configured() -> bool:
    values = [
        os.getenv("LLM_BASE_URL", "").strip(),
        os.getenv("LLM_API_KEY", "").strip(),
        os.getenv("LLM_MODEL", "").strip(),
    ]
    return all(value and value != "replace-me" for value in values)


def _visible_browser_default() -> bool:
    value = os.getenv("FLOWSCOUT_VISIBLE_BROWSER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _max_steps_default() -> int:
    try:
        return max(3, min(int(os.getenv("FLOWSCOUT_EXPLORE_STEPS", "8")), 12))
    except ValueError:
        return 8


def _test_signature(test: dict[str, Any]) -> tuple[str, str, str]:
    """Semantic identity used to avoid saving the same generated test twice."""
    return (
        _clean(test.get("name"), 160).lower(),
        str(test.get("target_url") or ""),
        json.dumps(test.get("actions") or [], sort_keys=True, default=str),
    )


class QARunManager:
    """Small thread-pool orchestrator with an explicit live-run gate."""

    def __init__(self, store: QAStore) -> None:
        self.store = store
        self._live_lock = threading.Lock()
        self._live_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="flowscout-live"
        )
        self._mock_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="flowscout-mock"
        )
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- config

    def config_summary(self) -> dict[str, Any]:
        protected = [
            item.strip()
            for item in os.getenv("CELESTO_KEEP_COMPUTER_IDS", "").split(",")
            if item.strip()
        ]
        return {
            "live_configured": _live_env_configured(),
            "planner_configured": _planner_env_configured(),
            "celesto_mode": os.getenv("CELESTO_MODE", "mock"),
            "celesto_size": os.getenv("CELESTO_SIZE_ID", "standard"),
            "celesto_template": os.getenv("CELESTO_TEMPLATE_ID", "browser-agent"),
            "celesto_image": os.getenv("CELESTO_IMAGE", "ubuntu-desktop-24.04"),
            "visible_browser": _visible_browser_default(),
            "store_backend": getattr(self.store, "backend", "unknown"),
            "protected_ids": protected,
            "allowlist": sorted(allowlist_from_env() or []),
            "default_max_steps": _max_steps_default(),
            "live_authorization_required": True,
        }

    # --------------------------------------------------------------- start

    def _validate_target(self, target_url: str, mode: str) -> str:
        raw = (target_url or "").strip()
        if not raw:
            raise RunRequestError("Enter a website URL")
        allow_private = mode == "mock"
        decision = validate_public_url(
            raw,
            allow_private=allow_private,
            allowed_hosts=allowlist_from_env(),
        )
        if not decision.allowed:
            raise RunRequestError(decision.reason)
        return decision.url

    def start_exploration(
        self,
        *,
        target_url: str,
        project_id: str | None = None,
        project_name: str | None = None,
        mode: str = "mock",
        authorize_live: bool = False,
        max_steps: int | None = None,
        use_ai: bool = True,
    ) -> dict[str, Any]:
        mode = (mode or "mock").strip().lower()
        if mode not in {"live", "mock"}:
            raise RunRequestError("mode must be 'live' or 'mock'")
        if mode == "live":
            if not authorize_live:
                raise LiveAuthorizationRequired(
                    "Live Celesto runs require explicit authorization (consumes credits)"
                )
            if not _live_env_configured():
                raise LiveNotConfigured(
                    "Live mode is not configured. Set CELESTO_MODE=live and a real "
                    "CELESTO_API_KEY, or choose mock mode."
                )
        target = self._validate_target(target_url, mode)
        project = (
            self.store.get_project(project_id)
            if project_id
            else self.store.get_or_create_project(target, project_name)
        )
        if project is None:
            raise RunRequestError("Project not found")
        if mode == "live" and not self._live_lock.acquire(blocking=False):
            raise LiveRunBusy("Another live Celesto run is already in progress")
        steps = max(3, min(int(max_steps or _max_steps_default()), 12))
        try:
            run = self.store.create_run(
                {
                    "run_id": new_id("run"),
                    "project_id": project["project_id"],
                    "kind": "exploration",
                    "mode": mode,
                    "target_url": target,
                    "status": "queued",
                    "phase": "queued",
                    "progress": 0,
                    "summary": {
                        "project_name": project["name"],
                        "max_steps": steps,
                        "use_ai": bool(use_ai),
                        "authorize_live": bool(authorize_live),
                    },
                }
            )
            executor = self._live_executor if mode == "live" else self._mock_executor
            executor.submit(
                self._run_exploration,
                run["run_id"],
                project["project_id"],
                target,
                mode,
                steps,
                bool(use_ai),
            )
        except Exception:
            if mode == "live":
                self._live_lock.release()
            raise
        return self.store.get_run(run["run_id"]) or run

    def start_regression(
        self,
        *,
        project_id: str,
        test_ids: list[str] | None = None,
        mode: str = "mock",
        authorize_live: bool = False,
    ) -> dict[str, Any]:
        mode = (mode or "mock").strip().lower()
        if mode not in {"live", "mock"}:
            raise RunRequestError("mode must be 'live' or 'mock'")
        if mode == "live":
            if not authorize_live:
                raise LiveAuthorizationRequired(
                    "Live Celesto runs require explicit authorization (consumes credits)"
                )
            if not _live_env_configured():
                raise LiveNotConfigured(
                    "Live mode is not configured. Set CELESTO_MODE=live and a real "
                    "CELESTO_API_KEY, or choose mock mode."
                )
        project = self.store.get_project(project_id)
        if project is None:
            raise RunRequestError("Project not found")
        all_tests = self.store.list_test_cases(project_id=project_id)
        if test_ids:
            wanted = set(test_ids)
            tests = [test for test in all_tests if test["test_id"] in wanted]
        else:
            tests = all_tests
        if not tests:
            raise RunRequestError("No saved test cases to run")
        validated: list[dict[str, Any]] = []
        for test in tests:
            try:
                validated.append(validate_test_case(test, allow_private=mode == "mock"))
            except Exception as exc:
                raise RunRequestError(f"Test {test.get('name')!r} is invalid: {exc}") from None
        if mode == "live" and not self._live_lock.acquire(blocking=False):
            raise LiveRunBusy("Another live Celesto run is already in progress")
        try:
            run = self.store.create_run(
                {
                    "run_id": new_id("run"),
                    "project_id": project_id,
                    "kind": "regression",
                    "mode": mode,
                    "target_url": project["target_url"],
                    "status": "queued",
                    "phase": "queued",
                    "progress": 0,
                    "summary": {
                        "project_name": project["name"],
                        "test_count": len(validated),
                        "authorize_live": bool(authorize_live),
                    },
                }
            )
            executor = self._live_executor if mode == "live" else self._mock_executor
            executor.submit(
                self._run_regression,
                run["run_id"],
                project_id,
                [test["test_id"] for test in validated],
                mode,
            )
        except Exception:
            if mode == "live":
                self._live_lock.release()
            raise
        return self.store.get_run(run["run_id"]) or run

    # ----------------------------------------------------------- execution

    def _evidence_dir(self, run_id: str) -> Path:
        path = EVIDENCE_ROOT / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _make_planner(self, use_ai: bool) -> Any:
        if not use_ai or not _planner_env_configured():
            return None
        try:
            return LLMPlanner()
        except PlannerError as exc:
            logger.warning("Planner requested but unavailable: %s", exc)
            return None

    def _make_driver(
        self,
        *,
        mode: str,
        adapter: SandboxAdapter,
        sandbox_name: str,
        target_url: str,
        evidence_dir: Path,
    ) -> HostBrowserDriver | GuestBrowserDriver:
        if mode == "live":
            return GuestBrowserDriver(
                adapter,  # type: ignore[arg-type]
                sandbox_name,
                target_url,
                evidence_dir,
                visible=_visible_browser_default(),
            )
        return HostBrowserDriver(target_url, evidence_dir, headless=True)

    def _run_exploration(
        self,
        run_id: str,
        project_id: str,
        target_url: str,
        mode: str,
        max_steps: int,
        use_ai: bool,
    ) -> None:
        started = time.monotonic()
        evidence_dir = self._evidence_dir(run_id)
        adapter: SandboxAdapter | None = None
        sandbox_name: str | None = None
        cleanup_ok: bool | None = None
        cleanup_message = ""
        result: dict[str, Any] | None = None
        error = ""
        self.store.update_run(
            run_id,
            status="running",
            phase="preparing",
            progress=2,
            started_at=utcnow(),
            logs=[{"ts": utcnow(), "level": "info", "message": "Run started"}],
        )

        def progress(
            phase: str,
            percent: int,
            message: str,
            partial: dict[str, Any] | None,
        ) -> None:
            fields: dict[str, Any] = {
                "status": "running",
                "phase": phase,
                "progress": percent,
            }
            if partial:
                for key in ("actions", "issues", "screenshots", "logs"):
                    if key in partial:
                        fields[key] = partial[key]
            try:
                self.store.update_run(run_id, **fields)
            except Exception as exc:
                logger.warning("Could not persist progress for %s: %s", run_id, exc)

        try:
            adapter = create_adapter(
                mode, command_log_path=evidence_dir / "commands.jsonl"
            )
            sandbox_name = generate_sandbox_name()
            self.store.update_run(
                run_id,
                sandbox_name=sandbox_name,
                phase="creating_sandbox",
                progress=5,
            )
            info = adapter.create(sandbox_name)
            self.store.update_run(
                run_id,
                provider_id=info.provider_id,
                phase="sandbox_ready",
                progress=18,
                logs=[
                    {"ts": utcnow(), "level": "info", "message": "Run started"},
                    {
                        "ts": utcnow(),
                        "level": "info",
                        "message": (
                            f"Sandbox {sandbox_name} ready "
                            f"({info.provider}, boot {info.boot_to_ready_s:.2f}s)"
                        ),
                    },
                ],
            )
            driver = self._make_driver(
                mode=mode,
                adapter=adapter,
                sandbox_name=sandbox_name,
                target_url=target_url,
                evidence_dir=evidence_dir,
            )
            planner = self._make_planner(use_ai)
            explorer = PublicSiteExplorer(
                driver=driver,
                target_url=target_url,
                evidence_dir=evidence_dir,
                run_id=run_id,
                planner=planner,
                max_steps=max_steps,
                progress=progress,
                allow_private=mode == "mock",
            )
            result = explorer.run()
            tests = result.get("tests") or []
            existing = {
                _test_signature(row): row
                for row in self.store.list_test_cases(project_id=project_id)
            }
            pending: list[dict[str, Any]] = []
            reused_ids: list[str] = []
            for test in tests:
                row = {
                    **test,
                    "project_id": project_id,
                    "created_at": test.get("created_at") or utcnow(),
                }
                signature = _test_signature(row)
                previous = existing.get(signature)
                if previous is not None:
                    reused_ids.append(previous["test_id"])
                    continue
                pending.append(row)
            created = self.store.create_test_cases(pending)
            test_ids = reused_ids + [row["test_id"] for row in created]
            summary = {
                **(result.get("summary") or {}),
                "project_name": (self.store.get_project(project_id) or {}).get("name", ""),
                "planner_mode": result.get("planner_mode"),
                "browser_runtime": result.get("browser_runtime"),
                "tests_reused": len(reused_ids),
                "sandbox": {
                    "name": sandbox_name,
                    "provider_id": info.provider_id,
                    "boot_to_ready_s": info.boot_to_ready_s,
                },
                "use_ai": bool(use_ai),
            }
            self.store.update_run(
                run_id,
                status="succeeded" if result.get("status") == "succeeded" else "failed",
                phase="completed" if result.get("status") == "succeeded" else "failed",
                progress=100,
                error=result.get("error") or "",
                summary=summary,
                issues=result.get("issues") or [],
                actions=result.get("actions") or [],
                logs=result.get("logs") or [],
                screenshots=result.get("screenshots") or [],
                generated_test_ids=test_ids,
                finished_at=utcnow(),
                duration_s=round(time.monotonic() - started, 2),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Exploration run %s failed", run_id)
            self.store.update_run(
                run_id,
                status="failed",
                phase="failed",
                progress=100,
                error=error,
                finished_at=utcnow(),
                duration_s=round(time.monotonic() - started, 2),
                logs=[
                    {"ts": utcnow(), "level": "error", "message": f"Run failed: {error}"}
                ],
            )
        finally:
            if adapter is not None and sandbox_name is not None:
                try:
                    self.store.update_run(run_id, phase="cleanup", progress=96)
                    cleanup = adapter.delete(sandbox_name)
                    cleanup_ok = cleanup.deleted
                    cleanup_message = cleanup.message
                except Exception as exc:
                    cleanup_ok = False
                    cleanup_message = f"cleanup failed: {type(exc).__name__}: {exc}"
                    logger.exception("Cleanup failed for %s", sandbox_name)
                try:
                    adapter.close()
                except Exception:
                    pass
            else:
                cleanup_message = "no sandbox was created"
            self._finalize_cleanup(run_id, cleanup_ok, cleanup_message)
            if mode == "live":
                try:
                    self._live_lock.release()
                except RuntimeError:
                    pass
            self._write_report(run_id, project_id)

    def _run_regression(
        self,
        run_id: str,
        project_id: str,
        test_ids: list[str],
        mode: str,
    ) -> None:
        started = time.monotonic()
        evidence_dir = self._evidence_dir(run_id)
        adapter: SandboxAdapter | None = None
        sandbox_name: str | None = None
        cleanup_ok: bool | None = None
        cleanup_message = ""
        result: dict[str, Any] | None = None
        self.store.update_run(
            run_id,
            status="running",
            phase="preparing",
            progress=2,
            started_at=utcnow(),
        )
        try:
            tests = [
                test
                for test in (self.store.get_test_case(test_id) for test_id in test_ids)
                if test is not None
            ]
            if not tests:
                raise RunRequestError("No saved test cases were found")
            adapter = create_adapter(
                mode, command_log_path=evidence_dir / "commands.jsonl"
            )
            sandbox_name = generate_sandbox_name()
            self.store.update_run(
                run_id,
                sandbox_name=sandbox_name,
                phase="creating_sandbox",
                progress=5,
            )
            info = adapter.create(sandbox_name)
            self.store.update_run(
                run_id,
                provider_id=info.provider_id,
                phase="sandbox_ready",
                progress=18,
            )
            driver = self._make_driver(
                mode=mode,
                adapter=adapter,
                sandbox_name=sandbox_name,
                target_url=(self.store.get_project(project_id) or {}).get(
                    "target_url", tests[0]["target_url"]
                ),
                evidence_dir=evidence_dir,
            )
            previous = self.store.previous_regression_results(project_id, run_id)

            def progress(
                phase: str,
                percent: int,
                message: str,
                partial: dict[str, Any] | None,
            ) -> None:
                fields: dict[str, Any] = {
                    "status": "running",
                    "phase": phase,
                    "progress": percent,
                }
                if partial:
                    if "logs" in partial:
                        fields["logs"] = partial["logs"]
                    if "test_results" in partial:
                        summary = (self.store.get_run(run_id) or {}).get("summary") or {}
                        summary = {
                            **summary,
                            "live_results": partial["test_results"],
                            "current_test": message,
                        }
                        fields["summary"] = summary
                try:
                    self.store.update_run(run_id, **fields)
                except Exception as exc:
                    logger.warning("Could not persist regression progress: %s", exc)

            runner = RegressionRunner(
                driver=driver,
                tests=tests,
                evidence_dir=evidence_dir,
                previous_results=previous,
                progress=progress,
                allow_private=mode == "mock",
            )
            result = runner.run()
            self.store.add_test_results(
                [{**row, "run_id": run_id} for row in result.get("results") or []]
            )
            failed = [row for row in result.get("results") or [] if row["status"] == "failed"]
            issues = []
            for row in failed:
                issues.append(
                    {
                        "issue_id": new_id("iss"),
                        "category": "regression_failure",
                        "title": f"Regression test failed: {row['test_name']}",
                        "description": row.get("error") or "The saved test did not meet its assertions.",
                        "severity": "high",
                        "url": next(
                            (test["target_url"] for test in tests if test["test_id"] == row["test_id"]),
                            "",
                        ),
                        "step": 0,
                        "evidence": row.get("screenshots") or [],
                        "detected_at": utcnow(),
                        "newly_failing": bool(row.get("newly_failing")),
                    }
                )
            summary = {
                **(result.get("summary") or {}),
                "project_name": (self.store.get_project(project_id) or {}).get("name", ""),
                "browser_runtime": result.get("browser_runtime"),
                "planner_mode": "none",
                "sandbox": {
                    "name": sandbox_name,
                    "provider_id": info.provider_id,
                    "boot_to_ready_s": info.boot_to_ready_s,
                },
                "test_ids": test_ids,
            }
            self.store.update_run(
                run_id,
                status="succeeded" if result.get("status") == "succeeded" else "failed",
                phase="completed" if result.get("status") == "succeeded" else "failed",
                progress=100,
                error=result.get("error") or "",
                summary=summary,
                issues=issues,
                logs=result.get("logs") or [],
                screenshots=[
                    shot
                    for row in result.get("results") or []
                    for shot in (row.get("screenshots") or [])
                ],
                generated_test_ids=test_ids,
                finished_at=utcnow(),
                duration_s=round(time.monotonic() - started, 2),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Regression run %s failed", run_id)
            self.store.update_run(
                run_id,
                status="failed",
                phase="failed",
                progress=100,
                error=error,
                finished_at=utcnow(),
                duration_s=round(time.monotonic() - started, 2),
                logs=[
                    {"ts": utcnow(), "level": "error", "message": f"Run failed: {error}"}
                ],
            )
        finally:
            if adapter is not None and sandbox_name is not None:
                try:
                    self.store.update_run(run_id, phase="cleanup", progress=96)
                    cleanup = adapter.delete(sandbox_name)
                    cleanup_ok = cleanup.deleted
                    cleanup_message = cleanup.message
                except Exception as exc:
                    cleanup_ok = False
                    cleanup_message = f"cleanup failed: {type(exc).__name__}: {exc}"
                    logger.exception("Cleanup failed for %s", sandbox_name)
                try:
                    adapter.close()
                except Exception:
                    pass
            else:
                cleanup_message = "no sandbox was created"
            self._finalize_cleanup(run_id, cleanup_ok, cleanup_message)
            if mode == "live":
                try:
                    self._live_lock.release()
                except RuntimeError:
                    pass
            self._write_report(run_id, project_id)

    # -------------------------------------------------------------- reports

    def _finalize_cleanup(
        self, run_id: str, cleanup_ok: bool | None, cleanup_message: str
    ) -> None:
        current = self.store.get_run(run_id) or {}
        status = current.get("status") or "failed"
        fields: dict[str, Any] = {
            "cleanup_ok": cleanup_ok,
            "cleanup_message": cleanup_message,
            "progress": 100,
            "finished_at": current.get("finished_at") or utcnow(),
        }
        if cleanup_ok is False:
            status = "failed"
            fields["status"] = "failed"
            fields["phase"] = "failed"
            fields["error"] = current.get("error") or (
                "The sandbox deletion was not confirmed by the provider."
            )
            issues = list(current.get("issues") or [])
            if not any(issue.get("category") == "cleanup_failed" for issue in issues):
                issues.append(
                    {
                        "issue_id": new_id("iss"),
                        "category": "cleanup_failed",
                        "title": "Sandbox cleanup was not confirmed",
                        "description": cleanup_message or "The provider did not confirm deletion.",
                        "severity": "high",
                        "url": current.get("target_url", ""),
                        "step": 0,
                        "evidence": [],
                        "detected_at": utcnow(),
                    }
                )
            fields["issues"] = issues
        else:
            fields["phase"] = "completed" if status == "succeeded" else "failed"
        self.store.update_run(run_id, **fields)

    def build_report(self, run_id: str) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        project = self.store.get_project(run["project_id"]) if run.get("project_id") else None
        results = self.store.list_test_results(run_id)
        return {
            "report_version": 1,
            "generated_at": utcnow(),
            "run": run,
            "project": project,
            "summary": run.get("summary") or {},
            "issues": run.get("issues") or [],
            "actions": run.get("actions") or [],
            "logs": run.get("logs") or [],
            "screenshots": run.get("screenshots") or [],
            "test_results": results,
        }

    def _write_report(self, run_id: str, project_id: str | None) -> None:
        try:
            report = self.build_report(run_id)
            if report is None:
                return
            atomic_write_json(self._evidence_dir(run_id) / "report.json", report)
        except Exception as exc:
            logger.warning("Could not write report for %s: %s", run_id, exc)

    def shutdown(self) -> None:
        self._live_executor.shutdown(wait=False, cancel_futures=True)
        self._mock_executor.shutdown(wait=False, cancel_futures=True)


def make_manager(store: QAStore) -> QARunManager:
    return QARunManager(store)
