"""Celesto sandbox adapter for FlowScout.

This module is the only place in the project that talks to the Celesto SDK.
It keeps the QA orchestration code independent of SDK details and provides:

* logical sandbox names validated against the challenge contract;
* structured command results;
* an append-only audit log of every command attempt, including denials;
* an explicit security policy applied before dispatch;
* deterministic mock execution for tests without Celesto credentials;
* ownership tracking and idempotent, best-effort cleanup.

Live mode uses the real Celesto Cloud `Computer` API. The SDK version inspected
for this repository (0.1.6) does not accept command callbacks on `Computer`;
policy is therefore enforced in this adapter before any SDK call. Celesto's
local `Celesto` facade does expose veto callbacks, but that is a different
runtime and is not used by this adapter.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.ownership import (
    OwnershipRegistry,
    OwnershipRegistryError,
    reclaim_min_age_s,
    reclaim_mode,
    record_age_s,
)

logger = logging.getLogger("flowscout.sandbox")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMMAND_LOG = PROJECT_ROOT / "logs" / "commands.jsonl"

SANDBOX_NAME_RE = re.compile(r"^sbx-[a-z0-9]{8,}$")
EXECUTION_TARGET = "celesto_sandbox"

# Commands may perform only these operations. This is intentionally narrow:
# the agent cannot run arbitrary code or install packages based on page content.
ALLOWED_COMMAND_PREFIXES: tuple[str, ...] = (
    "base64 -d",
    "base64 -w0 /workspace/flowscout/",
    "cat /etc/os-release",
    "cat /workspace/flowscout/",
    "command -v ",
    "echo ",
    "ls /workspace/flowscout",
    "mkdir -p /workspace/flowscout",
    "node --version",
    "npm --version",
    "nohup python3 /workspace/flowscout/",
    "npx playwright test /workspace/flowscout/",
    "printf ",
    "python3 /workspace/flowscout/",
    "test ",
    "true",
)

DENIED_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?:^|\s)rm\s+-rf\s+/", "recursive root deletion"),
    (r"\bmkfs(?:\.|\s|$)", "filesystem formatting"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bdd\s+if=/dev/(?:zero|random|urandom)", "raw device write"),
    (r"\b(?:shutdown|reboot|halt|poweroff)\b", "host power control"),
    (r"/etc/(?:passwd|shadow|sudoers|hosts)", "sensitive host file access"),
    (r"/root(?:/|\s|$)", "root home access"),
    (r"/proc/(?:self|\d+)/(?:environ|mem|cmdline)", "process introspection"),
    (r"/\.ssh(?:/|\s|$)", "SSH private material access"),
    (r"\b(?:printenv|env)\b\s*$", "environment dumping"),
    (r"\b(?:curl|wget)\b", "network fetch from a shell command"),
    (r"\b(?:pip|pip3|npm|apt|apt-get|apk|yum|dnf)\s+(?:install|add)\b", "package installation"),
    (r"\b(?:bash|sh|zsh|powershell|pwsh|cmd\.exe)\s+-c\b", "nested shell invocation"),
    (r"\b(?:python|python3|node|ruby|perl)\s+-[ce]\b", "inline interpreter execution"),
    (r"\b(?:eval|exec)\s*\(", "dynamic code evaluation"),
    (r"\bsource\s+/", "shell sourcing"),
    (r"\bchmod\s+(?:777|a\+x)\b", "permission widening"),
    (r"\b(?:nc|ncat|netcat|socat)\b", "raw network utility"),
    (r"/etc/flowscout-host-canary", "containment canary access"),
    (r"\b[A-Z]:\\\\", "Windows host path"),
)

# Operator-issued deploy commands that would otherwise match a deny pattern.
# These are exact full-string matches (never prefix matches), issued only by
# the deterministic deploy step, never derived from page content, and every
# attempt is still audited like any other command.
EXACT_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "pip install --quiet fastapi uvicorn python-multipart",
        "pip3 install --quiet fastapi uvicorn python-multipart",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
)


class SandboxError(RuntimeError):
    """Base class for adapter failures that callers should classify."""


class SandboxUnavailableError(SandboxError):
    """The configured sandbox runtime could not be used."""


class SandboxExecutionError(SandboxError):
    """A command was dispatched but the SDK or transport failed."""


class InvalidSandboxName(ValueError):
    """A logical sandbox name violates the required naming convention."""


def validate_sandbox_name(name: str) -> str:
    """Return a validated sandbox name or raise InvalidSandboxName."""
    if not isinstance(name, str) or not SANDBOX_NAME_RE.fullmatch(name):
        raise InvalidSandboxName(
            f"sandbox_name must match {SANDBOX_NAME_RE.pattern}; got {name!r}"
        )
    return name


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    """Redact common credential shapes before writing audit records."""
    value = str(text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: m.group(1) + "=[redacted]" if m.lastindex else "[redacted]", value)
    return value


@dataclass(frozen=True)
class PolicyDecision:
    decision: Literal["allow", "block"]
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class SecurityPolicy:
    """Deny-by-default policy for shell commands dispatched by FlowScout.

    The policy is applied before the SDK call. This is the actual enforcement
    boundary for Cloud computers, where SDK 0.1.6 has no per-command hook.
    """

    def __init__(
        self,
        denied_patterns: tuple[tuple[str, str], ...] | None = None,
        allowed_prefixes: tuple[str, ...] | None = None,
        exact_allowed: frozenset[str] | None = None,
    ) -> None:
        self._denied = denied_patterns if denied_patterns is not None else DENIED_COMMAND_PATTERNS
        self._allowed = allowed_prefixes if allowed_prefixes is not None else ALLOWED_COMMAND_PREFIXES
        self._exact_allowed = exact_allowed if exact_allowed is not None else EXACT_ALLOWED_COMMANDS

    def check(self, command: str) -> PolicyDecision:
        if not isinstance(command, str) or not command.strip():
            return PolicyDecision("block", "empty command")
        if len(command) > 9_800:
            return PolicyDecision("block", "command exceeds the 9,800 character limit")
        if any(ord(ch) < 32 and ch not in "\t" for ch in command):
            return PolicyDecision("block", "control characters are not allowed")
        if "\x00" in command:
            return PolicyDecision("block", "NUL byte is not allowed")

        stripped = command.strip()
        if stripped in self._exact_allowed:
            return PolicyDecision("allow", "exact-match deploy command exception")

        lowered = stripped.lower()
        for pattern, label in self._denied:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return PolicyDecision("block", f"denied operation: {label}")

        if not any(lowered == prefix or lowered.startswith(prefix.lower()) for prefix in self._allowed):
            return PolicyDecision("block", "command is not in the approved command allowlist")

        # No shell redirection except into the disposable workspace (fd
        # duplication like 2>&1 only re-routes an already-checked stream).
        for match in re.finditer(r">+\s*(\S+)", command):
            target = match.group(1)
            if not target.startswith("/workspace/flowscout/") and target not in ("&1", "&2"):
                return PolicyDecision("block", "redirection outside /workspace/flowscout")

        return PolicyDecision("allow")

    def check_target_url(self, url: str, allowed_hosts: set[str] | None = None) -> PolicyDecision:
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
        except ValueError:
            return PolicyDecision("block", "malformed URL")
        if parsed.scheme not in {"http", "https"}:
            return PolicyDecision("block", f"URL scheme {parsed.scheme!r} is not allowed")
        if not parsed.hostname:
            return PolicyDecision("block", "URL has no host")
        if parsed.username or parsed.password:
            return PolicyDecision("block", "credentials in URLs are not allowed")
        blocked_hosts = {
            "169.254.169.254",
            "metadata.google.internal",
            "metadata",
            "0.0.0.0",
        }
        host = parsed.hostname.lower()
        if host in blocked_hosts:
            return PolicyDecision("block", f"metadata/blocked host {host!r}")
        if allowed_hosts is not None and host not in allowed_hosts:
            return PolicyDecision("block", f"target host {host!r} is not in the allowlist")
        return PolicyDecision("allow")


class CommandLogger:
    """Thread-safe append-only JSONL logger for command attempts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DEFAULT_COMMAND_LOG)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(
        self,
        *,
        sandbox_name: str,
        command: str,
        decision: Literal["allow", "block"],
        execution_mode: str,
        provider_id: str | None = None,
        detail: str = "",
    ) -> None:
        validate_sandbox_name(sandbox_name)
        record: dict[str, Any] = {
            "ts": _utc_now(),
            "sandbox_name": sandbox_name,
            "command": redact(command),
            "decision": decision,
            "execution_target": EXECUTION_TARGET,
            "execution_mode": execution_mode,
        }
        if provider_id:
            record["provider_id"] = provider_id
        if detail:
            record["detail"] = redact(detail)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


@dataclass
class SandboxInfo:
    sandbox_name: str
    state: str = "created"
    provider: str = "mock"
    execution_mode: Literal["live", "mock"] = "mock"
    provider_id: Optional[str] = None
    created_at: str = ""
    boot_to_ready_s: float = 0.0
    browser_url: Optional[str] = None
    viewer_url: Optional[str] = None


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    sandbox_name: str
    command: str
    decision: Literal["allow", "block"]
    execution_target: str = EXECUTION_TARGET
    execution_mode: Literal["live", "mock"] = "mock"
    wall_clock_s: float = 0.0
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return self.decision == "allow" and self.exit_code == 0


@dataclass
class CleanupResult:
    sandbox_name: str
    deleted: bool
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.deleted


class SandboxAdapter:
    """Base interface used by the orchestration layer."""

    execution_mode: Literal["live", "mock"] = "mock"

    def __init__(
        self,
        command_log_path: Path | None = None,
        policy: SecurityPolicy | None = None,
    ) -> None:
        self.policy = policy or SecurityPolicy()
        self._logger = CommandLogger(command_log_path)
        self._sandboxes: dict[str, SandboxInfo] = {}
        self._lock = threading.RLock()

    def validate_sandbox_name(self, name: str) -> bool:
        return bool(SANDBOX_NAME_RE.fullmatch(name))

    def _info(self, name: str) -> SandboxInfo:
        info = self._sandboxes.get(name)
        if info is None:
            raise SandboxUnavailableError(f"sandbox {name!r} is not owned by this adapter")
        return info

    def create(self, sandbox_name: str) -> SandboxInfo:
        raise NotImplementedError

    def run_command(
        self,
        sandbox_name: str,
        command: str,
        timeout: int = 30,
        *,
        audit_command: str | None = None,
    ) -> SandboxResult:
        raise NotImplementedError

    def write_text(self, sandbox_name: str, path: str, content: str) -> SandboxResult:
        raise NotImplementedError

    def execute_playwright_test(self, sandbox_name: str, test_path: str, timeout: int = 120) -> SandboxResult:
        raise NotImplementedError

    def get_browser_url(self, sandbox_name: str) -> Optional[str]:
        return self._info(sandbox_name).browser_url

    def get_info(self, sandbox_name: str) -> Optional[SandboxInfo]:
        return self._sandboxes.get(sandbox_name)

    def list_sandboxes(self) -> list[SandboxInfo]:
        return list(self._sandboxes.values())

    def delete(self, sandbox_name: str) -> CleanupResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release adapter-local resources. Does not delete sandboxes."""


class MockSandboxAdapter(SandboxAdapter):
    """Deterministic local adapter used only when explicitly configured.

    Mock commands never reach a Celesto computer. Results are marked
    ``simulated=True`` and ``execution_mode="mock"``. Test execution commands
    return exit code 127 instead of pretending that a regression suite passed.
    """

    execution_mode: Literal["mock"] = "mock"

    def __init__(self, command_log_path: Path | None = None) -> None:
        super().__init__(command_log_path=command_log_path)
        self._files: dict[str, dict[str, str]] = {}

    def create(self, sandbox_name: str) -> SandboxInfo:
        validate_sandbox_name(sandbox_name)
        with self._lock:
            if sandbox_name in self._sandboxes:
                raise SandboxUnavailableError(f"sandbox {sandbox_name!r} already exists")
            started = time.monotonic()
            info = SandboxInfo(
                sandbox_name=sandbox_name,
                state="ready",
                provider="mock",
                execution_mode="mock",
                provider_id=f"mock-{sandbox_name}",
                created_at=_utc_now(),
                boot_to_ready_s=round(time.monotonic() - started, 4),
                browser_url=None,
                viewer_url=None,
            )
            self._sandboxes[sandbox_name] = info
            self._files[sandbox_name] = {}
            self._logger.log(
                sandbox_name=sandbox_name,
                command=f"create sandbox {sandbox_name}",
                decision="allow",
                execution_mode="mock",
                provider_id=info.provider_id,
                detail="simulated lifecycle operation",
            )
        logger.info("Created mock sandbox %s", sandbox_name)
        return info

    def run_command(
        self,
        sandbox_name: str,
        command: str,
        timeout: int = 30,
        *,
        audit_command: str | None = None,
    ) -> SandboxResult:
        info = self._info(sandbox_name)
        decision = self.policy.check(command)
        self._logger.log(
            sandbox_name=sandbox_name,
            command=audit_command or command,
            decision=decision.decision,
            execution_mode="mock",
            provider_id=info.provider_id,
            detail=decision.reason,
        )
        if not decision.allowed:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"BLOCKED: {decision.reason}",
                sandbox_name=sandbox_name,
                command=command,
                decision="block",
                execution_mode="mock",
                simulated=True,
            )

        started = time.monotonic()
        code = 0
        stdout = "SIMULATED: command accepted by mock adapter; no guest executed it."
        stderr = ""
        lowered = command.strip().lower()

        if lowered.startswith("echo "):
            stdout = command.strip()[5:].strip()
        elif lowered == "cat /etc/os-release":
            stdout = 'NAME="Alpine Linux"\nVERSION_ID="3.19"'
        elif lowered.startswith("node --version"):
            stdout = "v22.16.0 (simulated)"
        elif lowered.startswith("npm --version"):
            stdout = "10.9.2 (simulated)"
        elif lowered.startswith("command -v "):
            stdout = ""
        elif lowered.startswith("npx playwright test"):
            code = 127
            stderr = (
                "Mock adapter does not execute Playwright; generated tests must be "
                "verified by the host harness or a live Celesto sandbox."
            )
            stdout = ""
        elif lowered.startswith("cat /workspace/flowscout/"):
            path = command.split("cat ", 1)[1].strip()
            stdout = self._files.get(sandbox_name, {}).get(path, "")
        elif lowered.startswith("test -f "):
            path = command.split("test -f ", 1)[1].strip()
            code = 0 if path in self._files.get(sandbox_name, {}) else 1

        return SandboxResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            sandbox_name=sandbox_name,
            command=audit_command or command,
            decision="allow",
            execution_mode="mock",
            wall_clock_s=round(time.monotonic() - started, 4),
            simulated=True,
        )

    def write_text(self, sandbox_name: str, path: str, content: str) -> SandboxResult:
        self._info(sandbox_name)
        if not path.startswith("/workspace/flowscout/") or "/../" in path:
            raise ValueError("sandbox writes are restricted to /workspace/flowscout/")
        command = f"printf %s {shlex.quote(base64.b64encode(content.encode()).decode())} | base64 -d > {path}"
        decision = self.policy.check(command)
        self._logger.log(
            sandbox_name=sandbox_name,
            command=f"write {len(content)} bytes to {path}",
            decision=decision.decision,
            execution_mode="mock",
            provider_id=self._info(sandbox_name).provider_id,
            detail=decision.reason,
        )
        if not decision.allowed:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"BLOCKED: {decision.reason}",
                sandbox_name=sandbox_name,
                command=command,
                decision="block",
                execution_mode="mock",
                simulated=True,
            )
        with self._lock:
            self._files.setdefault(sandbox_name, {})[path] = content
        return SandboxResult(
            exit_code=0,
            stdout=f"SIMULATED: wrote {len(content)} bytes to {path}",
            stderr="",
            sandbox_name=sandbox_name,
            command=command,
            decision="allow",
            execution_mode="mock",
            simulated=True,
        )

    def execute_playwright_test(self, sandbox_name: str, test_path: str, timeout: int = 120) -> SandboxResult:
        if not test_path.startswith("/workspace/flowscout/"):
            raise ValueError("test_path must be inside /workspace/flowscout/")
        return self.run_command(
            sandbox_name,
            f"npx playwright test {test_path} --reporter=line",
            timeout=timeout,
            audit_command=f"npx playwright test {test_path}",
        )

    def delete(self, sandbox_name: str) -> CleanupResult:
        with self._lock:
            info = self._sandboxes.pop(sandbox_name, None)
            self._files.pop(sandbox_name, None)
            if info is None:
                return CleanupResult(sandbox_name, True, "already absent")
            self._logger.log(
                sandbox_name=sandbox_name,
                command=f"delete sandbox {sandbox_name}",
                decision="allow",
                execution_mode="mock",
                provider_id=info.provider_id,
                detail="simulated lifecycle operation",
            )
        logger.info("Deleted mock sandbox %s", sandbox_name)
        return CleanupResult(sandbox_name, True, "mock sandbox removed")


class CelestoCloudClient:
    """Minimal REST client for Celesto Cloud, the same API the SDK and CLI drive.

    The SDK's CloudOptions cannot request a named sandbox size and this
    account requires one ("resource_overrides_unavailable"), so the adapter
    speaks REST directly. Credentials never appear in logs or error text.
    """

    def __init__(self, api_key: str, base_url: str | None = None, *, timeout: float = 60.0) -> None:
        self._base_url = (base_url or os.getenv("CELESTO_API_URL", "https://api.celesto.ai")).rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else body
            if isinstance(detail, dict):
                code = detail.get("error_code") or detail.get("code") or response.status_code
                return f"{code}: {detail.get('message', '')}".strip()[:250]
            return str(detail or response.text)[:250]
        except Exception:
            return (response.text or "")[:250]

    @staticmethod
    def _retry_after(response: httpx.Response, fallback: float) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(1.0, float(header))
            except ValueError:
                pass
        return fallback

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int = 0,
    ) -> httpx.Response:
        backoff = 10.0
        attempt = 0
        while True:
            response = self._client.request(
                method, path, json=json_body, headers=headers, timeout=timeout or 60.0
            )
            if response.status_code != 429 or attempt >= retries:
                return response
            wait = self._retry_after(response, backoff)
            logger.warning(
                "Celesto API 429 on %s %s; retrying in %.0fs (attempt %d)",
                method, path, wait, attempt + 1,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, 60.0)
            attempt += 1

    def create_computer(self, *, template_id: str, size_id: str, image: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/computers",
            json_body={"template_id": template_id, "size_id": size_id, "image": image},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            retries=4,
        )
        if response.status_code >= 400:
            raise SandboxUnavailableError(
                f"Celesto create failed ({response.status_code}): {self._detail(response)}"
            )
        return response.json()

    def get_computer(self, computer_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/v1/computers/{computer_id}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SandboxExecutionError(f"Celesto get failed: {self._detail(response)}")
        return response.json()

    def list_computers(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/computers")
        if response.status_code >= 400:
            raise SandboxExecutionError(f"Celesto list failed: {self._detail(response)}")
        data = response.json()
        if isinstance(data, list):
            return data
        for key in ("computers", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    def wait_until_running(self, computer_id: str, timeout: float) -> tuple[str, float]:
        started = time.monotonic()
        deadline = started + timeout
        status = "creating"
        while time.monotonic() < deadline:
            computer = self.get_computer(computer_id)
            if computer is None:
                raise SandboxExecutionError("computer disappeared while waiting for boot")
            status = computer.get("status", status)
            if status in ("running", "error", "failed"):
                break
            time.sleep(3.0)
        return status, round(time.monotonic() - started, 3)

    def exec_command(self, computer_id: str, command: str, timeout: int = 30) -> dict[str, Any]:
        clamped = max(1, min(int(timeout), 300))
        response = self._request(
            "POST",
            f"/v1/computers/{computer_id}/exec",
            json_body={"command": command, "timeout": clamped},
            timeout=clamped + 20.0,
            retries=2,
        )
        if response.status_code >= 400:
            raise SandboxExecutionError(
                f"Celesto exec failed ({response.status_code}): {self._detail(response)}"
            )
        return response.json()

    def browser_url(self, computer_id: str, *, wait_seconds: float = 90.0) -> str:
        """Return a CDP websocket URL, retrying while the gateway warms up."""
        deadline = time.monotonic() + wait_seconds
        last_detail = ""
        while True:
            response = self._request("POST", f"/v1/computers/{computer_id}/browser")
            if response.status_code == 201:
                return self._cdp_url(response.json())
            last_detail = f"{response.status_code}: {self._detail(response)}"
            if response.status_code not in (501, 503) or time.monotonic() >= deadline:
                raise SandboxUnavailableError(
                    f"Celesto browser connection unavailable ({last_detail})"
                )
            time.sleep(5.0)

    @staticmethod
    def _cdp_url(body: dict[str, Any]) -> str:
        # Mirrors the SDK's cloud_connection_info conversion: wss gateway + token.
        url = urlsplit(str(body["gateway_url"]))
        scheme = {"https": "wss", "http": "ws"}.get(url.scheme, url.scheme)
        query = parse_qsl(url.query, keep_blank_values=True)
        return urlunsplit((scheme, url.netloc, url.path, urlencode([*query, ("token", body["token"])]), ""))

    def publish_port(self, computer_id: str, port: int) -> dict[str, Any]:
        response = self._request(
            "POST", f"/v1/computers/{computer_id}/published-ports", json_body={"port": port}
        )
        if response.status_code >= 400:
            raise SandboxExecutionError(
                f"Celesto publish_port failed ({response.status_code}): {self._detail(response)}"
            )
        entry = response.json()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and entry.get("status") not in ("ready", "active", "error", "failed"):
            listing = self._request("GET", f"/v1/computers/{computer_id}/published-ports")
            if listing.status_code == 200:
                for item in listing.json() or []:
                    if item.get("port") == port:
                        entry = item
                        break
            time.sleep(3.0)
        return entry

    def delete_computer(self, computer_id: str, timeout: float = 120.0) -> bool:
        response = self._request("DELETE", f"/v1/computers/{computer_id}")
        if response.status_code not in (200, 202, 204, 404):
            raise SandboxExecutionError(f"Celesto delete failed: {self._detail(response)}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            computer = self.get_computer(computer_id)
            if computer is None or computer.get("status") == "deleted":
                return True
            time.sleep(3.0)
        return False


class CelestoSandboxAdapter(SandboxAdapter):
    """Live adapter backed by Celesto Cloud through its REST API.

    The SDK (0.1.6/0.1.8) cannot request a named sandbox size, so cloud
    computers are created and driven over REST; SDK importability is still
    recorded for evidence. All commands pass the deny-by-default policy
    before any dispatch, exactly as in mock mode.
    """

    execution_mode: Literal["live"] = "live"

    WRITE_CHUNK_CHARS = 6000

    def __init__(
        self,
        command_log_path: Path | None = None,
        *,
        api_key: str | None = None,
        image: str | None = None,
        template_id: str | None = None,
        size_id: str | None = None,
        startup_timeout: float | None = None,
        client: Any = None,
        ownership_registry: OwnershipRegistry | None = None,
    ) -> None:
        super().__init__(command_log_path=command_log_path)
        self._api_key = (api_key if api_key is not None else os.getenv("CELESTO_API_KEY", "")).strip()
        self._image = image or os.getenv("CELESTO_IMAGE", "ubuntu-desktop-24.04")
        self._template_id = template_id or os.getenv("CELESTO_TEMPLATE_ID", "browser-agent")
        self._size_id = (size_id or os.getenv("CELESTO_SIZE_ID", "standard")).strip()
        self._startup_timeout = startup_timeout or float(os.getenv("CELESTO_STARTUP_TIMEOUT_S", "240"))
        self._computer_ids: dict[str, str] = {}
        self._ownership = ownership_registry or OwnershipRegistry()
        self._browser_gateway_down = False
        self._client: Any = client
        self._sdk_available = False
        self._sdk_error: str | None = None
        try:
            import celesto  # noqa: F401

            self._sdk_available = True
        except Exception as exc:  # Informational only: REST is the transport now.
            self._sdk_error = f"{type(exc).__name__}: {exc}"

    @property
    def sdk_available(self) -> bool:
        return self._sdk_available

    def _cloud(self) -> Any:
        if self._client is None:
            self._client = CelestoCloudClient(self._api_key)
        return self._client

    def _require_ready(self) -> None:
        key = self._api_key
        if not key or key == "replace-me":
            raise SandboxUnavailableError(
                "CELESTO_API_KEY is not configured; live Celesto mode requires a real key"
            )

    def create(self, sandbox_name: str) -> SandboxInfo:
        validate_sandbox_name(sandbox_name)
        self._require_ready()
        if sandbox_name in self._computer_ids:
            raise SandboxUnavailableError(f"sandbox {sandbox_name!r} already exists")

        patience = float(os.getenv("CELESTO_CREATE_PATIENCE_S", "1800"))
        deadline = time.monotonic() + patience
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._create_once(sandbox_name)
            except SandboxUnavailableError as exc:
                text = str(exc)
                throttled = "429" in text or "rate limit" in text.lower()
                if throttled and "retained_sandbox_limit" in text:
                    mode = reclaim_mode()
                    if mode == "off":
                        raise SandboxUnavailableError(
                            "Celesto retained-sandbox quota is exhausted and automatic "
                            "orphan reclamation is off (CELESTO_AUTO_RECLAIM=off). "
                            "Free a slot, raise the quota, or review and apply the "
                            "scoped reclaimer: `python scripts/reclaim_orphans.py` "
                            "then `python scripts/reclaim_orphans.py --apply --yes`."
                        ) from exc
                    candidates = self.reclaim_orphans(dry_run=(mode == "dry-run"))
                    if mode == "apply" and candidates:
                        continue
                    if mode == "dry-run":
                        raise SandboxUnavailableError(
                            "Celesto retained-sandbox quota is exhausted; "
                            f"{len(candidates)} FlowScout-owned candidate(s) can be "
                            "reclaimed. Review with `python scripts/reclaim_orphans.py` "
                            "and apply with "
                            "`python scripts/reclaim_orphans.py --apply --yes`."
                        ) from exc
                if not throttled or time.monotonic() >= deadline:
                    raise
                logger.warning(
                    "create of %s throttled by quota (attempt %d): %s; waiting for the "
                    "quota window before retrying",
                    sandbox_name, attempt, text[:120],
                )
                time.sleep(120.0)

    def plan_reclaim(
        self,
        *,
        min_age_s: float | None = None,
        keep: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Build a fail-closed reclamation plan without deleting anything.

        Only computers present in the local ownership registry are considered.
        The registry records the template used at create time, so a cloud
        computer whose template no longer matches is skipped. A missing or
        corrupt registry, or an unavailable cloud listing, yields no
        candidates.
        """
        keep_ids = set(self._computer_ids.values())
        keep_ids.update(
            item.strip()
            for item in os.getenv("CELESTO_KEEP_COMPUTER_IDS", "").split(",")
            if item.strip()
        )
        keep_ids.update(str(item).strip() for item in (keep or []) if str(item).strip())
        age_limit = reclaim_min_age_s() if min_age_s is None else max(0.0, float(min_age_s))
        plan: dict[str, Any] = {
            "registry": str(self._ownership.path),
            "min_age_s": age_limit,
            "keep_ids": sorted(keep_ids),
            "candidates": [],
            "skipped": [],
            "registry_error": None,
            "cloud_error": None,
        }
        try:
            records = self._ownership.records()
        except OwnershipRegistryError as exc:
            plan["registry_error"] = str(exc)
            logger.warning("Orphan reclamation disabled: %s", exc)
            return plan
        try:
            computers = self._cloud().list_computers()
        except Exception as exc:
            plan["cloud_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Orphan reclamation disabled: cloud listing failed: %s", exc)
            return plan
        cloud_by_id = {
            str(computer.get("id") or ""): computer
            for computer in computers
            if computer.get("id")
        }
        now = datetime.now(timezone.utc)
        for record in records:
            computer_id = str(record.get("computer_id") or "")
            if not computer_id:
                continue
            reason = ""
            if record.get("status") == "deleted":
                reason = "already_deleted"
            elif computer_id in keep_ids:
                reason = "keep_protected"
            else:
                age_s = record_age_s(record, now=now)
                if age_s is None:
                    reason = "invalid_created_at"
                elif age_s < age_limit:
                    reason = "too_young"
            if not reason:
                cloud = cloud_by_id.get(computer_id)
                if cloud is None:
                    reason = "not_present_in_cloud"
                elif str(cloud.get("status") or "") in {"deleted", "deleting"}:
                    reason = "cloud_deleted"
                else:
                    recorded_template = str(record.get("template_id") or "")
                    cloud_template = str(cloud.get("template_id") or "")
                    if (
                        recorded_template
                        and cloud_template
                        and recorded_template != cloud_template
                    ):
                        reason = "template_mismatch"
            if reason:
                plan["skipped"].append(
                    {"computer_id": computer_id, "reason": reason}
                )
                continue
            plan["candidates"].append(dict(record))
        return plan

    def apply_reclaim_plan(self, plan: dict[str, Any]) -> list[str]:
        """Delete the candidates in a plan, auditing every attempt."""
        reclaimed: list[str] = []
        for record in plan.get("candidates", []):
            computer_id = str(record.get("computer_id") or "")
            if not computer_id:
                continue
            try:
                confirmed = self._cloud().delete_computer(computer_id)
            except Exception as exc:
                logger.warning("orphan reclaim of %s failed: %s", computer_id, exc)
                try:
                    self._ownership.mark_deleted(
                        computer_id,
                        confirmed=False,
                        reason=f"reclaim failed: {type(exc).__name__}",
                    )
                except OwnershipRegistryError as registry_error:
                    logger.warning(
                        "could not record failed reclaim of %s: %s",
                        computer_id,
                        registry_error,
                    )
                self._logger.log(
                    sandbox_name=str(record.get("logical_name") or "sbx-orphanreclaim"),
                    command=f"reclaim orphan computer {computer_id}",
                    decision="allow",
                    execution_mode="live",
                    provider_id=computer_id,
                    detail=f"reclaim failed: {type(exc).__name__}",
                )
                continue
            try:
                self._ownership.mark_deleted(
                    computer_id,
                    confirmed=confirmed,
                    reason="scoped orphan reclaim",
                )
            except OwnershipRegistryError as exc:
                logger.warning(
                    "could not record reclaim of %s: %s", computer_id, exc
                )
            self._logger.log(
                sandbox_name=str(record.get("logical_name") or "sbx-orphanreclaim"),
                command=f"reclaim orphan computer {computer_id}",
                decision="allow",
                execution_mode="live",
                provider_id=computer_id,
                detail=(
                    "FlowScout-owned registry candidate; "
                    f"template={record.get('template_id')}; confirmed={confirmed}"
                ),
            )
            if confirmed:
                reclaimed.append(computer_id)
        if reclaimed:
            logger.info("reclaimed orphan computer(s): %s", ", ".join(reclaimed))
        return reclaimed

    def reclaim_orphans(
        self,
        *,
        dry_run: bool | None = None,
        min_age_s: float | None = None,
        keep: Iterable[str] | None = None,
    ) -> list[str]:
        """Reclaim only FlowScout-owned registry candidates.

        ``dry_run`` overrides ``CELESTO_AUTO_RECLAIM``. With no override, the
        environment decides: ``off`` (default) does nothing, ``dry-run``
        returns candidates without deleting, and ``apply`` deletes them.
        Unregistered cloud computers are never listed for deletion.
        """
        if dry_run is True:
            mode = "dry-run"
        elif dry_run is False:
            mode = "apply"
        else:
            mode = reclaim_mode()
        if mode == "off":
            return []
        plan = self.plan_reclaim(min_age_s=min_age_s, keep=keep)
        if plan["registry_error"] or plan["cloud_error"]:
            return []
        if mode == "dry-run":
            for record in plan["candidates"]:
                self._logger.log(
                    sandbox_name=str(record.get("logical_name") or "sbx-orphanreclaim"),
                    command=f"reclaim candidate {record.get('computer_id')} (dry-run)",
                    decision="allow",
                    execution_mode="live",
                    provider_id=str(record.get("computer_id") or ""),
                    detail=(
                        "no deletion performed; FlowScout ownership registry "
                        f"template={record.get('template_id')}"
                    ),
                )
            return [str(record["computer_id"]) for record in plan["candidates"]]
        return self.apply_reclaim_plan(plan)

    def _register_ownership(
        self, computer_id: str, sandbox_name: str, computer: dict[str, Any] | None = None
    ) -> None:
        try:
            self._ownership.register(
                computer_id=computer_id,
                logical_name=sandbox_name,
                template_id=str(
                    (computer or {}).get("template_id") or self._template_id
                ),
                size_id=str((computer or {}).get("size_id") or self._size_id),
                image=self._image,
            )
        except Exception as exc:
            # Reclamation is fail-closed: an unregistered computer is simply
            # never auto-reclaimed. The workflow still deletes it on cleanup.
            logger.error(
                "Ownership registration failed for %s: %s: %s",
                computer_id,
                type(exc).__name__,
                exc,
            )

    def _mark_ownership_deleted(
        self, computer_id: str, *, confirmed: bool, reason: str
    ) -> None:
        try:
            self._ownership.mark_deleted(
                computer_id, confirmed=confirmed, reason=reason
            )
        except Exception as exc:
            logger.warning(
                "Could not update ownership registry for %s: %s: %s",
                computer_id,
                type(exc).__name__,
                exc,
            )

    def _create_once(self, sandbox_name: str) -> SandboxInfo:
        cloud = self._cloud()
        computer = cloud.create_computer(
            template_id=self._template_id, size_id=self._size_id, image=self._image
        )
        computer_id = str(computer["id"])
        # Register immediately after the provider returns an id, before the
        # long wait for running. A crash after this point leaves a candidate
        # the scoped reclaimer can find; a crash before it leaves an
        # unregistered computer that is deliberately never touched.
        self._register_ownership(computer_id, sandbox_name, computer)
        try:
            status, boot_seconds = cloud.wait_until_running(computer_id, self._startup_timeout)
            if status != "running":
                raise SandboxUnavailableError(
                    f"Celesto computer did not reach running (status={status})"
                )
            browser_url: str | None = None
            if not self._browser_gateway_down:
                try:
                    browser_url = cloud.browser_url(computer_id, wait_seconds=5.0)
                except Exception as exc:
                    # e.g. Micro trial: interactive connections never become
                    # available. Remember so later sandboxes skip the wait.
                    self._browser_gateway_down = True
                    logger.warning(
                        "Celesto browser gateway unavailable (%s); in-guest browser "
                        "runtime will be used for this and later sandboxes",
                        str(exc)[:120],
                    )

            info = SandboxInfo(
                sandbox_name=sandbox_name,
                state="ready",
                provider="celesto_cloud",
                execution_mode="live",
                provider_id=computer_id,
                created_at=_utc_now(),
                boot_to_ready_s=boot_seconds,
                browser_url=browser_url,
            )
            self._computer_ids[sandbox_name] = computer_id
            self._sandboxes[sandbox_name] = info
            self._logger.log(
                sandbox_name=sandbox_name,
                command=f"create sandbox {sandbox_name}",
                decision="allow",
                execution_mode="live",
                provider_id=computer_id,
                detail=(
                    f"boot_to_ready_s={boot_seconds}; template={self._template_id}; "
                    f"size={self._size_id}"
                ),
            )
            logger.info("Created Celesto cloud computer %s for %s", computer_id, sandbox_name)
            return info
        except BaseException:
            # A failed start may still have allocated billable resources.
            try:
                deleted = cloud.delete_computer(computer_id)
            except Exception as cleanup_error:
                logger.error("Cleanup after failed start of %s failed: %s", sandbox_name, cleanup_error)
                self._mark_ownership_deleted(
                    computer_id,
                    confirmed=False,
                    reason=f"create failed; delete failed: {type(cleanup_error).__name__}",
                )
            else:
                self._mark_ownership_deleted(
                    computer_id,
                    confirmed=deleted,
                    reason="create failed",
                )
            raise

    def run_command(
        self,
        sandbox_name: str,
        command: str,
        timeout: int = 30,
        *,
        audit_command: str | None = None,
    ) -> SandboxResult:
        info = self._info(sandbox_name)
        computer_id = self._computer_ids.get(sandbox_name)
        if computer_id is None:
            raise SandboxUnavailableError(f"sandbox {sandbox_name!r} has no live computer handle")

        decision = self.policy.check(command)
        self._logger.log(
            sandbox_name=sandbox_name,
            command=audit_command or command,
            decision=decision.decision,
            execution_mode="live",
            provider_id=info.provider_id,
            detail=decision.reason,
        )
        if not decision.allowed:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"BLOCKED: {decision.reason}",
                sandbox_name=sandbox_name,
                command=command,
                decision="block",
                execution_mode="live",
            )

        started = time.monotonic()
        try:
            result = self._cloud().exec_command(computer_id, command, timeout=timeout)
        except SandboxError:
            raise
        except Exception as exc:
            logger.error("Command failed in live sandbox %s: %s", sandbox_name, type(exc).__name__)
            raise SandboxExecutionError(
                f"Celesto command dispatch failed for {sandbox_name}: {type(exc).__name__}"
            ) from exc

        return SandboxResult(
            exit_code=int(result.get("exit_code", -1)),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            sandbox_name=sandbox_name,
            command=audit_command or command,
            decision="allow",
            execution_mode="live",
            wall_clock_s=round(time.monotonic() - started, 3),
        )

    def write_text(self, sandbox_name: str, path: str, content: str) -> SandboxResult:
        if not path.startswith("/workspace/flowscout/") or "/../" in path:
            raise ValueError("sandbox writes are restricted to /workspace/flowscout/")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        total_chunks = max(1, (len(encoded) + self.WRITE_CHUNK_CHARS - 1) // self.WRITE_CHUNK_CHARS)
        result: SandboxResult | None = None
        for index in range(total_chunks):
            chunk = encoded[index * self.WRITE_CHUNK_CHARS : (index + 1) * self.WRITE_CHUNK_CHARS]
            redirect = ">" if index == 0 else ">>"
            prefix = "mkdir -p /workspace/flowscout && " if index == 0 else ""
            command = f"{prefix}printf %s {shlex.quote(chunk)} | base64 -d {redirect} {path}"
            result = self.run_command(
                sandbox_name,
                command,
                timeout=30,
                audit_command=(
                    f"write {len(content)} bytes to {path} (chunk {index + 1}/{total_chunks})"
                ),
            )
            if not result.ok:
                return result
        assert result is not None
        return result

    def execute_playwright_test(self, sandbox_name: str, test_path: str, timeout: int = 120) -> SandboxResult:
        if not test_path.startswith("/workspace/flowscout/"):
            raise ValueError("test_path must be inside /workspace/flowscout/")
        return self.run_command(
            sandbox_name,
            f"npx playwright test {test_path} --reporter=line",
            timeout=timeout,
            audit_command=f"npx playwright test {test_path}",
        )

    def read_workspace_base64(self, sandbox_name: str, path: str) -> bytes:
        if not re.fullmatch(r"/workspace/flowscout/[A-Za-z0-9._/-]+", path) or "/../" in path:
            raise ValueError("sandbox reads are restricted to /workspace/flowscout/")
        result = self.run_command(
            sandbox_name,
            f"base64 -w0 {path}",
            timeout=30,
            audit_command=f"read {path}",
        )
        if not result.ok:
            raise SandboxExecutionError(
                f"could not read {path} from {sandbox_name}: {result.stderr or result.stdout}"
            )
        try:
            return base64.b64decode(result.stdout, validate=True)
        except Exception as exc:
            raise SandboxExecutionError(
                f"invalid base64 while reading {path} from {sandbox_name}"
            ) from exc

    def get_browser_url(self, sandbox_name: str) -> Optional[str]:
        info = self._info(sandbox_name)
        computer_id = self._computer_ids.get(sandbox_name)
        if computer_id is None or self._browser_gateway_down:
            return info.browser_url
        try:
            url = self._cloud().browser_url(computer_id, wait_seconds=15.0)
            info.browser_url = url
            return url
        except Exception:
            return info.browser_url

    def publish_port(self, sandbox_name: str, port: int) -> dict[str, Any]:
        """Expose an in-guest port through Celesto as a public HTTPS URL."""
        info = self._info(sandbox_name)
        computer_id = self._computer_ids.get(sandbox_name)
        if computer_id is None:
            raise SandboxUnavailableError(f"sandbox {sandbox_name!r} has no live computer handle")
        entry = self._cloud().publish_port(computer_id, port)
        self._logger.log(
            sandbox_name=sandbox_name,
            command=f"publish port {port}",
            decision="allow",
            execution_mode="live",
            provider_id=info.provider_id,
            detail=f"status={entry.get('status')}",
        )
        return entry

    def delete(self, sandbox_name: str) -> CleanupResult:
        info = self._sandboxes.get(sandbox_name)
        computer_id = self._computer_ids.get(sandbox_name)
        if computer_id is None:
            return CleanupResult(sandbox_name, True, "already absent or never created")
        try:
            confirmed = self._cloud().delete_computer(computer_id)
        except Exception as exc:
            logger.error("Celesto delete failed for %s: %s", sandbox_name, exc)
            self._mark_ownership_deleted(
                computer_id,
                confirmed=False,
                reason=f"cleanup delete failed: {type(exc).__name__}",
            )
            self._logger.log(
                sandbox_name=sandbox_name,
                command=f"delete sandbox {sandbox_name}",
                decision="allow",
                execution_mode="live",
                provider_id=info.provider_id if info else None,
                detail=f"cleanup failed: {type(exc).__name__}",
            )
            return CleanupResult(sandbox_name, False, f"delete failed: {type(exc).__name__}")
        self._mark_ownership_deleted(
            computer_id,
            confirmed=confirmed,
            reason="workflow cleanup",
        )
        self._computer_ids.pop(sandbox_name, None)
        if info is not None:
            info.state = "deleted"
        self._logger.log(
            sandbox_name=sandbox_name,
            command=f"delete sandbox {sandbox_name}",
            decision="allow",
            execution_mode="live",
            provider_id=info.provider_id if info else None,
            detail="Celesto Cloud delete confirmed" if confirmed else "delete not confirmed in time",
        )
        self._sandboxes.pop(sandbox_name, None)
        logger.info("Deleted Celesto cloud computer for %s (confirmed=%s)", sandbox_name, confirmed)
        return CleanupResult(
            sandbox_name,
            confirmed,
            "Celesto Cloud delete confirmed" if confirmed else "delete did not confirm within timeout",
        )

    def close(self) -> None:
        # Deliberately do not delete resources here. The workflow owns cleanup.
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass


def create_adapter(
    mode: str | None = None,
    *,
    command_log_path: Path | None = None,
    ownership_registry: OwnershipRegistry | None = None,
) -> SandboxAdapter:
    """Create the configured adapter without silent fallback.

    ``CELESTO_MODE=mock`` is explicit. ``CELESTO_MODE=live`` either returns a
    real adapter or raises `SandboxUnavailableError`; it never degrades to mock.
    """
    selected = (mode or os.getenv("CELESTO_MODE", "mock")).strip().lower()
    if selected == "mock":
        return MockSandboxAdapter(command_log_path=command_log_path)
    if selected == "live":
        adapter = CelestoSandboxAdapter(
            command_log_path=command_log_path,
            ownership_registry=ownership_registry,
        )
        adapter._require_ready()
        return adapter
    raise SandboxUnavailableError(f"unsupported CELESTO_MODE {selected!r}; use 'live' or 'mock'")
