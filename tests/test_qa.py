from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.qa_agent import (
    PROMPT_INJECTION_REVIEW,
    ActionRecord,
    BrowserAction,
    BrowserSession,
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
    assert outcome.agent_wall_clock_s >= 0
    assert outcome.sandbox_creation_s >= 0
    assert outcome.browser_start_s >= 0
    assert outcome.cleanup_s >= 0
    assert outcome.total_wall_clock_s >= 0
    assert outcome.wall_clock_s == outcome.agent_wall_clock_s
    payload = outcome.to_dict()
    for key in (
        "sandbox_creation_s",
        "deploy_s",
        "browser_start_s",
        "workflow_s",
        "cleanup_s",
        "agent_wall_clock_s",
        "total_wall_clock_s",
        "end_to_end_wall_clock_s",
    ):
        assert key in payload
        assert payload[key] >= 0


def test_interrupt_cleanup_releases_sandbox(tmp_path: Path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    name = "sbx-cleanintr1"
    runner = QAWorkflowRunner(adapter, name, "http://127.0.0.1:8090")
    with patch("app.qa_agent.BrowserSession.start", side_effect=KeyboardInterrupt()):
        with pytest.raises(KeyboardInterrupt):
            runner.run(inject_failure=False)
    assert adapter.get_info(name) is None

def test_action_schema_accepts_condition_actions() -> None:
    exists = BrowserAction(
        ActionType.EXISTS, selector="[data-testid='failure-page']"
    )
    wait = BrowserAction(
        ActionType.WAIT_ATTRIBUTE_CHANGE,
        selector="[data-testid='cart-total']",
        attribute="data-value",
        value="129.97",
        timeout_ms=2000,
    )
    assert exists.action_type == ActionType.EXISTS
    assert wait.attribute == "data-value"


def test_action_schema_requires_selector_and_attribute_for_condition_actions() -> None:
    with pytest.raises(InvalidAction):
        BrowserAction(ActionType.EXISTS)
    with pytest.raises(InvalidAction):
        BrowserAction(
            ActionType.WAIT_ATTRIBUTE_CHANGE,
            selector="[data-testid='cart-total']",
        )


class ScriptedCouponBrowser:
    """Minimal browser double that records the coupon action sequence."""

    def __init__(
        self,
        *,
        failure_page: bool = False,
        total_changes: bool = True,
        wait_changes: bool = True,
    ) -> None:
        self.failure_page = failure_page
        self.total_changes = total_changes
        self.wait_changes = wait_changes
        self.actions: list[ActionType] = []
        self._reads = 0

    def execute(self, action: BrowserAction, step: int) -> ActionRecord:
        self.actions.append(action.action_type)
        output = ""
        if action.action_type == ActionType.READ_ATTRIBUTE:
            self._reads += 1
            if self._reads == 1:
                output = "129.97"
            elif self._reads == 2:
                output = "103.98" if self.total_changes else "129.97"
            else:
                output = (
                    "103.98"
                    if (self.total_changes or self.wait_changes)
                    else "129.97"
                )
        elif action.action_type == ActionType.EXISTS:
            output = "true" if self.failure_page else "false"
        elif action.action_type == ActionType.WAIT_ATTRIBUTE_CHANGE:
            output = "103.98" if self.wait_changes else action.value
        return ActionRecord(
            step=step,
            action=action,
            success=True,
            output=output,
        )


def make_coupon_runner(tmp_path: Path, browser: ScriptedCouponBrowser) -> QAWorkflowRunner:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "log.jsonl")
    runner = QAWorkflowRunner(adapter, "sbx-couponwait1", "http://127.0.0.1:8090")
    runner.browser = browser  # type: ignore[assignment]
    runner._step = 6
    return runner


def test_coupon_apply_uses_condition_checks_without_fixed_wait(tmp_path: Path) -> None:
    browser = ScriptedCouponBrowser()
    runner = make_coupon_runner(tmp_path, browser)

    assert runner._apply_coupon_ui() is True
    assert browser.actions == [
        ActionType.READ_ATTRIBUTE,
        ActionType.FILL,
        ActionType.CLICK,
        ActionType.EXISTS,
        ActionType.READ_ATTRIBUTE,
    ]
    assert ActionType.WAIT not in browser.actions


def test_coupon_apply_detects_503_failure_page_immediately(tmp_path: Path) -> None:
    browser = ScriptedCouponBrowser(failure_page=True)
    runner = make_coupon_runner(tmp_path, browser)

    assert runner._apply_coupon_ui(required=False) is False
    assert browser.actions == [
        ActionType.READ_ATTRIBUTE,
        ActionType.FILL,
        ActionType.CLICK,
        ActionType.EXISTS,
    ]
    assert ActionType.WAIT not in browser.actions


def test_coupon_apply_falls_back_to_attribute_change_wait(tmp_path: Path) -> None:
    browser = ScriptedCouponBrowser(total_changes=False, wait_changes=True)
    runner = make_coupon_runner(tmp_path, browser)

    assert runner._apply_coupon_ui() is True
    assert browser.actions == [
        ActionType.READ_ATTRIBUTE,
        ActionType.FILL,
        ActionType.CLICK,
        ActionType.EXISTS,
        ActionType.READ_ATTRIBUTE,
        ActionType.WAIT_ATTRIBUTE_CHANGE,
        ActionType.READ_ATTRIBUTE,
    ]


def test_coupon_apply_still_rejects_an_unchanged_total(tmp_path: Path) -> None:
    browser = ScriptedCouponBrowser(total_changes=False, wait_changes=False)
    runner = make_coupon_runner(tmp_path, browser)

    with pytest.raises(RuntimeError, match="did not change"):
        runner._apply_coupon_ui()


class FakeLocator:
    def __init__(self, *, count: int = 1, values: list[str] | None = None) -> None:
        self._count = count
        self._values = values or ["129.97"]
        self._calls = 0

    def count(self) -> int:
        return self._count

    def get_attribute(self, name: str, timeout: int | None = None) -> str:
        value = self._values[min(self._calls, len(self._values) - 1)]
        self._calls += 1
        return value


class FakePage:
    def __init__(self, locator: FakeLocator) -> None:
        self._locator = locator
        self.waits = 0

    def locator(self, selector: str) -> FakeLocator:
        return self._locator

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


def test_host_exists_and_wait_attribute_change(tmp_path: Path) -> None:
    session = BrowserSession("http://127.0.0.1:8090")
    locator = FakeLocator(values=["129.97", "103.98"])
    session._page = FakePage(locator)
    session._write_action_log = lambda record: None  # type: ignore[method-assign]

    exists = session.execute(
        BrowserAction(ActionType.EXISTS, selector="[data-testid='failure-page']"), 1
    )
    assert exists.success and exists.output == "true"

    changed = session.execute(
        BrowserAction(
            ActionType.WAIT_ATTRIBUTE_CHANGE,
            selector="[data-testid='cart-total']",
            attribute="data-value",
            value="129.97",
            timeout_ms=2000,
        ),
        2,
    )
    assert changed.success and changed.output == "103.98"
    assert session._page.waits == 1
