"""Execute browser actions inside the guest against the local CDP Chromium.

Usage (inside the sandbox):

    python3 /workspace/flowscout/guest_browser_worker.py '<json-actions>' [base_url] [cdp_port]

Each action uses the same validated shape as the host ``BrowserAction`` schema
(plus a ``fixture_post`` pseudo-action for fixture resets through the browser
context's cookies). Results print as ONE JSON line so the host can parse them.
Screenshots are JPEG to keep the base64 pull through exec small.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

SHOTS_DIR = Path("/workspace/flowscout/shots")
DEFAULT_BASE_URL = "http://127.0.0.1:8090"
DEFAULT_CDP_PORT = 9333


def execute_action(
    page: Any,
    context: Any,
    action: dict,
    base_url: str,
    *,
    index: int = 0,
) -> dict:
    """Run one validated action and return its result row.

    Kept separate from ``run_actions`` so the host and guest implementations
    can be tested with a fake page without starting Chromium.
    """
    started = time.monotonic()
    output, error, screenshot, status = "", "", "", None
    timeout = int(action.get("timeout_ms") or 5000)
    selector = action.get("selector") or ""
    try:
        kind = action.get("action_type")
        if kind == "fixture_post":
            response = context.request.post(
                f"{base_url}{action['path']}", data=action.get("payload") or {}
            )
            status = response.status
            output = f"HTTP {status}"
        elif kind == "navigate":
            response = page.goto(action["value"], wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                raise RuntimeError(f"navigation returned HTTP {response.status}")
            output = page.url
        elif kind == "click":
            page.locator(selector).click(timeout=timeout)
        elif kind == "fill":
            page.locator(selector).fill(action.get("value", ""), timeout=timeout)
        elif kind == "assert_text":
            actual = page.locator(selector).inner_text(timeout=timeout)
            if action.get("value", "") not in actual:
                raise AssertionError(
                    f"expected {action.get('value')!r} inside {actual!r}"
                )
            output = actual
        elif kind == "assert_attribute":
            actual = page.locator(selector).get_attribute(
                action.get("attribute", ""), timeout=timeout
            )
            if actual != action.get("value"):
                raise AssertionError(
                    f"expected {action.get('attribute')}={action.get('value')!r}, got {actual!r}"
                )
            output = actual or ""
        elif kind == "read_text":
            output = page.locator(selector).inner_text(timeout=timeout)
        elif kind == "read_attribute":
            output = (
                page.locator(selector).get_attribute(
                    action.get("attribute", ""), timeout=timeout
                )
                or ""
            )
        elif kind == "exists":
            output = "true" if page.locator(selector).count() > 0 else "false"
        elif kind == "wait_attribute_change":
            locator = page.locator(selector)
            deadline = time.monotonic() + min(timeout, 2000) / 1000.0
            current = locator.get_attribute(action.get("attribute", ""), timeout=timeout) or ""
            while current == action.get("value", "") and time.monotonic() < deadline:
                page.wait_for_timeout(100)
                current = locator.get_attribute(action.get("attribute", ""), timeout=timeout) or ""
            output = current
        elif kind == "wait":
            waited = min(timeout, 5000)
            page.wait_for_timeout(waited)
            output = f"waited {waited}ms"
        elif kind == "screenshot":
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            name = f"step-{int(action.get('step') or 0):02d}-{int(time.time() * 1000)}.jpg"
            page.screenshot(
                path=str(SHOTS_DIR / name), full_page=True, type="jpeg", quality=55
            )
            screenshot = name
            output = name
        else:
            raise ValueError(f"unsupported action_type {kind!r}")
    except Exception as exc:  # noqa: BLE001 - reported per action
        error = f"{type(exc).__name__}: {exc}"
    return {
        "index": index,
        "success": not error,
        "output": (output or "")[:4000],
        "error": error[:1200],
        "screenshot": screenshot,
        "status": status,
        "wall_ms": round((time.monotonic() - started) * 1000, 1),
    }


def run_actions(actions: list[dict], base_url: str, cdp_port: int) -> list[dict]:
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(7000)
        for index, action in enumerate(actions):
            results.append(execute_action(page, context, action, base_url, index=index))
    finally:
        playwright.stop()
    return results


if __name__ == "__main__":
    actions_argument = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE_URL
    port = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_CDP_PORT
    print(json.dumps({"results": run_actions(json.loads(actions_argument), base_url, port)}))
