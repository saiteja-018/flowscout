"""Execute browser actions inside the guest against the local CDP Chromium.

Usage (inside the sandbox):

    python3 /workspace/flowscout/guest_browser_worker.py '<json-actions>' [base_url] [cdp_port]

Each action uses the same validated shape as the host ``BrowserAction`` schema
(plus a ``fixture_post`` pseudo-action for fixture resets through the browser
context's cookies). Results print as ONE JSON line so the host can parse them.
Screenshots are JPEG to keep the base64 pull through exec small.

The worker also installs console/page/network listeners for the duration of a
batch.  ``assert_no_console_errors`` and ``assert_no_failed_requests`` use that
buffer, so a saved regression test can reproduce a browser-error issue.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SHOTS_DIR = Path("/workspace/flowscout/shots")
DEFAULT_BASE_URL = "http://127.0.0.1:8090"
DEFAULT_CDP_PORT = 9333
NOISE_FRAGMENTS = ("favicon.ico", "favicon", "robots.txt", "sitemap.xml")


def _clean(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _is_noise(url: str, text: str = "") -> bool:
    haystack = f"{url} {text}".lower()
    return any(fragment in haystack for fragment in NOISE_FRAGMENTS)


def _same_origin(url: str, base_url: str) -> bool:
    try:
        left = urlsplit(url)
        right = urlsplit(base_url)
    except ValueError:
        return False
    left_port = left.port or (443 if left.scheme == "https" else 80)
    right_port = right.port or (443 if right.scheme == "https" else 80)
    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left_port == right_port
    )


def _is_private_literal(url: str) -> bool:
    """True for loopback/private/metadata IP literals (not DNS names)."""
    import ipaddress

    try:
        host = (urlsplit(url).hostname or "").strip("[]")
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or str(address) in {"169.254.169.254", "169.254.170.2", "100.100.100.200"}
    )


def _target_is_public(base_url: str) -> bool:
    try:
        host = (urlsplit(base_url).hostname or "").strip("[]")
        import ipaddress

        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


SNAPSHOT_JS = r"""
() => {
  const clean = (value, limit) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit || 120);
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 2 && rect.height > 2 && style.visibility !== 'hidden' &&
      style.display !== 'none' && Number(style.opacity || 1) > 0.01;
  };
  let counter = 0;
  const refFor = (el) => {
    let ref = el.getAttribute('data-flowscout-ref');
    if (!ref) {
      counter += 1;
      ref = 'e' + counter;
      el.setAttribute('data-flowscout-ref', ref);
    }
    return ref;
  };
  const esc = (value) => String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const unique = (selector) => {
    try { return document.querySelectorAll(selector).length === 1; } catch (e) { return false; }
  };
  const stable = (el, kind) => {
    const testid = el.getAttribute('data-testid');
    if (testid) {
      const selector = `[data-testid='${esc(testid)}']`;
      if (unique(selector)) return selector;
    }
    const id = el.id || '';
    if (/^[A-Za-z][A-Za-z0-9_-]{0,80}$/.test(id) &&
        document.querySelectorAll('#' + id).length === 1) {
      return '#' + id;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      const name = el.getAttribute('name');
      if (name && !name.includes("'")) {
        const selector = `[name='${esc(name)}']`;
        if (unique(selector)) return selector;
      }
      const placeholder = el.getAttribute('placeholder');
      if (placeholder && !placeholder.includes("'")) {
        const selector = `[placeholder='${esc(placeholder)}']`;
        if (unique(selector)) return selector;
      }
      const aria = el.getAttribute('aria-label');
      if (aria && !aria.includes("'")) {
        const selector = `[aria-label='${esc(aria)}']`;
        if (unique(selector)) return selector;
      }
    }
    if (kind === 'button' || kind === 'link') {
      const aria = el.getAttribute('aria-label');
      if (aria && !aria.includes("'")) {
        const selector = `[aria-label='${esc(aria)}']`;
        if (unique(selector)) return selector;
      }
      const text = clean(el.innerText || el.textContent, 80);
      if (text && !text.includes('"')) {
        const same = [...document.querySelectorAll('a,button,[role="button"]')]
          .filter((other) => clean(other.innerText || other.textContent, 80) === text).length;
        if (same === 1) return `text="${text}"`;
      }
    }
    return '';
  };
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'), 120);
    const id = el.id;
    if (id) {
      const label = document.querySelector(`label[for="${id.replace(/"/g, '\\"')}"]`);
      if (label) return clean(label.innerText || label.textContent, 120);
    }
    const parent = el.closest('label');
    if (parent) return clean(parent.innerText || parent.textContent, 120);
    return '';
  };
  const headingRows = [...document.querySelectorAll('h1,h2,h3')]
    .filter(visible).slice(0, 15)
    .map((el) => ({ level: Number(el.tagName.slice(1)), text: clean(el.innerText || el.textContent, 160) }))
    .filter((row) => row.text);
  const links = [...document.querySelectorAll('a[href]')]
    .filter(visible).slice(0, 80)
    .map((el) => {
      const ref = refFor(el);
      return {
        ref,
        selector: `[data-flowscout-ref="${ref}"]`,
        stable_selector: stable(el, 'link'),
        text: clean(el.innerText || el.textContent || el.getAttribute('aria-label'), 120),
        href: el.href,
        aria_label: clean(el.getAttribute('aria-label'), 120),
      };
    })
    .filter((row) => row.text || row.aria_label);
  const buttons = [...document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')]
    .filter(visible).slice(0, 60)
    .map((el) => {
      const ref = refFor(el);
      return {
        ref,
        selector: `[data-flowscout-ref="${ref}"]`,
        stable_selector: stable(el, 'button'),
        text: clean(el.innerText || el.value || el.getAttribute('aria-label'), 120),
        aria_label: clean(el.getAttribute('aria-label'), 120),
        type: clean(el.getAttribute('type') || 'button', 30),
        disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
        in_form: Boolean(el.closest('form')),
      };
    })
    .filter((row) => row.text || row.aria_label);
  const inputs = [...document.querySelectorAll('input,select,textarea')]
    .filter(visible).slice(0, 60)
    .map((el) => {
      const ref = refFor(el);
      const tag = el.tagName.toLowerCase();
      return {
        ref,
        selector: `[data-flowscout-ref="${ref}"]`,
        stable_selector: stable(el, 'input'),
        tag,
        type: clean(el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : 'text'), 30).toLowerCase(),
        name: clean(el.getAttribute('name'), 80),
        placeholder: clean(el.getAttribute('placeholder'), 120),
        aria_label: clean(el.getAttribute('aria-label'), 120),
        label: labelFor(el),
        value: clean(el.value, 160),
        required: Boolean(el.required),
        disabled: Boolean(el.disabled),
        in_form: Boolean(el.closest('form')),
      };
    })
    .filter((row) => !row.disabled);
  const forms = [...document.querySelectorAll('form')]
    .filter(visible).slice(0, 12)
    .map((el) => {
      const ref = refFor(el);
      const submit = el.querySelector('button[type="submit"],input[type="submit"],button:not([type])');
      return {
        ref,
        selector: `[data-flowscout-ref="${ref}"]`,
        action: el.action || '',
        method: clean(el.getAttribute('method') || 'get', 10).toLowerCase(),
        input_count: el.querySelectorAll('input,select,textarea').length,
        has_password: Boolean(el.querySelector('input[type="password"]')),
        submit_text: clean(submit ? (submit.innerText || submit.value || submit.getAttribute('aria-label')) : '', 80),
      };
    });
  const images = [...document.images];
  const duplicateIds = (() => {
    const seen = new Set();
    let duplicates = 0;
    for (const el of document.querySelectorAll('[id]')) {
      if (seen.has(el.id)) duplicates += 1;
      seen.add(el.id);
    }
    return duplicates;
  })();
  return {
    url: location.href,
    title: document.title || '',
    lang: document.documentElement.lang || '',
    headings: headingRows,
    links,
    buttons,
    inputs,
    forms,
    images: {
      total: images.length,
      missing_alt: images.filter((img) => !img.getAttribute('alt')).length,
      broken: images.filter((img) => img.complete && img.naturalWidth === 0).length,
    },
    duplicate_ids: duplicateIds,
    links_count: document.querySelectorAll('a[href]').length,
    buttons_count: document.querySelectorAll('button,[role="button"]').length,
    inputs_count: document.querySelectorAll('input,select,textarea').length,
    text_excerpt: clean(document.body ? document.body.innerText : '', 1200),
  };
}
"""


def _new_diagnostics() -> dict[str, list[dict[str, Any]]]:
    return {
        "console_errors": [],
        "page_errors": [],
        "failed_requests": [],
        "http_errors": [],
    }


def attach_diagnostics(page: Any, base_url: str) -> dict[str, list[dict[str, Any]]]:
    diagnostics = _new_diagnostics()

    def on_console(message: Any) -> None:
        try:
            if getattr(message, "type", "") != "error":
                return
            text = _clean(getattr(message, "text", ""), 500)
            location = getattr(message, "location", {}) or {}
            url = str(location.get("url") or "")
            if _is_noise(url, text):
                return
            diagnostics["console_errors"].append(
                {
                    "text": text,
                    "url": url,
                    "line": location.get("lineNumber"),
                    "column": location.get("columnNumber"),
                    "same_origin": _same_origin(url, base_url) if url else True,
                }
            )
        except Exception:
            return

    def on_page_error(error: Any) -> None:
        try:
            text = _clean(error, 500)
            if _is_noise("", text):
                return
            diagnostics["page_errors"].append({"error": text})
        except Exception:
            return

    def on_request_failed(request: Any) -> None:
        try:
            url = str(getattr(request, "url", "") or "")
            if _is_noise(url):
                return
            failure = getattr(request, "failure", None)
            detail = failure if isinstance(failure, str) else str(failure or "")
            diagnostics["failed_requests"].append(
                {
                    "url": url,
                    "method": str(getattr(request, "method", "") or ""),
                    "failure": _clean(detail, 300),
                    "same_origin": _same_origin(url, base_url),
                }
            )
        except Exception:
            return

    def on_response(response: Any) -> None:
        try:
            status = int(getattr(response, "status", 0) or 0)
            url = str(getattr(response, "url", "") or "")
            if status < 400 or _is_noise(url):
                return
            diagnostics["http_errors"].append(
                {
                    "url": url,
                    "status": status,
                    "method": str(getattr(response, "request", None).method if getattr(response, "request", None) else ""),
                    "same_origin": _same_origin(url, base_url),
                }
            )
        except Exception:
            return

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    return diagnostics


def _diagnostic_summary(diagnostics: dict[str, list[dict[str, Any]]], key: str) -> list[dict[str, Any]]:
    rows = diagnostics.get(key) or []
    if key == "failed_requests":
        rows = [row for row in rows if row.get("same_origin") and not _is_noise(row.get("url", ""))]
    elif key == "http_errors":
        rows = [row for row in rows if row.get("same_origin")]
    return rows[:20]


def execute_action(
    page: Any,
    context: Any,
    action: dict,
    base_url: str,
    *,
    index: int = 0,
    diagnostics: dict[str, list[dict[str, Any]]] | None = None,
    shots_dir: Path | str | None = None,
) -> dict:
    """Run one validated action and return its result row.

    Kept separate from ``run_actions`` so the host and guest implementations
    can be tested with a fake page without starting Chromium.
    """
    started = time.monotonic()
    output, error, screenshot, status = "", "", "", None
    timeout = int(action.get("timeout_ms") or 5000)
    selector = action.get("selector") or ""
    diagnostics = diagnostics or _new_diagnostics()
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
            if _target_is_public(base_url) and _is_private_literal(page.url):
                raise RuntimeError("navigation reached a private address literal")
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
        elif kind == "assert_title":
            actual = page.title()
            if action.get("value", "") not in actual:
                raise AssertionError(
                    f"expected title to contain {action.get('value')!r}, got {actual!r}"
                )
            output = actual
        elif kind == "assert_url":
            actual = page.url
            expected = action.get("value", "")
            if expected not in actual:
                raise AssertionError(f"expected URL to contain {expected!r}, got {actual!r}")
            output = actual
        elif kind == "assert_value":
            actual = page.locator(selector).input_value(timeout=timeout)
            if actual != action.get("value"):
                raise AssertionError(
                    f"expected value {action.get('value')!r}, got {actual!r}"
                )
            output = actual
        elif kind == "assert_no_console_errors":
            rows = _diagnostic_summary(diagnostics, "console_errors")
            if rows:
                detail = "; ".join(row.get("text", "")[:180] for row in rows[:3])
                raise AssertionError(f"browser console errors: {detail}")
            output = "no console errors"
        elif kind == "assert_no_failed_requests":
            rows = _diagnostic_summary(diagnostics, "failed_requests") + _diagnostic_summary(
                diagnostics, "http_errors"
            )
            if rows:
                detail = "; ".join(
                    f"{row.get('url', '')[:120]} ({row.get('failure') or row.get('status')})"
                    for row in rows[:3]
                )
                raise AssertionError(f"failed same-origin requests: {detail}")
            output = "no failed same-origin requests"
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
        elif kind == "snapshot":
            output = json.dumps(page.evaluate(SNAPSHOT_JS), ensure_ascii=False)
        elif kind == "screenshot":
            directory = Path(shots_dir) if shots_dir else SHOTS_DIR
            directory.mkdir(parents=True, exist_ok=True)
            name = f"step-{int(action.get('step') or 0):02d}-{int(time.time() * 1000)}.jpg"
            page.screenshot(
                path=str(directory / name), full_page=True, type="jpeg", quality=55
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
        "output": (output or "")[:20000 if action.get("action_type") == "snapshot" else 4000],
        "error": error[:1200],
        "screenshot": screenshot,
        "status": status,
        "wall_ms": round((time.monotonic() - started) * 1000, 1),
    }


def _run_actions_with_diagnostics(
    actions: list[dict],
    base_url: str,
    cdp_port: int,
    *,
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    diagnostics = _new_diagnostics()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(7000)
        diagnostics = attach_diagnostics(page, base_url)
        for index, action in enumerate(actions):
            row = execute_action(
                page,
                context,
                action,
                base_url,
                index=index,
                diagnostics=diagnostics,
            )
            results.append(row)
            if stop_on_failure and not row["success"]:
                break
    finally:
        playwright.stop()
    return {"results": results, "diagnostics": diagnostics}


def run_actions(actions: list[dict], base_url: str, cdp_port: int) -> list[dict]:
    """Backward-compatible helper returning only result rows."""
    return _run_actions_with_diagnostics(actions, base_url, cdp_port)["results"]


def run_actions_with_diagnostics(
    actions: list[dict],
    base_url: str,
    cdp_port: int,
    *,
    stop_on_failure: bool = False,
) -> dict[str, Any]:
    return _run_actions_with_diagnostics(
        actions, base_url, cdp_port, stop_on_failure=stop_on_failure
    )


if __name__ == "__main__":
    actions_argument = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE_URL
    port = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_CDP_PORT
    print(
        json.dumps(
            run_actions_with_diagnostics(json.loads(actions_argument), base_url, port)
        )
    )
