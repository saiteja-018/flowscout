from __future__ import annotations

from pathlib import Path

import pytest

from app.report_schemas import ReportVerificationError
from scripts import run_live_demo as demo


@pytest.fixture()
def isolated_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(demo, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(demo, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(demo, "EVIDENCE_DIR", tmp_path / "reports" / "evidence")
    monkeypatch.setattr(demo, "GENERATED_TEST_DIR", tmp_path / "tests" / "generated")
    monkeypatch.setattr(demo, "COMMANDS_PATH", tmp_path / "logs" / "commands.jsonl")
    monkeypatch.setattr(demo, "DEMO_PATH", tmp_path / "demo" / "walkthrough.mp4")
    monkeypatch.setattr(
        demo, "ARTIFACT_BACKUP_ROOT", tmp_path / "tmp" / "artifact-backups"
    )
    monkeypatch.setattr(demo, "_ACTIVE_BACKUP", None)

    evidence = tmp_path / "reports" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "actions.jsonl").write_text("old actions\n", encoding="utf-8")
    reports = tmp_path / "reports"
    (reports / "reliability.json").write_text('{"old": true}\n', encoding="utf-8")
    (reports / "postmortem.md").write_text("old postmortem\n", encoding="utf-8")

    demo_path = tmp_path / "demo" / "walkthrough.mp4"
    demo_path.parent.mkdir(parents=True)
    demo_path.write_bytes(b"old video")

    commands = tmp_path / "logs" / "commands.jsonl"
    commands.parent.mkdir(parents=True)
    commands.write_text("old command\n", encoding="utf-8")

    generated = tmp_path / "tests" / "generated"
    generated.mkdir(parents=True)
    (generated / "old.spec.ts").write_text("old spec\n", encoding="utf-8")

    return tmp_path


def _write_new_artifacts(root: Path) -> None:
    (root / "reports" / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "evidence" / "actions.jsonl").write_text(
        "new actions\n", encoding="utf-8"
    )
    (root / "reports" / "reliability.json").write_text(
        '{"new": true}\n', encoding="utf-8"
    )
    (root / "demo").mkdir(parents=True, exist_ok=True)
    (root / "demo" / "walkthrough.mp4").write_bytes(b"new video")
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "commands.jsonl").write_text("new command\n", encoding="utf-8")
    (root / "tests" / "generated").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "generated" / "new.spec.ts").write_text(
        "new spec\n", encoding="utf-8"
    )


def test_prepare_artifacts_backs_up_and_clears(isolated_artifacts: Path) -> None:
    root = isolated_artifacts
    backup = demo.prepare_artifacts()

    assert backup.is_dir()
    assert (backup / "manifest.json").is_file()
    assert not (root / "reports" / "evidence" / "actions.jsonl").exists()
    assert not (root / "reports" / "reliability.json").exists()
    assert not (root / "reports" / "postmortem.md").exists()
    assert not (root / "demo" / "walkthrough.mp4").exists()
    assert (root / "logs" / "commands.jsonl").read_text(encoding="utf-8") == ""
    assert not (root / "tests" / "generated" / "old.spec.ts").exists()


def test_restore_artifacts_round_trips(isolated_artifacts: Path) -> None:
    root = isolated_artifacts
    backup = demo.prepare_artifacts()
    _write_new_artifacts(root)

    demo.restore_artifacts(backup)

    assert (
        root / "reports" / "evidence" / "actions.jsonl"
    ).read_text(encoding="utf-8") == "old actions\n"
    assert (root / "reports" / "reliability.json").read_text(
        encoding="utf-8"
    ) == '{"old": true}\n'
    assert (root / "reports" / "postmortem.md").read_text(
        encoding="utf-8"
    ) == "old postmortem\n"
    assert (root / "demo" / "walkthrough.mp4").read_bytes() == b"old video"
    assert (root / "logs" / "commands.jsonl").read_text(
        encoding="utf-8"
    ) == "old command\n"
    assert (root / "tests" / "generated" / "old.spec.ts").read_text(
        encoding="utf-8"
    ) == "old spec\n"
    assert not (root / "tests" / "generated" / "new.spec.ts").exists()


def test_restore_on_exit_rolls_back_failed_run(isolated_artifacts: Path) -> None:
    root = isolated_artifacts
    demo.prepare_artifacts()
    _write_new_artifacts(root)

    demo._restore_on_exit()

    assert (
        root / "reports" / "evidence" / "actions.jsonl"
    ).read_text(encoding="utf-8") == "old actions\n"
    assert (root / "demo" / "walkthrough.mp4").read_bytes() == b"old video"
    assert demo._ACTIVE_BACKUP is None


def test_commit_artifacts_keeps_new_run(isolated_artifacts: Path) -> None:
    root = isolated_artifacts
    backup = demo.prepare_artifacts()
    _write_new_artifacts(root)

    demo.commit_artifacts(verify=False)

    assert not backup.exists()
    assert demo._ACTIVE_BACKUP is None
    assert (root / "reports" / "evidence" / "actions.jsonl").read_text(
        encoding="utf-8"
    ) == "new actions\n"
    assert (root / "demo" / "walkthrough.mp4").read_bytes() == b"new video"


def test_commit_artifacts_rolls_back_when_verification_fails(
    isolated_artifacts: Path,
) -> None:
    root = isolated_artifacts
    backup = demo.prepare_artifacts()
    _write_new_artifacts(root)

    with pytest.raises(ReportVerificationError):
        demo.commit_artifacts()

    assert demo._ACTIVE_BACKUP is None
    assert backup.is_dir()
    assert (root / "reports" / "reliability.json").read_text(
        encoding="utf-8"
    ) == '{"old": true}\n'
    assert (root / "demo" / "walkthrough.mp4").read_bytes() == b"old video"
    assert not (root / "tests" / "generated" / "new.spec.ts").exists()
