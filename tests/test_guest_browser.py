from __future__ import annotations

import pytest

from app.guest_browser_launcher import browser_command, display_from_environment
from app.qa_agent import GuestBrowserSession


class DummyAdapter:
    """Only the launcher command is exercised here; no sandbox calls are made."""


def test_visible_command_is_headed() -> None:
    command = browser_command("/usr/bin/chromium", 9333, visible=True)
    assert "--headless" not in command
    assert "--start-maximized" in command
    assert "--window-size=1280,900" in command
    assert "--remote-debugging-port=9333" in command


def test_headless_command_stays_headless() -> None:
    command = browser_command("/usr/bin/chromium", 9333, visible=False)
    assert "--headless" in command
    assert "--start-maximized" not in command
    assert "--window-size=1280,900" not in command


def test_display_environment_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":42")
    assert display_from_environment() == ":42"


def test_guest_session_requests_visible_browser() -> None:
    session = GuestBrowserSession(
        DummyAdapter(),  # type: ignore[arg-type]
        "sbx-visibler1",
        "http://127.0.0.1:8090",
        visible=True,
    )
    command = session.launcher_command()
    assert "--visible" in command
    assert "--headless" not in command


def test_guest_session_can_request_headless_browser() -> None:
    session = GuestBrowserSession(
        DummyAdapter(),  # type: ignore[arg-type]
        "sbx-visibler2",
        "http://127.0.0.1:8090",
        visible=False,
    )
    assert "--headless" in session.launcher_command()

class GuestLocator:
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


class GuestPage:
    def __init__(self, locator: GuestLocator) -> None:
        self._locator = locator
        self.waits = 0
        self.now = 0.0

    def locator(self, selector: str) -> GuestLocator:
        return self._locator

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1
        self.now += ms / 1000.0


def test_guest_exists_is_immediate() -> None:
    from app.guest_browser_worker import execute_action

    page = GuestPage(GuestLocator(count=1))
    row = execute_action(
        page,
        object(),
        {"action_type": "exists", "selector": "[data-testid='failure-page']"},
        "http://127.0.0.1:8090",
    )
    assert row["success"] is True
    assert row["output"] == "true"
    assert page.waits == 0


def test_guest_wait_attribute_change_polls_until_changed() -> None:
    from app.guest_browser_worker import execute_action

    page = GuestPage(GuestLocator(values=["129.97", "103.98"]))
    row = execute_action(
        page,
        object(),
        {
            "action_type": "wait_attribute_change",
            "selector": "[data-testid='cart-total']",
            "attribute": "data-value",
            "value": "129.97",
            "timeout_ms": 2000,
        },
        "http://127.0.0.1:8090",
    )
    assert row["success"] is True
    assert row["output"] == "103.98"
    assert page.waits == 1


def test_guest_wait_attribute_change_caps_at_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import guest_browser_worker as guest

    page = GuestPage(GuestLocator(values=["129.97"]))
    monkeypatch.setattr(
        guest,
        "time",
        type(
            "FakeClock",
            (),
            {
                "monotonic": staticmethod(lambda: page.now),
                "time": staticmethod(lambda: page.now),
            },
        ),
    )
    row = guest.execute_action(
        page,
        object(),
        {
            "action_type": "wait_attribute_change",
            "selector": "[data-testid='cart-total']",
            "attribute": "data-value",
            "value": "129.97",
            "timeout_ms": 10000,
        },
        "http://127.0.0.1:8090",
    )
    assert row["success"] is True
    assert row["output"] == "129.97"
    assert page.waits == 20
