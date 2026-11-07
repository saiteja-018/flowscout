from __future__ import annotations

import pytest

from scripts.run_live_demo import cost_metadata, generate_postmortem, live_verification


def _run(**overrides: bool) -> dict[str, bool]:
    row = {"success": True, "browser_verified": True, "cleanup_ok": True}
    row.update(overrides)
    return row


def test_live_verification_requires_a_live_run() -> None:
    assert live_verification("live", [_run()]) is True
    assert live_verification("mock", [_run()]) is False


def test_live_verification_rejects_empty_runs() -> None:
    assert live_verification("live", []) is False


def test_live_verification_requires_browser_success() -> None:
    assert live_verification("live", [_run(browser_verified=False)]) is False
    assert live_verification("live", [_run(), _run(browser_verified=False)]) is False


def test_live_verification_requires_cleanup() -> None:
    assert live_verification("live", [_run(cleanup_ok=False)]) is False


def test_live_verification_requires_successful_runs() -> None:
    assert live_verification("live", [_run(success=False)]) is False


def test_cost_metadata_separates_agent_and_creation_clock() -> None:
    mock = cost_metadata(100.0, 120.0, "mock")
    assert mock["cost_usd"] == 0.0
    assert mock["cost_including_creation_usd"] == 0.0
    assert mock["cost_source"] == "mock_zero_cost"
    assert mock["rate_usd_per_hour"] == 0.0

    live = cost_metadata(3600.0, 7200.0, "live")
    assert live["rate_usd_per_hour"] > 0
    assert live["cost_source"] == "celesto_size_rate_wall_clock"
    assert live["cost_usd"] == pytest.approx(live["rate_usd_per_hour"], abs=1e-5)
    assert live["cost_including_creation_usd"] == pytest.approx(
        2 * live["rate_usd_per_hour"], abs=1e-5
    )


def _postmortem(mode: str) -> str:
    evaluation = {
        "evaluation_mode": mode,
        "browser_runtime": "host_playwright" if mode == "mock" else "guest_playwright_exec",
        "live_celesto_verified": mode == "live",
        "runs": [],
        "run_count": 0,
        "success_rate_pct": 0.0,
        "median_wall_clock_s": 0.0,
        "p90_wall_clock_s": 0.0,
        "median_boot_to_ready_s": 0.0,
        "total_cost_usd": 0.0,
    }
    matrix = {"cases": []}
    lifecycle = {
        "cleanup_on_success": True,
        "cleanup_on_failure": True,
        "cleanup_on_interrupt": True,
    }
    return generate_postmortem(evaluation, matrix, lifecycle)


def test_mock_postmortem_documents_live_20run_gap() -> None:
    assert "live 20-run benchmark was not attempted" in _postmortem("mock")


def test_live_postmortem_does_not_claim_a_gap() -> None:
    assert "live 20-run benchmark was not attempted" not in _postmortem("live")
