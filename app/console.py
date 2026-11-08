"""FlowScout QA console: server-rendered pages and a small JSON API.

The console is the product surface of FlowScout.  It reads only from the QA
store (PostgreSQL in production, an explicitly labelled local file store in
development) and from real run evidence.  There is no seeded demo data here:
an empty dashboard means no run has happened yet.

Routes are intentionally split into:

* HTML pages under ``/console``;
* JSON endpoints under ``/console/api`` used by the page scripts;
* a constrained evidence reader for screenshots and downloaded reports.

Live runs remain gated by :class:`app.qa_service.QARunManager`; this module
never creates a sandbox itself.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.qa_service import (
    EVIDENCE_ROOT,
    LiveAuthorizationRequired,
    LiveNotConfigured,
    LiveRunBusy,
    QARunManager,
    RunRequestError,
    make_manager,
)
from app.qa_store import QAStore, create_qa_store, utcnow

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
router = APIRouter(prefix="/console")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_EVIDENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_store_lock = threading.Lock()
_manager_lock = threading.Lock()


# ----------------------------------------------------------------- formatting


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date(value: Any, fmt: str = "%b %d, %Y %H:%M") -> str:
    parsed = _parse_ts(value)
    return parsed.strftime(fmt) if parsed else "—"


def _ago(value: Any) -> str:
    parsed = _parse_ts(value)
    if parsed is None:
        return "—"
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _duration(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number < 1:
        return f"{number * 1000:.0f} ms"
    if number < 60:
        return f"{number:.1f} s"
    return f"{number // 60:.0f}m {number % 60:.0f}s"


def _percent(value: Any) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "0%"


def _title(value: Any) -> str:
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown"


def _short(value: Any, length: int = 10) -> str:
    text = str(value or "")
    return text if len(text) <= length else f"{text[:length]}…"


def _status_class(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"succeeded", "passed", "completed"}:
        return "is-good"
    if text in {"failed", "cancelled"}:
        return "is-bad"
    if text in {"running", "queued", "cleanup"}:
        return "is-live"
    return "is-muted"


def _severity_class(value: Any) -> str:
    text = str(value or "").lower()
    return {"high": "is-bad", "medium": "is-warn", "low": "is-muted"}.get(text, "is-muted")


def _json(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}\n…"


templates.env.filters["dt"] = _date
templates.env.filters["ago"] = _ago
templates.env.filters["duration"] = _duration
templates.env.filters["percent"] = _percent
templates.env.filters["titlecase"] = _title
templates.env.filters["short"] = _short
templates.env.filters["status_class"] = _status_class
templates.env.filters["severity_class"] = _severity_class
templates.env.filters["prettyjson"] = _json
templates.env.filters["urlencode"] = lambda value: quote(str(value or ""), safe="")
templates.env.globals["now"] = utcnow


# ------------------------------------------------------------------- app state


def get_store(request: Request) -> QAStore:
    store = getattr(request.app.state, "qa_store", None)
    if store is not None:
        return store
    with _store_lock:
        store = getattr(request.app.state, "qa_store", None)
        if store is None:
            store = create_qa_store()
            request.app.state.qa_store = store
    return store


def get_manager(request: Request) -> QARunManager:
    manager = getattr(request.app.state, "qa_manager", None)
    if manager is not None:
        return manager
    with _manager_lock:
        manager = getattr(request.app.state, "qa_manager", None)
        if manager is None:
            manager = make_manager(get_store(request))
            request.app.state.qa_manager = manager
    return manager


# --------------------------------------------------------------------- helpers


def _project_map(store: QAStore) -> dict[str, dict[str, Any]]:
    return {row["project_id"]: row for row in store.list_projects()}


def _counts_by_project(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        project_id = str(row.get("project_id") or "")
        if project_id:
            counts[project_id] = counts.get(project_id, 0) + 1
    return counts


def _evidence_exists(run_id: str, name: str) -> bool:
    try:
        _safe_evidence_path(run_id, str(name))
        return True
    except HTTPException:
        return False


def _annotate_results(
    run_id: str, results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    missing = 0
    for result in results:
        row = dict(result)
        names = [str(name) for name in result.get("screenshots") or []]
        existing = [name for name in names if _evidence_exists(run_id, name)]
        missing += len(names) - len(existing)
        row["screenshots"] = existing
        rows.append(row)
    return rows, missing


def _screenshots(
    run: dict[str, Any], results: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing = 0
    run_id = str(run["run_id"])
    for shot in run.get("screenshots") or []:
        if isinstance(shot, str):
            shot = {"name": shot}
        name = str(shot.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        if not _evidence_exists(run_id, name):
            missing += 1
            continue
        rows.append(
            {
                "name": name,
                "caption": shot.get("caption") or "Run evidence",
                "step": shot.get("step"),
                "kind": shot.get("kind") or "exploration",
                "url": f"/console/evidence/{quote(run_id)}/{quote(name)}",
            }
        )
    for result in results:
        for name in result.get("screenshots") or []:
            name = str(name)
            if not name or name in seen:
                continue
            seen.add(name)
            if not _evidence_exists(run_id, name):
                missing += 1
                continue
            rows.append(
                {
                    "name": name,
                    "caption": f"Failure evidence — {result.get('test_name')}",
                    "step": None,
                    "kind": "failure",
                    "url": f"/console/evidence/{quote(run_id)}/{quote(name)}",
                }
            )
    return rows, missing


def _generated_tests(
    store: QAStore, run: dict[str, Any]
) -> list[dict[str, Any]]:
    tests = []
    for test_id in run.get("generated_test_ids") or []:
        test = store.get_test_case(str(test_id))
        if test is not None:
            tests.append(test)
    return tests


def _base_context(request: Request, active: str) -> dict[str, Any]:
    manager = get_manager(request)
    store = get_store(request)
    config = manager.config_summary()
    return {
        "request": request,
        "active": active,
        "config": config,
        "store_backend": config.get("store_backend", "unknown"),
        "page_title": "FlowScout Console",
    }


def _error_response(exc: RunRequestError) -> JSONResponse:
    if isinstance(exc, LiveAuthorizationRequired):
        status = 403
    elif isinstance(exc, LiveRunBusy):
        status = 409
    elif isinstance(exc, LiveNotConfigured):
        status = 409
    else:
        status = 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


def _safe_evidence_path(run_id: str, filename: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id) or not _EVIDENCE_NAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Evidence not found")
    root = (EVIDENCE_ROOT / run_id).resolve()
    path = (root / filename).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Evidence not found")
    return path


# ------------------------------------------------------------------- API models


class ExplorationRequest(BaseModel):
    target_url: str = Field(min_length=1, max_length=2048)
    project_id: str | None = Field(default=None, max_length=80)
    project_name: str | None = Field(default=None, max_length=120)
    mode: Literal["mock", "live"] = "mock"
    authorize_live: bool = False
    max_steps: int = Field(default=8, ge=3, le=12)
    use_ai: bool = True


class RegressionRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    test_ids: list[str] | None = None
    mode: Literal["mock", "live"] = "mock"
    authorize_live: bool = False


# ---------------------------------------------------------------------- pages


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    store = get_store(request)
    stats = store.stats()
    runs = store.list_runs(limit=8)
    projects = store.list_projects()
    tests = store.list_test_cases()[:5]
    context = {
        **_base_context(request, "dashboard"),
        "page_title": "Dashboard",
        "stats": stats,
        "runs": runs,
        "projects": projects[:6],
        "tests": tests,
        "projects_by_id": _project_map(store),
    }
    return templates.TemplateResponse(request=request, name="console/dashboard.html", context=context)


@router.get("/new", response_class=HTMLResponse)
def new_run(request: Request):
    store = get_store(request)
    context = {
        **_base_context(request, "new"),
        "page_title": "New QA run",
        "projects": store.list_projects(),
        "runs": store.list_runs(limit=5),
        "projects_by_id": _project_map(store),
    }
    return templates.TemplateResponse(request=request, name="console/new_run.html", context=context)


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    store = get_store(request)
    projects = store.list_projects()
    runs = store.list_runs(limit=500)
    tests = store.list_test_cases()
    run_counts = _counts_by_project(runs)
    test_counts = _counts_by_project(tests)
    last_run: dict[str, dict[str, Any]] = {}
    for run in runs:
        project_id = str(run.get("project_id") or "")
        if project_id and project_id not in last_run:
            last_run[project_id] = run
    context = {
        **_base_context(request, "projects"),
        "page_title": "Projects",
        "projects": projects,
        "run_counts": run_counts,
        "test_counts": test_counts,
        "last_run": last_run,
    }
    return templates.TemplateResponse(request=request, name="console/projects.html", context=context)


@router.get("/tests", response_class=HTMLResponse)
def tests_page(request: Request):
    store = get_store(request)
    projects = store.list_projects()
    selected = request.query_params.get("project_id") or ""
    tests = store.list_test_cases(project_id=selected or None)
    context = {
        **_base_context(request, "tests"),
        "page_title": "Test cases",
        "tests": tests,
        "projects": projects,
        "selected_project": selected,
        "projects_by_id": _project_map(store),
    }
    return templates.TemplateResponse(request=request, name="console/tests.html", context=context)


@router.get("/regression", response_class=HTMLResponse)
def regression_page(request: Request):
    store = get_store(request)
    projects = store.list_projects()
    selected = request.query_params.get("project_id") or ""
    if not selected and projects:
        selected = projects[0]["project_id"]
    tests = store.list_test_cases(project_id=selected or None) if selected else []
    runs = [
        run
        for run in store.list_runs(limit=100)
        if run.get("kind") == "regression"
    ][:8]
    previous: dict[str, dict[str, Any]] = {}
    if selected and runs:
        previous = store.previous_regression_results(selected, "")
    context = {
        **_base_context(request, "regression"),
        "page_title": "Regression testing",
        "projects": projects,
        "selected_project": selected,
        "tests": tests,
        "runs": runs,
        "previous": previous,
        "projects_by_id": _project_map(store),
    }
    return templates.TemplateResponse(request=request, name="console/regression.html", context=context)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    store = get_store(request)
    kind = request.query_params.get("kind") or ""
    status = request.query_params.get("status") or ""
    mode = request.query_params.get("mode") or ""
    project_id = request.query_params.get("project_id") or ""
    runs = store.list_runs(limit=300, project_id=project_id or None)
    if kind:
        runs = [run for run in runs if run.get("kind") == kind]
    if status:
        runs = [run for run in runs if run.get("status") == status]
    if mode:
        runs = [run for run in runs if run.get("mode") == mode]
    context = {
        **_base_context(request, "runs"),
        "page_title": "Run history",
        "runs": runs,
        "projects": store.list_projects(),
        "projects_by_id": _project_map(store),
        "filters": {"kind": kind, "status": status, "mode": mode, "project_id": project_id},
    }
    return templates.TemplateResponse(request=request, name="console/runs.html", context=context)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    store = get_store(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    project = store.get_project(run["project_id"]) if run.get("project_id") else None
    results, missing_results = _annotate_results(run_id, store.list_test_results(run_id))
    screenshots, missing_run = _screenshots(run, results)
    context = {
        **_base_context(request, "runs"),
        "page_title": f"Run {run_id}",
        "run": run,
        "project": project,
        "results": results,
        "screenshots": screenshots,
        "missing_evidence": missing_results + missing_run,
        "generated_tests": _generated_tests(store, run),
        "live": run.get("status") in {"queued", "running"},
    }
    return templates.TemplateResponse(request=request, name="console/run_detail.html", context=context)


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    store = get_store(request)
    runs = [
        run
        for run in store.list_runs(limit=300)
        if run.get("status") in {"succeeded", "failed"}
    ]
    context = {
        **_base_context(request, "reports"),
        "page_title": "Reports",
        "runs": runs,
        "projects_by_id": _project_map(store),
    }
    return templates.TemplateResponse(request=request, name="console/reports.html", context=context)


@router.get("/reports/{run_id}", response_class=HTMLResponse)
def report_detail(request: Request, run_id: str):
    store = get_store(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Report not found")
    project = store.get_project(run["project_id"]) if run.get("project_id") else None
    results, missing_results = _annotate_results(run_id, store.list_test_results(run_id))
    screenshots, missing_run = _screenshots(run, results)
    context = {
        **_base_context(request, "reports"),
        "page_title": f"Report {run_id}",
        "run": run,
        "project": project,
        "results": results,
        "screenshots": screenshots,
        "missing_evidence": missing_results + missing_run,
        "generated_tests": _generated_tests(store, run),
    }
    return templates.TemplateResponse(request=request, name="console/report_detail.html", context=context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    store = get_store(request)
    context = {
        **_base_context(request, "settings"),
        "page_title": "Settings",
        "stats": store.stats(),
    }
    return templates.TemplateResponse(request=request, name="console/settings.html", context=context)


# ------------------------------------------------------------------------ API


@router.get("/api/config")
def api_config(request: Request):
    return JSONResponse(content=get_manager(request).config_summary())


@router.get("/api/runs")
def api_runs(request: Request):
    store = get_store(request)
    return JSONResponse(content=store.list_runs(limit=100))


@router.post("/api/runs", status_code=201)
def api_create_run(request: Request, payload: ExplorationRequest):
    manager = get_manager(request)
    target_url = payload.target_url.strip()
    if payload.mode == "mock" and target_url.startswith("/"):
        target_url = str(request.base_url).rstrip("/") + target_url
    try:
        run = manager.start_exploration(
            target_url=target_url,
            project_id=payload.project_id,
            project_name=payload.project_name,
            mode=payload.mode,
            authorize_live=payload.authorize_live,
            max_steps=payload.max_steps,
            use_ai=payload.use_ai,
        )
    except RunRequestError as exc:
        return _error_response(exc)
    return JSONResponse(
        status_code=201,
        content={
            "run": run,
            "url": f"/console/runs/{run['run_id']}",
        },
    )


@router.get("/api/runs/{run_id}")
def api_run(request: Request, run_id: str):
    store = get_store(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return JSONResponse(
        content={
            "run": run,
            "results": store.list_test_results(run_id),
        }
    )


@router.post("/api/regression", status_code=201)
def api_create_regression(request: Request, payload: RegressionRequest):
    manager = get_manager(request)
    try:
        run = manager.start_regression(
            project_id=payload.project_id,
            test_ids=payload.test_ids,
            mode=payload.mode,
            authorize_live=payload.authorize_live,
        )
    except RunRequestError as exc:
        return _error_response(exc)
    return JSONResponse(
        status_code=201,
        content={
            "run": run,
            "url": f"/console/runs/{run['run_id']}",
        },
    )


@router.delete("/api/tests/{test_id}")
def api_delete_test(request: Request, test_id: str):
    store = get_store(request)
    if not store.delete_test_case(test_id):
        raise HTTPException(status_code=404, detail="Test case not found")
    return JSONResponse(content={"deleted": True, "test_id": test_id})


# ------------------------------------------------------------------- evidence


@router.get("/evidence/{run_id}/{filename}")
def evidence_file(run_id: str, filename: str):
    path = _safe_evidence_path(run_id, filename)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    disposition = "inline" if media_type.startswith(("image/", "text/", "application/pdf")) else "attachment"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{path.name}"'},
    )


@router.get("/runs/{run_id}/report.json")
def download_report(request: Request, run_id: str):
    store = get_store(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Report not found")
    project = store.get_project(run["project_id"]) if run.get("project_id") else None
    report = {
        "report_version": 1,
        "generated_at": utcnow(),
        "run": run,
        "project": project,
        "summary": run.get("summary") or {},
        "issues": run.get("issues") or [],
        "actions": run.get("actions") or [],
        "logs": run.get("logs") or [],
        "screenshots": run.get("screenshots") or [],
        "test_results": store.list_test_results(run_id),
        "generated_tests": _generated_tests(store, run),
    }
    body = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="flowscout-{run_id}-report.json"'
        },
    )
