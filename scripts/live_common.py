"""Shared helpers for live Celesto runs.

Keeping deploy and visibility logic here lets the reproducible single-run
runner avoid importing the full evidence orchestrator while still using the
exact same in-guest fixture deployment as the benchmark script.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.provenance import utcnow
from app.sandbox_runner import SandboxAdapter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DEFAULT = "http://127.0.0.1:8090"
GUEST_APP_BASE_URL = "http://127.0.0.1:8090"
GUEST_APP_PORT = 8090

# Hourly rates by named size, from /v1/billing/sizes.
LIVE_HOURLY_USD = {
    "micro": 0.015,
    "small": 0.02,
    "standard": 0.03,
    "memory": 0.04,
    "large": 0.08,
    "xl": 0.16,
}


def live_size_id() -> str:
    return os.getenv("CELESTO_SIZE_ID", "standard").strip().lower()


def live_hourly_rate() -> float:
    return LIVE_HOURLY_USD.get(live_size_id(), 0.03)


def visible_guest_browser_default() -> bool:
    value = os.getenv("FLOWSCOUT_VISIBLE_BROWSER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_rows(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def deploy_guest_fixture(adapter: SandboxAdapter, sandbox_name: str) -> None:
    """Deploy and start the ShopDemo fixture inside the named sandbox.

    The browser lives in the same guest, so it reaches the fixture on
    localhost; no host service or public tunnel is involved. Every command
    passes the deny-by-default policy (the pip install is an exact-match
    deploy exception) and is audited like any other command.
    """
    app_source = (PROJECT_ROOT / "app" / "target_saas_app.py").read_text(encoding="utf-8")
    healthcheck_source = (PROJECT_ROOT / "app" / "guest_healthcheck.py").read_text(
        encoding="utf-8"
    )
    browser_worker_source = (PROJECT_ROOT / "app" / "guest_browser_worker.py").read_text(
        encoding="utf-8"
    )
    browser_launcher_source = (PROJECT_ROOT / "app" / "guest_browser_launcher.py").read_text(
        encoding="utf-8"
    )

    uploads = (
        ("/workspace/flowscout/target_saas_app.py", app_source),
        ("/workspace/flowscout/guest_healthcheck.py", healthcheck_source),
        ("/workspace/flowscout/guest_browser_worker.py", browser_worker_source),
        ("/workspace/flowscout/guest_browser_launcher.py", browser_launcher_source),
    )
    for path, source in uploads:
        write = adapter.write_text(sandbox_name, path, source)
        if not write.ok:
            raise RuntimeError(f"guest upload of {path} failed: {write.stderr}")

    # Exact-match policy exception: only this precise install string is allowed.
    install = adapter.run_command(
        sandbox_name,
        "pip install --quiet fastapi uvicorn python-multipart",
        timeout=240,
    )
    if not install.ok:
        raise RuntimeError(
            f"guest dependency install failed: {install.stderr or install.stdout}"
        )

    start = adapter.run_command(
        sandbox_name,
        "nohup python3 /workspace/flowscout/target_saas_app.py --serve "
        "> /workspace/flowscout/server.log 2>&1 &",
        timeout=30,
        audit_command="start guest ShopDemo fixture (nohup, background)",
    )
    if not start.ok:
        raise RuntimeError(f"guest fixture start failed: {start.stderr or start.stdout}")

    health = adapter.run_command(
        sandbox_name,
        "python3 /workspace/flowscout/guest_healthcheck.py",
        timeout=150,
        audit_command="healthcheck guest ShopDemo fixture",
    )
    if not health.ok:
        log_tail = adapter.run_command(
            sandbox_name,
            "cat /workspace/flowscout/server.log",
            timeout=30,
            audit_command="read guest server log",
        )
        raise RuntimeError(
            "guest fixture did not become healthy: "
            f"{health.stderr or health.stdout}; server log: {log_tail.stdout[-500:]}"
        )
