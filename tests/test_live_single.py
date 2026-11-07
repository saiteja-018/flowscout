from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

import scripts.run_live_single as live
from app.qa_agent import DefectClassification, DefectReport
from app.report_schemas import (
    LiveRecoveryRecord,
    LiveVisibleRecord,
    VerificationResult,
)

PROVENANCE = {
    "git_commit": "8aed25e",
    "git_branch": "main",
    "git_dirty": False,
    "git_untracked_count": 0,
    "captured_at": "2026-11-07T13:00:00+00:00",
    "note": "tracked tree clean at capture time",
}


def defect() -> DefectReport:
    return DefectReport(
        classification=DefectClassification.CONFIRMED,
        title="Coupon SAVE20 can be stacked without limit",
        description="The checkout accepts SAVE20 repeatedly and the total goes negative.",
        steps_to_reproduce=["Open checkout", "Apply SAVE20 six times"],
        expected_behavior="The coupon is rejected after its first application.",
        actual_behavior="All six applications succeed and the total is negative.",
    )


class FakeOutcome:
    def __init__(
        self,
        *,
        sandbox_name: str = "sbx-abcdefgh",
        success: bool = True,
        cleanup_ok: bool = True,
        browser_runtime: str = "guest_playwright_exec",
    ) -> None:
        self.sandbox_name = sandbox_name
        self.success = success
        self.cleanup_ok = cleanup_ok
        self.browser_runtime = browser_runtime
        self.classification = DefectClassification.CONFIRMED
        self.wall_clock_s = 102.39
        self.boot_to_ready_s = 4.391
        self.defect = defect()
        self.error = ""


def write_run_files(
    root: Path,
    *,
    sandbox_name: str = "sbx-abcdefgh",
    trace_rows: list[dict[str, object]] | None = None,
    command_rows: list[dict[str, object]] | None = None,
) -> Path:
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "actions.jsonl").write_text(
        '{"action": "navigate", "success": true}\n', encoding="utf-8"
    )
    Image.new("RGB", (16, 12), "white").save(evidence / "step-04.jpg")
    rows = trace_rows or [
        {
            "ts": "2026-11-07T13:10:29+00:00",
            "event": "sandbox_created",
            "state": "Created",
            "sandbox_name": sandbox_name,
            "provider_id": "cmp_abcdef",
        },
        {
            "ts": "2026-11-07T13:12:09+00:00",
            "event": "state_transition",
            "state": "Deleted",
            "sandbox_name": sandbox_name,
        },
    ]
    (root / "trace.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    commands = command_rows or [
        {
            "ts": "2026-11-07T13:12:09+00:00",
            "sandbox_name": sandbox_name,
            "command": f"delete sandbox {sandbox_name}",
            "detail": "Celesto Cloud delete confirmed",
            "provider_id": "cmp_abcdef",
        }
    ]
    (root / "commands.jsonl").write_text(
        "\n".join(json.dumps(row) for row in commands) + "\n", encoding="utf-8"
    )
    return root


def test_default_run_id_uses_mode_prefix() -> None:
    fixed = datetime(2026, 11, 7, 13, 10, tzinfo=timezone.utc)
    assert live.default_run_id(False, fixed) == "live-single-20261107T131000Z"
    assert live.default_run_id(True, fixed) == "live-recovery-20261107T131000Z"


def test_build_visible_record_validates(tmp_path: Path) -> None:
    output = write_run_files(tmp_path / "run")
    record = live.build_visible_record(
        run_id="live-single-test",
        output_dir=output,
        outcome=FakeOutcome(),
        trace_rows=live.load_jsonl(output / "trace.jsonl"),
        command_rows=live.load_jsonl(output / "commands.jsonl"),
        provenance=PROVENANCE,
        requested_visible=True,
    )
    validated = LiveVisibleRecord.model_validate(record)
    assert validated.sandbox.name == "sbx-abcdefgh"
    assert validated.sandbox.cleanup_ok is True
    assert validated.live_guest_browser_verified is True
    assert validated.trace is not None
    assert validated.provenance is not None
    assert validated.provenance.git_commit == "8aed25e"


def test_build_visible_record_requires_cleanup_confirmation(tmp_path: Path) -> None:
    output = write_run_files(
        tmp_path / "run",
        command_rows=[
            {
                "ts": "2026-11-07T13:12:09+00:00",
                "sandbox_name": "sbx-abcdefgh",
                "command": "delete sandbox sbx-abcdefgh",
                "detail": "",
                "provider_id": "cmp_abcdef",
            }
        ],
    )
    with pytest.raises(RuntimeError, match="cleanup was not confirmed"):
        live.build_visible_record(
            run_id="live-single-test",
            output_dir=output,
            outcome=FakeOutcome(),
            trace_rows=live.load_jsonl(output / "trace.jsonl"),
            command_rows=live.load_jsonl(output / "commands.jsonl"),
            provenance=PROVENANCE,
            requested_visible=True,
        )


def recovery_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    trace = [
        {
            "ts": "2026-11-07T13:10:29+00:00",
            "event": "sandbox_created",
            "state": "Created",
            "step": 1,
            "sandbox_name": "sbx-failed01",
            "provider_id": "cmp_failed",
        },
        {
            "ts": "2026-11-07T13:11:00+00:00",
            "event": "failure",
            "state": "MidTaskFailure",
            "step": 7,
            "detail": "Checkout returned HTTP 503 during step 7; no defect claim is made from this state",
            "sandbox_name": "sbx-failed01",
        },
        {
            "ts": "2026-11-07T13:11:01+00:00",
            "event": "policy_applied",
            "state": "RolledBack",
            "step": 7,
            "detail": "policy=fresh: deleting the failed sandbox, creating a clean replacement",
        },
        {
            "ts": "2026-11-07T13:11:20+00:00",
            "event": "sandbox_recreated",
            "state": "RolledBack",
            "step": 7,
            "sandbox_name": "sbx-replace01",
            "detail": "replacement=sbx-replace01; provider=celesto_cloud",
        },
        {
            "ts": "2026-11-07T13:11:30+00:00",
            "event": "step_start",
            "state": "Running",
            "step": 7,
            "detail": "Retrying second coupon application",
            "sandbox_name": "sbx-replace01",
        },
        {
            "ts": "2026-11-07T13:12:09+00:00",
            "event": "step_complete",
            "state": "Succeeded",
            "step": 10,
            "detail": "workflow complete",
            "sandbox_name": "sbx-replace01",
        },
        {
            "ts": "2026-11-07T13:12:10+00:00",
            "event": "state_transition",
            "state": "Deleted",
            "step": 10,
            "sandbox_name": "sbx-replace01",
        },
    ]
    commands = [
        {
            "ts": "2026-11-07T13:10:29+00:00",
            "sandbox_name": "sbx-failed01",
            "command": "create sandbox sbx-failed01",
            "detail": "boot_to_ready_s=4.1; template=browser-agent; size=micro",
            "provider_id": "cmp_failed",
        },
        {
            "ts": "2026-11-07T13:11:05+00:00",
            "sandbox_name": "sbx-failed01",
            "command": "delete sandbox sbx-failed01",
            "detail": "Celesto Cloud delete confirmed",
            "provider_id": "cmp_failed",
        },
        {
            "ts": "2026-11-07T13:11:20+00:00",
            "sandbox_name": "sbx-replace01",
            "command": "create sandbox sbx-replace01",
            "detail": "boot_to_ready_s=3.9; template=browser-agent; size=micro",
            "provider_id": "cmp_replace",
        },
        {
            "ts": "2026-11-07T13:11:20+00:00",
            "sandbox_name": "sbx-replace01",
            "command": "write 100 bytes to /workspace/flowscout/app.py",
            "provider_id": "cmp_replace",
        },
        {
            "ts": "2026-11-07T13:12:10+00:00",
            "sandbox_name": "sbx-replace01",
            "command": "delete sandbox sbx-replace01",
            "detail": "Celesto Cloud delete confirmed",
            "provider_id": "cmp_replace",
        },
    ]
    return trace, commands


def test_build_recovery_record_validates(tmp_path: Path) -> None:
    trace, commands = recovery_rows()
    output = write_run_files(
        tmp_path / "run", sandbox_name="sbx-replace01", trace_rows=trace, command_rows=commands
    )
    record = live.build_recovery_record(
        run_id="live-recovery-test",
        output_dir=output,
        outcome=FakeOutcome(sandbox_name="sbx-replace01"),
        trace_rows=live.load_jsonl(output / "trace.jsonl"),
        command_rows=live.load_jsonl(output / "commands.jsonl"),
        provenance=PROVENANCE,
        requested_visible=True,
    )
    validated = LiveRecoveryRecord.model_validate(record)
    assert validated.failure.http_status == 503
    assert validated.failure.defect_claim is False
    assert validated.failed_sandbox.name == "sbx-failed01"
    assert validated.replacement_sandbox.name == "sbx-replace01"
    assert validated.failed_sandbox.cleanup_ok is True
    assert validated.replacement_sandbox.cleanup_ok is True
    assert validated.failed_sandbox.boot_to_ready_s == 4.1
    assert validated.replacement_sandbox.boot_to_ready_s == 3.9
    assert validated.browser["runtime"] == "guest_playwright_exec"


def test_build_recovery_record_requires_replacement_boot_time(tmp_path: Path) -> None:
    trace, commands = recovery_rows()
    commands = [
        row
        for row in commands
        if not (
            row.get("sandbox_name") == "sbx-replace01"
            and str(row.get("command", "")).startswith("create sandbox")
        )
    ]
    output = write_run_files(
        tmp_path / "run", sandbox_name="sbx-replace01", trace_rows=trace, command_rows=commands
    )
    with pytest.raises(RuntimeError, match="no boot time"):
        live.build_recovery_record(
            run_id="live-recovery-test",
            output_dir=output,
            outcome=FakeOutcome(sandbox_name="sbx-replace01"),
            trace_rows=live.load_jsonl(output / "trace.jsonl"),
            command_rows=live.load_jsonl(output / "commands.jsonl"),
            provenance=PROVENANCE,
            requested_visible=True,
        )


def test_build_recovery_record_requires_failed_sandbox_cleanup(tmp_path: Path) -> None:
    trace, commands = recovery_rows()
    commands = [
        row
        for row in commands
        if row.get("sandbox_name") != "sbx-failed01"
    ]
    output = write_run_files(
        tmp_path / "run", sandbox_name="sbx-replace01", trace_rows=trace, command_rows=commands
    )
    with pytest.raises(RuntimeError, match="failed sandbox cleanup"):
        live.build_recovery_record(
            run_id="live-recovery-test",
            output_dir=output,
            outcome=FakeOutcome(sandbox_name="sbx-replace01"),
            trace_rows=live.load_jsonl(output / "trace.jsonl"),
            command_rows=live.load_jsonl(output / "commands.jsonl"),
            provenance=PROVENANCE,
            requested_visible=True,
        )


def test_promote_restores_index_on_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_dir = tmp_path / "live-evidence"
    live_dir.mkdir()
    index_path = live_dir / "index.json"
    index = {
        "generated_at": "2026-11-07T13:00:00+00:00",
        "current": {
            "path": "reports/live-evidence/old.json",
            "status": "verified",
            "live_guest_browser_verified": True,
            "live_celesto_cdp_verified": False,
        },
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")
    before = index_path.read_bytes()
    record_path = live_dir / "runs" / "test" / "record.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live, "LIVE_EVIDENCE_DIR", live_dir)
    monkeypatch.setattr(
        live,
        "verify_submission",
        lambda root: VerificationResult(ok=False, errors=["forced failure"]),
    )
    with pytest.raises(RuntimeError, match="restored"):
        live.promote_record(
            {"live_guest_browser_verified": True, "live_celesto_cdp_verified": False},
            record_path,
            recovery=False,
            provenance=PROVENANCE,
        )
    assert index_path.read_bytes() == before


def test_promote_recovery_sets_recovery_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_dir = tmp_path / "live-evidence"
    live_dir.mkdir()
    index_path = live_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-07T13:00:00+00:00",
                "current": {
                    "path": "reports/live-evidence/old.json",
                    "status": "verified",
                    "live_guest_browser_verified": True,
                    "live_celesto_cdp_verified": False,
                },
            }
        ),
        encoding="utf-8",
    )
    record_path = live_dir / "runs" / "test" / "record.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live, "LIVE_EVIDENCE_DIR", live_dir)
    monkeypatch.setattr(live, "verify_submission", lambda root: VerificationResult(ok=True))
    live.promote_record(
        {
            "browser": {"runtime": "guest_playwright_exec"},
        },
        record_path,
        recovery=True,
        provenance=PROVENANCE,
    )
    updated = json.loads(index_path.read_text(encoding="utf-8"))
    assert updated["recovery"]["path"] == live.relative_path(record_path)
    assert updated["recovery"]["live_guest_browser_verified"] is True
    assert updated["current"]["path"] == "reports/live-evidence/old.json"
    assert updated["schema_version"] == 1
