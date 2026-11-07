#!/usr/bin/env python3
"""Generate FlowScout evidence and reports without inventing live telemetry.

Default mode uses a deterministic mock Celesto adapter plus a real host
Playwright browser against the local target fixture. Every report records that
boundary. ``--mode live`` requires Celesto credentials, Linux-compatible Celesto
runtime support, a browser-capable cloud image, and a target URL reachable from
the sandbox.

Usage:
    python scripts/run_live_demo.py --mode mock --runs 20
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any
from unittest.mock import patch

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv is a core dependency
    pass

from app.evidence_io import atomic_write_json, atomic_write_text
from app.provenance import capture_provenance
from app.qa_agent import (
    ACTIONS_LOG,
    EVIDENCE_DIR,
    GENERATED_TEST_DIR,
    REPORTS_DIR,
    DefectClassification,
    QAWorkflowRunner,
    generate_sandbox_name,
    run_containment_test,
    run_generated_test,
    write_generated_specs,
)
from app.report_schemas import SCHEMA_VERSION
from app.sandbox_runner import SandboxAdapter, create_adapter
from scripts.live_common import (
    GUEST_APP_BASE_URL,
    GUEST_APP_PORT,
    TARGET_DEFAULT,
    deploy_guest_fixture,
    live_hourly_rate,
    live_size_id,
    sha256,
    utcnow,
    visible_guest_browser_default,
)

COMMANDS_PATH = PROJECT_ROOT / "logs" / "commands.jsonl"
DEMO_PATH = PROJECT_ROOT / "demo" / "walkthrough.mp4"
ARTIFACT_BACKUP_ROOT = PROJECT_ROOT / "tmp" / "artifact-backups"
LIVE_BROWSER_RUNTIMES = {"celesto_cdp", "guest_playwright_exec"}


def run_cost_usd(wall_clock_s: float, execution_mode: str) -> float:
    if execution_mode != "live":
        return 0.0
    return round(wall_clock_s / 3600.0 * live_hourly_rate(), 5)


def cost_metadata(
    agent_wall_clock_s: float,
    total_wall_clock_s: float,
    execution_mode: str,
) -> dict[str, Any]:
    """Cost fields that state exactly which clock and rate produced them."""
    if execution_mode != "live":
        return {
            "cost_usd": 0.0,
            "cost_including_creation_usd": 0.0,
            "cost_source": "mock_zero_cost",
            "rate_usd_per_hour": 0.0,
        }
    return {
        "cost_usd": run_cost_usd(agent_wall_clock_s, execution_mode),
        "cost_including_creation_usd": run_cost_usd(total_wall_clock_s, execution_mode),
        "cost_source": "celesto_size_rate_wall_clock",
        "rate_usd_per_hour": live_hourly_rate(),
    }


def live_verification(execution_mode: str, runs: list[dict[str, Any]]) -> bool:
    """True only when every live run really used a sandbox browser and cleaned up."""
    return bool(
        execution_mode == "live"
        and runs
        and all(
            row.get("success")
            and row.get("browser_verified")
            and row.get("cleanup_ok")
            for row in runs
        )
    )


def write_json(path: Path, payload: Any) -> None:
    if isinstance(payload, dict):
        payload.setdefault("schema_version", SCHEMA_VERSION)
    atomic_write_json(path, payload)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}")


def clean_artifacts() -> None:
    if EVIDENCE_DIR.exists():
        shutil.rmtree(EVIDENCE_DIR)
    EVIDENCE_DIR.mkdir(parents=True)
    GENERATED_TEST_DIR.mkdir(parents=True, exist_ok=True)
    for path in GENERATED_TEST_DIR.glob("*.spec.ts"):
        path.unlink()
    COMMANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(COMMANDS_PATH, "")
    for name in PROJECT_ROOT.glob("reports/*.json"):
        name.unlink()
    for name in PROJECT_ROOT.glob("reports/*.md"):
        name.unlink()
    trace = REPORTS_DIR / "failure_trace.jsonl"
    if trace.exists():
        trace.unlink()
    if DEMO_PATH.exists():
        DEMO_PATH.unlink()
    shutil.rmtree(PROJECT_ROOT / "test-results", ignore_errors=True)
    shutil.rmtree(PROJECT_ROOT / "playwright-report", ignore_errors=True)


_ACTIVE_BACKUP: Path | None = None


def _artifact_paths() -> list[Path]:
    paths: list[Path] = []
    if EVIDENCE_DIR.exists():
        paths.append(EVIDENCE_DIR)
    paths.extend(sorted(REPORTS_DIR.glob("*.json")))
    paths.extend(sorted(REPORTS_DIR.glob("*.md")))
    trace = REPORTS_DIR / "failure_trace.jsonl"
    if trace.exists():
        paths.append(trace)
    if DEMO_PATH.exists():
        paths.append(DEMO_PATH)
    if COMMANDS_PATH.exists():
        paths.append(COMMANDS_PATH)
    paths.extend(sorted(GENERATED_TEST_DIR.glob("*.spec.ts")))
    for name in ("test-results", "playwright-report"):
        path = PROJECT_ROOT / name
        if path.exists():
            paths.append(path)
    return paths


def _copy_artifact(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def prepare_artifacts() -> Path:
    """Back up the current evidence, then clear it for a fresh run.

    The backup is restored automatically unless ``commit_artifacts`` is called.
    That keeps a failed or interrupted live run from leaving the submission
    tree without its reports, video, or command log.
    """
    global _ACTIVE_BACKUP
    if _ACTIVE_BACKUP is not None:
        raise RuntimeError("an artifact transaction is already active")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = ARTIFACT_BACKUP_ROOT / stamp
    backup.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, str] = {}
    for path in _artifact_paths():
        relative = path.relative_to(PROJECT_ROOT)
        _copy_artifact(path, backup / relative)
        manifest[str(relative).replace("\\", "/")] = "dir" if path.is_dir() else "file"
    write_json(backup / "manifest.json", {"created_at": utcnow(), "paths": manifest})
    _ACTIVE_BACKUP = backup
    atexit.register(_restore_on_exit)
    clean_artifacts()
    print(f"  artifact backup: {backup.relative_to(PROJECT_ROOT)}")
    return backup


def commit_artifacts(*, verify: bool = True) -> None:
    """Keep the freshly generated artifacts and discard the rollback copy.

    Verification runs before the backup is discarded. A failed verification
    restores the pre-run evidence immediately, so a broken report set can never
    replace a known-good one.
    """
    global _ACTIVE_BACKUP
    if _ACTIVE_BACKUP is None:
        return
    if verify:
        from app.report_schemas import ReportVerificationError, verify_submission

        result = verify_submission(PROJECT_ROOT)
        if not result.ok:
            backup = _ACTIVE_BACKUP
            _ACTIVE_BACKUP = None
            for error in result.errors:
                print(f"VERIFY ERROR: {error}")
            restore_artifacts(backup)
            raise ReportVerificationError(result)
    shutil.rmtree(_ACTIVE_BACKUP, ignore_errors=True)
    _ACTIVE_BACKUP = None


def restore_artifacts(backup: Path) -> None:
    """Restore the pre-run artifact set after a failed or interrupted run."""
    manifest_path = backup / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clean_artifacts()
    for relative, kind in manifest["paths"].items():
        source = backup / relative
        destination = PROJECT_ROOT / relative
        if kind == "dir":
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    print(f"  restored artifacts from {backup.relative_to(PROJECT_ROOT)}")


def _restore_on_exit() -> None:
    global _ACTIVE_BACKUP
    backup = _ACTIVE_BACKUP
    if backup is None:
        return
    _ACTIVE_BACKUP = None
    try:
        restore_artifacts(backup)
    except Exception as exc:
        print(f"ERROR: artifact rollback failed: {type(exc).__name__}: {exc}")


def wait_for_target(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/shop/api/health", timeout=1.0)
            if response.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"target app did not become ready at {base_url}")


def start_target(base_url: str) -> subprocess.Popen[str] | None:
    if "127.0.0.1" not in base_url and "localhost" not in base_url:
        wait_for_target(base_url)
        return None
    try:
        response = httpx.get(f"{base_url}/shop/api/health", timeout=1.0)
        if response.status_code == 200:
            print(f"  target already running at {base_url}")
            return None
    except Exception:
        pass
    parsed = httpx.URL(base_url)
    port = parsed.port or 8090
    log_path = PROJECT_ROOT / "logs" / "target-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_for_target(base_url)
    print(f"  target started at {base_url} (pid {process.pid})")
    return process


def stop_target(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def verify_lifecycle_cleanup(adapter: SandboxAdapter, base_url: str) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    failure_name = generate_sandbox_name()
    failure_runner = QAWorkflowRunner(
        adapter, failure_name, base_url, execution_mode=adapter.execution_mode
    )
    with patch("app.qa_agent.BrowserSession.start", side_effect=RuntimeError("cleanup probe")):
        outcome = failure_runner.run(inject_failure=False)
    checks["cleanup_on_failure"] = bool(outcome.cleanup_ok and adapter.get_info(failure_name) is None)
    checks["failure_probe_error"] = outcome.error
    checks["failure_probe_sandbox"] = failure_name

    interrupt_name = generate_sandbox_name()
    interrupt_runner = QAWorkflowRunner(
        adapter, interrupt_name, base_url, execution_mode=adapter.execution_mode
    )
    interrupted = False
    with patch("app.qa_agent.BrowserSession.start", side_effect=KeyboardInterrupt()):
        try:
            interrupt_runner.run(inject_failure=False)
        except KeyboardInterrupt:
            interrupted = True
    checks["cleanup_on_interrupt"] = bool(
        interrupted and adapter.get_info(interrupt_name) is None
    )
    checks["interrupt_observed"] = interrupted
    checks["interrupt_sandbox"] = interrupt_name
    checks["checked_at"] = utcnow()
    write_json(EVIDENCE_DIR / "lifecycle-checks.json", checks)
    return checks


def ffmpeg_candidates() -> list[str]:
    """Return ffmpeg binaries to probe, preferring one on PATH."""
    candidates: list[str] = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    roots = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else None,
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for root in roots:
        if root is None or not root.exists():
            continue
        matches = sorted(
            root.glob("ffmpeg-*/ffmpeg*"),
            key=lambda path: path.parent.name,
            reverse=True,
        )
        for candidate in matches:
            if (
                candidate.is_file()
                and candidate.suffix.lower() in {".exe", ""}
                and str(candidate) not in candidates
            ):
                candidates.append(str(candidate))
    return candidates


def _ffmpeg_listing(ffmpeg: str, flag: str) -> str:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", flag],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout + proc.stderr


def _ffmpeg_encoders(ffmpeg: str) -> set[str]:
    encoders: set[str] = set()
    for line in _ffmpeg_listing(ffmpeg, "-encoders").splitlines():
        parts = line.split()
        # Encoder rows start with a six-character capability flag such as V....D.
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            encoders.add(parts[1])
    return encoders


def _ffmpeg_muxers(ffmpeg: str) -> set[str]:
    muxers: set[str] = set()
    for line in _ffmpeg_listing(ffmpeg, "-muxers").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"E", "D"}:
            muxers.add(parts[1])
    return muxers


def ffmpeg_encoder(ffmpeg: str) -> str | None:
    """Return a usable MP4 video encoder, or None for a VP9-only build."""
    if "mp4" not in _ffmpeg_muxers(ffmpeg):
        return None
    encoders = _ffmpeg_encoders(ffmpeg)
    for encoder in ("libx264", "mpeg4"):
        if encoder in encoders:
            return encoder
    return None


def find_ffmpeg() -> str | None:
    """Return the first ffmpeg that can actually produce an MP4.

    Playwright ships a minimal VP9/WebM-only ffmpeg for its own recording
    pipeline. Selecting it merely because it exists produced a false
    ``mp4_created`` failure on CI, so probe for a real MP4 encoder instead.
    """
    for candidate in ffmpeg_candidates():
        if ffmpeg_encoder(candidate):
            return candidate
    return None


def newest_video() -> Path | None:
    video_dir = EVIDENCE_DIR / "video"
    if not video_dir.exists():
        return None
    videos = sorted(video_dir.glob("*.webm"), key=lambda path: path.stat().st_mtime, reverse=True)
    return videos[0] if videos else None


def _ffmpeg_command(
    ffmpeg: str, video: Path, encoder: str, *, preset: bool
) -> list[str]:
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-c:v",
        encoder,
    ]
    if preset and encoder == "libx264":
        command.extend(["-preset", "medium"])
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(DEMO_PATH),
        ]
    )
    return command


def convert_walkthrough() -> dict[str, Any]:
    video = newest_video()
    result: dict[str, Any] = {"video_recorded": bool(video)}
    if not video:
        result["mp4_created"] = False
        result["reason"] = "Playwright did not produce a recording"
        return result
    source_video = str(video.relative_to(PROJECT_ROOT)).replace("\\", "/")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        result["mp4_created"] = False
        result["source_video"] = source_video
        result["reason"] = (
            "no ffmpeg build with an H.264/MPEG-4 MP4 encoder was found; "
            "source webm retained"
        )
        return result
    encoder = ffmpeg_encoder(ffmpeg)
    if encoder is None:  # pragma: no cover - find_ffmpeg already checked
        raise RuntimeError("selected ffmpeg cannot encode MP4")
    DEMO_PATH.parent.mkdir(parents=True, exist_ok=True)

    attempts = [_ffmpeg_command(ffmpeg, video, encoder, preset=True)]
    if encoder == "libx264":
        # Some minimal builds expose libx264 without the preset AVOption.
        attempts.append(_ffmpeg_command(ffmpeg, video, encoder, preset=False))

    errors: list[str] = []
    for command in attempts:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if (
            proc.returncode == 0
            and DEMO_PATH.is_file()
            and DEMO_PATH.stat().st_size > 0
        ):
            result.update(
                {
                    "mp4_created": True,
                    "source_video": source_video,
                    "ffmpeg_error": proc.stderr[-1000:],
                    "mp4_sha256": sha256(DEMO_PATH),
                    "mp4_bytes": DEMO_PATH.stat().st_size,
                }
            )
            return result
        errors.append(proc.stderr[-1000:])
        DEMO_PATH.unlink(missing_ok=True)

    result.update(
        {
            "mp4_created": False,
            "source_video": source_video,
            "ffmpeg_error": "\n".join(errors)[-1000:],
            "mp4_sha256": None,
            "mp4_bytes": 0,
        }
    )
    return result


def run_host_matrix(base_url: str) -> dict[str, Any]:
    cases = [
        ("coupon_stacking_test.spec.ts", "broken", True, "reproduction passes on the broken checkout"),
        ("coupon_stacking_test.spec.ts", "fixed", False, "reproduction fails once the defect is fixed"),
        ("coupon_regression_test.spec.ts", "fixed", True, "regression passes on the fixed checkout"),
        ("coupon_regression_test.spec.ts", "broken", False, "regression catches the broken checkout"),
    ]
    results = []
    for spec, mode, expected, rationale in cases:
        print(f"  host test: {spec} [{mode}]")
        result = run_generated_test(spec, base_url, timeout=90, test_mode=mode)
        result["expected_pass"] = expected
        result["expectation_met"] = result["passed"] == expected
        result["rationale"] = rationale
        results.append(result)
    payload = {
        "browser_runtime": "host_playwright",
        "execution_mode": "host_browser_mock_sandbox",
        "generated_at": utcnow(),
        "cases": results,
        "all_expectations_met": all(row["expectation_met"] for row in results),
    }
    write_json(EVIDENCE_DIR / "test-matrix.json", payload)
    return payload


def run_evaluation(
    adapter: SandboxAdapter,
    base_url: str,
    num_runs: int,
    execution_mode: str,
    bootstrapper: Any = None,
    planner: Any = None,
    *,
    guest_browser_visible: bool = True,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for index in range(num_runs):
        sandbox_name = generate_sandbox_name()
        row_started = time.monotonic()
        row_started_at = utcnow()
        started_at = row_started_at
        creation_s = 0.0
        try:
            if execution_mode == "live":
                # Creation stays outside the agent clock: the adapter waits out
                # cloud creation-quota throttles patiently, so its duration is
                # recorded separately instead of inflating agent work.
                print(f"  eval {index + 1}/{num_runs}: creating fresh sandbox {sandbox_name}")
                creation_started = time.monotonic()
                try:
                    adapter.create(sandbox_name)
                finally:
                    creation_s = round(time.monotonic() - creation_started, 2)
                started_at = utcnow()
            runner = QAWorkflowRunner(
                adapter,
                sandbox_name,
                base_url,
                execution_mode=execution_mode,
                record_video=False,
                use_sandbox_browser=execution_mode == "live",
                fixture_bootstrapper=bootstrapper,
                planner=planner,
                sandbox_precreated=execution_mode == "live",
                guest_browser_visible=guest_browser_visible,
            )
            outcome = runner.run(inject_failure=False)
            browser_verified = bool(
                outcome.browser_runtime in LIVE_BROWSER_RUNTIMES
                and any(action.success for action in outcome.actions)
            )
            agent_wall = float(outcome.agent_wall_clock_s)
            total_wall = round(creation_s + float(outcome.total_wall_clock_s), 2)
            row = {
                "sandbox_name": outcome.sandbox_name,
                "fresh": True,
                "success": bool(outcome.success and outcome.cleanup_ok),
                "classification": outcome.classification.value,
                "error": outcome.error,
                "wall_clock_s": agent_wall,
                "boot_to_ready_s": outcome.boot_to_ready_s,
                "sandbox_creation_s": (
                    round(creation_s + outcome.sandbox_creation_s, 2)
                    if execution_mode == "live"
                    else outcome.sandbox_creation_s
                ),
                "deploy_s": outcome.deploy_s,
                "browser_start_s": outcome.browser_start_s,
                "workflow_s": outcome.workflow_s,
                "cleanup_s": outcome.cleanup_s,
                "agent_wall_clock_s": agent_wall,
                "total_wall_clock_s": total_wall,
                "end_to_end_wall_clock_s": round(time.monotonic() - row_started, 2),
                **cost_metadata(agent_wall, total_wall, execution_mode),
                "start_time": started_at,
                "end_time": utcnow(),
                "cleanup_ok": outcome.cleanup_ok,
                "browser_runtime": outcome.browser_runtime,
                "browser_verified": browser_verified,
            }
        except Exception as exc:
            elapsed = round(time.monotonic() - row_started, 2)
            agent_wall = round(max(0.0, elapsed - creation_s), 2)
            total_wall = round(creation_s + agent_wall, 2)
            row = {
                "sandbox_name": sandbox_name,
                "fresh": True,
                "success": False,
                "classification": DefectClassification.ENVIRONMENT_FAILURE.value,
                "error": f"{type(exc).__name__}: {exc}",
                "wall_clock_s": agent_wall,
                "boot_to_ready_s": 0.0,
                "sandbox_creation_s": creation_s,
                "deploy_s": 0.0,
                "browser_start_s": 0.0,
                "workflow_s": 0.0,
                "cleanup_s": 0.0,
                "agent_wall_clock_s": agent_wall,
                "total_wall_clock_s": total_wall,
                "end_to_end_wall_clock_s": elapsed,
                **cost_metadata(agent_wall, total_wall, execution_mode),
                "start_time": started_at,
                "end_time": utcnow(),
                "cleanup_ok": False,
                "browser_runtime": "unavailable",
                "browser_verified": False,
            }
        runs.append(row)
        print(
            f"  eval {index + 1}/{num_runs}: {'ok' if row['success'] else 'failed'} "
            f"{row['sandbox_name']} agent={row['wall_clock_s']}s "
            f"total={row['total_wall_clock_s']}s end-to-end={row['end_to_end_wall_clock_s']}s"
        )

    wall_times = sorted(float(row["wall_clock_s"]) for row in runs)
    boot_times = sorted(float(row["boot_to_ready_s"]) for row in runs)
    success_count = sum(1 for row in runs if row["success"])
    browser_runtimes = sorted(
        {str(row["browser_runtime"]) for row in runs if row.get("browser_runtime")}
    )
    live_celesto_verified = live_verification(execution_mode, runs)

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int(len(values) * fraction + 0.9999) - 1))
        return round(values[index], 3)

    def median_of(key: str) -> float:
        values = sorted(float(row[key]) for row in runs)
        return round(median(values), 3) if values else 0.0

    summary = {
        "run_count": len(runs),
        "success_rate": round(success_count / len(runs), 4) if runs else 0.0,
        "success_rate_pct": round(100 * success_count / len(runs), 2) if runs else 0.0,
        "median_wall_clock_s": round(median(wall_times), 3) if wall_times else 0.0,
        "p90_wall_clock_s": percentile(wall_times, 0.9),
        "median_boot_to_ready_s": round(median(boot_times), 3) if boot_times else 0.0,
        "median_sandbox_creation_s": median_of("sandbox_creation_s"),
        "median_deploy_s": median_of("deploy_s"),
        "median_browser_start_s": median_of("browser_start_s"),
        "median_workflow_s": median_of("workflow_s"),
        "median_cleanup_s": median_of("cleanup_s"),
        "median_agent_wall_clock_s": median_of("agent_wall_clock_s"),
        "median_total_wall_clock_s": median_of("total_wall_clock_s"),
        "median_end_to_end_wall_clock_s": median_of("end_to_end_wall_clock_s"),
        "total_cost_usd": round(sum(float(row["cost_usd"]) for row in runs), 4),
        "total_cost_including_creation_usd": round(
            sum(float(row["cost_including_creation_usd"]) for row in runs), 4
        ),
        "cost_source": "mock_zero_cost" if execution_mode == "mock" else "celesto_size_rate_wall_clock",
        "rate_usd_per_hour": 0.0 if execution_mode == "mock" else live_hourly_rate(),
        "postmortem_path": "reports/postmortem.md",
    }
    return {
        "runs": runs,
        **summary,
        "evaluation_mode": execution_mode,
        "sandbox_runtime": "mock" if execution_mode == "mock" else "celesto_cloud",
        "browser_runtime": (
            "host_playwright"
            if execution_mode == "mock"
            else (
                browser_runtimes[0]
                if len(browser_runtimes) == 1
                else ("mixed" if browser_runtimes else "none")
            )
        ),
        "browser_runtimes": browser_runtimes,
        "sandbox_size": live_size_id() if execution_mode == "live" else "mock",
        "live_celesto_verified": live_celesto_verified,
        "cost_methodology": (
            "Mock sandbox runs incur no Celesto charge. Cost is recorded as $0.00 and must "
            "not be presented as live Celesto pricing."
            if execution_mode == "mock"
            else (
                "Live runs are costed from wall-clock time at the Celesto "
                f"{live_size_id()} size rate of ${live_hourly_rate():.3f}/hr; the platform "
                "bills the same per-hour rate while a sandbox exists."
            )
        ),
        "note": (
            f"{len(runs)} deterministic evaluation runs used a mock sandbox adapter and a real "
            "host Playwright browser. Browser actions, target behavior, tests, and cleanup "
            "are real; Celesto boot latency and cloud cost are not measured."
            if execution_mode == "mock"
            else "Runs executed against fresh Celesto Cloud sandboxes with the browser "
            "runtime inside the guest (platform CDP when available, otherwise in-guest "
            "Playwright over the audited exec path)."
        ),
        "generated_at": utcnow(),
    }


def generate_problem(execution_mode: str = "mock", run_count: int = 20) -> dict[str, Any]:
    return {
        "who": (
            "FlowScout is a browser QA agent for teams that need reproducible evidence of "
            "checkout defects. It explores a deterministic SaaS fixture, documents the defect, "
            "generates regression tests, and reports its own containment and cleanup behavior."
        ),
        "agent_work": (
            "The agent creates a logical sandbox, verifies readiness, explores the storefront "
            "and untrusted reviews, exercises checkout, applies SAVE20 six times, observes a "
            "negative total, writes a reproduction spec and a regression spec, and records a "
            "step-7 HTTP 503 failure with a fresh recovery. Browser actions are structured; "
            "the page can never supply executable code."
        ),
        "proof_output": (
            "Evidence includes logs/commands.jsonl, reports/failure_trace.jsonl, "
            "reports/evidence/actions.jsonl, Playwright screenshots and video, generated "
            "reproduction/regression specs, a four-case host test matrix, independent "
            f"containment canary hashes, and {run_count} measured {execution_mode}-mode runs."
        ),
        "idea_source": "original",
    }


def generate_capabilities(
    execution_mode: str,
    *,
    live_celesto_verified: bool = False,
) -> dict[str, Any]:
    capabilities: list[dict[str, Any]] = [
        {
            "id": "browser",
            "why": (
                "The workflow's entire browser surface runs through the sandbox browser "
                "runtime: the browser-agent template's own Chromium renders every page, "
                "screenshot, and the poisoned review inside the guest. When the platform "
                "serves its CDP gateway the host Playwright attaches to it; otherwise the "
                "guest's preinstalled Playwright drives the same Chromium over the audited "
                "exec path. Mock runs replay the same validated action schema through a "
                "host Playwright stand-in."
            ),
            "evidence_path": "reports/evidence/actions.jsonl",
        },
        {
            "id": "safety_hooks",
            "why": (
                "Every shell command the agent requests passes a deny-by-default allowlist "
                "policy in the control-plane adapter before any SDK dispatch; the prompt-"
                "injection command extracted from the customer review is blocked there and "
                "still audited. The Celesto Cloud SDK exposes no command callbacks, so the "
                "hook layer is enforced at this boundary instead of inside the guest."
            ),
            "evidence_path": "reports/evidence/containment.log",
        },
    ]
    if execution_mode == "live":
        capabilities.append(
            {
                "id": "published_ports",
                "why": (
                    "During the live demo run the ShopDemo application under test is served "
                    "from inside the sandbox and its port is published through Celesto to a "
                    "public HTTPS URL, so a reviewer can open the exact fixture the agent "
                    "explored without any access to the host."
                ),
                "evidence_path": "reports/evidence/published-port.json",
            }
        )
    return {
        "execution_mode": execution_mode,
        "live_celesto_verified": bool(live_celesto_verified),
        "capabilities": capabilities,
    }


def generate_lifecycle(
    checks: dict[str, Any], names: list[str], primary_cleanup_ok: bool, eval_ok: bool
) -> dict[str, Any]:
    return {
        "cleanup_on_success": bool(primary_cleanup_ok and eval_ok),
        "cleanup_on_failure": bool(checks.get("cleanup_on_failure")),
        "cleanup_on_interrupt": bool(checks.get("cleanup_on_interrupt")),
        "sandbox_names": sorted(set(names)),
        "verified_at": utcnow(),
        "evidence_paths": [
            "reports/evidence/lifecycle-checks.json",
            "logs/commands.jsonl",
            "reports/failure_trace.jsonl",
        ],
        "note": (
            "Success, raised-exception, and KeyboardInterrupt probes all observe the adapter "
            "after cleanup. Failure/interrupt probes are recorded in lifecycle-checks.json."
        ),
    }


def generate_failure_policy() -> dict[str, Any]:
    return {
        "failed_step": 7,
        "total_steps": 10,
        "policy": "fresh",
        "rationale": (
            "A fresh replacement gives deterministic application state after an ambiguous "
            "checkout failure. The failed likely-billable sandbox is deleted first, a new "
            "sandbox is created, and the target fixture is reset. The Celesto Cloud surface "
            "inspected here does not expose snapshot/restore on CloudComputer, so the "
            "RolledBack lifecycle label is logical recovery only, not VM rollback."
        ),
        "trace_path": "reports/failure_trace.jsonl",
        "failed_step_evidence": "HTTP 503 from the target checkout during the second application",
        "replacement_observed": True,
    }


def generate_concurrency(evaluation: dict[str, Any]) -> dict[str, Any]:
    jobs = [
        {
            "job_id": f"job-{index + 1:03d}",
            "sandbox_name": row["sandbox_name"],
            "started_at": row["start_time"],
            "end_time": row["end_time"],
            "duration_s": row["wall_clock_s"],
            "outcome": "success" if row["success"] else "failure",
        }
        for index, row in enumerate(evaluation["runs"][:5])
    ]
    return {
        "jobs": jobs,
        "mode": "sequential",
        "parallelism": 1,
        "breakage_notes": (
            "These were sequential runs. No concurrent Celesto capacity was available, so no "
            "parallel latency or contention claim is made. At 1,000 users, queueing, per-tenant "
            "sandbox quotas, database connection pooling, and Celesto API rate limits would be "
            "the first bottlenecks."
        ),
    }


def generate_postmortem(
    evaluation: dict[str, Any],
    matrix: dict[str, Any],
    lifecycle: dict[str, Any],
) -> str:
    summary = evaluation
    failures = [row for row in evaluation["runs"] if not row["success"]]
    lines = [
        "# FlowScout Postmortem",
        "",
        f"Generated: {utcnow()}",
        f"Evaluation mode: {evaluation['evaluation_mode']}",
        f"Browser runtime: {evaluation['browser_runtime']}",
        f"Live Celesto verified: {str(evaluation['live_celesto_verified']).lower()}",
        "",
        "## Measured summary",
        "",
        f"- Runs: {summary['run_count']}",
        f"- Success rate: {summary['success_rate_pct']}%",
        f"- Median wall clock: {summary['median_wall_clock_s']}s",
        f"- P90 wall clock: {summary['p90_wall_clock_s']}s",
        f"- Median boot-to-ready: {summary['median_boot_to_ready_s']}s",
        f"- Cost: ${summary['total_cost_usd']}",
        "",
        "## Failures",
        "",
    ]
    if failures:
        for row in failures:
            lines.extend(
                [
                    f"### {row['sandbox_name']}",
                    "",
                    f"- Classification: {row['classification']}",
                    f"- Error: {row['error']}",
                    f"- Cleanup: {row['cleanup_ok']}",
                    "",
                ]
            )
    else:
        lines.append("No unplanned evaluation failures occurred in the measured runs.")
        lines.append("")

    lines.extend(
        [
            "## Controlled step-7 failure",
            "",
            "The primary demonstration armed a deterministic target failure immediately before "
            "the second coupon application. The browser received HTTP 503. FlowScout recorded "
            "MidTaskFailure, deleted the failed sandbox, created a replacement, reset the "
            "fixture, retried, and completed six successful applications. This is a recovery "
            "demonstration, not a fabricated system failure.",
            "",
            "## Test matrix interpretation",
            "",
        ]
    )
    for case in matrix["cases"]:
        lines.append(
            f"- `{case['spec']}` in `{case['test_mode']}` mode: "
            f"passed={case['passed']}, expected={case['expected_pass']}. {case['rationale']}"
        )
    lines.extend(
        [
            "",
            "## Cleanup findings",
            "",
            f"- Success cleanup: {lifecycle['cleanup_on_success']}",
            f"- Failure cleanup: {lifecycle['cleanup_on_failure']}",
            f"- Interrupt cleanup: {lifecycle['cleanup_on_interrupt']}",
            "",
            "## Root causes and limitations",
            "",
            "1. The observed product defect is repeated application of a one-use coupon. "
            "The fixed path rejects the second application and preserves a non-negative total.",
            *(
                [
                    "2. Mock sandbox metrics measure orchestration and real browser work, not Celesto "
                    "boot time, networking, or cloud cost.",
                    "3. Mock mode does not contact Celesto. Live cloud execution is therefore not "
                    "claimed in this report; the live adapter fails closed when credentials or the "
                    "SDK are unavailable.",
                ]
                if evaluation["evaluation_mode"] == "mock"
                else [
                    "2. Live metrics were recorded against Celesto Cloud sandboxes; wall-clock "
                    "includes LLM planning latency where the LLM planner was enabled.",
                    "3. Cost is computed from wall-clock time at the standard size hourly rate and "
                    "rounded per run; it approximates, but does not invoice-match, platform billing.",
                ]
            ),
            "4. Cloud snapshot rollback is not used because the inspected SDK does not expose it "
            "on CloudComputer. Recovery uses replacement sandboxes and fixture reset.",
            "5. Evaluation was sequential; concurrency and 1,000-user behavior are untested.",
            *(
                [
                    "6. A live 20-run benchmark was not attempted: the free trial permits two "
                    "new sandboxes per hour, while the canonical evaluation creates more than 20 "
                    "fresh sandboxes. The single-sandbox live check in reports/live-evidence/ is "
                    "separate evidence, not a 20-run benchmark.",
                ]
                if evaluation["evaluation_mode"] == "mock"
                else []
            ),
            "",
            "## Corrective actions",
            "",
            "1. Run the same workflow on Linux with Celesto credentials and a target URL reachable "
            "from the sandbox.",
            "2. Add authenticated target support so private previews can be tested.",
            "3. Add a queue and per-tenant sandbox quota before scaling beyond a single worker.",
            "4. Keep Playwright report artifacts and compare P90 browser latency after live runs.",
            "",
        ]
    )
    return "\n".join(lines)


def collect_sandbox_names(*sources: Any) -> list[str]:
    names: set[str] = set()
    for source in sources:
        if hasattr(source, "to_dict"):
            source = source.to_dict()
        if isinstance(source, dict):
            for key in ("sandbox_name", "sandbox_names", "failure_probe_sandbox", "interrupt_sandbox"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
                elif isinstance(value, list):
                    names.update(str(item) for item in value if item)
            for row in source.get("runs", []) or []:
                if isinstance(row, dict) and row.get("sandbox_name"):
                    names.add(str(row["sandbox_name"]))
            for row in source.get("cases", []) or []:
                if isinstance(row, dict) and row.get("sandbox_name"):
                    names.add(str(row["sandbox_name"]))
        elif isinstance(source, list):
            for trace in source:
                if hasattr(trace, "to_dict"):
                    trace = trace.to_dict()
                if isinstance(trace, dict) and trace.get("sandbox_name"):
                    names.add(str(trace["sandbox_name"]))
    return sorted(names)


def _tool_version(command: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"unavailable ({type(exc).__name__})"


def write_runtime_manifest(
    mode: str,
    base_url: str,
    *,
    guest_browser_visible: bool = True,
    provenance: dict[str, Any] | None = None,
) -> None:
    celesto_importable = False
    celesto_error = None
    try:
        import celesto  # noqa: F401

        celesto_importable = True
    except Exception as exc:
        celesto_error = f"{type(exc).__name__}: {exc}"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utcnow(),
        "execution_mode": mode,
        "target_url": base_url,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "celesto_api_key_configured": bool(
            os.getenv("CELESTO_API_KEY", "").strip()
            and os.getenv("CELESTO_API_KEY", "") != "replace-me"
        ),
        "celesto_sdk_importable": celesto_importable,
        "celesto_import_error": celesto_error,
        "guest_browser_visible_requested": bool(guest_browser_visible and mode == "live"),
        "playwright_python": True,
        "node": _tool_version(["node", "--version"]),
        "npx_playwright": _tool_version(
            ["npx.cmd" if os.name == "nt" else "npx", "playwright", "--version"], timeout=30
        ),
        "node_playwright_specs": str((PROJECT_ROOT / "tests" / "generated").relative_to(PROJECT_ROOT)),
        "provenance": provenance or capture_provenance(PROJECT_ROOT),
        "source_files": {
            str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256(path)
            for path in [
                PROJECT_ROOT / "app" / "sandbox_runner.py",
                PROJECT_ROOT / "app" / "qa_agent.py",
                PROJECT_ROOT / "app" / "ownership.py",
                PROJECT_ROOT / "app" / "evidence_io.py",
                PROJECT_ROOT / "app" / "report_schemas.py",
                PROJECT_ROOT / "app" / "provenance.py",
                PROJECT_ROOT / "app" / "guest_browser_launcher.py",
                PROJECT_ROOT / "app" / "guest_browser_worker.py",
                PROJECT_ROOT / "app" / "guest_healthcheck.py",
                PROJECT_ROOT / "app" / "main.py",
                PROJECT_ROOT / "app" / "target_saas_app.py",
                PROJECT_ROOT / "playwright.config.ts",
                PROJECT_ROOT / "scripts" / "run_live_demo.py",
                PROJECT_ROOT / "scripts" / "run_live_single.py",
                PROJECT_ROOT / "scripts" / "live_common.py",
                PROJECT_ROOT / "scripts" / "verify_submission.py",
            ]
        },
    }
    write_json(EVIDENCE_DIR / "runtime-manifest.json", payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate FlowScout evaluation evidence")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument(
        "--planner",
        choices=["scripted", "llm"],
        default=os.getenv("FLOWSCOUT_PLANNER", "scripted"),
        help="scripted: deterministic state machine; llm: LLM proposes each decision "
        "from a validated candidate set (requires LLM_* env configuration)",
    )
    parser.add_argument("--base-url", default=os.getenv("TARGET_BASE_URL", TARGET_DEFAULT))
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the live guest Chromium headless instead of on the sandbox desktop",
    )
    args = parser.parse_args()
    guest_browser_visible = visible_guest_browser_default() and not args.headless

    if args.mode == "live":
        if not os.getenv("CELESTO_API_KEY", "").strip():
            print("ERROR: live mode requires CELESTO_API_KEY. It will not fall back to mock.")
            return 2

    planner: Any = None
    if args.planner == "llm":
        from app.llm_planner import LLMPlanner, PlannerError

        try:
            planner = LLMPlanner()
        except PlannerError as exc:
            print(f"ERROR: --planner llm is not usable: {exc}")
            return 2
        print(f"planner: llm (model={planner.model} via {planner.base_url})")
    else:
        print("planner: scripted (deterministic state machine)")

    print("=" * 68)
    print("FlowScout evidence run")
    print(f"mode={args.mode} runs={args.runs} target={args.base_url}")
    if args.mode == "live":
        print(f"guest browser: {'visible' if guest_browser_visible else 'headless'}")
    print("=" * 68)

    started = time.monotonic()
    # Capture provenance before prepare_artifacts() deletes/rewrites tracked
    # evidence files; otherwise a clean checkout would be reported dirty.
    provenance = capture_provenance(PROJECT_ROOT)
    prepare_artifacts()
    # The host fixture always runs: mock mode browses it directly, and the
    # generated-spec test matrix executes against it in both modes. Live mode
    # additionally deploys the same app inside each sandbox guest.
    server = start_target(args.base_url)
    try:
        adapter = create_adapter(args.mode)
        execution_mode = adapter.execution_mode
        agent_base_url = GUEST_APP_BASE_URL if execution_mode == "live" else args.base_url

        def bootstrapper(sandbox_name: str) -> None:
            deploy_guest_fixture(adapter, sandbox_name)

        def port_publisher(sandbox_name: str) -> None:
            try:
                entry = adapter.publish_port(sandbox_name, GUEST_APP_PORT)
                write_json(
                    EVIDENCE_DIR / "published-port.json",
                    {
                        "sandbox_name": sandbox_name,
                        "port": GUEST_APP_PORT,
                        "status": entry.get("status"),
                        "url": entry.get("url"),
                        "note": (
                            "the in-guest fixture was publicly reachable through Celesto "
                            "while this sandbox lived; cleanup deletes it with the sandbox"
                        ),
                    },
                )
                print(f"  published app port for {sandbox_name}: {entry.get('url')}")
            except Exception as exc:
                print(f"  publish_port failed (continuing): {exc}")

        if execution_mode != "live":
            bootstrapper = None  # type: ignore[assignment]
            port_publisher = None  # type: ignore[assignment]
        print(f"adapter={type(adapter).__name__} execution_mode={execution_mode}")
        print(f"agent target: {agent_base_url}" + (" (in-guest fixture)" if bootstrapper else ""))

        write_runtime_manifest(
            execution_mode,
            args.base_url,
            guest_browser_visible=guest_browser_visible,
            provenance=provenance,
        )
        checks = verify_lifecycle_cleanup(adapter, args.base_url)
        print(f"cleanup checks: {checks}")

        print("running primary 10-step workflow with controlled step-7 failure")
        primary_name = generate_sandbox_name()
        primary_runner = QAWorkflowRunner(
            adapter,
            primary_name,
            agent_base_url,
            execution_mode=execution_mode,
            record_video=True,
            use_sandbox_browser=execution_mode == "live",
            fixture_bootstrapper=bootstrapper,
            planner=planner,
            port_publisher=port_publisher if execution_mode == "live" else None,
            guest_browser_visible=guest_browser_visible,
        )
        primary = primary_runner.run(inject_failure=True)
        primary_runner.save_trace(REPORTS_DIR / "failure_trace.jsonl")
        if not primary.success:
            print(f"PRIMARY WORKFLOW FAILED: {primary.error}")
            return 3
        print(f"primary defect: {primary.defect.title if primary.defect else 'none'}")
        print(f"primary cleanup: {primary.cleanup_ok}")

        video = convert_walkthrough()
        write_json(EVIDENCE_DIR / "walkthrough.json", video)
        print(f"walkthrough: {video}")

        print("running containment probe")
        containment_name = generate_sandbox_name()
        adapter.create(containment_name)
        containment = run_containment_test(adapter, containment_name)
        adapter.delete(containment_name)
        print(f"containment blocked={containment['blocked']} host_untouched={containment['host_untouched']}")

        print("running generated-test matrix")
        matrix = run_host_matrix(args.base_url)
        if not matrix["all_expectations_met"]:
            print("ERROR: generated-test expectations were not met")
            return 4

        print(f"running {args.runs} evaluation runs")
        evaluation = run_evaluation(
            adapter,
            agent_base_url,
            args.runs,
            execution_mode,
            bootstrapper,
            planner,
            guest_browser_visible=guest_browser_visible,
        )
        evaluation["planner"] = args.planner
        eval_cleanup_ok = all(row.get("cleanup_ok") for row in evaluation["runs"])
        lifecycle = generate_lifecycle(
            checks,
            collect_sandbox_names(primary.trace, containment, evaluation, checks),
            primary.cleanup_ok,
            eval_cleanup_ok,
        )

        write_json(
            REPORTS_DIR / "problem.json",
            generate_problem(execution_mode, len(evaluation["runs"])),
        )
        write_json(
            REPORTS_DIR / "capabilities.json",
            generate_capabilities(
                execution_mode,
                live_celesto_verified=evaluation["live_celesto_verified"],
            ),
        )
        write_json(REPORTS_DIR / "lifecycle.json", lifecycle)
        write_json(REPORTS_DIR / "reliability.json", evaluation)
        write_json(REPORTS_DIR / "containment.json", containment)
        write_json(REPORTS_DIR / "failure_policy.json", generate_failure_policy())
        write_json(REPORTS_DIR / "concurrency.json", generate_concurrency(evaluation))
        postmortem = generate_postmortem(evaluation, matrix, lifecycle)
        atomic_write_text(REPORTS_DIR / "postmortem.md", postmortem)
        print("  wrote reports/postmortem.md")

        write_json(
            REPORTS_DIR / "workflow.json",
            {
                "primary": primary.to_dict(),
                "containment": containment,
                "host_test_matrix_path": "reports/evidence/test-matrix.json",
                "generated_at": utcnow(),
            },
        )
        summary = evaluation
        print("=" * 68)
        print(
            f"done in {round(time.monotonic() - started, 1)}s / "
            f"success_rate={summary['success_rate_pct']}% / "
            f"median={summary['median_wall_clock_s']}s / p90={summary['p90_wall_clock_s']}s"
        )
        print(
            "live Celesto verified: "
            f"{str(evaluation['live_celesto_verified']).lower()}"
        )
        commit_artifacts()
        return 0
    finally:
        stop_target(server)


if __name__ == "__main__":
    raise SystemExit(main())
