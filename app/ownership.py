"""Fail-closed local ownership registry for FlowScout cloud computers.

Celesto's documented create API has no metadata, labels, or caller-supplied
name, so ownership cannot be recorded cloud-side. FlowScout therefore keeps a
local registry of every computer it creates. Orphan reclamation only considers
records in this registry; an unregistered cloud computer is never touched.

The registry is deliberately boring:

* one JSON document at ``logs/ownership/registry.json`` (gitignored);
* atomic writes through a same-directory temporary file and ``os.replace``;
* a short-lived lock file with stale-lock recovery;
* missing or corrupt state is an error, not an empty registry.

The dedicated FlowScout template is recorded on every entry so it can become a
second ownership signal once that template exists.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "logs" / "ownership" / "registry.json"
REGISTRY_VERSION = 1
LOCK_STALE_SECONDS = 60.0

logger = logging.getLogger(__name__)


class OwnershipRegistryError(RuntimeError):
    """The ownership registry is missing, unreadable, or malformed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_keep_ids() -> set[str]:
    return {
        item.strip()
        for item in os.getenv("CELESTO_KEEP_COMPUTER_IDS", "").split(",")
        if item.strip()
    }


def reclaim_mode() -> str:
    """Return the configured automatic-reclamation mode.

    Unknown values fail closed as ``off``. The default is ``off`` so a live
    create never deletes cloud resources unless an operator opted in.
    """
    raw = os.getenv("CELESTO_AUTO_RECLAIM", "off").strip().lower()
    aliases = {
        "off": "off",
        "dry-run": "dry-run",
        "dry_run": "dry-run",
        "dryrun": "dry-run",
        "apply": "apply",
    }
    mode = aliases.get(raw)
    if mode is None:
        logger.warning(
            "Unknown CELESTO_AUTO_RECLAIM=%r; treating it as 'off' (fail closed)",
            raw,
        )
        return "off"
    return mode


def reclaim_min_age_s() -> float:
    """Minimum age before a registry entry may be reclaimed (default 1 hour)."""
    raw = os.getenv("CELESTO_RECLAIM_MIN_AGE_S", "3600")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid CELESTO_RECLAIM_MIN_AGE_S=%r; using 3600 seconds", raw
        )
        return 3600.0


def record_age_s(record: dict[str, Any], *, now: datetime | None = None) -> float | None:
    """Return the age of a registry record in seconds, or ``None`` if invalid."""
    value = record.get("created_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - created).total_seconds())


class OwnershipRegistry:
    """Atomic, process-safe local registry of FlowScout-owned computers."""

    def __init__(self, path: Path | str | None = None) -> None:
        env_path = os.getenv("FLOWSCOUT_OWNERSHIP_REGISTRY", "").strip()
        selected = path or env_path or DEFAULT_REGISTRY_PATH
        candidate = Path(selected).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        self.path = candidate.resolve()
        self.run_id = os.getenv("FLOWSCOUT_RUN_ID", "").strip() or uuid.uuid4().hex
        self._pid = os.getpid()
        self._host = os.getenv("COMPUTERNAME", "").strip() or os.getenv("HOSTNAME", "").strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def register(
        self,
        *,
        computer_id: str,
        logical_name: str,
        template_id: str = "",
        size_id: str = "",
        image: str = "",
    ) -> dict[str, Any]:
        """Record a freshly created computer before it reaches ``running``."""
        computer_id = str(computer_id).strip()
        if not computer_id:
            raise OwnershipRegistryError("cannot register an empty computer id")
        with self._locked():
            data = self._read_or_empty()
            computers = data["computers"]
            existing = computers.get(computer_id)
            if isinstance(existing, dict):
                # Idempotent re-registration: keep the original creation time
                # and status so a retry cannot make an old computer look young.
                existing.update(
                    {
                        "logical_name": logical_name or existing.get("logical_name", ""),
                        "template_id": template_id or existing.get("template_id", ""),
                        "size_id": size_id or existing.get("size_id", ""),
                        "image": image or existing.get("image", ""),
                    }
                )
                self._write(data)
                return dict(existing)
            record = {
                "computer_id": computer_id,
                "logical_name": logical_name,
                "template_id": template_id,
                "size_id": size_id,
                "image": image,
                "created_at": _utc_now(),
                "status": "active",
                "pid": self._pid,
                "host": self._host,
                "run_id": self.run_id,
                "deleted_at": None,
                "delete_confirmed": None,
                "detail": "",
            }
            computers[computer_id] = record
            self._write(data)
            return dict(record)

    def mark_deleted(
        self,
        computer_id: str,
        *,
        confirmed: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        """Record a delete attempt and whether the cloud confirmed it."""
        computer_id = str(computer_id).strip()
        if not computer_id:
            raise OwnershipRegistryError("cannot mark an empty computer id as deleted")
        with self._locked():
            data = self._read()
            record = data["computers"].get(computer_id)
            if not isinstance(record, dict):
                raise OwnershipRegistryError(
                    f"computer {computer_id!r} is not present in the ownership registry"
                )
            record["status"] = "deleted" if confirmed else "delete_failed"
            record["deleted_at"] = _utc_now()
            record["delete_confirmed"] = bool(confirmed)
            record["detail"] = reason
            self._write(data)
            return dict(record)

    def records(self) -> list[dict[str, Any]]:
        """Return every registry record.

        Raises ``OwnershipRegistryError`` when the registry is missing or
        corrupt. Callers must treat that as "no candidates", never as an empty
        registry.
        """
        with self._locked():
            return [dict(record) for record in self._read()["computers"].values()]

    def candidates(
        self,
        *,
        min_age_s: float,
        keep_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return active records older than ``min_age_s`` and not protected."""
        keep = {str(item) for item in keep_ids}
        eligible: list[dict[str, Any]] = []
        for record in self.records():
            computer_id = str(record.get("computer_id") or "")
            if not computer_id or computer_id in keep:
                continue
            if record.get("status") == "deleted":
                continue
            age = record_age_s(record, now=now)
            if age is None or age < max(0.0, float(min_age_s)):
                continue
            eligible.append(record)
        return eligible

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _read_or_empty(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": REGISTRY_VERSION, "computers": {}}
        return self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            raise OwnershipRegistryError(
                f"ownership registry not found: {self.path} (fail closed)"
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            raise OwnershipRegistryError(
                f"ownership registry is unreadable: {self.path}: {type(exc).__name__}: {exc}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != REGISTRY_VERSION
            or not isinstance(data.get("computers"), dict)
        ):
            raise OwnershipRegistryError(
                f"ownership registry has an unsupported shape: {self.path}"
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + 10.0
        acquired = False
        while not acquired:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > LOCK_STALE_SECONDS:
                    logger.warning("Removing stale ownership-registry lock %s", lock_path)
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise OwnershipRegistryError(
                        f"ownership registry lock is held: {lock_path}"
                    )
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"pid": self._pid, "host": self._host, "ts": _utc_now()}
                    )
                )
            acquired = True
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
