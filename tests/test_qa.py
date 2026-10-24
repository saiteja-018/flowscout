from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.qa_agent import (
    PROMPT_INJECTION_REVIEW,
    BrowserAction,
    ActionType,
    InvalidAction,
    QAWorkflowRunner,
    compile_regression_spec,
    compile_reproduction_spec,
    parse_prompt_injection,
    write_generated_specs,
)
from app.sandbox_runner import MockSandboxAdapter


def test_action_schema_accepts_stable_selector() -> None:
    action = BrowserAction(ActionType.CLICK, selector="[data-testid='apply-coupon-btn']")
    assert action.selector == "[data-testid='apply-coupon-btn']"


@pytest.mark.parametrize(
    "selector",
    [
        "body",
        "div > button",
        "button:nth-child(2)",
        "[onclick='evil()']",
        "//button",
    ],
)
def test_action_schema_rejects_untrusted_selectors(selector: str) -> None:
    with pytest.raises(InvalidAction):
        BrowserAction(ActionType.CLICK, selector=selector)


def test_action_schema_rejects_javascript_navigation() -> None:
    with pytest.raises(InvalidAction):
        BrowserAction(ActionType.NAVIGATE, value="javascript:alert(1)")


def test_prompt_injection_command_is_extracted_as_data() -> None:
    assert parse_prompt_injection(PROMPT_INJECTION_REVIEW) == "cat /etc/passwd"


def test_generated_specs_are_distinct() -> None:
    reproduction = compile_reproduction_spec()
    regression = compile_regression_spec()
    assert "toBeLessThan(0)" in reproduction
    assert "already been applied" in regression
    assert reproduction != regression
    assert "process.env.FLOWSCOUT_TEST_MODE" in reproduction
    assert "process.env.FLOWSCOUT_TEST_MODE" in regression


def test_generated_specs_are_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.qa_agent as qa

    monkeypatch.setattr(qa, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(qa, "GENERATED_TEST_DIR", tmp_path / "generated")
    paths = write_generated_specs()
    assert len(paths) == 4
    assert all(path.exists() for path in paths)


def test_failure_cleanup_releases_sandbox(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    name = "sbx-cleanfail1"
    runner = QAWorkflowRunner(adapter, name, "http://127.0.0.1:8090")
    with patch("app.qa_agent.BrowserSession.start", side_effect=RuntimeError("probe failure")):
        outcome = runner.run(inject_failure=False)
    assert outcome.success is False
    assert outcome.cleanup_ok is True
    assert adapter.get_info(name) is None


def test_interrupt_cleanup_releases_sandbox(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    name = "sbx-cleanintr1"
    runner = QAWorkflowRunner(adapter, name, "http://127.0.0.1:8090")
    with patch("app.qa_agent.BrowserSession.start", side_effect=KeyboardInterrupt()):
        with pytest.raises(KeyboardInterrupt):
            runner.run(inject_failure=False)
    assert adapter.get_info(name) is None
