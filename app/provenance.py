"""Capture git provenance for generated reports and live evidence.

Provenance is captured *before* a run writes artifacts, otherwise every run
would report itself dirty merely because it created reports, screenshots, or
logs. ``git_dirty`` covers tracked changes only; untracked files are counted
separately so local scratch files do not hide a clean, reproducible checkout.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT_S = 10.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command; return stdout or None when unavailable."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def build_provenance(
    *,
    commit: str | None,
    branch: str | None,
    status: str | None,
    untracked: str | None,
    captured_at: str,
) -> dict[str, Any]:
    """Turn raw git output into the provenance object stored in evidence."""
    if commit is None:
        return {
            "git_commit": None,
            "git_branch": None,
            "git_dirty": None,
            "git_untracked_count": None,
            "captured_at": captured_at,
            "note": "git metadata unavailable; run outside a git work tree or git is not installed",
        }

    dirty = None if status is None else bool(status.strip())
    untracked_count = None
    if untracked is not None:
        untracked_count = len([line for line in untracked.splitlines() if line.strip()])

    if dirty:
        note = "tracked modifications present at capture time"
    elif untracked_count:
        note = "tracked tree clean at capture time; untracked files present"
    else:
        note = "tracked tree clean at capture time"

    return {
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "git_untracked_count": untracked_count,
        "captured_at": captured_at,
        "note": note,
    }


def capture_provenance(
    root: Path | str = PROJECT_ROOT,
    *,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Capture the current commit, branch, and tracked/untracked state."""
    root = Path(root)
    return build_provenance(
        commit=_git(root, "rev-parse", "HEAD"),
        branch=_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        status=_git(root, "status", "--porcelain", "--untracked-files=no"),
        untracked=_git(root, "ls-files", "--others", "--exclude-standard"),
        captured_at=captured_at or utcnow(),
    )
