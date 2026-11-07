"""Run one real Celesto browser smoke and record live-only evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from app.evidence_io import atomic_write_bytes, atomic_write_json
from app.qa_agent import QAWorkflowRunner, generate_sandbox_name
from app.sandbox_runner import SecurityPolicy, create_adapter

LIVE_EVIDENCE_DIR = PROJECT_ROOT / "reports" / "live-evidence"
COMMAND_LOG = LIVE_EVIDENCE_DIR / "logs" / "commands.jsonl"
SCREENSHOT_PATH = LIVE_EVIDENCE_DIR / "live-smoke.png"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def visible_guest_browser_default() -> bool:
    value = os.getenv("FLOWSCOUT_VISIBLE_BROWSER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def build_sandbox_probe(base_url: str, *, visible: bool = True) -> str:
    headless_literal = "False" if visible else "True"
    return f'''import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = {json.dumps(base_url.rstrip("/") + "/shop/")}
SCREENSHOT = "/workspace/flowscout/live-smoke.png"
HEADLESS = {headless_literal}

if not HEADLESS and not os.getenv("DISPLAY"):
    for socket in sorted(Path("/tmp/.X11-unix").glob("X*")):
        suffix = socket.name[1:]
        if suffix.isdigit():
            os.environ["DISPLAY"] = ":" + suffix
            break
if not HEADLESS and not os.getenv("DISPLAY"):
    HEADLESS = True

with sync_playwright() as p:
    browser = p.chromium.launch(headless=HEADLESS, args=["--disable-dev-shm-usage"])
    try:
        page = browser.new_page(viewport={{"width": 1280, "height": 900}})
        response = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("[data-testid='shop-title']", timeout=15000)
        shop_title = page.locator("[data-testid='shop-title']").inner_text().strip()
        product = page.locator("[data-testid='product-widget-pro']").inner_text().strip()
        title = page.title()
        h1 = page.locator("h1").inner_text().strip()
        page.screenshot(path=SCREENSHOT, full_page=True)
        result = {{
            "status": response.status if response else None,
            "title": title,
            "h1": h1,
            "shop_title": shop_title,
            "product": product,
            "url": page.url,
            "screenshot": SCREENSHOT,
            "headless": HEADLESS,
            "display": os.getenv("DISPLAY", ""),
        }}
        print("LIVE_SMOKE_RESULT=" + json.dumps(result))
        if result["status"] != 200 or shop_title != "ShopDemo" or "Widget Pro" not in product:
            raise SystemExit(1)
    finally:
        browser.close()
'''


def parse_probe(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith("LIVE_SMOKE_RESULT="):
            try:
                return json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                return None
    return None


def result_payload(result) -> dict | None:
    if result is None:
        return None
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "decision": result.decision,
        "wall_clock_s": result.wall_clock_s,
    }


def run_sandbox_smoke(base_url: str, timeout: int, *, visible: bool = True) -> tuple[dict, int]:
    SCREENSHOT_PATH.unlink(missing_ok=True)
    adapter = None
    info = None
    sandbox_name = generate_sandbox_name()
    write_result = None
    command_result = None
    probe = None
    screenshot = b""
    cleanup = None
    failure = ""

    try:
        adapter = create_adapter("live", command_log_path=COMMAND_LOG)
        if adapter.execution_mode != "live":
            raise RuntimeError("adapter did not enter live mode")
        info = adapter.create(sandbox_name)
        write_result = adapter.write_text(
            sandbox_name,
            "/workspace/flowscout/live_smoke.py",
            build_sandbox_probe(base_url, visible=visible),
        )
        if not write_result.ok:
            raise RuntimeError(f"probe write failed: {write_result.stderr or write_result.stdout}")
        command_result = adapter.run_command(
            sandbox_name,
            "python3 /workspace/flowscout/live_smoke.py",
            timeout=timeout,
            audit_command="python3 live_smoke.py",
        )
        probe = parse_probe(command_result.stdout)
        if command_result.ok:
            screenshot = adapter.read_workspace_base64(
                sandbox_name, "/workspace/flowscout/live-smoke.png"
            )
            atomic_write_bytes(SCREENSHOT_PATH, screenshot)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        if "HTTP 429" in failure:
            failure += " (free-trial create limit observed: 2 new sandboxes per hour)"
    finally:
        if adapter is not None and info is not None:
            try:
                cleanup = adapter.delete(sandbox_name)
            except Exception as exc:
                failure = (failure + f"; cleanup: {type(exc).__name__}: {exc}").strip("; ")
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                failure = (failure + f"; close: {type(exc).__name__}: {exc}").strip("; ")

    verified = bool(
        info is not None
        and write_result is not None
        and write_result.ok
        and command_result is not None
        and command_result.ok
        and probe is not None
        and probe.get("status") == 200
        and probe.get("shop_title") == "ShopDemo"
        and "Widget Pro" in str(probe.get("product", ""))
        and screenshot
        and cleanup is not None
        and cleanup.ok
    )
    payload = {
        "generated_at": utcnow(),
        "workflow": "live_sandbox_browser_smoke",
        "execution_mode": "live",
        "browser_runtime": "sandbox_playwright",
        "live_celesto_verified": verified,
        "verification_scope": (
            "Live-only single-sandbox browser smoke; verified only when "
            "live_celesto_verified is true. This is not the 20-run benchmark."
        ),
        "base_url": base_url,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "celesto_api_key_configured": bool(os.getenv("CELESTO_API_KEY", "").strip()),
        "celesto_template_id": os.getenv("CELESTO_TEMPLATE_ID", "browser-agent"),
        "sandbox_names": [sandbox_name],
        "provider_id": info.provider_id if info is not None else None,
        "boot_to_ready_s": info.boot_to_ready_s if info is not None else None,
        "browser_cdp_url_available": bool(info.browser_url) if info is not None else None,
        "probe": probe,
        "write": result_payload(write_result),
        "command": result_payload(command_result),
        "screenshot": {
            "path": "reports/live-evidence/live-smoke.png",
            "bytes": len(screenshot),
            "sha256": sha256_bytes(screenshot) if screenshot else None,
        },
        "cleanup_ok": cleanup.ok if cleanup is not None else info is None,
        "cleanup_message": cleanup.message if cleanup is not None else "no sandbox created",
        "requested_guest_browser_visible": visible,
        "failure": failure,
    }
    return payload, 0 if verified else 1


def run_cdp_workflow(
    base_url: str,
    timeout: int,
    inject_failure: bool,
    *,
    visible: bool = True,
) -> tuple[dict, int]:
    adapter = None
    outcome = None
    failure = ""
    sandbox_name = generate_sandbox_name()
    try:
        adapter = create_adapter("live", command_log_path=COMMAND_LOG)
        if adapter.execution_mode != "live":
            raise RuntimeError("adapter did not enter live mode")
        runner = QAWorkflowRunner(
            adapter,
            sandbox_name,
            base_url,
            execution_mode="live",
            record_video=False,
            use_sandbox_browser=True,
            guest_browser_visible=visible,
        )
        outcome = runner.run(inject_failure=inject_failure)
        runner.save_trace(LIVE_EVIDENCE_DIR / "live-failure-trace.jsonl")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                failure = (failure + f"; close: {type(exc).__name__}: {exc}").strip("; ")

    verified = bool(
        outcome is not None
        and outcome.success
        and outcome.cleanup_ok
        and outcome.browser_runtime == "celesto_cdp"
    )
    payload = {
        "generated_at": utcnow(),
        "workflow": "live_cdp_workflow",
        "execution_mode": "live",
        "browser_runtime": "celesto_cdp",
        "live_celesto_verified": verified,
        "verification_scope": (
            "One full FlowScout workflow over a Celesto CDP browser connection. "
            "This is not the 20-run benchmark."
        ),
        "base_url": base_url,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "celesto_api_key_configured": bool(os.getenv("CELESTO_API_KEY", "").strip()),
        "inject_failure": inject_failure,
        "sandbox_names": [sandbox_name],
        "outcome": outcome.to_dict() if outcome is not None else None,
        "requested_guest_browser_visible": visible,
        "failure": failure,
        "timeout_s": timeout,
    }
    return payload, 0 if verified else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one live Celesto browser smoke")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--mode", choices=("sandbox", "cdp"), default="sandbox")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--inject-failure", action="store_true")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the live guest Chromium headless instead of on the sandbox desktop",
    )
    args = parser.parse_args()
    visible = visible_guest_browser_default() and not args.headless

    base_url = args.base_url.rstrip("/")
    decision = SecurityPolicy().check_target_url(base_url)
    if not decision.allowed:
        print(f"ERROR: target URL rejected: {decision.reason}")
        return 2
    if "127.0.0.1" in base_url or "localhost" in base_url:
        print("ERROR: live runs require a target reachable from Celesto Cloud.")
        return 2

    LIVE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    if args.mode == "cdp":
        payload, code = run_cdp_workflow(
            base_url,
            args.timeout,
            args.inject_failure,
            visible=visible,
        )
        evidence_path = LIVE_EVIDENCE_DIR / "live-cdp-smoke.json"
    else:
        payload, code = run_sandbox_smoke(base_url, args.timeout, visible=visible)
        evidence_path = LIVE_EVIDENCE_DIR / "live-smoke.json"
    atomic_write_json(evidence_path, payload)
    print(json.dumps(payload, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
