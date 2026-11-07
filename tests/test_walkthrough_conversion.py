from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import run_live_demo as demo


def test_ffmpeg_encoder_requires_an_mp4_muxer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo, "_ffmpeg_muxers", lambda _: {"webm"})
    monkeypatch.setattr(demo, "_ffmpeg_encoders", lambda _: {"libvpx-vp9"})
    assert demo.ffmpeg_encoder("ffmpeg") is None

    monkeypatch.setattr(demo, "_ffmpeg_muxers", lambda _: {"mp4"})
    monkeypatch.setattr(
        demo, "_ffmpeg_encoders", lambda _: {"libx264", "libvpx-vp9"}
    )
    assert demo.ffmpeg_encoder("ffmpeg") == "libx264"


def test_find_ffmpeg_skips_a_vp9_only_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    playwright_ffmpeg = str(tmp_path / "playwright-ffmpeg")
    system_ffmpeg = str(tmp_path / "system-ffmpeg")
    monkeypatch.setattr(
        demo, "ffmpeg_candidates", lambda: [playwright_ffmpeg, system_ffmpeg]
    )
    monkeypatch.setattr(
        demo,
        "ffmpeg_encoder",
        lambda path: None if path == playwright_ffmpeg else "libx264",
    )
    assert demo.find_ffmpeg() == system_ffmpeg


def test_convert_walkthrough_retries_without_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "reports" / "evidence" / "video" / "page@abc.webm"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"webm")
    demo_path = tmp_path / "demo" / "walkthrough.mp4"

    monkeypatch.setattr(demo, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(demo, "EVIDENCE_DIR", video.parent.parent)
    monkeypatch.setattr(demo, "DEMO_PATH", demo_path)
    monkeypatch.setattr(demo, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(demo, "ffmpeg_encoder", lambda _: "libx264")

    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(
                command, 1, "", "Unrecognized option 'preset'."
            )
        demo_path.parent.mkdir(parents=True, exist_ok=True)
        demo_path.write_bytes(b"mp4" * 5000)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    result = demo.convert_walkthrough()

    assert result["mp4_created"] is True
    assert result["mp4_bytes"] == len(b"mp4" * 5000)
    assert "-preset" in commands[0]
    assert "-preset" not in commands[1]


def test_convert_walkthrough_explains_a_missing_mp4_encoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "reports" / "evidence" / "video" / "page@abc.webm"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"webm")

    monkeypatch.setattr(demo, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(demo, "EVIDENCE_DIR", video.parent.parent)
    monkeypatch.setattr(demo, "DEMO_PATH", tmp_path / "demo" / "walkthrough.mp4")
    monkeypatch.setattr(demo, "find_ffmpeg", lambda: None)

    result = demo.convert_walkthrough()

    assert result["video_recorded"] is True
    assert result["mp4_created"] is False
    assert "no ffmpeg build" in result["reason"]
