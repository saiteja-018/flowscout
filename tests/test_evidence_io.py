from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evidence_io import atomic_write_bytes, atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_parents_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.md"
    atomic_write_text(target, "first")
    assert target.read_text(encoding="utf-8") == "first"

    atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"
    assert list(target.parent.glob("*.tmp")) == []


def test_atomic_write_json_is_valid_and_utf8(tmp_path: Path) -> None:
    target = tmp_path / "reports" / "reliability.json"
    payload = {"name": "café", "values": [1, 2, 3]}
    atomic_write_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert target.read_text(encoding="utf-8").endswith("\n")
    assert list(target.parent.glob("*.tmp")) == []


def test_atomic_write_bytes_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "evidence" / "shot.jpg"
    atomic_write_bytes(target, b"old")
    atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert list(target.parent.glob("*.tmp")) == []


def test_failed_serialization_preserves_previous_document(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    atomic_write_json(target, {"ok": True})

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert list(target.parent.glob("*.tmp")) == []
