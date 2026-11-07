"""Atomic evidence and report writes.

Reports are read by the dashboard, the verifier, and reviewers while a run may
still be writing. A crash mid-write must never leave a truncated JSON document
that looks like evidence, so every write goes to a temporary file in the same
directory and is then atomically renamed over the destination.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    return _atomic_write(Path(path), text.encode(encoding))


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    return _atomic_write(Path(path), data)


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    text = json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
    return atomic_write_text(path, text)
