from __future__ import annotations

from pathlib import Path

from app.provenance import build_provenance, capture_provenance

CAPTURED = "2026-11-07T13:00:00+00:00"


def test_clean_tracked_tree_is_not_dirty() -> None:
    result = build_provenance(
        commit="8aed25e",
        branch="main",
        status="",
        untracked="",
        captured_at=CAPTURED,
    )
    assert result["git_commit"] == "8aed25e"
    assert result["git_dirty"] is False
    assert result["git_untracked_count"] == 0
    assert "tracked tree clean" in result["note"]


def test_tracked_modifications_mark_tree_dirty() -> None:
    result = build_provenance(
        commit="8aed25e",
        branch="main",
        status=" M app/report_schemas.py\n",
        untracked="",
        captured_at=CAPTURED,
    )
    assert result["git_dirty"] is True
    assert "modifications" in result["note"]


def test_untracked_files_are_counted_but_not_tracked_dirty() -> None:
    result = build_provenance(
        commit="8aed25e",
        branch="main",
        status="",
        untracked="tmp/one.txt\ntmp/two.txt\n",
        captured_at=CAPTURED,
    )
    assert result["git_dirty"] is False
    assert result["git_untracked_count"] == 2
    assert "untracked files present" in result["note"]


def test_unavailable_git_is_explicit() -> None:
    result = build_provenance(
        commit=None,
        branch=None,
        status=None,
        untracked=None,
        captured_at=CAPTURED,
    )
    assert result["git_commit"] is None
    assert result["git_dirty"] is None
    assert result["git_untracked_count"] is None
    assert "unavailable" in result["note"]


def test_capture_outside_a_repository_returns_nulls(tmp_path: Path) -> None:
    result = capture_provenance(tmp_path)
    assert result["git_commit"] is None
    assert result["git_dirty"] is None
    assert "unavailable" in result["note"]
