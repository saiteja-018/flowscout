from __future__ import annotations

from pathlib import Path

from app.qa_store import LocalQAStore, MemoryQAStore


def _test_case(project_id: str, name: str = "Checkout loads") -> dict:
    return {
        "test_id": "tc_test_1",
        "project_id": project_id,
        "name": name,
        "description": "A real test",
        "target_url": "https://example.com/",
        "actions": [{"action_type": "navigate", "value": "https://example.com/"}],
        "expected_results": ["Page loads"],
        "assertions": [{"kind": "title_contains", "description": "Title", "value": "Example"}],
        "source": "exploration",
        "tags": ["smoke"],
    }


def test_local_store_persists_projects_runs_and_tests(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    store = LocalQAStore(path)
    project = store.get_or_create_project("https://example.com/", "Example")
    assert store.get_or_create_project("https://example.com", "Duplicate")["project_id"] == project["project_id"]

    run = store.create_run(
        {
            "run_id": "run_1",
            "project_id": project["project_id"],
            "kind": "exploration",
            "mode": "mock",
            "target_url": "https://example.com/",
        }
    )
    assert run["status"] == "queued"
    updated = store.update_run(
        "run_1",
        status="succeeded",
        phase="completed",
        progress=100,
        finished_at="2026-01-01T00:00:00+00:00",
        summary={"pages_visited": 2},
    )
    assert updated is not None
    assert updated["summary"]["pages_visited"] == 2

    created = store.create_test_cases([_test_case(project["project_id"])])
    assert len(created) == 1
    assert store.get_test_case("tc_test_1")["name"] == "Checkout loads"

    reloaded = LocalQAStore(path)
    assert reloaded.get_project(project["project_id"])["name"] == "Example"
    assert reloaded.get_run("run_1")["status"] == "succeeded"
    assert reloaded.list_test_cases(project_id=project["project_id"])[0]["test_id"] == "tc_test_1"


def test_store_prevents_duplicate_run_and_test_ids(tmp_path: Path) -> None:
    store = LocalQAStore(tmp_path / "store.json")
    project = store.create_project("Example", "https://example.com/")
    store.create_run({"run_id": "run_1", "project_id": project["project_id"]})
    try:
        store.create_run({"run_id": "run_1", "project_id": project["project_id"]})
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate run id was accepted")

    first = store.create_test_cases([_test_case(project["project_id"])])
    second = store.create_test_cases([_test_case(project["project_id"])])
    assert len(first) == 1
    assert second == []


def test_previous_regression_results_uses_latest_other_run(tmp_path: Path) -> None:
    store = LocalQAStore(tmp_path / "store.json")
    project = store.create_project("Example", "https://example.com/")
    older = store.create_run(
        {
            "run_id": "run_old",
            "project_id": project["project_id"],
            "kind": "regression",
            "status": "succeeded",
            "finished_at": "2026-01-01T00:00:00+00:00",
        }
    )
    newer = store.create_run(
        {
            "run_id": "run_new",
            "project_id": project["project_id"],
            "kind": "regression",
            "status": "succeeded",
            "finished_at": "2026-01-02T00:00:00+00:00",
        }
    )
    store.add_test_results(
        [
            {
                "result_id": "tr_old",
                "run_id": older["run_id"],
                "test_id": "tc_1",
                "test_name": "Old",
                "status": "passed",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    )
    store.add_test_results(
        [
            {
                "result_id": "tr_new",
                "run_id": newer["run_id"],
                "test_id": "tc_1",
                "test_name": "New",
                "status": "failed",
                "created_at": "2026-01-02T00:00:00+00:00",
            }
        ]
    )
    previous = store.previous_regression_results(project["project_id"], "run_current")
    assert previous["tc_1"]["status"] == "failed"
    assert previous["tc_1"]["test_name"] == "New"

    excluded = store.previous_regression_results(project["project_id"], newer["run_id"])
    assert excluded["tc_1"]["status"] == "passed"


def test_stats_and_test_deletion(tmp_path: Path) -> None:
    store = LocalQAStore(tmp_path / "store.json")
    project = store.create_project("Example", "https://example.com/")
    store.create_run(
        {
            "run_id": "run_1",
            "project_id": project["project_id"],
            "status": "succeeded",
            "issues": [
                {"category": "console_error", "title": "Issue", "url": "https://example.com/", "description": "Boom"},
                {"category": "console_error", "title": "Issue", "url": "https://example.com/", "description": "Boom"},
            ],
        }
    )
    store.create_test_cases([_test_case(project["project_id"])])
    store.add_test_results(
        [
            {
                "result_id": "tr_1",
                "run_id": "run_1",
                "test_id": "tc_test_1",
                "test_name": "Checkout loads",
                "status": "failed",
                "newly_failing": True,
            }
        ]
    )
    stats = store.stats()
    assert stats["projects"] == 1
    assert stats["runs"] == 1
    assert stats["test_cases"] == 1
    assert stats["test_results"] == 1
    assert stats["passed_results"] == 0
    assert stats["failed_results"] == 1
    assert stats["issues"] == 1
    assert stats["newly_failing"] == 1
    assert store.delete_test_case("tc_test_1") is True
    assert store.delete_test_case("tc_test_1") is False


def test_memory_store_has_no_filesystem_side_effects() -> None:
    store = MemoryQAStore()
    project = store.create_project("Example", "https://example.com/")
    store.create_run({"run_id": "run_mem", "project_id": project["project_id"]})
    assert store.get_run("run_mem")["run_id"] == "run_mem"
    assert store.backend == "memory"
