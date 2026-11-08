from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.console as console_module
from app.main import MemoryLedger, create_app
from app.qa_service import QARunManager
from app.qa_store import MemoryQAStore


@pytest.fixture()
def console():
    application = create_app(MemoryLedger())
    store = MemoryQAStore()
    application.state.qa_store = store
    application.state.qa_manager = QARunManager(store)
    with TestClient(application) as client:
        yield client, store


def test_console_pages_render_with_an_empty_store(console) -> None:
    client, _ = console
    for path in [
        "/console",
        "/console/",
        "/console/new",
        "/console/projects",
        "/console/tests",
        "/console/regression",
        "/console/runs",
        "/console/reports",
        "/console/settings",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "FlowScout" in response.text


def test_console_config_never_exposes_secrets(console) -> None:
    client, _ = console
    response = client.get("/console/api/config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["store_backend"] == "memory"
    assert "api_key" not in json.dumps(payload).lower()


def test_live_run_requires_explicit_authorization(console) -> None:
    client, store = console
    response = client.post(
        "/console/api/runs",
        json={"target_url": "https://example.com", "mode": "live"},
    )
    assert response.status_code == 403
    assert "authorization" in response.json()["detail"].lower()
    assert store.stats()["runs"] == 0


def test_live_run_requires_live_configuration(console, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = console
    monkeypatch.delenv("CELESTO_MODE", raising=False)
    monkeypatch.delenv("CELESTO_API_KEY", raising=False)
    response = client.post(
        "/console/api/runs",
        json={
            "target_url": "https://example.com",
            "mode": "live",
            "authorize_live": True,
        },
    )
    assert response.status_code == 409
    assert "not configured" in response.json()["detail"].lower()
    assert store.stats()["runs"] == 0


def test_mock_run_rejects_metadata_target(console) -> None:
    client, store = console
    response = client.post(
        "/console/api/runs",
        json={"target_url": "http://169.254.169.254/latest/meta-data/", "mode": "mock"},
    )
    assert response.status_code == 400
    assert store.stats()["runs"] == 0


def test_run_detail_report_and_download_render_real_store_data(console, tmp_path, monkeypatch) -> None:
    client, store = console
    monkeypatch.setattr(console_module, "EVIDENCE_ROOT", tmp_path)
    run_dir = tmp_path / "run_detail_1"
    run_dir.mkdir()
    (run_dir / "shot.jpg").write_bytes(b"jpeg-bytes")
    (run_dir / "failure.jpg").write_bytes(b"jpeg-bytes")
    project = store.create_project("Example", "https://example.com/")
    run = store.create_run(
        {
            "run_id": "run_detail_1",
            "project_id": project["project_id"],
            "kind": "exploration",
            "mode": "mock",
            "target_url": "https://example.com/",
            "status": "succeeded",
            "phase": "completed",
            "progress": 100,
            "cleanup_ok": True,
            "cleanup_message": "mock sandbox removed",
            "summary": {"pages_visited": 1, "actions": 2, "planner_mode": "scripted_heuristic"},
            "issues": [
                {
                    "category": "console_error",
                    "title": "Browser console error",
                    "description": "ReferenceError: x is not defined",
                    "severity": "high",
                    "url": "https://example.com/",
                    "step": 1,
                }
            ],
            "actions": [
                {
                    "step": 1,
                    "role": "setup",
                    "action_type": "navigate",
                    "value": "https://example.com/",
                    "description": "Open the page",
                    "success": True,
                    "wall_ms": 10,
                }
            ],
            "logs": [{"ts": "2026-01-01T00:00:00+00:00", "level": "info", "message": "Started"}],
            "screenshots": [{"name": "shot.jpg", "caption": "Homepage"}],
            "generated_test_ids": ["tc_detail_1"],
        }
    )
    store.create_test_cases(
        [
            {
                "test_id": "tc_detail_1",
                "project_id": project["project_id"],
                "name": "Checkout title",
                "target_url": "https://example.com/",
                "actions": [{"action_type": "navigate", "value": "https://example.com/"}],
                "assertions": [{"kind": "title_contains", "description": "Title"}],
            }
        ]
    )
    store.add_test_results(
        [
            {
                "result_id": "tr_detail_1",
                "run_id": run["run_id"],
                "test_id": "tc_detail_1",
                "test_name": "Checkout title",
                "status": "failed",
                "error": "AssertionError: expected title",
                "assertions": [
                    {
                        "kind": "title_contains",
                        "description": "Title matches",
                        "status": "failed",
                        "error": "expected title",
                    }
                ],
                "screenshots": ["failure.jpg"],
            }
        ]
    )

    detail = client.get("/console/runs/run_detail_1")
    assert detail.status_code == 200
    assert "Example" in detail.text
    assert "Browser console error" in detail.text
    assert "Checkout title" in detail.text
    assert "/console/evidence/run_detail_1/shot.jpg" in detail.text
    assert "/console/evidence/run_detail_1/failure.jpg" in detail.text
    assert "Evidence files missing" not in detail.text

    report = client.get("/console/reports/run_detail_1")
    assert report.status_code == 200
    assert "QA report" in report.text
    assert "/console/evidence/run_detail_1/shot.jpg" in report.text
    assert "Evidence files missing" not in report.text

    download = client.get("/console/runs/run_detail_1/report.json")
    assert download.status_code == 200
    payload = download.json()
    assert payload["run"]["run_id"] == "run_detail_1"
    assert payload["issues"][0]["severity"] == "high"
    assert payload["test_results"][0]["status"] == "failed"
    assert payload["generated_tests"][0]["test_id"] == "tc_detail_1"


def test_missing_evidence_is_not_linked_and_shows_a_warning(
    console, tmp_path, monkeypatch
) -> None:
    client, store = console
    monkeypatch.setattr(console_module, "EVIDENCE_ROOT", tmp_path)
    project = store.create_project("Example", "https://example.com/")
    store.create_run(
        {
            "run_id": "run_missing_evidence_1",
            "project_id": project["project_id"],
            "kind": "exploration",
            "mode": "mock",
            "target_url": "https://example.com/",
            "status": "succeeded",
            "phase": "completed",
            "progress": 100,
            "screenshots": [{"name": "gone.jpg", "caption": "Lost evidence"}],
        }
    )
    store.add_test_results(
        [
            {
                "result_id": "tr_missing_evidence_1",
                "run_id": "run_missing_evidence_1",
                "test_id": "tc_missing_evidence_1",
                "test_name": "Checkout title",
                "status": "failed",
                "screenshots": ["failure-gone.jpg"],
            }
        ]
    )

    detail = client.get("/console/runs/run_missing_evidence_1")
    assert detail.status_code == 200
    assert "Evidence files missing" in detail.text
    assert "/console/evidence/run_missing_evidence_1/gone.jpg" not in detail.text
    assert "/console/evidence/run_missing_evidence_1/failure-gone.jpg" not in detail.text

    report = client.get("/console/reports/run_missing_evidence_1")
    assert report.status_code == 200
    assert "Evidence files missing" in report.text
    assert "/console/evidence/run_missing_evidence_1/gone.jpg" not in report.text


def test_evidence_reader_is_constrained_to_the_run_directory(
    console, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _ = console
    root = tmp_path / "runs"
    run_dir = root / "run_evidence_1"
    run_dir.mkdir(parents=True)
    (run_dir / "shot.jpg").write_bytes(b"jpeg-bytes")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(console_module, "EVIDENCE_ROOT", root)

    ok = client.get("/console/evidence/run_evidence_1/shot.jpg")
    assert ok.status_code == 200
    assert ok.content == b"jpeg-bytes"
    assert ok.headers["content-disposition"].startswith("inline")

    missing = client.get("/console/evidence/run_evidence_1/missing.jpg")
    assert missing.status_code == 404

    traversal = client.get("/console/evidence/run_evidence_1/..%2F..%2Fsecret.txt")
    assert traversal.status_code == 404


def test_delete_unknown_test_returns_404(console) -> None:
    client, _ = console
    response = client.delete("/console/api/tests/tc_missing")
    assert response.status_code == 404


def test_unknown_run_and_report_return_404(console) -> None:
    client, _ = console
    assert client.get("/console/runs/run_missing").status_code == 404
    assert client.get("/console/reports/run_missing").status_code == 404
    assert client.get("/console/runs/run_missing/report.json").status_code == 404
