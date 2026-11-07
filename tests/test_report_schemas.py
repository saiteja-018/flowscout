from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.report_schemas import (
    ConcurrencyReport,
    LiveEvidenceIndex,
    ReliabilityReport,
    WorkflowOutcomeSchema,
    verify_submission,
)


def mock_run(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sandbox_name": "sbx-abcdefgh",
        "fresh": True,
        "success": True,
        "classification": "confirmed_reproducible_defect",
        "error": "",
        "wall_clock_s": 10.0,
        "boot_to_ready_s": 0.0,
        "sandbox_creation_s": 1.0,
        "deploy_s": 2.0,
        "browser_start_s": 1.0,
        "workflow_s": 5.0,
        "cleanup_s": 1.0,
        "agent_wall_clock_s": 10.0,
        "total_wall_clock_s": 10.0,
        "end_to_end_wall_clock_s": 10.5,
        "cost_usd": 0.0,
        "cost_including_creation_usd": 0.0,
        "cost_source": "mock_zero_cost",
        "rate_usd_per_hour": 0.0,
        "start_time": "2026-11-05T00:00:00+00:00",
        "end_time": "2026-11-05T00:00:10+00:00",
        "cleanup_ok": True,
        "browser_runtime": "host_playwright",
        "browser_verified": False,
    }
    row.update(overrides)
    return row


def mock_reliability(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "runs": [mock_run()],
        "run_count": 1,
        "success_rate": 1.0,
        "success_rate_pct": 100.0,
        "median_wall_clock_s": 10.0,
        "p90_wall_clock_s": 10.0,
        "median_boot_to_ready_s": 0.0,
        "median_sandbox_creation_s": 1.0,
        "median_deploy_s": 2.0,
        "median_browser_start_s": 1.0,
        "median_workflow_s": 5.0,
        "median_cleanup_s": 1.0,
        "median_agent_wall_clock_s": 10.0,
        "median_total_wall_clock_s": 10.0,
        "median_end_to_end_wall_clock_s": 10.5,
        "total_cost_usd": 0.0,
        "total_cost_including_creation_usd": 0.0,
        "cost_source": "mock_zero_cost",
        "rate_usd_per_hour": 0.0,
        "postmortem_path": "reports/postmortem.md",
        "evaluation_mode": "mock",
        "sandbox_runtime": "mock",
        "browser_runtime": "host_playwright",
        "browser_runtimes": ["host_playwright"],
        "sandbox_size": "mock",
        "live_celesto_verified": False,
        "cost_methodology": "Mock sandbox runs incur no Celesto charge.",
        "note": (
            "20 deterministic evaluation runs used a mock sandbox adapter and a real "
            "host Playwright browser; Celesto boot latency is not measured."
        ),
        "generated_at": "2026-11-05T00:00:10+00:00",
        "planner": "scripted",
    }
    payload.update(overrides)
    return payload


def workflow_outcome(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": True,
        "classification": "confirmed_reproducible_defect",
        "defect": {
            "classification": "confirmed_reproducible_defect",
            "title": "Coupon SAVE20 can be stacked",
            "description": "The checkout accepts SAVE20 repeatedly and the total goes negative.",
            "steps_to_reproduce": ["Open checkout", "Apply SAVE20 six times"],
            "expected_behavior": "The coupon is rejected after its first application.",
            "actual_behavior": "All six applications succeed and the total is negative.",
            "evidence": ["reports/evidence/actions.jsonl"],
            "severity": "high",
        },
        "error": "",
        "sandbox_name": "sbx-abcdefgh",
        "wall_clock_s": 10.0,
        "boot_to_ready_s": 0.0,
        "sandbox_creation_s": 1.0,
        "deploy_s": 2.0,
        "browser_start_s": 1.0,
        "workflow_s": 5.0,
        "cleanup_s": 1.0,
        "agent_wall_clock_s": 10.0,
        "total_wall_clock_s": 10.0,
        "end_to_end_wall_clock_s": 10.5,
        "cleanup_ok": True,
        "test_paths": ["tests/generated/coupon_stacking_test.spec.ts"],
        "execution_mode": "mock",
        "browser_runtime": "host_playwright",
        "planner_mode": "scripted",
        "actions": [],
    }
    payload.update(overrides)
    return payload


def test_reliability_schema_accepts_consistent_mock_report() -> None:
    report = ReliabilityReport.model_validate(mock_reliability())
    assert report.run_count == 1
    assert report.cost_source == "mock_zero_cost"


def test_reliability_schema_rejects_mismatched_median() -> None:
    with pytest.raises(ValidationError, match="median_wall_clock_s"):
        ReliabilityReport.model_validate(mock_reliability(median_wall_clock_s=11.0))


def test_reliability_schema_rejects_mock_cost_claim() -> None:
    row = mock_run(cost_usd=0.01)
    with pytest.raises(ValidationError, match="mock costs"):
        ReliabilityReport.model_validate(mock_reliability(runs=[row]))


def test_live_cost_fields_must_match_clock_and_rate() -> None:
    rate = 0.03
    agent = 36.0
    total = 40.0
    row = mock_run(
        success=True,
        boot_to_ready_s=5.0,
        sandbox_creation_s=4.0,
        deploy_s=2.0,
        browser_start_s=1.0,
        workflow_s=32.0,
        cleanup_s=1.0,
        agent_wall_clock_s=agent,
        wall_clock_s=agent,
        total_wall_clock_s=total,
        end_to_end_wall_clock_s=41.0,
        cost_usd=round(agent / 3600.0 * rate, 5),
        cost_including_creation_usd=round(total / 3600.0 * rate, 5),
        cost_source="celesto_size_rate_wall_clock",
        rate_usd_per_hour=rate,
        browser_runtime="guest_playwright_exec",
        browser_verified=True,
    )
    payload = mock_reliability(
        runs=[row],
        evaluation_mode="live",
        sandbox_runtime="celesto_cloud",
        browser_runtime="guest_playwright_exec",
        browser_runtimes=["guest_playwright_exec"],
        sandbox_size="standard",
        live_celesto_verified=True,
        cost_source="celesto_size_rate_wall_clock",
        rate_usd_per_hour=rate,
        total_cost_usd=row["cost_usd"],
        total_cost_including_creation_usd=row["cost_including_creation_usd"],
        median_boot_to_ready_s=5.0,
        median_sandbox_creation_s=4.0,
        median_deploy_s=2.0,
        median_browser_start_s=1.0,
        median_workflow_s=32.0,
        median_cleanup_s=1.0,
        median_agent_wall_clock_s=agent,
        median_total_wall_clock_s=total,
        median_end_to_end_wall_clock_s=41.0,
        median_wall_clock_s=agent,
        p90_wall_clock_s=agent,
    )
    assert ReliabilityReport.model_validate(payload).live_celesto_verified is True

    row["cost_usd"] = 999.0
    with pytest.raises(ValidationError, match="cost_usd"):
        ReliabilityReport.model_validate({**payload, "runs": [row]})


def test_workflow_timing_must_add_up() -> None:
    assert WorkflowOutcomeSchema.model_validate(workflow_outcome()).success is True
    with pytest.raises(ValidationError, match="phase timing sum"):
        WorkflowOutcomeSchema.model_validate(workflow_outcome(workflow_s=6.0))


def test_live_index_requires_verified_current_entry() -> None:
    entry = {
        "path": "reports/live-evidence/current.json",
        "status": "historical_failure",
    }
    with pytest.raises(ValidationError, match="current live evidence"):
        LiveEvidenceIndex.model_validate(
            {"generated_at": "2026-11-05T00:00:00+00:00", "current": entry}
        )


def test_verifier_reports_a_missing_tree(tmp_path: Path) -> None:
    result = verify_submission(tmp_path)
    assert result.ok is False
    assert any("report problem.json: missing" in error for error in result.errors)
    assert any("live evidence" in error for error in result.errors)


def test_concurrency_accepts_a_short_smoke_run() -> None:
    jobs = [
        {
            "job_id": f"job-{index:03d}",
            "sandbox_name": f"sbx-smoke{index:03d}",
            "started_at": "2026-11-07T00:00:00+00:00",
            "end_time": "2026-11-07T00:00:01+00:00",
            "duration_s": 1.0,
            "outcome": "success",
        }
        for index in (1, 2)
    ]
    payload = {
        "jobs": jobs,
        "mode": "sequential",
        "parallelism": 1,
        "breakage_notes": "Short smoke run; no concurrency claim is made.",
    }
    report = ConcurrencyReport.model_validate(payload)
    assert len(report.jobs) == 2
    with pytest.raises(ValidationError, match="jobs"):
        ConcurrencyReport.model_validate({**payload, "jobs": []})
