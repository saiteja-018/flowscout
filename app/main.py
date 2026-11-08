"""FlowScout FastAPI control plane and QA console.

The API exposes an intentionally small run ledger and the console:

* ``GET /health``
* ``GET /api/runs``
* ``POST /api/runs``
* ``GET /console`` and the other console pages
* ``GET /`` redirects to ``/console``
* legacy ``/demo``, ``/live``, ``/ops`` and ``/dashboard`` redirect to ``/console``

The console is the only product surface. The retired public-site routes are
kept as redirects so existing bookmarks do not 404.

PostgreSQL is the production ledger. A read can degrade to a clearly marked
snapshot containing the required seed record when the database is unreachable,
because the console should still explain its own state. A write never falls
back to memory: if PostgreSQL is unavailable, ``POST /api/runs`` returns 503.
Tests inject an explicit ``MemoryLedger`` through ``create_app``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.console import router as console_router
from app.target_saas_app import router as shop_router

logger = logging.getLogger("flowscout")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"
REPORTS_DIR = PROJECT_ROOT / "reports"
COMMANDS_PATH = PROJECT_ROOT / "logs" / "commands.jsonl"
DEFAULT_DATABASE_URL = "postgresql://ledger:ledger@localhost:5432/ledger"

SANDBOX_NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
VALID_STATES = {"created", "ready", "running", "succeeded", "failed", "deleted"}
VALID_EXECUTION_TARGETS = {"celesto_sandbox"}

SEED_RUN = {
    "run_id": "run-seed-0001",
    "sandbox_name": "sbx-seeddemo1",
    "problem_id": None,
    "state": "deleted",
    "execution_target": "celesto_sandbox",
    "created_at": "2026-01-01T00:00:00+00:00",
    "updated_at": "2026-01-01T00:00:00+00:00",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sandbox_name TEXT NOT NULL,
    problem_id TEXT,
    state TEXT NOT NULL DEFAULT 'created'
        CHECK (state IN ('created','ready','running','succeeded','failed','deleted')),
    execution_target TEXT NOT NULL DEFAULT 'celesto_sandbox'
        CHECK (execution_target = 'celesto_sandbox'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO runs (run_id, sandbox_name, state, execution_target)
VALUES ('run-seed-0001', 'sbx-seeddemo1', 'deleted', 'celesto_sandbox')
ON CONFLICT DO NOTHING;
"""


class LedgerUnavailable(RuntimeError):
    pass


class DuplicateRun(ValueError):
    pass


class RunLedger(Protocol):
    def list_runs(self) -> list[dict[str, Any]]: ...
    def create_run(self, record: dict[str, Any]) -> dict[str, Any]: ...


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("created_at", "updated_at"):
        result[key] = _iso(result.get(key))
    return result


class MemoryLedger:
    """Explicit test ledger. Never selected as a fallback in production."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = [dict(SEED_RUN)]

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in sorted(self._rows, key=lambda row: row["created_at"])]

    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        if any(row["run_id"] == record["run_id"] for row in self._rows):
            raise DuplicateRun(record["run_id"])
        row = _normalize(record)
        self._rows.append(row)
        return dict(row)


class PostgresLedger:
    def __init__(self, database_url: str, *, connect_timeout: int = 3) -> None:
        self.database_url = database_url
        self.connect_timeout = connect_timeout
        self._schema_checked = False

    def _connect(self):
        if not self.database_url:
            raise LedgerUnavailable("DATABASE_URL is not configured")
        try:
            conn = psycopg2.connect(self.database_url, connect_timeout=self.connect_timeout)
        except psycopg2.Error as exc:
            raise LedgerUnavailable(str(exc).splitlines()[0]) from None
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
        except psycopg2.Error as exc:
            conn.rollback()
            raise LedgerUnavailable(str(exc).splitlines()[0]) from None
        finally:
            conn.close()

    def list_runs(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT run_id, sandbox_name, problem_id, state, execution_target, "
                    "created_at, updated_at FROM runs ORDER BY created_at, run_id"
                )
                rows = [dict(row) for row in cur.fetchall()]
            return [_normalize(row) for row in rows]
        except psycopg2.Error as exc:
            raise LedgerUnavailable(str(exc).splitlines()[0]) from None
        finally:
            conn.close()

    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO runs (run_id, sandbox_name, problem_id, state, "
                    "execution_target, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        record["run_id"],
                        record["sandbox_name"],
                        record["problem_id"],
                        record["state"],
                        record["execution_target"],
                        record["created_at"],
                        record["updated_at"],
                    ),
                )
            conn.commit()
            return _normalize(record)
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise DuplicateRun(record["run_id"]) from None
        except psycopg2.Error as exc:
            conn.rollback()
            raise LedgerUnavailable(str(exc).splitlines()[0]) from None
        finally:
            conn.close()


class RunCreate(BaseModel):
    run_id: str = Field(min_length=3, max_length=128)
    sandbox_name: str
    problem_id: str | None = Field(default=None, max_length=200)
    state: Literal["created", "ready", "running", "succeeded", "failed", "deleted"] = "created"
    execution_target: Literal["celesto_sandbox"] = "celesto_sandbox"

    @field_validator("run_id")
    @classmethod
    def valid_run_id(cls, value: str) -> str:
        if not RUN_ID_RE.fullmatch(value):
            raise ValueError("run_id must contain only letters, digits, dot, underscore, or hyphen")
        return value

    @field_validator("sandbox_name")
    @classmethod
    def valid_sandbox_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.fullmatch(value):
            raise ValueError(f"sandbox_name must match {SANDBOX_NAME_RE.pattern}")
        return value


def _load_json(name: str) -> Any:
    try:
        return json.loads((REPORTS_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def _esc(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _metric(value: Any, suffix: str = "") -> str:
    if value is None:
        return "Not measured"
    return f"{_esc(value)}{suffix}"


def _render_dashboard(data: dict[str, Any], *, degraded: bool) -> str:
    reliability = data.get("reliability") or {}
    summary = {
        key: reliability.get(key)
        for key in (
            "run_count",
            "success_rate",
            "median_wall_clock_s",
            "p90_wall_clock_s",
            "median_boot_to_ready_s",
            "total_cost_usd",
        )
    }
    runs = reliability.get("runs") or []
    commands = data.get("commands") or []
    traces = data.get("failure_trace") or []
    lifecycle = data.get("lifecycle") or {}
    containment = data.get("containment") or {}
    failure_policy = data.get("failure_policy") or {}
    problem = data.get("problem") or {}

    mode = reliability.get("evaluation_mode", "not recorded")
    browser_runtime = reliability.get("browser_runtime", "not recorded")
    truth_label = "MOCK SANDBOX / HOST BROWSER" if mode == "mock" else str(mode).upper()
    degraded_banner = (
        '<div class="banner warning">Ledger unavailable. Showing read-only seed snapshot; '
        "writes are rejected until PostgreSQL is reachable.</div>"
        if degraded
        else ""
    )

    run_rows = "".join(
        f"""<tr>
          <td class="mono">{_esc(r.get("sandbox_name"))}</td>
          <td><span class="status {'ok' if r.get('success') else 'bad'}">{'succeeded' if r.get('success') else 'failed'}</span></td>
          <td class="num">{_esc(r.get("wall_clock_s"))}s</td>
          <td class="num">{_esc(r.get("boot_to_ready_s"))}s</td>
          <td class="num">${_esc(r.get("cost_usd"))}</td>
        </tr>"""
        for r in runs[-20:]
    )
    if not run_rows:
        run_rows = '<tr><td colspan="5" class="empty">No reliability runs recorded.</td></tr>'

    command_rows = "".join(
        f"""<tr>
          <td class="mono small">{_esc(c.get("ts"))}</td>
          <td class="mono small">{_esc(c.get("sandbox_name"))}</td>
          <td class="mono code">{_esc(c.get("command"))}</td>
          <td><span class="status {'warn' if c.get('decision') == 'block' else 'ok'}">{_esc(c.get("decision"))}</span></td>
          <td class="mono small">{_esc(c.get("execution_mode"))}</td>
        </tr>"""
        for c in commands[-25:]
    )
    if not command_rows:
        command_rows = '<tr><td colspan="5" class="empty">No command attempts recorded.</td></tr>'

    trace_rows = "".join(
        f"""<tr>
          <td class="mono small">{_esc(t.get("ts"))}</td>
          <td class="num">{_esc(t.get("step"))}</td>
          <td>{_esc(t.get("event"))}</td>
          <td><span class="state">{_esc(t.get("state"))}</span></td>
          <td class="small">{_esc(t.get("detail"))}</td>
        </tr>"""
        for t in traces[-30:]
    )
    if not trace_rows:
        trace_rows = '<tr><td colspan="5" class="empty">No failure trace recorded.</td></tr>'

    evidence_links = "".join(
        f'<li><a href="/reports/{_esc(name)}">{_esc(name)}</a></li>'
        for name in (
            "problem.json",
            "capabilities.json",
            "lifecycle.json",
            "reliability.json",
            "postmortem.md",
            "containment.json",
            "failure_policy.json",
            "failure_trace.jsonl",
            "concurrency.json",
            "evidence/containment.log",
            "evidence/actions.jsonl",
            "evidence/coupon_stacking_test.spec.ts",
            "evidence/coupon_regression_test.spec.ts",
        )
    )

    lifecycle_checks = [
        ("Cleanup on success", lifecycle.get("cleanup_on_success")),
        ("Cleanup on failure", lifecycle.get("cleanup_on_failure")),
        ("Cleanup on interrupt", lifecycle.get("cleanup_on_interrupt")),
    ]
    lifecycle_html = "".join(
        f'<div class="check"><span>{_esc(label)}</span><strong class="{"ok" if ok else "bad"}">'
        f'{"verified" if ok else "not verified"}</strong></div>'
        for label, ok in lifecycle_checks
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowScout Flight Deck</title>
  <style>
    :root {{
      --bg: #0c100e; --panel: #121814; --panel2: #172019; --line: #2a342c;
      --text: #e7e4d8; --muted: #94a096; --acid: #c9f27c; --amber: #e9b35b;
      --red: #ef7a6b; --cyan: #8bd4cf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background:
        linear-gradient(rgba(12,16,14,.94), rgba(12,16,14,.98)),
        repeating-linear-gradient(90deg, transparent 0 39px, #1b241d 40px);
      color: var(--text); font: 15px/1.5 "Segoe UI", system-ui, sans-serif;
    }}
    a {{ color: inherit; }}
    .shell {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    header {{
      display: flex; justify-content: space-between; align-items: flex-end; gap: 20px;
      border-bottom: 1px solid var(--line); padding: 16px 0 18px; flex-wrap: wrap;
    }}
    .brand {{ font: 700 1.3rem/1 ui-monospace, Consolas, monospace; letter-spacing: .16em; }}
    .brand span {{ color: var(--acid); }}
    .sub {{ color: var(--muted); font: .72rem/1.5 ui-monospace, monospace; letter-spacing: .08em; }}
    .truth {{
      border: 1px solid var(--amber); color: var(--amber); padding: 7px 10px;
      font: .7rem/1.2 ui-monospace, monospace; letter-spacing: .08em;
    }}
    .banner {{ padding: 11px 14px; margin: 16px 0 0; border-left: 3px solid var(--amber); background: #1b1810; }}
    .banner.warning {{ color: #f0d59f; }}
    .grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin: 22px 0; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); padding: 14px; min-height: 92px; }}
    .metric .label {{ color: var(--muted); font: .67rem/1.4 ui-monospace, monospace; text-transform: uppercase; letter-spacing: .08em; }}
    .metric .value {{ margin-top: 9px; font: 700 1.55rem/1.1 ui-monospace, Consolas, monospace; color: var(--acid); overflow-wrap: anywhere; }}
    .columns {{ display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; margin-bottom: 18px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); }}
    .panel h2 {{
      margin: 0; padding: 12px 14px; border-bottom: 1px solid var(--line);
      font: 600 .72rem/1.3 ui-monospace, monospace; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
    }}
    .panel .body {{ padding: 14px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ color: var(--muted); font: .65rem/1.3 ui-monospace, monospace; text-transform: uppercase; letter-spacing: .08em; text-align: left; padding: 8px; border-bottom: 1px solid var(--line); }}
    td {{ padding: 8px; border-bottom: 1px solid #202a22; vertical-align: top; }}
    tr:last-child td {{ border-bottom: 0; }}
    .num {{ text-align: right; font-family: ui-monospace, Consolas, monospace; white-space: nowrap; }}
    .mono {{ font-family: ui-monospace, Consolas, monospace; }}
    .small {{ font-size: .76rem; }}
    .code {{ font-size: .76rem; overflow-wrap: anywhere; max-width: 440px; }}
    .status {{ display: inline-block; border: 1px solid currentColor; padding: 1px 6px; font: .66rem/1.5 ui-monospace, monospace; text-transform: uppercase; }}
    .status.ok, .ok {{ color: var(--acid); }}
    .status.bad, .bad {{ color: var(--red); }}
    .status.warn {{ color: var(--amber); }}
    .state {{ color: var(--cyan); font: .74rem/1.4 ui-monospace, monospace; }}
    .empty {{ color: var(--muted); text-align: center; padding: 20px; }}
    .check {{ display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid #202a22; }}
    .check:last-child {{ border-bottom: 0; }}
    .check strong {{ font: .7rem/1.6 ui-monospace, monospace; text-transform: uppercase; }}
    .kv {{ display: flex; justify-content: space-between; gap: 12px; padding: 5px 0; }}
    .kv span:first-child {{ color: var(--muted); }}
    ul.evidence {{ margin: 0; padding-left: 18px; columns: 2; column-gap: 22px; }}
    ul.evidence li {{ margin: 3px 0; font: .73rem/1.5 ui-monospace, monospace; break-inside: avoid; }}
    footer {{ margin-top: 30px; padding-top: 14px; border-top: 1px solid var(--line); color: var(--muted); font: .7rem/1.6 ui-monospace, monospace; }}
    @media (max-width: 1000px) {{ .grid {{ grid-template-columns: repeat(3, 1fr); }} .columns {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 620px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} .shell {{ padding: 14px; }} ul.evidence {{ columns: 1; }} }}
  </style>
</head>
<body>
<main class="shell">
  <header>
    <div>
      <div class="brand">FLOW<span>SCOUT</span> / FLIGHT DECK</div>
      <div class="sub">browser QA agent / run ledger / containment telemetry</div>
    </div>
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <a class="mono small" href="/" style="color:var(--acid);text-decoration:none">← Public site</a>
      <div class="truth">{_esc(truth_label)}</div>
    </div>
  </header>
  {degraded_banner}

  <section class="grid">
    <div class="metric"><div class="label">Runs measured</div><div class="value">{_metric(summary.get("run_count"))}</div></div>
    <div class="metric"><div class="label">Success rate</div><div class="value">{_metric(summary.get("success_rate"))}</div></div>
    <div class="metric"><div class="label">Median duration</div><div class="value">{_metric(summary.get("median_wall_clock_s"), "s")}</div></div>
    <div class="metric"><div class="label">P90 duration</div><div class="value">{_metric(summary.get("p90_wall_clock_s"), "s")}</div></div>
    <div class="metric"><div class="label">Boot to ready</div><div class="value">{_metric(summary.get("median_boot_to_ready_s"), "s")}</div></div>
    <div class="metric"><div class="label">Recorded cost</div><div class="value">${_metric(summary.get("total_cost_usd"))}</div></div>
  </section>

  <section class="columns">
    <div>
      <div class="panel">
        <h2>Problem under test</h2>
        <div class="body">
          <p>{_esc(problem.get("proof_output", "Problem report has not been generated."))}</p>
          <div class="kv"><span>Browser runtime</span><strong class="mono">{_esc(browser_runtime)}</strong></div>
          <div class="kv"><span>Failure policy</span><strong class="mono">{_esc(failure_policy.get("policy", "not recorded"))}</strong></div>
          <div class="kv"><span>Failure step</span><strong class="mono">{_esc(failure_policy.get("failed_step", "not recorded"))} / {_esc(failure_policy.get("total_steps", "not recorded"))}</strong></div>
        </div>
      </div>
      <div class="panel" style="margin-top:18px">
        <h2>Evaluation runs</h2>
        <table>
          <thead><tr><th>Sandbox</th><th>Outcome</th><th>Wall</th><th>Boot</th><th>Cost</th></tr></thead>
          <tbody>{run_rows}</tbody>
        </table>
      </div>
    </div>
    <div>
      <div class="panel">
        <h2>Lifecycle verification</h2>
        <div class="body">{lifecycle_html}</div>
      </div>
      <div class="panel" style="margin-top:18px">
        <h2>Containment</h2>
        <div class="body">
          <div class="kv"><span>Attack</span><strong class="mono">{_esc(containment.get("attack", "not recorded"))}</strong></div>
          <div class="kv"><span>Blocked</span><strong class="{'ok' if containment.get('blocked') else 'bad'}">{_esc(containment.get("blocked"))}</strong></div>
          <div class="kv"><span>Host untouched</span><strong class="{'ok' if containment.get('host_untouched') else 'bad'}">{_esc(containment.get("host_untouched"))}</strong></div>
          <p><a class="mono small" href="/reports/evidence/containment.log">Open independent evidence log</a></p>
        </div>
      </div>
      <div class="panel" style="margin-top:18px">
        <h2>Evidence index</h2>
        <div class="body"><ul class="evidence">{evidence_links}</ul></div>
      </div>
    </div>
  </section>

  <div class="panel">
    <h2>Command audit log</h2>
    <table>
      <thead><tr><th>Time</th><th>Sandbox</th><th>Command / action</th><th>Decision</th><th>Mode</th></tr></thead>
      <tbody>{command_rows}</tbody>
    </table>
  </div>

  <div class="panel" style="margin-top:18px">
    <h2>Lifecycle and recovery trace</h2>
    <table>
      <thead><tr><th>Time</th><th>Step</th><th>Event</th><th>State</th><th>Detail</th></tr></thead>
      <tbody>{trace_rows}</tbody>
    </table>
  </div>

  <footer>FlowScout / Celesto AI Sandbox Challenge / truthful mock and host-browser evidence only when live Celesto telemetry is absent.</footer>
</main>
</body>
</html>"""


def create_app(ledger: RunLedger | None = None) -> FastAPI:
    selected_ledger = ledger
    if selected_ledger is None:
        mode = os.getenv("FLOWSCOUT_LEDGER", "postgres").lower()
        if mode == "memory":
            selected_ledger = MemoryLedger()
        elif mode == "postgres":
            selected_ledger = PostgresLedger(
                os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
                connect_timeout=int(os.getenv("DATABASE_CONNECT_TIMEOUT", "3")),
            )
        else:
            raise ValueError("FLOWSCOUT_LEDGER must be 'postgres' or 'memory'")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        manager = getattr(app.state, "qa_manager", None)
        if manager is not None:
            manager.shutdown()

    application = FastAPI(title="FlowScout QA Console", version="1.0.0", lifespan=lifespan)
    application.state.ledger = selected_ledger
    application.state.qa_store = None
    application.state.qa_manager = None
    application.include_router(shop_router)
    application.include_router(console_router)

    if REPORTS_DIR.is_dir():
        application.mount("/reports", StaticFiles(directory=str(REPORTS_DIR)), name="reports")
    if WEB_DIR.is_dir():
        application.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR / "static")),
            name="static",
        )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        messages = []
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc", []) if part != "body")
            messages.append(f"{field}: {error['msg']}")
        return JSONResponse(status_code=400, content={"detail": "; ".join(messages)})

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/runs")
    def list_runs():
        try:
            rows = selected_ledger.list_runs()
            return JSONResponse(content=rows, headers={"X-FlowScout-Degraded": "false"})
        except LedgerUnavailable as exc:
            logger.warning("Run ledger unavailable for read: %s", exc)
            return JSONResponse(
                content=[dict(SEED_RUN)],
                headers={
                    "X-FlowScout-Degraded": "true",
                    "X-FlowScout-Ledger": "unavailable",
                },
            )

    @application.post("/api/runs", status_code=201)
    def create_run(run: RunCreate):
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "run_id": run.run_id,
            "sandbox_name": run.sandbox_name,
            "problem_id": run.problem_id,
            "state": run.state,
            "execution_target": run.execution_target,
            "created_at": now,
            "updated_at": now,
        }
        try:
            return selected_ledger.create_run(record)
        except DuplicateRun:
            raise HTTPException(status_code=409, detail=f"run_id {run.run_id!r} already exists")
        except LedgerUnavailable as exc:
            logger.error("Run ledger unavailable for write: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Run ledger unavailable; creation was not persisted.",
            ) from None

    @application.get("/", response_class=RedirectResponse)
    def root():
        return RedirectResponse(url="/console", status_code=307)

    @application.get("/demo", response_class=RedirectResponse)
    @application.get("/live", response_class=RedirectResponse)
    @application.get("/ops", response_class=RedirectResponse)
    @application.get("/dashboard", response_class=RedirectResponse)
    def retired_public_site():
        return RedirectResponse(url="/console", status_code=307)

    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", "8080")),
    )
