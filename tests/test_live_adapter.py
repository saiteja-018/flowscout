from __future__ import annotations

import json

import httpx
import pytest

from app.qa_agent import QAWorkflowRunner
from app.sandbox_runner import (
    CelestoCloudClient,
    CelestoSandboxAdapter,
    MockSandboxAdapter,
    create_adapter,
)


class FakeCloud:
    """In-memory stand-in for CelestoCloudClient used by adapter tests."""

    def __init__(self, *, browser_error: Exception | None = None) -> None:
        self.computers: dict[str, dict] = {}
        self.executed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.published: list[tuple[str, int]] = []
        self._browser_error = browser_error
        self._counter = 0

    def create_computer(self, *, template_id: str, size_id: str, image: str) -> dict:
        self._counter += 1
        computer_id = f"cmp_fake{self._counter:03d}"
        self.computers[computer_id] = {
            "id": computer_id,
            "status": "running",
            "template_id": template_id,
            "size_id": size_id,
        }
        return self.computers[computer_id]

    def wait_until_running(self, computer_id: str, timeout: float) -> tuple[str, float]:
        return self.computers[computer_id]["status"], 1.25

    def exec_command(self, computer_id: str, command: str, timeout: int = 30) -> dict:
        self.executed.append((computer_id, command))
        return {"exit_code": 0, "stdout": "live-ok", "stderr": ""}

    def browser_url(self, computer_id: str, *, wait_seconds: float = 90.0) -> str:
        if self._browser_error is not None:
            raise self._browser_error
        return "wss://cdp.example.test/browser?token=x"

    def publish_port(self, computer_id: str, port: int) -> dict:
        self.published.append((computer_id, port))
        return {"port": port, "status": "ready", "url": "https://preview.example.test"}

    def delete_computer(self, computer_id: str, timeout: float = 120.0) -> bool:
        self.deleted.append(computer_id)
        self.computers.pop(computer_id, None)
        return True


def make_adapter(tmp_path, fake: FakeCloud) -> CelestoSandboxAdapter:
    adapter = CelestoSandboxAdapter(
        command_log_path=tmp_path / "commands.jsonl",
        api_key="test-key",
        client=fake,
    )
    return adapter


def test_live_adapter_policy_and_lifecycle(tmp_path) -> None:
    fake = FakeCloud()
    adapter = make_adapter(tmp_path, fake)
    info = adapter.create("sbx-livetest01")
    assert info.execution_mode == "live"
    assert info.provider == "celesto_cloud"
    assert info.boot_to_ready_s == 1.25
    assert info.browser_url == "wss://cdp.example.test/browser?token=x"
    assert fake.computers and list(fake.computers)[0] == info.provider_id

    allowed = adapter.run_command("sbx-livetest01", "echo live-ok")
    assert allowed.ok
    assert allowed.execution_mode == "live"
    assert not allowed.simulated

    blocked = adapter.run_command("sbx-livetest01", "cat /etc/passwd")
    assert blocked.decision == "block"
    # The blocked command never reached the guest.
    assert [command for _, command in fake.executed] == ["echo live-ok"]

    published = adapter.publish_port("sbx-livetest01", 8090)
    assert published["url"] == "https://preview.example.test"
    assert fake.published == [(info.provider_id, 8090)]

    assert adapter.delete("sbx-livetest01").ok
    assert fake.deleted == [info.provider_id]

    # Audit log recorded allow, block, publish, and delete rows.
    rows = [
        json.loads(line)
        for line in (tmp_path / "commands.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decisions = {row["decision"] for row in rows}
    assert decisions == {"allow", "block"}
    assert all(row["execution_target"] == "celesto_sandbox" for row in rows)


def test_live_adapter_deploy_exception_is_exact_match(tmp_path) -> None:
    fake = FakeCloud()
    adapter = make_adapter(tmp_path, fake)
    adapter.create("sbx-livetest02")

    allowed = adapter.run_command("sbx-livetest02", "pip install --quiet fastapi uvicorn")
    assert allowed.decision == "allow"

    # A near-miss is still blocked: no prefix or fuzzy matching.
    blocked = adapter.run_command(
        "sbx-livetest02", "pip install --quiet fastapi uvicorn requests"
    )
    assert blocked.decision == "block"
    assert blocked.stderr.startswith("BLOCKED:")

    commands = [command for _, command in fake.executed]
    assert commands == ["pip install --quiet fastapi uvicorn"]


def test_live_adapter_chunked_write(tmp_path) -> None:
    fake = FakeCloud()
    adapter = make_adapter(tmp_path, fake)
    adapter.create("sbx-livetest03")
    content = "x" * (adapter.WRITE_CHUNK_CHARS * 2 + 100)  # needs 3 chunks
    result = adapter.write_text("sbx-livetest03", "/workspace/flowscout/big.txt", content)
    assert result.ok
    writes = [command for _, command in fake.executed if "base64 -d" in command]
    assert len(writes) == 3
    assert ">" in writes[0] and ">>" in writes[1] and ">>" in writes[2]
    with pytest.raises(ValueError):
        adapter.write_text("sbx-livetest03", "/etc/passwd", "nope")


def test_live_adapter_refuses_missing_browser_url(tmp_path) -> None:
    from app.sandbox_runner import SandboxUnavailableError

    fake = FakeCloud(browser_error=SandboxUnavailableError("Celesto browser connection unavailable (501)"))
    adapter = make_adapter(tmp_path, fake)
    runner = QAWorkflowRunner(
        adapter,
        "sbx-livetest04",
        "http://127.0.0.1:8090",
        execution_mode="live",
        use_sandbox_browser=True,
    )
    outcome = runner.run(inject_failure=False)
    assert outcome.success is False
    assert "did not expose a CDP browser URL" in outcome.error
    assert outcome.cleanup_ok is True
    assert fake.deleted  # the failed sandbox was still cleaned up


def test_sandbox_browser_mode_never_falls_back_to_host(tmp_path) -> None:
    adapter = MockSandboxAdapter(command_log_path=tmp_path / "commands.jsonl")
    runner = QAWorkflowRunner(
        adapter,
        "sbx-nohost001",
        "http://127.0.0.1:8090",
        execution_mode="live",
        use_sandbox_browser=True,
    )
    outcome = runner.run(inject_failure=False)
    assert outcome.success is False
    assert "refusing to substitute a host browser" in outcome.error
    assert outcome.cleanup_ok is True
    assert adapter.get_info("sbx-nohost001") is None


def test_live_mode_requires_real_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CELESTO_API_KEY", "replace-me")
    with pytest.raises(Exception):
        create_adapter("live", command_log_path=tmp_path / "commands.jsonl")


class RESTMachine(httpx.MockTransport):
    """Tiny scripted REST backend for CelestoCloudClient tests."""

    def __init__(self) -> None:
        self.state = {"cmp_rest001": "creating"}
        self.browser_calls = 0
        self.create_calls = 0
        self.exec_calls = 0
        super().__init__(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/v1/computers" and method == "POST":
            self.create_calls += 1
            if self.create_calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            body = json.loads(request.content.decode() or "{}")
            assert body["size_id"] == "standard"
            assert body["template_id"] == "browser-agent"
            assert request.headers.get("idempotency-key")
            return httpx.Response(201, json={"id": "cmp_rest001", "status": "creating"})
        if path == "/v1/computers/cmp_rest001" and method == "GET":
            if self.state["cmp_rest001"] == "creating":
                self.state["cmp_rest001"] = "running"
            return httpx.Response(200, json={"id": "cmp_rest001", "status": self.state["cmp_rest001"]})
        if path == "/v1/computers/cmp_rest001/exec" and method == "POST":
            self.exec_calls += 1
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok", "stderr": ""})
        if path == "/v1/computers/cmp_rest001/browser" and method == "POST":
            self.browser_calls += 1
            if self.browser_calls == 1:
                return httpx.Response(501, json={"detail": "not yet"})
            return httpx.Response(
                201,
                json={
                    "gateway_url": "https://gw.example.test/cdp",
                    "token": "tok123",
                    "expires_at": "2100-01-01T00:00:00+00:00",
                },
            )
        if path == "/v1/computers/cmp_rest001/published_ports" and method == "POST":
            return httpx.Response(201, json={"port": 8090, "status": "ready", "url": "https://p.example.test"})
        if path == "/v1/computers/cmp_rest001" and method == "DELETE":
            self.state["cmp_rest001"] = "deleted"
            return httpx.Response(200, json={"id": "cmp_rest001", "status": "deleted"})
        return httpx.Response(404, json={"detail": "nope"})


def test_rest_client_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    machine = RESTMachine()
    client = CelestoCloudClient("test-key")
    client._client = httpx.Client(
        base_url="https://api.celesto.ai",
        transport=machine,
        headers={"Authorization": "Bearer test-key"},
    )

    computer = client.create_computer(template_id="browser-agent", size_id="standard", image="ubuntu-desktop-24.04")
    assert computer["id"] == "cmp_rest001"
    assert machine.create_calls == 2  # one 429, then success
    assert sleeps and sleeps[0] == 1.0  # Retry-After honoured, clamped to >= 1s

    status, boot = client.wait_until_running("cmp_rest001", 10)
    assert status == "running" and boot >= 0

    result = client.exec_command("cmp_rest001", "echo ok")
    assert result["exit_code"] == 0

    url = client.browser_url("cmp_rest001")
    assert url == "wss://gw.example.test/cdp?token=tok123"
    assert machine.browser_calls == 2  # one 501 warm-up, then 201

    entry = client.publish_port("cmp_rest001", 8090)
    assert entry["url"] == "https://p.example.test"

    assert client.delete_computer("cmp_rest001") is True
