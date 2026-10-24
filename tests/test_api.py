from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import LedgerUnavailable, MemoryLedger, create_app


@pytest.fixture()
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture()
def client(ledger: MemoryLedger) -> TestClient:
    return TestClient(create_app(ledger))


def test_health_contract(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_seed_record_is_present(client: TestClient) -> None:
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.headers["x-flowscout-degraded"] == "false"
    rows = response.json()
    seed = next(row for row in rows if row["run_id"] == "run-seed-0001")
    assert seed["sandbox_name"] == "sbx-seeddemo1"
    assert seed["state"] == "deleted"
    assert seed["execution_target"] == "celesto_sandbox"


def test_post_creates_run(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={
            "run_id": "run-valid-0001",
            "sandbox_name": "sbx-validname1",
            "problem_id": "coupon-stacking",
            "state": "created",
            "execution_target": "celesto_sandbox",
        },
    )
    assert response.status_code == 201
    assert response.json()["run_id"] == "run-valid-0001"
    rows = client.get("/api/runs").json()
    assert any(row["run_id"] == "run-valid-0001" for row in rows)


def test_invalid_sandbox_name_returns_400(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={"run_id": "run-invalid-1", "sandbox_name": "sandbox-1"},
    )
    assert response.status_code == 400
    assert "sandbox_name" in response.json()["detail"]


def test_invalid_state_returns_400(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={"run_id": "run-invalid-2", "sandbox_name": "sbx-validname2", "state": "exploded"},
    )
    assert response.status_code == 400


def test_invalid_execution_target_returns_400(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={
            "run_id": "run-invalid-3",
            "sandbox_name": "sbx-validname3",
            "execution_target": "host_shell",
        },
    )
    assert response.status_code == 400
    assert "execution_target" in response.json()["detail"]


def test_duplicate_run_returns_409(client: TestClient) -> None:
    payload = {"run_id": "run-duplicate-1", "sandbox_name": "sbx-duplicate1"}
    assert client.post("/api/runs", json=payload).status_code == 201
    assert client.post("/api/runs", json=payload).status_code == 409


def test_write_does_not_fall_back_to_memory() -> None:
    class UnavailableLedger:
        def list_runs(self):
            raise LedgerUnavailable("offline")

        def create_run(self, record):
            raise LedgerUnavailable("offline")

    client = TestClient(create_app(UnavailableLedger()))
    response = client.post(
        "/api/runs",
        json={"run_id": "run-offline-1", "sandbox_name": "sbx-offlinename"},
    )
    assert response.status_code == 503
    assert "not persisted" in response.json()["detail"]


def test_read_degradation_is_explicit() -> None:
    class UnavailableLedger:
        def list_runs(self):
            raise LedgerUnavailable("offline")

        def create_run(self, record):
            raise LedgerUnavailable("offline")

    client = TestClient(create_app(UnavailableLedger()))
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.headers["x-flowscout-degraded"] == "true"
    assert response.json()[0]["run_id"] == "run-seed-0001"


def test_dashboard_renders_escaped_operational_view(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "FLIGHT DECK" in response.text
    assert "Ledger unavailable" not in response.text
    assert "Content-Security-Policy" in response.headers


def test_dashboard_alias(client: TestClient) -> None:
    assert client.get("/dashboard").status_code == 200
