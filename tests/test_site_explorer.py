from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.qa_agent import InvalidAction
from app.site_explorer import (
    PublicSiteExplorer,
    RegressionRunner,
    generate_tests,
    validate_test_case,
)


def _resolver(*addresses: str):
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))
            for address in addresses
        ]

    return fake_getaddrinfo


class FakeDriver:
    runtime = "fake"

    def __init__(self, *, outputs: dict[str, str] | None = None, fail_types: set[str] | None = None):
        self.outputs = outputs or {}
        self.fail_types = fail_types or set()
        self.calls: list[list[dict]] = []

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reset_diagnostics(self) -> None:
        pass

    def execute_actions(self, actions: list[dict], *, stop_on_failure: bool = False) -> dict:
        self.calls.append(actions)
        rows = []
        for index, action in enumerate(actions):
            kind = str(action.get("action_type") or "")
            selector = str(action.get("selector") or "")
            success = kind not in self.fail_types
            output = self.outputs.get(selector, self.outputs.get(kind, ""))
            screenshot = ""
            error = ""
            if kind == "screenshot" and success:
                screenshot = "shot.jpg"
                output = screenshot
            if not success:
                error = "AssertionError: forced failure"
            rows.append(
                {
                    "index": index,
                    "success": success,
                    "output": output,
                    "error": error,
                    "screenshot": screenshot,
                    "wall_ms": 1.0,
                }
            )
            if not success and stop_on_failure:
                break
        return {"results": rows, "diagnostics": {}, "error": ""}


def _valid_test() -> dict:
    return {
        "test_id": "tc_valid",
        "name": "Page title",
        "description": "Checks the title",
        "target_url": "https://93.184.216.34/",
        "actions": [
            {"action_type": "navigate", "value": "https://93.184.216.34/"},
            {"action_type": "assert_title", "value": "Example"},
        ],
        "expected_results": ["Title contains Example"],
        "assertions": [
            {
                "action_index": 1,
                "kind": "title_contains",
                "description": "Title matches",
                "value": "Example",
            }
        ],
    }


def test_validate_test_case_accepts_structured_assertions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    validated = validate_test_case(_valid_test())
    assert validated["test_id"] == "tc_valid"
    assert validated["actions"][1]["action_type"] == "assert_title"


def test_validate_test_case_requires_an_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    test = _valid_test()
    test["actions"] = test["actions"][:1]
    with pytest.raises(ValueError, match="assertion"):
        validate_test_case(test)


@pytest.mark.parametrize("action_type", ["snapshot", "screenshot", "fixture_post"])
def test_validate_test_case_rejects_ephemeral_actions(
    monkeypatch: pytest.MonkeyPatch, action_type: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    test = _valid_test()
    test["actions"].append({"action_type": action_type})
    with pytest.raises(ValueError):
        validate_test_case(test)


def test_validate_test_case_rejects_session_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    test = _valid_test()
    test["actions"].insert(
        1,
        {
            "action_type": "click",
            "selector": "[data-flowscout-ref='e1']",
        },
    )
    with pytest.raises(ValueError, match="data-flowscout-ref"):
        validate_test_case(test)


def test_validate_test_case_rejects_private_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    test = _valid_test()
    test["actions"][0]["value"] = "http://127.0.0.1/"
    with pytest.raises((ValueError, InvalidAction)):
        validate_test_case(test, allow_private=False)


def test_generate_tests_builds_page_and_form_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    tests = generate_tests(
        project_id="prj_1",
        target_url="https://example.com/",
        pages=[
            {
                "url": "https://example.com/",
                "title": "Example",
                "headings": [{"level": 1, "text": "Welcome"}],
            }
        ],
        interactions=[
            {
                "kind": "form",
                "page_url": "https://example.com/",
                "selector": "[data-testid='email']",
                "value": "qa@example.com",
                "label": "Email",
            }
        ],
        issues=[],
    )
    names = [test["name"] for test in tests]
    assert "Page loads: Example" in names
    assert "Form accepts input: Email" in names
    form_test = next(test for test in tests if test["name"].startswith("Form"))
    assert any(action["action_type"] == "assert_value" for action in form_test["actions"])
    assert form_test["source"] == "exploration"


def test_regression_runner_passes_and_reports_assertions(tmp_path: Path) -> None:
    driver = FakeDriver(outputs={"assert_title": "Example"})
    runner = RegressionRunner(
        driver=driver,
        tests=[_valid_test()],
        evidence_dir=tmp_path,
        allow_private=False,
    )
    result = runner.run()
    assert result["status"] == "succeeded"
    assert result["summary"]["total"] == 1
    assert result["summary"]["passed"] == 1
    assert result["summary"]["failed"] == 0
    row = result["results"][0]
    assert row["status"] == "passed"
    assert row["assertions"][0]["status"] == "passed"
    assert row["previous_status"] is None


def test_regression_runner_marks_newly_failing_and_captures_screenshot(tmp_path: Path) -> None:
    driver = FakeDriver(fail_types={"assert_title"})
    runner = RegressionRunner(
        driver=driver,
        tests=[_valid_test()],
        evidence_dir=tmp_path,
        previous_results={"tc_valid": {"status": "passed"}},
        allow_private=False,
    )
    result = runner.run()
    assert result["summary"]["failed"] == 1
    assert result["summary"]["newly_failing"] == 1
    row = result["results"][0]
    assert row["status"] == "failed"
    assert row["newly_failing"] is True
    assert row["screenshots"] == ["shot.jpg"]
    assert row["previous_status"] == "passed"


def test_regression_runner_marks_fixed_test(tmp_path: Path) -> None:
    driver = FakeDriver(outputs={"assert_title": "Example"})
    runner = RegressionRunner(
        driver=driver,
        tests=[_valid_test()],
        evidence_dir=tmp_path,
        previous_results={"tc_valid": {"status": "failed"}},
        allow_private=False,
    )
    result = runner.run()
    row = result["results"][0]
    assert row["status"] == "passed"
    assert row["fixed"] is True
    assert result["summary"]["fixed"] == 1


def test_demo_probe_detects_duplicate_coupon(tmp_path: Path) -> None:
    driver = FakeDriver(
        outputs={
            "[data-testid='applied-coupons']": "Applied: SAVE20 x2",
            "[data-testid='cart-total']": "$77.98",
        }
    )
    explorer = PublicSiteExplorer(
        driver=driver,
        target_url="http://127.0.0.1:8099/shop/",
        evidence_dir=tmp_path,
        allow_private=True,
    )
    test = explorer._demo_coupon_probe()
    assert test is not None
    assert test["source"] == "demo-probe"
    assert any(action["action_type"] == "assert_text" for action in test["actions"])
    assert explorer.issues[0]["category"] == "coupon_stacking"
    assert explorer.issues[0]["evidence"] == ["shot.jpg"]


def test_demo_probe_skips_non_demo_and_fixed_behavior(tmp_path: Path) -> None:
    other = PublicSiteExplorer(
        driver=FakeDriver(),
        target_url="https://example.com/",
        evidence_dir=tmp_path,
        allow_private=True,
    )
    assert other._demo_coupon_probe() is None
    assert other.issues == []

    fixed = PublicSiteExplorer(
        driver=FakeDriver(
            outputs={
                "[data-testid='applied-coupons']": "Applied: SAVE20 x1",
                "[data-testid='cart-total']": "$103.98",
            }
        ),
        target_url="http://127.0.0.1:8099/shop/",
        evidence_dir=tmp_path,
        allow_private=True,
    )
    assert fixed._demo_coupon_probe() is None
    assert fixed.issues == []


class _FakeHealthResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def test_demo_probe_detects_verified_public_shopdemo(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        return _FakeHealthResponse({"status": "ok", "target": "shopdemo"})

    monkeypatch.setattr(socket, "getaddrinfo", _resolver("93.184.216.34"))
    monkeypatch.setattr("app.site_explorer.httpx.get", fake_get)
    driver = FakeDriver(
        outputs={
            "[data-testid='applied-coupons']": "Applied: SAVE20 x2",
            "[data-testid='cart-total']": "$77.98",
        }
    )
    explorer = PublicSiteExplorer(
        driver=driver,
        target_url="https://flowscout-shopdemo.onrender.com/shop/",
        evidence_dir=tmp_path,
        allow_private=False,
    )
    test = explorer._demo_coupon_probe()
    assert test is not None
    assert test["source"] == "demo-probe"
    assert test["target_url"] == "https://flowscout-shopdemo.onrender.com/shop/"
    assert calls == ["https://flowscout-shopdemo.onrender.com/shop/api/health"]
    assert explorer.issues[0]["category"] == "coupon_stacking"


def test_demo_probe_does_not_verify_arbitrary_public_site(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        return _FakeHealthResponse({"status": "ok", "target": "shopdemo"})

    monkeypatch.setattr("app.site_explorer.httpx.get", fake_get)
    explorer = PublicSiteExplorer(
        driver=FakeDriver(),
        target_url="https://example.com/shop/",
        evidence_dir=tmp_path,
        allow_private=False,
    )
    assert explorer._demo_coupon_probe() is None
    assert explorer.issues == []
    assert calls == []


def test_demo_probe_requires_health_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.site_explorer.httpx.get",
        lambda *args, **kwargs: _FakeHealthResponse({"status": "ok", "target": "other"}),
    )
    explorer = PublicSiteExplorer(
        driver=FakeDriver(),
        target_url="https://flowscout-shopdemo.onrender.com/shop/",
        evidence_dir=tmp_path,
        allow_private=False,
    )
    assert explorer._demo_coupon_probe() is None
    assert explorer.issues == []


def test_verified_public_shopdemo_allows_checkout_navigation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.site_explorer.httpx.get",
        lambda *args, **kwargs: _FakeHealthResponse({"status": "ok", "target": "shopdemo"}),
    )
    explorer = PublicSiteExplorer(
        driver=FakeDriver(),
        target_url="https://flowscout-shopdemo.onrender.com/shop/",
        evidence_dir=tmp_path,
        allow_private=False,
    )
    assert (
        explorer._safe_navigation(
            "https://flowscout-shopdemo.onrender.com/shop/checkout",
            "Proceed to checkout",
        )
        is True
    )
    assert (
        explorer._safe_navigation(
            "https://example.com/shop/checkout",
            "Proceed to checkout",
        )
        is False
    )


def test_loopback_shopdemo_allows_checkout_navigation(tmp_path: Path) -> None:
    explorer = PublicSiteExplorer(
        driver=FakeDriver(),
        target_url="http://127.0.0.1:8099/shop/",
        evidence_dir=tmp_path,
        allow_private=True,
    )
    assert (
        explorer._safe_navigation(
            "http://127.0.0.1:8099/shop/checkout",
            "Proceed to checkout",
        )
        is True
    )
