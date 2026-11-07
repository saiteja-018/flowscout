#!/usr/bin/env python3
"""Run one real Celesto sandbox workflow and archive reproducible evidence.

The default run creates one Micro sandbox, deploys ShopDemo inside the guest,
executes the ten-step workflow with the guest browser, deletes the sandbox, and
writes a ``record.json`` plus actions, commands, trace, and screenshots under
``reports/live-evidence/runs/<run_id>/``.

``--inject-failure`` exercises the controlled step-7 HTTP 503 and the fresh
sandbox recovery path, writing a ``LiveRecoveryRecord`` instead.

The canonical index is only updated with ``--promote``; promotion refuses a
dirty tree and runs the submission verifier before keeping the change.

Usage:
    python scripts/run_live_single.py
    python scripts/run_live_single.py --inject-failure --promote
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

import app.qa_agent as qa  # noqa: E402
from app.evidence_io import atomic_write_bytes, atomic_write_json  # noqa: E402
from app.provenance import capture_provenance, utcnow  # noqa: E402
from app.qa_agent import QAWorkflowRunner, generate_sandbox_name  # noqa: E402
from app.report_schemas import (  # noqa: E402
    SCHEMA_VERSION,
    LiveEvidenceBundle,
    LiveEvidenceFile,
    LiveOutcomeSummary,
    LiveRecoveryEvent,
    LiveRecoveryFailure,
    LiveRecoveryRecord,
    LiveRecoveryRetry,
    LiveSandboxSummary,
    LiveScreenshot,
    LiveVisibleRecord,
    verify_submission,
)
from app.sandbox_runner import create_adapter  # noqa: E402
from scripts.live_common import (  # noqa: E402
    GUEST_APP_BASE_URL,
    deploy_guest_fixture,
    file_rows,
    live_size_id,
    sha256,
    visible_guest_browser_default,
)

LIVE_EVIDENCE_DIR = PROJECT_ROOT / "reports" / "live-evidence"
DEFAULT_RUNS_DIR = LIVE_EVIDENCE_DIR / "runs"
RECOVERY_EVENT_NAMES = {
    "failure",
    "policy_applied",
    "sandbox_recreated",
    "step_start",
    "state_transition",
    "step_complete",
}
REQUIRED_RECOVERY_EVENTS = (
    "failure",
    "policy_applied",
    "sandbox_recreated",
    "step_complete",
)


def default_run_id(inject_failure: bool, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    prefix = "live-recovery" if inject_failure else "live-single"
    return f"{prefix}-{stamp}"


def relative_path(path: Path) -> str:
    path = Path(path).resolve()
    try:
        return path.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evidence_file(path: Path, note: str = "") -> LiveEvidenceFile:
    return LiveEvidenceFile(
        path=relative_path(path),
        sha256=sha256(path),
        bytes=path.stat().st_size,
        rows=file_rows(path),
        note=note,
    )


def collect_screenshots(evidence_dir: Path) -> list[LiveScreenshot]:
    shots = sorted(evidence_dir.glob("step-*.jpg"))
    if not shots:
        raise RuntimeError("the workflow produced no screenshots")
    result: list[LiveScreenshot] = []
    for path in shots:
        with Image.open(path) as image:
            width, height = image.size
        result.append(
            LiveScreenshot(
                archived_path=relative_path(path),
                sha256=sha256(path),
                width=width,
                height=height,
                note=f"Live screenshot captured by the guest browser: {path.name}",
            )
        )
    return result


def first_event(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((row for row in rows if row.get("event") == name), None)


def event_index(rows: list[dict[str, Any]], name: str) -> int | None:
    for index, row in enumerate(rows):
        if row.get("event") == name:
            return index
    return None


def delete_row(
    command_rows: list[dict[str, Any]], sandbox_name: str
) -> dict[str, Any] | None:
    rows = [
        row
        for row in command_rows
        if row.get("sandbox_name") == sandbox_name
        and str(row.get("command", "")).startswith("delete sandbox")
    ]
    return rows[-1] if rows else None


def delete_confirmation(row: dict[str, Any] | None) -> str:
    if row is None:
        return ""
    detail = str(row.get("detail") or "")
    return detail if "delete confirmed" in detail.lower() else ""


def boot_seconds_for(
    command_rows: list[dict[str, Any]], sandbox_name: str
) -> float | None:
    """Read the provider-reported boot time from the audited create row."""
    for row in command_rows:
        if (
            row.get("sandbox_name") == sandbox_name
            and str(row.get("command", "")).startswith("create sandbox")
        ):
            match = re.search(r"boot_to_ready_s=([0-9.]+)", str(row.get("detail", "")))
            if match:
                return float(match.group(1))
    return None


def provider_id_for(
    command_rows: list[dict[str, Any]], sandbox_name: str
) -> str | None:
    for row in command_rows:
        if row.get("sandbox_name") == sandbox_name and row.get("provider_id"):
            return str(row["provider_id"])
    return None


def sandbox_summary(
    *,
    sandbox_name: str,
    provider_id: str | None,
    created_at: str,
    boot_to_ready_s: float,
    deleted_at: str,
    cleanup_ok: bool,
    confirmation: str,
    template_id: str,
    size_id: str,
) -> LiveSandboxSummary:
    return LiveSandboxSummary(
        name=sandbox_name,
        provider_id=provider_id,
        template_id=template_id,
        size_id=size_id,
        created_at=created_at,
        boot_to_ready_s=boot_to_ready_s,
        deleted_at=deleted_at,
        cleanup_ok=cleanup_ok,
        delete_confirmation=confirmation,
    )


def browser_summary(outcome: Any, requested_visible: bool) -> dict[str, Any]:
    runtime = str(outcome.browser_runtime or "")
    if runtime == "celesto_cdp":
        return {
            "requested_visible": requested_visible,
            "runtime": runtime,
            "cdp_gateway_used": True,
            "cdp_gateway": "connected",
            "note": "The platform CDP gateway drove the browser inside the sandbox.",
        }
    if runtime == "guest_playwright_exec":
        return {
            "requested_visible": requested_visible,
            "runtime": runtime,
            "cdp_gateway_used": False,
            "cdp_gateway": "unavailable; the in-guest Playwright browser was used",
            "note": "All browser actions ran inside the Celesto sandbox guest.",
        }
    raise RuntimeError(f"no verified live browser runtime was selected: {runtime!r}")


def build_visible_record(
    *,
    run_id: str,
    output_dir: Path,
    outcome: Any,
    trace_rows: list[dict[str, Any]],
    command_rows: list[dict[str, Any]],
    provenance: dict[str, Any],
    requested_visible: bool,
) -> dict[str, Any]:
    sandbox_name = str(outcome.sandbox_name)
    created = first_event(trace_rows, "sandbox_created")
    if created is None or created.get("sandbox_name") != sandbox_name:
        raise RuntimeError("trace does not contain the created sandbox")
    deleted = next(
        (
            row
            for row in trace_rows
            if row.get("event") == "state_transition"
            and row.get("state") == "Deleted"
            and row.get("sandbox_name") == sandbox_name
        ),
        None,
    )
    if deleted is None:
        raise RuntimeError("trace does not confirm sandbox deletion")
    deletion = delete_row(command_rows, sandbox_name)
    confirmation = delete_confirmation(deletion)
    if not outcome.cleanup_ok or not confirmation:
        raise RuntimeError("sandbox cleanup was not confirmed by the provider")

    evidence_dir = output_dir / "evidence"
    sandbox = sandbox_summary(
        sandbox_name=sandbox_name,
        provider_id=created.get("provider_id")
        or provider_id_for(command_rows, sandbox_name),
        created_at=str(created["ts"]),
        boot_to_ready_s=float(outcome.boot_to_ready_s),
        deleted_at=str(deleted["ts"]),
        cleanup_ok=True,
        confirmation=confirmation,
        template_id=os.getenv("CELESTO_TEMPLATE_ID", "browser-agent"),
        size_id=live_size_id(),
    )
    browser = browser_summary(outcome, requested_visible)
    runtime = str(browser["runtime"])
    evidence = LiveEvidenceBundle(
        actions_log=evidence_file(
            evidence_dir / "actions.jsonl",
            note=f"Live action log for sandbox {sandbox_name}.",
        ),
        screenshots=collect_screenshots(evidence_dir),
        command_log=evidence_file(
            output_dir / "commands.jsonl",
            note=f"Live command audit for sandbox {sandbox_name}.",
        ),
    )
    trace = evidence_file(
        output_dir / "trace.jsonl",
        note="Lifecycle trace for the single-sandbox live workflow.",
    )
    record = LiveVisibleRecord(
        schema_version=SCHEMA_VERSION,
        generated_at=utcnow(),
        run_id=run_id,
        execution_mode="live",
        scope="single-sandbox live workflow; not the 20-run benchmark",
        live_guest_browser_verified=runtime == "guest_playwright_exec",
        live_celesto_cdp_verified=runtime == "celesto_cdp",
        sandbox=sandbox,
        browser=browser,
        outcome=LiveOutcomeSummary(
            success=bool(outcome.success),
            classification=outcome.classification.value,
            wall_clock_s=float(outcome.wall_clock_s),
            defect=outcome.defect.to_dict() if outcome.defect else None,
        ),
        evidence=evidence,
        trace=trace,
        provenance=provenance,
        limitations=[
            "One sandbox and one workflow were run, not a 20-run benchmark.",
            "No video was recorded (record_video=False).",
            (
                "The platform CDP gateway was unavailable on this sandbox; the "
                "in-guest Playwright browser executed the workflow."
                if runtime == "guest_playwright_exec"
                else "The platform CDP gateway executed the browser inside the sandbox."
            ),
            "Automatic orphan reclamation was left at its configured default.",
            (
                "The retained sandbox cmp_3e12f5354e89 is protected by "
                "CELESTO_KEEP_COMPUTER_IDS and was not touched."
            ),
            (
                "Provenance: commit "
                f"{provenance.get('git_commit') or 'unknown'}, "
                f"git_dirty={provenance.get('git_dirty')}."
            ),
        ],
    )
    return record.model_dump(mode="json")


def build_recovery_record(
    *,
    run_id: str,
    output_dir: Path,
    outcome: Any,
    trace_rows: list[dict[str, Any]],
    command_rows: list[dict[str, Any]],
    provenance: dict[str, Any],
    requested_visible: bool,
) -> dict[str, Any]:
    failure = first_event(trace_rows, "failure")
    if failure is None or failure.get("state") != "MidTaskFailure":
        raise RuntimeError("trace does not contain the controlled MidTaskFailure")
    if "503" not in str(failure.get("detail", "")):
        raise RuntimeError("controlled failure does not mention HTTP 503")

    policy = first_event(trace_rows, "policy_applied")
    if policy is None or "policy=fresh" not in str(policy.get("detail", "")):
        raise RuntimeError("trace does not contain the fresh recovery policy")

    recreated = first_event(trace_rows, "sandbox_recreated")
    if recreated is None:
        raise RuntimeError("trace does not contain the replacement sandbox")

    created = first_event(trace_rows, "sandbox_created")
    if created is None:
        raise RuntimeError("trace does not contain the failed sandbox")
    failed_name = str(created["sandbox_name"])
    replacement_name = str(recreated.get("sandbox_name") or "")
    match = re.search(r"replacement=(sbx-[a-z0-9]+)", str(recreated.get("detail", "")))
    if match:
        replacement_name = match.group(1)
    if not replacement_name or replacement_name == failed_name:
        raise RuntimeError("replacement sandbox name is missing or unchanged")

    failed_delete = delete_row(command_rows, failed_name)
    failed_confirmation = delete_confirmation(failed_delete)
    if failed_delete is None or not failed_confirmation:
        raise RuntimeError("failed sandbox cleanup was not confirmed")

    failed_boot = boot_seconds_for(command_rows, failed_name)
    if failed_boot is None:
        failed_boot = float(outcome.boot_to_ready_s)
    replacement_boot = boot_seconds_for(command_rows, replacement_name)
    if replacement_boot is None:
        raise RuntimeError("replacement sandbox create row has no boot time")

    replacement_delete = delete_row(command_rows, replacement_name)
    replacement_confirmation = delete_confirmation(replacement_delete)
    if not outcome.cleanup_ok or replacement_delete is None or not replacement_confirmation:
        raise RuntimeError("replacement sandbox cleanup was not confirmed")

    deleted = next(
        (
            row
            for row in trace_rows
            if row.get("event") == "state_transition"
            and row.get("state") == "Deleted"
            and row.get("sandbox_name") == replacement_name
        ),
        None,
    )
    replacement_deleted_at = (
        str(deleted["ts"]) if deleted is not None else str(replacement_delete["ts"])
    )

    start = event_index(trace_rows, "failure")
    events = [
        LiveRecoveryEvent(
            ts=str(row["ts"]),
            event=str(row["event"]),
            state=str(row["state"]),
            detail=str(row.get("detail", "")),
        )
        for row in trace_rows[start:]
        if row.get("event") in RECOVERY_EVENT_NAMES
    ]
    event_names = {event.event for event in events}
    for required in REQUIRED_RECOVERY_EVENTS:
        if required not in event_names:
            raise RuntimeError(f"recovery trace is missing {required!r}")

    browser = browser_summary(outcome, requested_visible)
    runtime = str(browser["runtime"])
    evidence_dir = output_dir / "evidence"
    evidence = LiveEvidenceBundle(
        actions_log=evidence_file(
            evidence_dir / "actions.jsonl",
            note=(
                f"Live action log spanning the failed sandbox {failed_name} and "
                f"replacement {replacement_name}."
            ),
        ),
        screenshots=collect_screenshots(evidence_dir),
        command_log=evidence_file(
            output_dir / "commands.jsonl",
            note="Live command audit for both sandboxes in the recovery run.",
        ),
    )
    trace = evidence_file(
        output_dir / "trace.jsonl",
        note="Lifecycle trace for the controlled 503 and fresh-sandbox recovery.",
    )
    record = LiveRecoveryRecord(
        schema_version=SCHEMA_VERSION,
        generated_at=utcnow(),
        run_id=run_id,
        execution_mode="live",
        scope="single-sandbox live recovery demonstration; not the 20-run benchmark",
        recovery_policy="fresh",
        failure=LiveRecoveryFailure(
            step=int(failure.get("step", 7)),
            http_status=503,
            message=str(failure.get("detail", "")),
            defect_claim=False,
        ),
        failed_sandbox=sandbox_summary(
            sandbox_name=failed_name,
            provider_id=created.get("provider_id")
            or provider_id_for(command_rows, failed_name),
            created_at=str(created["ts"]),
            boot_to_ready_s=failed_boot,
            deleted_at=str(failed_delete["ts"]),
            cleanup_ok=True,
            confirmation=failed_confirmation,
            template_id=os.getenv("CELESTO_TEMPLATE_ID", "browser-agent"),
            size_id=live_size_id(),
        ),
        replacement_sandbox=sandbox_summary(
            sandbox_name=replacement_name,
            provider_id=provider_id_for(command_rows, replacement_name),
            created_at=str(recreated["ts"]),
            boot_to_ready_s=replacement_boot,
            deleted_at=replacement_deleted_at,
            cleanup_ok=True,
            confirmation=replacement_confirmation,
            template_id=os.getenv("CELESTO_TEMPLATE_ID", "browser-agent"),
            size_id=live_size_id(),
        ),
        retry=LiveRecoveryRetry(
            success=bool(outcome.success),
            classification=outcome.classification.value,
            wall_clock_s=float(outcome.wall_clock_s),
        ),
        events=events,
        browser=browser,
        evidence=evidence,
        trace=trace,
        provenance=provenance,
        limitations=[
            "Two sandboxes were used: the failed sandbox and a fresh replacement.",
            "This is one recovery demonstration, not a 20-run benchmark.",
            (
                "The platform CDP gateway was unavailable on this sandbox; the "
                "in-guest Playwright browser executed the workflow."
                if runtime == "guest_playwright_exec"
                else "The platform CDP gateway executed the browser inside the sandbox."
            ),
            "The free trial allows two new sandboxes per hour; a quota 429 is reported, not hidden.",
            (
                "The retained sandbox cmp_3e12f5354e89 is protected by "
                "CELESTO_KEEP_COMPUTER_IDS and was not touched."
            ),
            (
                "Provenance: commit "
                f"{provenance.get('git_commit') or 'unknown'}, "
                f"git_dirty={provenance.get('git_dirty')}."
            ),
        ],
    )
    return record.model_dump(mode="json")


def promote_record(
    record: dict[str, Any],
    record_path: Path,
    *,
    recovery: bool,
    provenance: dict[str, Any],
) -> None:
    index_path = LIVE_EVIDENCE_DIR / "index.json"
    original = index_path.read_bytes()
    index = json.loads(original.decode("utf-8"))
    runtime = str((record.get("browser") or {}).get("runtime", ""))
    guest_verified = bool(
        record.get("live_guest_browser_verified")
        if not recovery
        else runtime == "guest_playwright_exec"
    )
    cdp_verified = bool(
        record.get("live_celesto_cdp_verified")
        if not recovery
        else runtime == "celesto_cdp"
    )
    entry = {
        "path": relative_path(record_path),
        "status": "verified",
        "live_guest_browser_verified": guest_verified,
        "live_celesto_cdp_verified": cdp_verified,
        "note": (
            f"Single-sandbox live {'recovery ' if recovery else ''}workflow at "
            f"commit {provenance.get('git_commit')}; not the 20-run benchmark."
        ),
    }
    if recovery:
        index["recovery"] = entry
    else:
        previous = index.get("current")
        if previous:
            previous = dict(previous)
            previous["note"] = (
                "Previous verified single-sandbox run, superseded as current."
            )
            index["historical"] = [
                previous,
                *[
                    item
                    for item in index.get("historical", [])
                    if item.get("path") != previous.get("path")
                ],
            ]
        index["current"] = entry
    index["schema_version"] = SCHEMA_VERSION
    index["generated_at"] = utcnow()
    index["provenance"] = provenance
    atomic_write_json(index_path, index)

    result = verify_submission(PROJECT_ROOT)
    if not result.ok:
        atomic_write_bytes(index_path, original)
        raise RuntimeError(
            "promotion verification failed and the index was restored: "
            + "; ".join(result.errors)
        )


def _write_run_summary(output_dir: Path, payload: dict[str, Any]) -> Path:
    path = output_dir / "run.json"
    atomic_write_json(path, payload)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one live Celesto workflow and archive reproducible evidence"
    )
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="exercise the controlled step-7 HTTP 503 and fresh-sandbox recovery",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the guest Chromium headless instead of on the sandbox desktop",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="update reports/live-evidence/index.json after verification",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--base-url",
        default=os.getenv("TARGET_BASE_URL", GUEST_APP_BASE_URL),
        help="in-guest ShopDemo base URL",
    )
    parser.add_argument(
        "--create-patience",
        type=float,
        default=180.0,
        help="seconds to wait for a throttled sandbox create before failing",
    )
    args = parser.parse_args(argv)

    if not os.getenv("CELESTO_API_KEY", "").strip():
        print("ERROR: live mode requires CELESTO_API_KEY. It will not fall back to mock.")
        return 2

    provenance = capture_provenance(PROJECT_ROOT)
    if args.promote and (
        provenance.get("git_commit") is None or provenance.get("git_dirty") is not False
    ):
        print(
            "ERROR: --promote requires a clean tracked tree pinned to a git commit. "
            "Commit the code and rerun from a clean checkout."
        )
        return 2

    run_id = args.run_id or default_run_id(args.inject_failure)
    output_dir = (args.output_dir or DEFAULT_RUNS_DIR / run_id).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"ERROR: output directory is not empty: {output_dir}")
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    qa.EVIDENCE_DIR = evidence_dir
    qa.ACTIONS_LOG = evidence_dir / "actions.jsonl"
    qa.GENERATED_TEST_DIR = output_dir / "generated"
    qa.GENERATED_TEST_DIR.mkdir(parents=True, exist_ok=True)

    # Keep an interactive recovery run from waiting an hour on a free-trial 429.
    os.environ["CELESTO_CREATE_PATIENCE_S"] = str(max(1.0, args.create_patience))

    requested_visible = visible_guest_browser_default() and not args.headless
    started = time.monotonic()
    adapter = None
    outcome = None
    error = ""
    record: dict[str, Any] | None = None
    record_path = output_dir / "record.json"
    promoted = False

    try:
        adapter = create_adapter("live", command_log_path=output_dir / "commands.jsonl")
        if adapter.execution_mode != "live":
            raise RuntimeError("adapter did not enter live mode")
        runner = QAWorkflowRunner(
            adapter,
            generate_sandbox_name(),
            args.base_url,
            execution_mode="live",
            record_video=False,
            use_sandbox_browser=True,
            fixture_bootstrapper=lambda name: deploy_guest_fixture(adapter, name),
            guest_browser_visible=requested_visible,
            headless=args.headless,
        )
        try:
            outcome = runner.run(inject_failure=args.inject_failure)
        finally:
            try:
                runner.save_trace(output_dir / "trace.jsonl")
            except Exception as exc:  # pragma: no cover - trace is best effort
                print(f"warning: could not save trace: {type(exc).__name__}: {exc}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:  # pragma: no cover - close is best effort
                print(f"warning: adapter close failed: {type(exc).__name__}: {exc}")

    if outcome is not None and not error:
        trace_path = output_dir / "trace.jsonl"
        command_path = output_dir / "commands.jsonl"
        try:
            trace_rows = load_jsonl(trace_path) if trace_path.is_file() else []
            command_rows = load_jsonl(command_path) if command_path.is_file() else []
            if args.inject_failure:
                record = build_recovery_record(
                    run_id=run_id,
                    output_dir=output_dir,
                    outcome=outcome,
                    trace_rows=trace_rows,
                    command_rows=command_rows,
                    provenance=provenance,
                    requested_visible=requested_visible,
                )
            else:
                record = build_visible_record(
                    run_id=run_id,
                    output_dir=output_dir,
                    outcome=outcome,
                    trace_rows=trace_rows,
                    command_rows=command_rows,
                    provenance=provenance,
                    requested_visible=requested_visible,
                )
            atomic_write_json(record_path, record)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            record = None
    elif outcome is not None:
        error = error or str(outcome.error or "workflow failed")
    elif not error:
        error = "workflow did not return an outcome"

    if record is not None and args.promote:
        try:
            promote_record(
                record,
                record_path,
                recovery=args.inject_failure,
                provenance=provenance,
            )
            promoted = True
        except Exception as exc:
            error = f"promotion failed: {type(exc).__name__}: {exc}"
            record = None

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "recovery" if args.inject_failure else "single",
        "output_dir": relative_path(output_dir),
        "record": relative_path(record_path) if record is not None else None,
        "promoted": promoted,
        "provenance": provenance,
        "elapsed_s": round(time.monotonic() - started, 2),
        "error": error,
        "outcome": (
            {
                "success": bool(outcome.success),
                "classification": outcome.classification.value,
                "cleanup_ok": bool(outcome.cleanup_ok),
                "browser_runtime": outcome.browser_runtime,
                "wall_clock_s": float(outcome.wall_clock_s),
                "sandbox_name": outcome.sandbox_name,
            }
            if outcome is not None
            else None
        ),
    }
    summary_path = _write_run_summary(output_dir, summary)
    print(json.dumps(summary, indent=2))
    print(f"run summary: {summary_path}")
    if record is not None:
        print(f"record: {record_path}")
    if error:
        if "429" in error or "rate limit" in error.lower():
            print(
                "Celesto free trial allows two new sandboxes per hour. "
                "Retry after the quota window resets."
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
