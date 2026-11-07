"""Report and evidence schemas for the FlowScout submission.

The schemas validate shape and internal arithmetic; :func:`verify_submission`
adds the cross-file invariants that cannot live in a single document (evidence
paths, hashes, trace recovery, live-verification truthfulness, and the
quarantine index). The verifier is intentionally independent of the code that
generates the reports so a bug in the generator cannot silently bless itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SANDBOX_NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LIVE_BROWSER_RUNTIMES = {"celesto_cdp", "guest_playwright_exec", "sandbox_playwright"}

CLASSIFICATIONS = Literal[
    "confirmed_reproducible_defect",
    "suspected_defect_insufficient_evidence",
    "environment_or_infrastructure_failure",
    "invalid_or_inconclusive_test",
    "action_blocked_by_security_policy",
    "successful_test_run",
]
ACTION_TYPES = Literal[
    "navigate",
    "click",
    "fill",
    "assert_text",
    "assert_attribute",
    "read_text",
    "read_attribute",
    "exists",
    "wait_attribute_change",
    "screenshot",
    "wait",
]
CAPABILITY_IDS = Literal[
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
]
SEVERITIES = Literal["low", "medium", "high", "critical"]
POLICIES = Literal["resume", "rollback", "fresh"]
COST_SOURCES = Literal["mock_zero_cost", "celesto_size_rate_wall_clock"]
LIVE_ENTRY_STATUSES = Literal[
    "verified",
    "historical_failure",
    "stale_failure",
    "retained",
    "supporting",
    "quarantined",
]

REQUIRED_SOURCE_FILES = {
    "app/sandbox_runner.py",
    "app/qa_agent.py",
    "app/ownership.py",
    "app/evidence_io.py",
    "app/report_schemas.py",
    "app/guest_browser_launcher.py",
    "app/guest_browser_worker.py",
    "app/guest_healthcheck.py",
    "app/main.py",
    "app/target_saas_app.py",
    "playwright.config.ts",
    "scripts/run_live_demo.py",
    "scripts/verify_submission.py",
}


class _Schema(BaseModel):
    model_config = ConfigDict(extra="allow")


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _is_close(left: float, right: float, tolerance: float = 0.01) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)


def _median(values: list[float]) -> float:
    return round(median(values), 3) if values else 0.0


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return round(ordered[rank - 1], 3)


class ProblemReport(_Schema):
    who: str = Field(min_length=40)
    agent_work: str = Field(min_length=40)
    proof_output: str = Field(min_length=20)
    idea_source: Literal["idea_bank", "original"]


class Capability(_Schema):
    id: CAPABILITY_IDS
    why: str = Field(min_length=80)
    evidence_path: str = Field(min_length=1)


class CapabilitiesReport(_Schema):
    execution_mode: Literal["mock", "live"]
    live_celesto_verified: bool
    capabilities: list[Capability] = Field(min_length=2)

    @model_validator(mode="after")
    def check_capabilities(self) -> "CapabilitiesReport":
        _unique([capability.id for capability in self.capabilities], "capability ids")
        if self.live_celesto_verified and self.execution_mode != "live":
            raise ValueError("live_celesto_verified requires execution_mode=live")
        return self


class LifecycleReport(_Schema):
    cleanup_on_success: bool
    cleanup_on_failure: bool
    cleanup_on_interrupt: bool
    sandbox_names: list[str] = Field(min_length=3)
    verified_at: str = Field(min_length=1)
    evidence_paths: list[str] = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator("sandbox_names")
    @classmethod
    def valid_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not SANDBOX_NAME_RE.match(value):
                raise ValueError(f"invalid sandbox name: {value!r}")
        return values

    @model_validator(mode="after")
    def check_lifecycle(self) -> "LifecycleReport":
        _unique(self.sandbox_names, "lifecycle sandbox names")
        if not all(
            (self.cleanup_on_success, self.cleanup_on_failure, self.cleanup_on_interrupt)
        ):
            raise ValueError("all lifecycle cleanup probes must be true")
        return self


class DefectReport(_Schema):
    classification: CLASSIFICATIONS
    title: str = Field(min_length=10)
    description: str = Field(min_length=20)
    steps_to_reproduce: list[str] = Field(min_length=1)
    expected_behavior: str = Field(min_length=10)
    actual_behavior: str = Field(min_length=10)
    evidence: list[str] = Field(min_length=1)
    severity: SEVERITIES


class ActionRecord(_Schema):
    action_type: ACTION_TYPES
    selector: str = ""
    value: str = ""
    attribute: str = ""
    timeout_ms: int = Field(ge=0)
    description: str = ""
    step: int = Field(ge=0)
    success: bool
    output: str = ""
    error: str = ""
    screenshot: str = ""
    wall_ms: float = Field(ge=0)


class WorkflowOutcomeSchema(_Schema):
    success: bool
    classification: CLASSIFICATIONS
    defect: DefectReport | None
    error: str
    sandbox_name: str
    wall_clock_s: float = Field(ge=0)
    boot_to_ready_s: float = Field(ge=0)
    sandbox_creation_s: float = Field(ge=0)
    deploy_s: float = Field(ge=0)
    browser_start_s: float = Field(ge=0)
    workflow_s: float = Field(ge=0)
    cleanup_s: float = Field(ge=0)
    agent_wall_clock_s: float = Field(ge=0)
    total_wall_clock_s: float = Field(ge=0)
    end_to_end_wall_clock_s: float = Field(ge=0)
    cleanup_ok: bool
    test_paths: list[str]
    execution_mode: Literal["mock", "live"]
    browser_runtime: str = Field(min_length=1)
    planner_mode: Literal["scripted", "llm"]
    actions: list[ActionRecord]

    @field_validator("sandbox_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.match(value):
            raise ValueError(f"invalid sandbox name: {value!r}")
        return value

    @model_validator(mode="after")
    def check_timing_and_claims(self) -> "WorkflowOutcomeSchema":
        if not _is_close(self.wall_clock_s, self.agent_wall_clock_s):
            raise ValueError("wall_clock_s must equal agent_wall_clock_s")
        components = (
            self.sandbox_creation_s
            + self.deploy_s
            + self.browser_start_s
            + self.workflow_s
            + self.cleanup_s
        )
        if (self.success or not self.error) and not _is_close(
            self.total_wall_clock_s, components, tolerance=0.1
        ):
            raise ValueError("total_wall_clock_s must equal the phase timing sum")
        if self.end_to_end_wall_clock_s + 0.1 < self.total_wall_clock_s:
            raise ValueError("end_to_end_wall_clock_s cannot be shorter than the lifecycle")
        if self.success:
            if self.defect is None:
                raise ValueError("a successful workflow must record a defect")
            if self.classification != "confirmed_reproducible_defect":
                raise ValueError("a successful workflow must confirm the defect")
        elif not self.error:
            raise ValueError("a failed workflow must record an error")
        if self.defect is not None and self.defect.classification != self.classification:
            raise ValueError("defect classification must match the outcome classification")
        return self


class RunRecord(_Schema):
    sandbox_name: str
    fresh: bool
    success: bool
    classification: CLASSIFICATIONS
    error: str
    wall_clock_s: float = Field(ge=0)
    boot_to_ready_s: float = Field(ge=0)
    sandbox_creation_s: float = Field(ge=0)
    deploy_s: float = Field(ge=0)
    browser_start_s: float = Field(ge=0)
    workflow_s: float = Field(ge=0)
    cleanup_s: float = Field(ge=0)
    agent_wall_clock_s: float = Field(ge=0)
    total_wall_clock_s: float = Field(ge=0)
    end_to_end_wall_clock_s: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_including_creation_usd: float = Field(ge=0)
    cost_source: COST_SOURCES
    rate_usd_per_hour: float = Field(ge=0)
    start_time: str = Field(min_length=1)
    end_time: str = Field(min_length=1)
    cleanup_ok: bool
    browser_runtime: str = Field(min_length=1)
    browser_verified: bool

    @field_validator("sandbox_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.match(value):
            raise ValueError(f"invalid sandbox name: {value!r}")
        return value

    @model_validator(mode="after")
    def check_run(self) -> "RunRecord":
        if not _is_close(self.wall_clock_s, self.agent_wall_clock_s):
            raise ValueError("wall_clock_s must equal agent_wall_clock_s")
        if self.success and not self.cleanup_ok:
            raise ValueError("a successful run must clean up")
        if self.browser_verified and self.browser_runtime not in LIVE_BROWSER_RUNTIMES:
            raise ValueError("browser_verified requires a live browser runtime")
        components = (
            self.sandbox_creation_s
            + self.deploy_s
            + self.browser_start_s
            + self.workflow_s
            + self.cleanup_s
        )
        if (self.success or not self.error) and not _is_close(
            self.total_wall_clock_s, components, tolerance=0.1
        ):
            raise ValueError("total_wall_clock_s must equal the phase timing sum")
        if self.end_to_end_wall_clock_s + 0.1 < self.total_wall_clock_s:
            raise ValueError("end_to_end_wall_clock_s cannot be shorter than the lifecycle")
        if self.cost_source == "mock_zero_cost":
            if any(
                value != 0.0
                for value in (
                    self.cost_usd,
                    self.cost_including_creation_usd,
                    self.rate_usd_per_hour,
                )
            ):
                raise ValueError("mock costs must all be zero")
        else:
            if self.rate_usd_per_hour <= 0:
                raise ValueError("live cost source requires a positive hourly rate")
            expected_agent = round(self.agent_wall_clock_s / 3600.0 * self.rate_usd_per_hour, 5)
            expected_total = round(
                self.total_wall_clock_s / 3600.0 * self.rate_usd_per_hour, 5
            )
            if not _is_close(self.cost_usd, expected_agent, tolerance=1e-4):
                raise ValueError("cost_usd does not match the agent clock and rate")
            if not _is_close(
                self.cost_including_creation_usd, expected_total, tolerance=1e-4
            ):
                raise ValueError("cost_including_creation_usd does not match its clock")
        return self


class ReliabilityReport(_Schema):
    runs: list[RunRecord] = Field(min_length=1)
    run_count: int = Field(ge=1)
    success_rate: float = Field(ge=0, le=1)
    success_rate_pct: float = Field(ge=0, le=100)
    median_wall_clock_s: float = Field(ge=0)
    p90_wall_clock_s: float = Field(ge=0)
    median_boot_to_ready_s: float = Field(ge=0)
    median_sandbox_creation_s: float = Field(ge=0)
    median_deploy_s: float = Field(ge=0)
    median_browser_start_s: float = Field(ge=0)
    median_workflow_s: float = Field(ge=0)
    median_cleanup_s: float = Field(ge=0)
    median_agent_wall_clock_s: float = Field(ge=0)
    median_total_wall_clock_s: float = Field(ge=0)
    median_end_to_end_wall_clock_s: float = Field(ge=0)
    total_cost_usd: float = Field(ge=0)
    total_cost_including_creation_usd: float = Field(ge=0)
    cost_source: COST_SOURCES
    rate_usd_per_hour: float = Field(ge=0)
    postmortem_path: str = Field(min_length=1)
    evaluation_mode: Literal["mock", "live"]
    sandbox_runtime: str = Field(min_length=1)
    browser_runtime: str = Field(min_length=1)
    browser_runtimes: list[str]
    sandbox_size: str = Field(min_length=1)
    live_celesto_verified: bool
    cost_methodology: str = Field(min_length=20)
    note: str = Field(min_length=40)
    generated_at: str = Field(min_length=1)
    planner: Literal["scripted", "llm"] | None = None

    @model_validator(mode="after")
    def check_reliability(self) -> "ReliabilityReport":
        runs = self.runs
        count = len(runs)
        if self.run_count != count:
            raise ValueError("run_count must equal the number of runs")
        _unique([run.sandbox_name for run in runs], "run sandbox names")

        successes = sum(1 for run in runs if run.success)
        if not _is_close(self.success_rate, successes / count, tolerance=0.001):
            raise ValueError("success_rate does not match the runs")
        if not _is_close(
            self.success_rate_pct, round(100 * successes / count, 2), tolerance=0.01
        ):
            raise ValueError("success_rate_pct does not match the runs")

        wall = [run.wall_clock_s for run in runs]
        boot = [run.boot_to_ready_s for run in runs]
        if not _is_close(self.median_wall_clock_s, _median(wall), tolerance=0.001):
            raise ValueError("median_wall_clock_s does not match the runs")
        if not _is_close(self.p90_wall_clock_s, _p90(wall), tolerance=0.001):
            raise ValueError("p90_wall_clock_s does not match the runs")
        if not _is_close(self.median_boot_to_ready_s, _median(boot), tolerance=0.001):
            raise ValueError("median_boot_to_ready_s does not match the runs")

        for field_name, key in (
            ("median_sandbox_creation_s", "sandbox_creation_s"),
            ("median_deploy_s", "deploy_s"),
            ("median_browser_start_s", "browser_start_s"),
            ("median_workflow_s", "workflow_s"),
            ("median_cleanup_s", "cleanup_s"),
            ("median_agent_wall_clock_s", "agent_wall_clock_s"),
            ("median_total_wall_clock_s", "total_wall_clock_s"),
            ("median_end_to_end_wall_clock_s", "end_to_end_wall_clock_s"),
        ):
            expected = _median([float(getattr(run, key)) for run in runs])
            if not _is_close(getattr(self, field_name), expected, tolerance=0.001):
                raise ValueError(f"{field_name} does not match the runs")

        if not _is_close(
            self.total_cost_usd, round(sum(run.cost_usd for run in runs), 4), tolerance=0.01
        ):
            raise ValueError("total_cost_usd does not match the runs")
        if not _is_close(
            self.total_cost_including_creation_usd,
            round(sum(run.cost_including_creation_usd for run in runs), 4),
            tolerance=0.01,
        ):
            raise ValueError("total_cost_including_creation_usd does not match the runs")

        if any(run.cost_source != self.cost_source for run in runs):
            raise ValueError("run cost_source values must match the report")
        if any(not _is_close(run.rate_usd_per_hour, self.rate_usd_per_hour, tolerance=1e-9) for run in runs):
            raise ValueError("run hourly rates must match the report")

        expected_verified = bool(
            self.evaluation_mode == "live"
            and all(run.success and run.browser_verified and run.cleanup_ok for run in runs)
        )
        if self.live_celesto_verified != expected_verified:
            raise ValueError("live_celesto_verified does not match the run evidence")

        if self.evaluation_mode == "mock":
            if self.cost_source != "mock_zero_cost":
                raise ValueError("mock reports must use mock_zero_cost")
            if any(run.cost_usd != 0 or run.boot_to_ready_s != 0 for run in runs):
                raise ValueError("mock runs must not claim cost or cloud boot time")
            if "host Playwright" not in self.note or "Celesto boot latency" not in self.note:
                raise ValueError("mock note must state the host-browser truth boundary")
        else:
            if self.cost_source != "celesto_size_rate_wall_clock":
                raise ValueError("live reports must use the Celesto size rate")
            if self.rate_usd_per_hour <= 0:
                raise ValueError("live reports need a positive hourly rate")
            if not self.browser_runtimes:
                raise ValueError("live reports must list browser runtimes")
            if not all(run.boot_to_ready_s > 0 for run in runs if run.success):
                raise ValueError("successful live runs must record boot-to-ready time")
        return self


class ContainmentReport(_Schema):
    attack: Literal["prompt_injection_exfil", "destructive_command", "suspicious_package"]
    blocked: bool
    host_untouched: bool
    evidence_paths: list[str] = Field(min_length=1)
    sandbox_name: str
    attack_command: str = Field(min_length=1)
    policy_decision: Literal["allow", "block"]
    execution_mode: Literal["mock", "live"]
    host_canary_sha256: str
    credential_content_absent: bool

    @field_validator("sandbox_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.match(value):
            raise ValueError(f"invalid sandbox name: {value!r}")
        return value

    @field_validator("host_canary_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError("host_canary_sha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def check_containment(self) -> "ContainmentReport":
        if not self.blocked:
            raise ValueError("containment must be blocked")
        if not self.host_untouched:
            raise ValueError("containment must leave the host untouched")
        if not self.credential_content_absent:
            raise ValueError("containment must not leak credential content")
        if self.policy_decision != "block":
            raise ValueError("blocked containment requires a block decision")
        return self


class FailurePolicyReport(_Schema):
    failed_step: int = Field(ge=1)
    total_steps: int = Field(ge=1)
    policy: POLICIES
    rationale: str = Field(min_length=200)
    trace_path: str = Field(min_length=1)
    failed_step_evidence: str = Field(min_length=10)
    replacement_observed: bool

    @model_validator(mode="after")
    def check_policy(self) -> "FailurePolicyReport":
        if self.failed_step != 7:
            raise ValueError("the controlled failure must be step 7")
        if self.total_steps != 10:
            raise ValueError("the workflow must have 10 steps")
        if self.failed_step > self.total_steps:
            raise ValueError("failed_step cannot exceed total_steps")
        if self.trace_path != "reports/failure_trace.jsonl":
            raise ValueError("failure policy must point at the failure trace")
        return self


class ConcurrencyJob(_Schema):
    job_id: str = Field(min_length=1)
    sandbox_name: str
    started_at: str = Field(min_length=1)
    end_time: str = Field(min_length=1)
    duration_s: float = Field(ge=0)
    outcome: Literal["success", "failure"]

    @field_validator("sandbox_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.match(value):
            raise ValueError(f"invalid sandbox name: {value!r}")
        return value


class ConcurrencyReport(_Schema):
    jobs: list[ConcurrencyJob] = Field(min_length=5)
    mode: Literal["sequential"]
    parallelism: Literal[1]
    breakage_notes: str = Field(min_length=40)

    @model_validator(mode="after")
    def check_jobs(self) -> "ConcurrencyReport":
        _unique([job.sandbox_name for job in self.jobs], "concurrency sandbox names")
        return self


class WorkflowReport(_Schema):
    primary: WorkflowOutcomeSchema
    containment: ContainmentReport
    host_test_matrix_path: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_workflow(self) -> "WorkflowReport":
        if self.primary.execution_mode != self.containment.execution_mode:
            raise ValueError("workflow and containment execution modes must match")
        return self


class RuntimeManifest(_Schema):
    generated_at: str = Field(min_length=1)
    execution_mode: Literal["mock", "live"]
    target_url: str = Field(min_length=1)
    python: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    celesto_api_key_configured: bool
    celesto_sdk_importable: bool
    celesto_import_error: str | None = None
    guest_browser_visible_requested: bool
    playwright_python: bool
    node: str = Field(min_length=1)
    npx_playwright: str = Field(min_length=1)
    node_playwright_specs: str = Field(min_length=1)
    source_files: dict[str, str] = Field(min_length=1)

    @field_validator("source_files")
    @classmethod
    def valid_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        for name, digest in values.items():
            if not SHA256_RE.match(digest):
                raise ValueError(f"{name} is not a lowercase SHA-256")
        return values


class LiveEvidenceEntry(_Schema):
    path: str = Field(min_length=1)
    status: LIVE_ENTRY_STATUSES
    note: str = ""
    failure: str = ""
    live_celesto_verified: bool | None = None
    live_guest_browser_verified: bool | None = None


class LiveEvidenceIndex(_Schema):
    generated_at: str = Field(min_length=1)
    current: LiveEvidenceEntry
    historical: list[LiveEvidenceEntry] = Field(default_factory=list)
    quarantined: list[LiveEvidenceEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_current(self) -> "LiveEvidenceIndex":
        if self.current.status != "verified":
            raise ValueError("the current live evidence entry must be verified")
        return self


class LiveSandboxSummary(_Schema):
    name: str
    provider_id: str | None = None
    template_id: str = Field(min_length=1)
    size_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    boot_to_ready_s: float = Field(ge=0)
    deleted_at: str = Field(min_length=1)
    cleanup_ok: bool
    delete_confirmation: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SANDBOX_NAME_RE.match(value):
            raise ValueError(f"invalid sandbox name: {value!r}")
        return value


class LiveOutcomeSummary(_Schema):
    success: bool
    classification: CLASSIFICATIONS
    wall_clock_s: float = Field(ge=0)
    defect: dict[str, Any] | None = None


class LiveEvidenceFile(_Schema):
    path: str = Field(min_length=1)
    sha256: str
    bytes: int = Field(ge=0)
    rows: int = Field(ge=0)
    note: str = ""

    @field_validator("sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError("evidence sha256 must be lowercase SHA-256")
        return value


class LiveScreenshot(_Schema):
    archived_path: str = Field(min_length=1)
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    note: str = ""

    @field_validator("sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256_RE.match(value):
            raise ValueError("screenshot sha256 must be lowercase SHA-256")
        return value


class LiveEvidenceBundle(_Schema):
    actions_log: LiveEvidenceFile
    screenshots: list[LiveScreenshot] = Field(min_length=1)
    command_log: LiveEvidenceFile


class LiveVisibleRecord(_Schema):
    generated_at: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    execution_mode: Literal["live"]
    scope: str = Field(min_length=20)
    live_guest_browser_verified: bool
    live_celesto_cdp_verified: bool
    sandbox: LiveSandboxSummary
    browser: dict[str, Any]
    outcome: LiveOutcomeSummary
    evidence: LiveEvidenceBundle
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_live_record(self) -> "LiveVisibleRecord":
        if not (self.live_guest_browser_verified or self.live_celesto_cdp_verified):
            raise ValueError("live record must verify at least one browser path")
        if not self.sandbox.cleanup_ok:
            raise ValueError("live record must confirm sandbox cleanup")
        if not self.outcome.success:
            raise ValueError("live record must record a successful workflow")
        if self.live_guest_browser_verified:
            runtime = str(self.browser.get("runtime", ""))
            if runtime != "guest_playwright_exec":
                raise ValueError("guest verification requires the guest Playwright runtime")
        return self


@dataclass
class VerificationResult:
    ok: bool
    checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class ReportVerificationError(RuntimeError):
    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        super().__init__("report verification failed: " + "; ".join(result.errors))


def _format_validation(label: str, exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        errors.append(f"{label}: {location}: {item['msg']}")
    return errors


def _read_json(path: Path, label: str, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: cannot read JSON: {type(exc).__name__}: {exc}")
        return None


def _read_jsonl(path: Path, label: str, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"{label}: cannot read: {type(exc).__name__}: {exc}")
        return rows
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}:{number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{label}:{number}: row must be a JSON object")
            continue
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_file(
    root: Path,
    relative: str,
    label: str,
    errors: list[str],
    checked: list[str],
    *,
    min_bytes: int = 0,
) -> Path | None:
    path = root / relative
    if not path.is_file():
        errors.append(f"{label}: missing {relative}")
        return None
    if path.stat().st_size < min_bytes:
        errors.append(f"{label}: {relative} is smaller than {min_bytes} bytes")
        return None
    checked.append(relative)
    return path


def _check_relative_paths(
    root: Path,
    paths: list[str],
    label: str,
    errors: list[str],
    checked: list[str],
) -> None:
    for relative in paths:
        _check_file(root, relative, label, errors, checked)


def _read_env_key(root: Path) -> str:
    env_path = root / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("CELESTO_API_KEY="):
            value = line.strip().partition("=")[2].strip().strip('"').strip("'")
            return value if value and value != "replace-me" else ""
    return ""


def _secret_scan(root: Path, key: str, errors: list[str], checked: list[str]) -> None:
    if not key:
        return
    candidates = list((root / "reports").rglob("*")) if (root / "reports").is_dir() else []
    for path in candidates:
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webm", ".mp4"}:
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if key in text:
            errors.append(f"secret material found in {path.relative_to(root).as_posix()}")
    checked.append("secret-scan")


def _check_command_log(root: Path, errors: list[str], checked: list[str]) -> list[dict[str, Any]]:
    path = root / "logs" / "commands.jsonl"
    if not path.is_file():
        errors.append("command log: missing logs/commands.jsonl")
        return []
    rows = _read_jsonl(path, "logs/commands.jsonl", errors)
    if not rows:
        errors.append("command log: no rows recorded")
        return rows
    for index, row in enumerate(rows, start=1):
        for key in ("ts", "sandbox_name", "command", "decision", "execution_target"):
            if key not in row:
                errors.append(f"command log:{index}: missing {key}")
        if row.get("execution_target") != "celesto_sandbox":
            errors.append(f"command log:{index}: execution_target must be celesto_sandbox")
        if row.get("decision") not in {"allow", "block"}:
            errors.append(f"command log:{index}: decision must be allow or block")
        if not SANDBOX_NAME_RE.match(str(row.get("sandbox_name", ""))):
            errors.append(f"command log:{index}: invalid sandbox_name")
        if not str(row.get("command", "")).strip():
            errors.append(f"command log:{index}: empty command")
    blocked = [
        row for row in rows if row.get("command") == "cat /etc/passwd" and row.get("decision") == "block"
    ]
    if not blocked:
        errors.append("command log: the containment command was not recorded as blocked")
    checked.append("logs/commands.jsonl")
    return rows


def _check_failure_trace(
    root: Path,
    policy: FailurePolicyReport | None,
    errors: list[str],
    checked: list[str],
) -> None:
    path = root / "reports" / "failure_trace.jsonl"
    if not path.is_file():
        errors.append("failure trace: missing reports/failure_trace.jsonl")
        return
    rows = _read_jsonl(path, "reports/failure_trace.jsonl", errors)
    if not rows:
        errors.append("failure trace: no events recorded")
        return
    for index, row in enumerate(rows, start=1):
        for key in ("ts", "step", "event", "state"):
            if key not in row:
                errors.append(f"failure trace:{index}: missing {key}")
    events = {row.get("event") for row in rows}
    states = {row.get("state") for row in rows}
    for required in ("failure", "policy_applied", "sandbox_recreated"):
        if required not in events:
            errors.append(f"failure trace: missing {required} event")
    for required in ("MidTaskFailure", "RolledBack", "Deleted"):
        if required not in states:
            errors.append(f"failure trace: missing {required} state")
    failures = [row for row in rows if row.get("event") == "failure"]
    if not failures:
        errors.append("failure trace: no failure event")
    elif failures[0].get("step") != 7:
        errors.append("failure trace: the controlled failure must be step 7")
    if policy is not None:
        applied = [row for row in rows if row.get("event") == "policy_applied"]
        if not any(row.get("policy") == policy.policy for row in applied):
            errors.append("failure trace: policy_applied does not match failure_policy.json")
    checked.append("reports/failure_trace.jsonl")


def _check_actions_log(root: Path, errors: list[str], checked: list[str]) -> None:
    path = root / "reports" / "evidence" / "actions.jsonl"
    if not path.is_file():
        errors.append("actions log: missing reports/evidence/actions.jsonl")
        return
    rows = _read_jsonl(path, "reports/evidence/actions.jsonl", errors)
    if len(rows) <= 20:
        errors.append("actions log: expected more than 20 browser actions")
    if not any(row.get("action") == "navigate" for row in rows):
        errors.append("actions log: no navigate action")
    if not any(row.get("action") == "screenshot" for row in rows):
        errors.append("actions log: no screenshot action")
    if not all("ts" in row for row in rows):
        errors.append("actions log: every row needs a timestamp")
    checked.append("reports/evidence/actions.jsonl")


def _check_test_matrix(root: Path, errors: list[str], checked: list[str]) -> None:
    path = root / "reports" / "evidence" / "test-matrix.json"
    if not path.is_file():
        errors.append("test matrix: missing reports/evidence/test-matrix.json")
        return
    payload = _read_json(path, "test matrix", errors)
    if not isinstance(payload, dict):
        return
    if payload.get("all_expectations_met") is not True:
        errors.append("test matrix: all_expectations_met must be true")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 4:
        errors.append("test matrix: expected four cases")
    else:
        matrix = {
            (case.get("spec"), case.get("test_mode")): case
            for case in cases
            if isinstance(case, dict)
        }
        if matrix.get(("coupon_stacking_test.spec.ts", "broken"), {}).get("passed") is not True:
            errors.append("test matrix: reproduction must pass on the broken build")
        if matrix.get(("coupon_regression_test.spec.ts", "fixed"), {}).get("passed") is not True:
            errors.append("test matrix: regression must pass on the fixed build")
    checked.append("reports/evidence/test-matrix.json")


def _check_containment_log(root: Path, errors: list[str], checked: list[str]) -> None:
    path = root / "reports" / "evidence" / "containment.log"
    if not path.is_file():
        errors.append("containment log: missing reports/evidence/containment.log")
        return
    text = path.read_text(encoding="utf-8")
    for token in (
        "host_canary_sha256_before=",
        "host_canary_sha256_after=",
        "blocked=true",
        "host_untouched=true",
    ):
        if token not in text:
            errors.append(f"containment log: missing {token}")
    checked.append("reports/evidence/containment.log")


def _check_lifecycle_checks(root: Path, errors: list[str], checked: list[str]) -> None:
    path = root / "reports" / "evidence" / "lifecycle-checks.json"
    if not path.is_file():
        errors.append("lifecycle checks: missing reports/evidence/lifecycle-checks.json")
        return
    payload = _read_json(path, "lifecycle checks", errors)
    if not isinstance(payload, dict):
        return
    for key in ("cleanup_on_failure", "cleanup_on_interrupt", "interrupt_observed"):
        if payload.get(key) is not True:
            errors.append(f"lifecycle checks: {key} must be true")
    checked.append("reports/evidence/lifecycle-checks.json")


def _check_walkthrough(root: Path, errors: list[str], checked: list[str]) -> None:
    metadata_path = root / "reports" / "evidence" / "walkthrough.json"
    if not metadata_path.is_file():
        errors.append("walkthrough: missing reports/evidence/walkthrough.json")
    else:
        metadata = _read_json(metadata_path, "walkthrough", errors)
        if isinstance(metadata, dict):
            if metadata.get("mp4_created") is not True:
                errors.append("walkthrough: mp4_created must be true")
            if not str(metadata.get("source_video", "")).endswith(".webm"):
                errors.append("walkthrough: source_video must be a webm recording")
        checked.append("reports/evidence/walkthrough.json")
    _check_file(root, "demo/walkthrough.mp4", "walkthrough", errors, checked, min_bytes=10_000)


def _check_runtime_manifest(
    root: Path,
    manifest: RuntimeManifest | None,
    errors: list[str],
    checked: list[str],
) -> None:
    if manifest is None:
        return
    missing = sorted(REQUIRED_SOURCE_FILES - set(manifest.source_files))
    if missing:
        errors.append("runtime manifest: missing source hashes: " + ", ".join(missing))
    for relative, digest in sorted(manifest.source_files.items()):
        path = root / relative
        if not path.is_file():
            errors.append(f"runtime manifest: hashed file is missing: {relative}")
            continue
        actual = _sha256(path)
        if actual != digest:
            errors.append(f"runtime manifest: hash mismatch for {relative}")
    checked.append("reports/evidence/runtime-manifest.json")


def _check_postmortem(
    root: Path,
    reliability: ReliabilityReport | None,
    errors: list[str],
    checked: list[str],
) -> None:
    path = root / "reports" / "postmortem.md"
    if not path.is_file():
        errors.append("postmortem: missing reports/postmortem.md")
        return
    text = path.read_text(encoding="utf-8")
    if len(text) < 200:
        errors.append("postmortem: shorter than 200 characters")
    if "Controlled step-7 failure" not in text:
        errors.append("postmortem: missing the controlled step-7 section")
    if "concurrency" not in text.lower():
        errors.append("postmortem: missing the concurrency limitation")
    if reliability is not None:
        if f"Evaluation mode: {reliability.evaluation_mode}" not in text:
            errors.append("postmortem: evaluation mode does not match reliability.json")
        if (
            reliability.evaluation_mode == "mock"
            and "live 20-run benchmark was not attempted" not in text
        ):
            errors.append("postmortem: mock mode must state the live 20-run gap")
    checked.append("reports/postmortem.md")


def _check_live_evidence(
    root: Path,
    errors: list[str],
    checked: list[str],
) -> None:
    evidence_dir = root / "reports" / "live-evidence"
    index_path = evidence_dir / "index.json"
    if not index_path.is_file():
        errors.append("live evidence: missing reports/live-evidence/index.json")
        return
    payload = _read_json(index_path, "live evidence index", errors)
    if not isinstance(payload, dict):
        return
    try:
        index = LiveEvidenceIndex.model_validate(payload)
    except ValidationError as exc:
        errors.extend(_format_validation("live evidence index", exc))
        return
    checked.append("reports/live-evidence/index.json")

    current_path = root / index.current.path
    if not current_path.is_file():
        errors.append(f"live evidence: current record is missing: {index.current.path}")
    else:
        current_payload = _read_json(current_path, "current live evidence", errors)
        if isinstance(current_payload, dict):
            try:
                record = LiveVisibleRecord.model_validate(current_payload)
            except ValidationError as exc:
                errors.extend(_format_validation("current live evidence", exc))
            else:
                _check_live_record_files(root, record, errors, checked)
                if not (
                    index.current.live_guest_browser_verified
                    or index.current.live_celesto_cdp_verified
                ):
                    errors.append("live evidence: index current entry must state a verified path")
                if (
                    index.current.live_guest_browser_verified
                    is not None
                    and index.current.live_guest_browser_verified
                    != record.live_guest_browser_verified
                ):
                    errors.append("live evidence: index guest-browser flag disagrees with the record")
                if (
                    index.current.live_celesto_cdp_verified is not None
                    and index.current.live_celesto_cdp_verified
                    != record.live_celesto_cdp_verified
                ):
                    errors.append("live evidence: index CDP flag disagrees with the record")
        checked.append(index.current.path)

    for entry in [*index.historical, *index.quarantined]:
        path = root / entry.path
        if not path.is_file():
            errors.append(f"live evidence: indexed file is missing: {entry.path}")
            continue
        if path.suffix.lower() == ".json":
            value = _read_json(path, f"live evidence {entry.path}", errors)
            if isinstance(value, dict) and value.get("live_celesto_verified") is True:
                errors.append(
                    f"live evidence: {entry.path} is indexed as {entry.status} but claims verification"
                )
        checked.append(entry.path)

    for stale in ("live-smoke-429.json", "live-smoke-429-1802.json"):
        if (evidence_dir / stale).exists():
            errors.append(f"live evidence: stale failure {stale} must be quarantined and indexed")
    live_smoke = evidence_dir / "live-smoke.json"
    if live_smoke.exists():
        value = _read_json(live_smoke, "live evidence live-smoke.json", errors)
        if not isinstance(value, dict) or value.get("live_celesto_verified") is not True:
            errors.append(
                "live evidence: unverified live-smoke.json must be quarantined and indexed"
            )


def _check_live_record_files(
    root: Path,
    record: LiveVisibleRecord,
    errors: list[str],
    checked: list[str],
) -> None:
    for label, item in (
        ("actions log", record.evidence.actions_log),
        ("command log", record.evidence.command_log),
    ):
        path = root / item.path
        if not path.is_file():
            errors.append(f"live evidence: missing {label}: {item.path}")
            continue
        if path.stat().st_size != item.bytes:
            errors.append(f"live evidence: {label} byte count does not match")
        if _sha256(path) != item.sha256:
            errors.append(f"live evidence: {label} hash does not match")
        rows = _read_jsonl(path, f"live evidence {label}", errors)
        if len(rows) != item.rows:
            errors.append(f"live evidence: {label} row count does not match")
        checked.append(item.path)
    for shot in record.evidence.screenshots:
        path = root / shot.archived_path
        if not path.is_file():
            errors.append(f"live evidence: missing screenshot {shot.archived_path}")
            continue
        if _sha256(path) != shot.sha256:
            errors.append(f"live evidence: screenshot hash does not match {shot.archived_path}")
        checked.append(shot.archived_path)


def verify_submission(root: Path | str) -> VerificationResult:
    """Validate the current report/evidence tree.

    Returns a result instead of raising so callers can print every problem at
    once. ``commit_artifacts`` converts a failed result into a rollback.
    """
    root = Path(root)
    errors: list[str] = []
    warnings: list[str] = []
    checked: list[str] = []

    report_models: dict[str, type[BaseModel]] = {
        "problem.json": ProblemReport,
        "capabilities.json": CapabilitiesReport,
        "lifecycle.json": LifecycleReport,
        "reliability.json": ReliabilityReport,
        "containment.json": ContainmentReport,
        "failure_policy.json": FailurePolicyReport,
        "concurrency.json": ConcurrencyReport,
        "workflow.json": WorkflowReport,
    }
    loaded: dict[str, BaseModel] = {}
    for name, model in report_models.items():
        path = root / "reports" / name
        if not path.is_file():
            errors.append(f"report {name}: missing")
            continue
        payload = _read_json(path, f"report {name}", errors)
        if payload is None:
            continue
        try:
            loaded[name] = model.model_validate(payload)
        except ValidationError as exc:
            errors.extend(_format_validation(f"report {name}", exc))
            continue
        checked.append(f"reports/{name}")

    manifest_path = root / "reports" / "evidence" / "runtime-manifest.json"
    manifest: RuntimeManifest | None = None
    if not manifest_path.is_file():
        errors.append("runtime manifest: missing reports/evidence/runtime-manifest.json")
    else:
        payload = _read_json(manifest_path, "runtime manifest", errors)
        if isinstance(payload, dict):
            try:
                manifest = RuntimeManifest.model_validate(payload)
            except ValidationError as exc:
                errors.extend(_format_validation("runtime manifest", exc))
            else:
                checked.append("reports/evidence/runtime-manifest.json")

    problem = loaded.get("problem.json")
    capabilities = loaded.get("capabilities.json")
    lifecycle = loaded.get("lifecycle.json")
    reliability = loaded.get("reliability.json")
    containment = loaded.get("containment.json")
    failure_policy = loaded.get("failure_policy.json")
    workflow = loaded.get("workflow.json")

    if isinstance(capabilities, CapabilitiesReport):
        _check_relative_paths(
            root,
            [capability.evidence_path for capability in capabilities.capabilities],
            "capabilities evidence",
            errors,
            checked,
        )
    if isinstance(lifecycle, LifecycleReport):
        _check_relative_paths(
            root, lifecycle.evidence_paths, "lifecycle evidence", errors, checked
        )
    if isinstance(containment, ContainmentReport):
        _check_relative_paths(
            root, containment.evidence_paths, "containment evidence", errors, checked
        )
    if isinstance(failure_policy, FailurePolicyReport):
        _check_file(
            root, failure_policy.trace_path, "failure policy trace", errors, checked
        )
    if isinstance(workflow, WorkflowReport):
        _check_file(
            root,
            workflow.host_test_matrix_path,
            "workflow test matrix",
            errors,
            checked,
        )

    _check_postmortem(root, reliability, errors, checked)
    _check_failure_trace(root, failure_policy, errors, checked)
    _check_actions_log(root, errors, checked)
    _check_test_matrix(root, errors, checked)
    _check_containment_log(root, errors, checked)
    _check_lifecycle_checks(root, errors, checked)
    _check_walkthrough(root, errors, checked)
    _check_runtime_manifest(root, manifest, errors, checked)
    _check_live_evidence(root, errors, checked)
    command_rows = _check_command_log(root, errors, checked)

    if isinstance(lifecycle, LifecycleReport):
        known = {str(row.get("sandbox_name")) for row in command_rows}
        if isinstance(reliability, ReliabilityReport):
            known.update(run.sandbox_name for run in reliability.runs)
        unknown = sorted(set(lifecycle.sandbox_names) - known)
        if unknown:
            errors.append("lifecycle: unknown sandbox names: " + ", ".join(unknown))
        if isinstance(workflow, WorkflowReport):
            if workflow.primary.sandbox_name not in lifecycle.sandbox_names:
                errors.append("workflow: primary sandbox is not in lifecycle.json")
            if workflow.containment.sandbox_name not in lifecycle.sandbox_names:
                errors.append("workflow: containment sandbox is not in lifecycle.json")

    if isinstance(reliability, ReliabilityReport):
        if not (root / reliability.postmortem_path).is_file():
            errors.append("reliability: postmortem_path does not exist")
        if isinstance(capabilities, CapabilitiesReport):
            if reliability.evaluation_mode != capabilities.execution_mode:
                errors.append("reliability and capabilities execution modes differ")
            if reliability.live_celesto_verified != capabilities.live_celesto_verified:
                errors.append("reliability and capabilities live flags differ")
        if isinstance(workflow, WorkflowReport):
            if reliability.evaluation_mode != workflow.primary.execution_mode:
                errors.append("reliability and workflow execution modes differ")
        if isinstance(manifest, RuntimeManifest):
            if reliability.evaluation_mode != manifest.execution_mode:
                errors.append("reliability and runtime manifest execution modes differ")
        if isinstance(problem, ProblemReport) and reliability.evaluation_mode == "mock":
            if str(reliability.run_count) not in problem.proof_output:
                warnings.append(
                    "problem.proof_output does not mention the evaluation run count"
                )

    _secret_scan(root, _read_env_key(root), errors, checked)

    return VerificationResult(
        ok=not errors,
        checked=sorted(set(checked)),
        errors=errors,
        warnings=warnings,
    )
