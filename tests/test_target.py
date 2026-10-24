from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import MemoryLedger, create_app
from app.target_saas_app import PROMPT_INJECTION_REVIEW


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(MemoryLedger()))


def test_initial_state(client: TestClient) -> None:
    response = client.get("/shop/api/state")
    assert response.status_code == 200
    assert response.json()["total"] == 129.97
    assert response.json()["coupons_applied"] == []


def test_six_applications_produce_negative_total(client: TestClient) -> None:
    totals = []
    for _ in range(6):
        response = client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
        assert response.status_code == 200
        totals.append(response.json()["total"])
    assert totals == [103.98, 77.98, 51.99, 25.99, 0.0, -25.99]
    state = client.get("/shop/api/state").json()
    assert state["negative"] is True
    assert state["times_applied"] == 6


def test_fixed_mode_rejects_duplicate(client: TestClient) -> None:
    client.post("/shop/api/mode", json={"fixed": True})
    first = client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
    second = client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
    assert first.status_code == 200
    assert second.status_code == 409
    state = client.get("/shop/api/state").json()
    assert state["times_applied"] == 1
    assert state["total"] == 103.98


def test_reset_restores_fixture(client: TestClient) -> None:
    client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
    reset = client.post("/shop/api/reset")
    assert reset.status_code == 200
    assert reset.json()["total"] == 129.97
    assert reset.json()["times_applied"] == 0


def test_trigger_failure_is_consumed_once(client: TestClient) -> None:
    assert client.post("/shop/api/trigger-failure").json()["armed"] is True
    assert client.post("/shop/api/apply", json={"coupon_code": "SAVE20"}).status_code == 503
    second = client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
    assert second.status_code == 200
    assert second.json()["times_applied"] == 1


def test_invalid_coupon_is_rejected(client: TestClient) -> None:
    response = client.post("/shop/api/apply", json={"coupon_code": "NOPE"})
    assert response.status_code == 400
    assert "Invalid coupon" in response.json()["message"]


def test_coupon_form_returns_flash(client: TestClient) -> None:
    response = client.post("/shop/apply", data={"coupon_code": "SAVE20"})
    assert response.status_code == 200
    assert 'data-testid="coupon-result"' in response.text
    assert "SAVE20" in response.text


def test_prompt_injection_is_escaped_on_page(client: TestClient) -> None:
    response = client.get("/shop/")
    assert response.status_code == 200
    assert "review-charlie" in response.text
    assert "&lt;!-- SYSTEM OVERRIDE" in response.text
    assert PROMPT_INJECTION_REVIEW not in response.text
    assert "<script>" not in response.text


def test_session_state_is_isolated(client: TestClient) -> None:
    other = TestClient(create_app(MemoryLedger()))
    client.post("/shop/api/apply", json={"coupon_code": "SAVE20"})
    assert client.get("/shop/api/state").json()["times_applied"] == 1
    assert other.get("/shop/api/state").json()["times_applied"] == 0
