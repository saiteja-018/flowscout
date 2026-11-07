from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS = PROJECT_ROOT / "reports"
EVIDENCE = REPORTS / "evidence"
SANDBOX_NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
ALLOWED_CAPABILITY_IDS = {
    "persistence",
    "snapshots",
    "celestofs",
    "browser",
    "published_ports",
    "network_policy",
    "safety_hooks",
    "host_mounts",
    "coding_agent_preset",
    "agentor_durable",
}
ALLOWED_ATTACKS = {"prompt_injection_exfil", "destructive_command", "suspicious_package"}


def load(name: str):
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize(
    "relative",
    [
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
    ],
)
def test_required_report_exists(relative: str) -> None:
    path = REPORTS / relative
    assert path.is_file(), f"missing required report: {relative}"
    assert path.stat().st_size > 0


def test_problem_schema() -> None:
    report = load("problem.json")
    assert len(report["who"]) >= 40
    assert len(report["agent_work"]) >= 40
    assert len(report["proof_output"]) >= 20
    assert report["idea_source"] in {"idea_bank", "original"}


def test_capabilities_schema_and_evidence() -> None:
    report = load("capabilities.json")
    capabilities = report["capabilities"]
    assert len(capabilities) >= 2
    ids = [capability["id"] for capability in capabilities]
    assert len(ids) == len(set(ids)), "capability ids must be unique"
    for capability in capabilities:
        assert capability["id"] in ALLOWED_CAPABILITY_IDS
        assert len(capability["why"]) >= 80
        assert (PROJECT_ROOT / capability["evidence_path"]).is_file()


def test_lifecycle_contract() -> None:
    report = load("lifecycle.json")
    assert report["cleanup_on_success"] is True
    assert report["cleanup_on_failure"] is True
    assert report["cleanup_on_interrupt"] is True
    names = report["sandbox_names"]
    assert len(names) >= 3
    assert len(names) == len(set(names))
    assert all(SANDBOX_NAME_RE.match(name) for name in names)
    known = known_sandbox_names()
    assert all(name in known for name in names)


def known_sandbox_names() -> set[str]:
    names: set[str] = set()
    for row in load_jsonl(PROJECT_ROOT / "logs" / "commands.jsonl"):
        names.add(row["sandbox_name"])
    reliability = load("reliability.json")
    names.update(run["sandbox_name"] for run in reliability["runs"])
    return names


def test_failure_policy_contract() -> None:
    report = load("failure_policy.json")
    assert report["failed_step"] == 7
    assert report["total_steps"] == 10
    assert report["policy"] in {"resume", "rollback", "fresh"}
    assert len(report["rationale"]) >= 200
    assert report["trace_path"] == "reports/failure_trace.jsonl"
    assert (PROJECT_ROOT / report["trace_path"]).is_file()


def test_failure_trace_policy_event_matches() -> None:
    rows = load_jsonl(REPORTS / "failure_trace.jsonl")
    policy = load("failure_policy.json")["policy"]
    applied = [row for row in rows if row["event"] == "policy_applied"]
    assert applied
    assert any(row.get("policy") == policy for row in applied)


def test_containment_contract() -> None:
    report = load("containment.json")
    assert report["attack"] in ALLOWED_ATTACKS
    assert report["blocked"] is True
    assert report["host_untouched"] is True
    assert report["evidence_paths"]
    for path in report["evidence_paths"]:
        assert (PROJECT_ROOT / path).is_file(), f"missing evidence: {path}"


def test_containment_log_is_independent() -> None:
    log = (EVIDENCE / "containment.log").read_text(encoding="utf-8")
    assert "host_canary_sha256_before=" in log
    assert "host_canary_sha256_after=" in log
    assert "blocked=true" in log
    assert "host_untouched=true" in log


def test_reliability_top_level_schema() -> None:
    report = load("reliability.json")
    runs = report["runs"]
    assert report["run_count"] == len(runs)
    assert report["run_count"] >= 20
    assert report["postmortem_path"] == "reports/postmortem.md"
    assert (PROJECT_ROOT / report["postmortem_path"]).stat().st_size >= 200
    names = [run["sandbox_name"] for run in runs]
    assert len(names) == len(set(names))
    assert all(SANDBOX_NAME_RE.match(name) for name in names)
    for run in runs:
        assert run["fresh"] is True
        assert isinstance(run["success"], bool)
        assert run["wall_clock_s"] >= 0
        assert run["boot_to_ready_s"] >= 0
        assert run["cost_usd"] >= 0


def spec_median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def test_reliability_stats_match_runs() -> None:
    report = load("reliability.json")
    runs = report["runs"]
    n = len(runs)
    successes = sum(1 for run in runs if run["success"])
    assert report["success_rate"] == pytest.approx(successes / n, abs=0.001)
    wall = [run["wall_clock_s"] for run in runs]
    boot = [run["boot_to_ready_s"] for run in runs]
    assert report["median_wall_clock_s"] == pytest.approx(spec_median(wall), abs=0.001)
    p90_rank = -(-9 * n // 10)  # ceil(0.9 * n), 1-based
    assert report["p90_wall_clock_s"] == pytest.approx(sorted(wall)[p90_rank - 1], abs=0.001)
    assert report["median_boot_to_ready_s"] == pytest.approx(spec_median(boot), abs=0.001)
    assert report["total_cost_usd"] == pytest.approx(sum(run["cost_usd"] for run in runs), abs=0.01)


def test_reliability_mode_is_honest() -> None:
    report = load("reliability.json")
    mode = report["evaluation_mode"]
    assert mode in {"mock", "live"}
    expected_verified = bool(
        mode == "live"
        and report["runs"]
        and all(
            run.get("success")
            and run.get("browser_verified")
            and run.get("cleanup_ok")
            for run in report["runs"]
        )
    )
    assert report["live_celesto_verified"] is expected_verified
    if mode == "live":
        assert report["browser_runtimes"]
        assert all(run["browser_runtime"] for run in report["runs"])
    if mode == "mock":
        assert all(run["cost_usd"] == 0.0 for run in report["runs"])
        assert all(run["boot_to_ready_s"] == 0.0 for run in report["runs"])
        assert "host Playwright" in report["note"]
        assert "Celesto boot latency" in report["note"]
    else:
        successful = [run for run in report["runs"] if run["success"]]
        assert successful, "live evaluation recorded no successful runs"
        assert all(run["boot_to_ready_s"] > 0 for run in successful)
        assert all(run["cost_usd"] > 0 for run in report["runs"])


def test_command_log_required_fields() -> None:
    rows = load_jsonl(PROJECT_ROOT / "logs" / "commands.jsonl")
    assert len(rows) > 0
    for row in rows:
        assert row["execution_target"] == "celesto_sandbox"
        for key in ("ts", "sandbox_name", "command", "decision", "execution_target"):
            assert key in row
        assert row["decision"] in {"allow", "block"}
        assert SANDBOX_NAME_RE.match(row["sandbox_name"])
        assert row["command"].strip()


def test_command_log_contains_containment_block() -> None:
    rows = load_jsonl(PROJECT_ROOT / "logs" / "commands.jsonl")
    blocked = [row for row in rows if row["command"] == "cat /etc/passwd"]
    assert blocked
    assert blocked[-1]["decision"] == "block"
    assert blocked[-1]["execution_target"] == "celesto_sandbox"


def test_failure_trace_has_recovery() -> None:
    rows = load_jsonl(REPORTS / "failure_trace.jsonl")
    events = {row["event"] for row in rows}
    states = {row["state"] for row in rows}
    assert "failure" in events
    assert "policy_applied" in events
    assert "sandbox_recreated" in events
    assert "MidTaskFailure" in states
    assert "RolledBack" in states
    assert "Deleted" in states


def test_failure_trace_records_step_seven() -> None:
    rows = load_jsonl(REPORTS / "failure_trace.jsonl")
    failures = [row for row in rows if row["event"] == "failure"]
    assert failures
    assert failures[0]["step"] == 7


def test_generated_tests_exist() -> None:
    stacking = EVIDENCE / "coupon_stacking_test.spec.ts"
    regression = EVIDENCE / "coupon_regression_test.spec.ts"
    assert stacking.is_file()
    assert regression.is_file()
    text = stacking.read_text(encoding="utf-8")
    assert "toBeLessThan(0)" in text
    assert "SAVE20" in text


def test_generated_test_matrix() -> None:
    report = load("evidence/test-matrix.json")
    assert report["all_expectations_met"] is True
    assert len(report["cases"]) == 4
    matrix = {(case["spec"], case["test_mode"]): case for case in report["cases"]}
    assert matrix[("coupon_stacking_test.spec.ts", "broken")]["passed"] is True
    assert matrix[("coupon_regression_test.spec.ts", "fixed")]["passed"] is True


def test_action_log_has_browser_evidence() -> None:
    rows = load_jsonl(EVIDENCE / "actions.jsonl")
    assert len(rows) > 20
    assert any(row["action"] == "navigate" for row in rows)
    assert any(row["action"] == "screenshot" for row in rows)
    assert all("ts" in row for row in rows)


def test_walkthrough_video_is_real_and_nonempty() -> None:
    video = PROJECT_ROOT / "demo" / "walkthrough.mp4"
    metadata = load("evidence/walkthrough.json")
    assert video.is_file()
    assert video.stat().st_size > 10_000
    assert metadata["mp4_created"] is True
    assert metadata["source_video"].endswith(".webm")


def test_postmortem_documents_truth_boundary() -> None:
    reliability = load("reliability.json")
    text = (REPORTS / "postmortem.md").read_text(encoding="utf-8")
    assert f"Evaluation mode: {reliability['evaluation_mode']}" in text
    assert "Controlled step-7 failure" in text
    assert "concurrency" in text.lower()
    if reliability["evaluation_mode"] == "mock":
        assert "live 20-run benchmark was not attempted" in text
    assert len(text) >= 200


def test_concurrency_contract() -> None:
    report = load("concurrency.json")
    jobs = report["jobs"]
    assert len(jobs) >= 5
    names = [job["sandbox_name"] for job in jobs]
    assert len(names) == len(set(names))
    assert all(SANDBOX_NAME_RE.match(name) for name in names)
    for job in jobs:
        assert job["started_at"]
    assert len(report["breakage_notes"]) >= 40
    assert report["parallelism"] == 1
    assert report["mode"] == "sequential"


def test_runtime_manifest_records_no_secret_material() -> None:
    report = load("evidence/runtime-manifest.json")
    raw = (EVIDENCE / "runtime-manifest.json").read_text(encoding="utf-8")
    assert report["execution_mode"] in {"mock", "live"}
    assert report["source_files"]
    # Whatever the mode, the key value itself must never appear in evidence.
    env_key = read_env_key()
    if env_key:
        assert env_key not in raw


def read_env_key() -> str:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("CELESTO_API_KEY="):
            value = line.strip().partition("=")[2].strip().strip('"').strip("'")
            return value if value and value != "replace-me" else ""
    return ""


def test_lifecycle_probe_evidence() -> None:
    report = load("evidence/lifecycle-checks.json")
    assert report["cleanup_on_failure"] is True
    assert report["cleanup_on_interrupt"] is True
    assert report["interrupt_observed"] is True


def test_runs_are_not_overclaimed() -> None:
    reliability = load("reliability.json")
    if reliability["evaluation_mode"] == "mock":
        assert reliability["note"].startswith(
            f"{reliability['run_count']} deterministic evaluation runs"
        )
