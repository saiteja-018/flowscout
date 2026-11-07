from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ownership import (
    OwnershipRegistry,
    OwnershipRegistryError,
    reclaim_min_age_s,
    reclaim_mode,
)
from app.sandbox_runner import (
    CelestoSandboxAdapter,
    SandboxUnavailableError,
)


class ReclaimCloud:
    """In-memory Celesto stand-in with registry-visible computers."""

    def __init__(self) -> None:
        self.computers: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.list_calls = 0
        self.create_failures: list[Exception] = []
        self._counter = 0

    def add(
        self,
        computer_id: str,
        *,
        status: str = "running",
        template_id: str = "browser-agent",
        size_id: str = "standard",
    ) -> dict:
        self.computers[computer_id] = {
            "id": computer_id,
            "status": status,
            "template_id": template_id,
            "size_id": size_id,
        }
        return self.computers[computer_id]

    def create_computer(self, *, template_id: str, size_id: str, image: str) -> dict:
        if self.create_failures:
            raise self.create_failures.pop(0)
        self._counter += 1
        computer_id = f"cmp_new{self._counter:03d}"
        self.computers[computer_id] = {
            "id": computer_id,
            "status": "running",
            "template_id": template_id,
            "size_id": size_id,
        }
        return self.computers[computer_id]

    def wait_until_running(self, computer_id: str, timeout: float) -> tuple[str, float]:
        return self.computers[computer_id]["status"], 1.0

    def exec_command(self, computer_id: str, command: str, timeout: int = 30) -> dict:
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    def browser_url(self, computer_id: str, *, wait_seconds: float = 90.0) -> str | None:
        return None

    def publish_port(self, computer_id: str, port: int) -> dict:
        return {"port": port, "status": "ready", "url": "https://preview.example.test"}

    def list_computers(self) -> list[dict]:
        self.list_calls += 1
        return list(self.computers.values())

    def delete_computer(self, computer_id: str, timeout: float = 120.0) -> bool:
        self.deleted.append(computer_id)
        self.computers.pop(computer_id, None)
        return True


def make_adapter(tmp_path: Path, cloud: ReclaimCloud) -> CelestoSandboxAdapter:
    return CelestoSandboxAdapter(
        command_log_path=tmp_path / "commands.jsonl",
        api_key="test-key",
        client=cloud,
        ownership_registry=OwnershipRegistry(tmp_path / "ownership.json"),
    )


def register_old(
    adapter: CelestoSandboxAdapter,
    computer_id: str,
    *,
    age_s: float = 7200,
    template_id: str = "browser-agent",
) -> None:
    adapter._ownership.register(
        computer_id=computer_id,
        logical_name=f"sbx-{computer_id.replace('_', '').lower()}",
        template_id=template_id,
        size_id="standard",
        image="ubuntu-desktop-24.04",
    )
    registry_path = adapter._ownership.path
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    data["computers"][computer_id]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=age_s)
    ).isoformat()
    registry_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_registry_round_trip_and_delete_marker(tmp_path: Path) -> None:
    registry = OwnershipRegistry(tmp_path / "ownership.json")
    record = registry.register(
        computer_id="cmp_roundtrip",
        logical_name="sbx-roundtrip1",
        template_id="browser-agent",
        size_id="standard",
    )
    assert record["status"] == "active"
    assert registry.records()[0]["computer_id"] == "cmp_roundtrip"

    marked = registry.mark_deleted("cmp_roundtrip", confirmed=True, reason="test")
    assert marked["status"] == "deleted"
    assert marked["delete_confirmed"] is True
    assert registry.records()[0]["status"] == "deleted"


def test_missing_registry_fails_closed(tmp_path: Path) -> None:
    registry = OwnershipRegistry(tmp_path / "missing.json")
    with pytest.raises(OwnershipRegistryError):
        registry.records()


def test_corrupt_registry_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ownership.json"
    path.write_text("{not json", encoding="utf-8")
    registry = OwnershipRegistry(path)
    with pytest.raises(OwnershipRegistryError):
        registry.records()


def test_stale_lock_is_recovered(tmp_path: Path) -> None:
    registry = OwnershipRegistry(tmp_path / "ownership.json")
    lock = registry.path.with_name(registry.path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    registry.register(computer_id="cmp_stale", logical_name="sbx-stalelock1")
    assert registry.records()[0]["computer_id"] == "cmp_stale"
    assert not lock.exists()


def test_reclaim_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELESTO_AUTO_RECLAIM", raising=False)
    assert reclaim_mode() == "off"


def test_reclaim_mode_unknown_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELESTO_AUTO_RECLAIM", "yes-please")
    assert reclaim_mode() == "off"


def test_reclaim_min_age_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELESTO_RECLAIM_MIN_AGE_S", raising=False)
    assert reclaim_min_age_s() == 3600.0
    monkeypatch.setenv("CELESTO_RECLAIM_MIN_AGE_S", "not-a-number")
    assert reclaim_min_age_s() == 3600.0


def test_unregistered_computer_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_unregistered")
    adapter = make_adapter(tmp_path, cloud)

    # No registry at all: fail closed before even listing the cloud.
    assert adapter.reclaim_orphans(dry_run=False, min_age_s=0) == []
    assert cloud.list_calls == 0
    assert cloud.deleted == []

    # A registry with a different candidate must not make the unregistered
    # computer eligible.
    register_old(adapter, "cmp_owned")
    cloud.add("cmp_owned")
    assert adapter.reclaim_orphans(dry_run=False, min_age_s=0) == ["cmp_owned"]
    assert cloud.deleted == ["cmp_owned"]
    assert "cmp_unregistered" in cloud.computers


def test_dry_run_lists_candidates_without_deleting(tmp_path: Path) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")

    assert adapter.reclaim_orphans(dry_run=True, min_age_s=0) == ["cmp_owned"]
    assert cloud.deleted == []
    assert "cmp_owned" in cloud.computers
    assert adapter._ownership.records()[0]["status"] == "active"


def test_keep_list_is_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_keep")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_keep")
    monkeypatch.setenv("CELESTO_KEEP_COMPUTER_IDS", "cmp_keep")

    assert adapter.reclaim_orphans(dry_run=False, min_age_s=0) == []
    assert cloud.deleted == []
    plan = adapter.plan_reclaim(min_age_s=0)
    assert plan["skipped"] == [{"computer_id": "cmp_keep", "reason": "keep_protected"}]


def test_young_entry_is_not_deleted(tmp_path: Path) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_young")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_young", age_s=10)

    assert adapter.reclaim_orphans(dry_run=False, min_age_s=3600) == []
    assert cloud.deleted == []
    plan = adapter.plan_reclaim(min_age_s=3600)
    assert plan["skipped"] == [{"computer_id": "cmp_young", "reason": "too_young"}]


def test_template_mismatch_is_not_deleted(tmp_path: Path) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_template", template_id="someone-elses-template")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_template", template_id="browser-agent")

    assert adapter.reclaim_orphans(dry_run=False, min_age_s=0) == []
    assert cloud.deleted == []
    plan = adapter.plan_reclaim(min_age_s=0)
    assert plan["skipped"] == [
        {"computer_id": "cmp_template", "reason": "template_mismatch"}
    ]


def test_cloud_deleting_is_not_deleted(tmp_path: Path) -> None:
    cloud = ReclaimCloud()
    cloud.add("cmp_deleting", status="deleting")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_deleting")

    assert adapter.reclaim_orphans(dry_run=False, min_age_s=0) == []
    assert cloud.deleted == []
    plan = adapter.plan_reclaim(min_age_s=0)
    assert plan["skipped"] == [
        {"computer_id": "cmp_deleting", "reason": "cloud_deleted"}
    ]


def test_auto_reclaim_defaults_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CELESTO_AUTO_RECLAIM", raising=False)
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")

    assert adapter.reclaim_orphans() == []
    assert cloud.list_calls == 0
    assert cloud.deleted == []


def test_auto_reclaim_apply_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELESTO_AUTO_RECLAIM", "apply")
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")

    assert adapter.reclaim_orphans() == ["cmp_owned"]
    assert cloud.deleted == ["cmp_owned"]


def test_429_retained_limit_off_raises_without_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELESTO_AUTO_RECLAIM", "off")
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")
    cloud.create_failures = [
        SandboxUnavailableError(
            "Celesto create failed (429): retained_sandbox_limit: quota exhausted"
        )
    ]

    with pytest.raises(SandboxUnavailableError, match="reclamation is off"):
        adapter.create("sbx-ratelimit1")
    assert cloud.deleted == []
    assert "cmp_owned" in cloud.computers


def test_429_retained_limit_dry_run_reports_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELESTO_AUTO_RECLAIM", "dry-run")
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")
    cloud.create_failures = [
        SandboxUnavailableError(
            "Celesto create failed (429): retained_sandbox_limit: quota exhausted"
        )
    ]

    with pytest.raises(SandboxUnavailableError, match="candidate"):
        adapter.create("sbx-ratelimit2")
    assert cloud.deleted == []
    assert "cmp_owned" in cloud.computers


def test_429_retained_limit_apply_reclaims_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELESTO_AUTO_RECLAIM", "apply")
    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")
    cloud.create_failures = [
        SandboxUnavailableError(
            "Celesto create failed (429): retained_sandbox_limit: quota exhausted"
        )
    ]

    info = adapter.create("sbx-ratelimit3")
    assert info.provider_id.startswith("cmp_new")
    assert cloud.deleted == ["cmp_owned"]
    assert "cmp_owned" not in cloud.computers
    records = {record["computer_id"]: record for record in adapter._ownership.records()}
    assert records[info.provider_id]["status"] == "active"
    assert adapter.delete("sbx-ratelimit3").ok


def test_cli_defaults_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import reclaim_orphans as cli

    cloud = ReclaimCloud()
    cloud.add("cmp_owned")
    adapter = make_adapter(tmp_path, cloud)
    register_old(adapter, "cmp_owned")

    monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: adapter)
    assert cli.main(["--json", "--registry", str(adapter._ownership.path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert [item["computer_id"] for item in payload["candidates"]] == ["cmp_owned"]
    assert payload["reclaimed"] == []
    assert cloud.deleted == []
