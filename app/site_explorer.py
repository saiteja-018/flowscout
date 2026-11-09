"""Generic public-website exploration and structured regression execution.

This module deliberately stays below the web layer: it takes a browser driver,
runs a bounded exploration, produces evidence and validated test cases, and can
rerun those test cases against a fresh browser.  The console service owns the
Celesto sandbox lifecycle.

Safety boundaries:

* every action is validated by :class:`app.qa_agent.BrowserAction`;
* the explorer only follows same-origin http(s) links;
* destructive-looking links, downloads, credentials forms, and payment paths
  are never selected;
* saved tests only use stable selectors (never session-only element refs);
* page content is treated as data, never as executable instructions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.evidence_io import atomic_write_bytes, atomic_write_json
from app.guest_browser_worker import attach_diagnostics, execute_action
from app.qa_agent import ActionType, BrowserAction, InvalidAction, GuestBrowserSession
from app.sandbox_runner import CelestoSandboxAdapter
from app.url_policy import is_same_origin, origin_of, validate_public_url

logger = logging.getLogger("flowscout.explorer")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_SCREENSHOTS = 8
MAX_ISSUES = 60
MAX_TEST_ACTIONS = 24

SHOPDEMO_PUBLIC_HOSTS = {"flowscout-shopdemo.onrender.com"}
SHOPDEMO_HEALTH_PATH = "/shop/api/health"
SHOPDEMO_TARGET_MARKER = "shopdemo"

ProgressCallback = Callable[[str, int, str, dict[str, Any] | None], None]


def _shopdemo_hosts() -> set[str]:
    """Public hosts that may prove they are the ShopDemo fixture.

    The default covers the deployed demo. A deployment can add its own
    ShopDemo host with ``FLOWSCOUT_SHOPDEMO_HOSTS``; the health check below
    still has to confirm the fixture before checkout/coupon probing is enabled.
    """
    hosts = set(SHOPDEMO_PUBLIC_HOSTS)
    raw = os.getenv("FLOWSCOUT_SHOPDEMO_HOSTS", "").strip()
    if raw:
        hosts.update(
            item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip()
        )
    return hosts


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _canonical(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def _text_key(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


UNSAFE_TEXT = re.compile(
    r"\b(?:delete|remove|destroy|drop|reset|clear|cancel|unsubscribe|log\s*out|sign\s*out|"
    r"buy|purchase|pay|checkout|add\s+to\s+cart|remove\s+from\s+cart|subscribe|register|"
    r"sign\s*up|create\s+account|send|submit|confirm|order|download|install|upgrade|"
    r"deactivate|close\s+account|withdraw|transfer)\b",
    re.IGNORECASE,
)
UNSAFE_HREF = re.compile(
    r"(?:logout|signout|sign-out|delete|remove|destroy|unsubscribe|checkout|payment|"
    r"pay|buy|purchase|order|cart|admin|wp-admin|reset|deactivate|withdraw|transfer)",
    re.IGNORECASE,
)
DOWNLOAD_EXT = re.compile(
    r"\.(?:pdf|zip|tar|gz|rar|7z|exe|dmg|msi|apk|iso|mp4|mp3|wav|csv|xlsx?|docx?|pptx?|"
    r"png|jpe?g|gif|webp|svg|ico)(?:$|\?)",
    re.IGNORECASE,
)
SAFE_BUTTON_TEXT = re.compile(
    r"^(?:search|go|next|previous|prev|view|open|details?|learn\s+more|read\s+more|more|"
    r"show|expand|collapse|menu|close|back|continue|see\s+more|browse|explore|products?|"
    r"docs?|documentation|about|features?|pricing|contact|support|blog|home)\b",
    re.IGNORECASE,
)
SEARCH_SUBMIT = re.compile(r"^(?:search|go|find)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- drivers


class BrowserDriver(Protocol):
    runtime: str
    target_url: str

    def start(self) -> None: ...
    def close(self) -> None: ...
    def execute_actions(
        self, actions: list[dict[str, Any]], *, stop_on_failure: bool = False
    ) -> dict[str, Any]: ...
    def reset_diagnostics(self) -> None: ...


class HostBrowserDriver:
    """Real Chromium on the host. Used only in explicitly selected mock mode."""

    runtime = "host_playwright"

    def __init__(self, target_url: str, evidence_dir: Path, *, headless: bool = True) -> None:
        self.target_url = target_url
        self.evidence_dir = Path(evidence_dir)
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-dev-shm-usage"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 FlowScoutQA/1.0"
            ),
        )
        self._context.set_default_timeout(7000)
        self._context.set_default_navigation_timeout(15000)
        self._page = self._context.new_page()
        self._diagnostics = attach_diagnostics(self._page, self.target_url)

    def reset_diagnostics(self) -> None:
        for rows in self._diagnostics.values():
            rows.clear()

    def execute_actions(
        self, actions: list[dict[str, Any]], *, stop_on_failure: bool = False
    ) -> dict[str, Any]:
        if self._page is None:
            raise RuntimeError("host browser has not been started")
        results: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            row = execute_action(
                self._page,
                self._context,
                action,
                self.target_url,
                index=index,
                diagnostics=self._diagnostics,
                shots_dir=self.evidence_dir,
            )
            if action.get("action_type") == "snapshot" and row.get("success"):
                try:
                    row["snapshot"] = json.loads(row["output"])
                    row["output"] = "page snapshot"
                except json.JSONDecodeError:
                    pass
            results.append(row)
            if stop_on_failure and not row.get("success"):
                break
        return {"results": results, "diagnostics": self._diagnostics}

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                if self._playwright is not None:
                    self._playwright.stop()


class GuestBrowserDriver:
    """Playwright inside the Celesto guest, driven through the validated worker."""

    runtime = "celesto_guest_playwright"

    def __init__(
        self,
        adapter: CelestoSandboxAdapter,
        sandbox_name: str,
        target_url: str,
        evidence_dir: Path,
        *,
        cdp_port: int = 9333,
        visible: bool = True,
    ) -> None:
        self.adapter = adapter
        self.sandbox_name = sandbox_name
        self.target_url = target_url
        self.evidence_dir = Path(evidence_dir)
        self.cdp_port = cdp_port
        self.visible = visible
        self._session: GuestBrowserSession | None = None

    def start(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("guest_browser_worker.py", "guest_browser_launcher.py"):
            source = (PROJECT_ROOT / "app" / filename).read_text(encoding="utf-8")
            write = self.adapter.write_text(
                self.sandbox_name, f"/workspace/flowscout/{filename}", source
            )
            if not write.ok:
                raise RuntimeError(
                    f"guest upload of {filename} failed: {write.stderr or write.stdout}"
                )
        self._session = GuestBrowserSession(
            self.adapter,
            self.sandbox_name,
            self.target_url,
            cdp_port=self.cdp_port,
            visible=self.visible,
        )
        self._session.start()

    def reset_diagnostics(self) -> None:
        # Each worker invocation starts a fresh diagnostics buffer.
        return

    def _pull_screenshot(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name):
            return ""
        try:
            data = self.adapter.read_workspace_base64(
                self.sandbox_name, f"/workspace/flowscout/shots/{name}"
            )
        except Exception as exc:
            logger.warning("Could not pull guest screenshot %s: %s", name, exc)
            return ""
        local_name = f"{Path(name).stem}-{uuid.uuid4().hex[:6]}.jpg"
        atomic_write_bytes(self.evidence_dir / local_name, data)
        return local_name

    def execute_actions(
        self, actions: list[dict[str, Any]], *, stop_on_failure: bool = False
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("guest browser has not been started")
        payload = json.dumps(actions, ensure_ascii=False)
        if len(payload) > 8000:
            # Keep the audited command below the policy limit. Tests are
            # intentionally small; if a caller sends more, split the batch.
            midpoint = max(1, len(actions) // 2)
            first = self.execute_actions(actions[:midpoint], stop_on_failure=stop_on_failure)
            if stop_on_failure and any(not row["success"] for row in first["results"]):
                return first
            second = self.execute_actions(actions[midpoint:], stop_on_failure=stop_on_failure)
            for index, row in enumerate(second["results"]):
                row["index"] = index + len(first["results"])
            merged = {
                "results": first["results"] + second["results"],
                "diagnostics": {
                    key: (first["diagnostics"].get(key, []) + second["diagnostics"].get(key, []))
                    for key in set(first["diagnostics"]) | set(second["diagnostics"])
                },
            }
            return merged
        command = (
            "python3 /workspace/flowscout/guest_browser_worker.py "
            f"{shlex.quote(payload)} {shlex.quote(self.target_url)} {self.cdp_port}"
        )
        result = self.adapter.run_command(
            self.sandbox_name,
            command,
            timeout=300,
            audit_command=(
                f"guest browser batch ({len(actions)} action(s), "
                f"first={actions[0].get('action_type') if actions else 'none'})"
            ),
        )
        if not result.ok:
            raise RuntimeError(
                f"guest browser worker failed: {(result.stderr or result.stdout)[-800:]}"
            )
        line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"guest browser worker returned invalid JSON: {exc}") from exc
        for index, row in enumerate(data.get("results", [])):
            row.setdefault("index", index)
            if row.get("screenshot"):
                row["screenshot"] = self._pull_screenshot(row["screenshot"])
            action = actions[index] if index < len(actions) else {}
            if action.get("action_type") == "snapshot" and row.get("success"):
                try:
                    row["snapshot"] = json.loads(row["output"])
                    row["output"] = "page snapshot"
                except json.JSONDecodeError:
                    pass
        return data

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


# --------------------------------------------------------------------- test schema


def _action_dict(action: BrowserAction) -> dict[str, Any]:
    return action.to_dict()


def _assertion(action_index: int, kind: str, description: str, **extra: Any) -> dict[str, Any]:
    return {
        "action_index": action_index,
        "kind": kind,
        "description": description,
        **extra,
    }


def validate_test_case(test: dict[str, Any], *, allow_private: bool = False) -> dict[str, Any]:
    """Validate a stored/generated test. Raises ``ValueError`` if unsafe."""
    if not isinstance(test, dict):
        raise ValueError("test case must be an object")
    name = _clean(test.get("name"), 160)
    target_url = str(test.get("target_url") or "")
    if not name:
        raise ValueError("test case has no name")
    if not target_url:
        raise ValueError("test case has no target_url")
    decision = validate_public_url(target_url, allow_private=allow_private)
    if not decision.allowed:
        raise ValueError(f"test target URL rejected: {decision.reason}")
    actions = test.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("test case has no actions")
    if len(actions) > MAX_TEST_ACTIONS:
        raise ValueError(f"test case exceeds {MAX_TEST_ACTIONS} actions")
    assertion_actions = 0
    validated_actions: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("each test action must be an object")
        action_type = str(action.get("action_type") or "")
        if action_type in {"snapshot", "screenshot", "fixture_post"}:
            raise ValueError(f"{action_type!r} is not allowed in a saved regression test")
        parsed = BrowserAction(
            action_type=ActionType(action_type),
            selector=str(action.get("selector") or ""),
            value=str(action.get("value") or ""),
            attribute=str(action.get("attribute") or ""),
            timeout_ms=int(action.get("timeout_ms") or 5000),
            description=_clean(action.get("description"), 200),
        )
        if "data-flowscout-ref" in parsed.selector:
            raise ValueError(
                "session-only data-flowscout-ref selectors are not allowed in saved tests"
            )
        if parsed.action_type == ActionType.NAVIGATE:
            nav_decision = validate_public_url(parsed.value, allow_private=allow_private)
            if not nav_decision.allowed:
                raise ValueError(f"navigate action rejected: {nav_decision.reason}")
        if parsed.action_type.value.startswith("assert_"):
            assertion_actions += 1
        validated_actions.append(parsed.to_dict())
    if assertion_actions == 0:
        raise ValueError("test case must contain at least one assertion action")
    return {
        "test_id": test.get("test_id") or _id("tc"),
        "name": name,
        "description": _clean(test.get("description"), 500),
        "target_url": decision.url,
        "actions": validated_actions,
        "expected_results": [
            _clean(item, 300) for item in (test.get("expected_results") or []) if str(item).strip()
        ][:12],
        "assertions": test.get("assertions") or [],
        "source": test.get("source") or "exploration",
        "tags": [str(item)[:40] for item in (test.get("tags") or [])][:8],
        "created_at": test.get("created_at") or utcnow(),
    }


def _page_assertion_actions(
    page_url: str, title: str, heading: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    actions = [
        _action_dict(BrowserAction(ActionType.NAVIGATE, value=page_url, description="Open the page")),
        _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=500, description="Let the page settle")),
    ]
    assertions: list[dict[str, Any]] = []
    expected: list[str] = ["The page loads without an HTTP error"]
    if title:
        actions.append(
            _action_dict(
                BrowserAction(
                    ActionType.ASSERT_TITLE,
                    value=title,
                    description=f"Title contains {title!r}",
                )
            )
        )
        assertions.append(_assertion(2, "title_contains", "Page title matches", value=title))
        expected.append(f'Title contains "{title}"')
    elif heading:
        heading_selector = re.sub(r"[\"'{}]", "", heading).strip()[:100]
        if not heading_selector:
            return [], [], []
        actions.append(
            _action_dict(
                BrowserAction(
                    ActionType.ASSERT_TEXT,
                    selector=f"text={heading_selector}",
                    value=heading,
                    description=f"Page contains {heading!r}",
                )
            )
        )
        assertions.append(
            _assertion(2, "text_contains", "Page text contains the heading", value=heading)
        )
        expected.append(f'Page contains "{heading}"')
    else:
        return [], [], []
    return actions, assertions, expected


def generate_tests(
    *,
    project_id: str | None,
    target_url: str,
    pages: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    allow_private: bool = False,
) -> list[dict[str, Any]]:
    """Turn an exploration into bounded, validated, reusable tests."""
    generated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(
        name: str,
        description: str,
        actions: list[dict[str, Any]],
        assertions: list[dict[str, Any]],
        expected: list[str],
        tags: list[str],
    ) -> None:
        key = (name.lower(), target_url)
        if key in seen:
            return
        seen.add(key)
        candidate = {
            "test_id": _id("tc"),
            "project_id": project_id,
            "name": name,
            "description": description,
            "target_url": target_url,
            "actions": actions,
            "expected_results": expected,
            "assertions": assertions,
            "source": "exploration",
            "tags": tags,
            "created_at": utcnow(),
        }
        try:
            generated.append(validate_test_case(candidate, allow_private=allow_private))
        except (ValueError, InvalidAction) as exc:
            logger.info("Skipped generated test %r: %s", name, exc)

    # One page-load test per distinct page, capped.
    for page in pages[:6]:
        page_url = str(page.get("url") or target_url)
        title = _clean(page.get("title"), 200)
        heading = ""
        for row in page.get("headings") or []:
            if row.get("text"):
                heading = _clean(row["text"], 180)
                break
        actions, assertions, expected = _page_assertion_actions(page_url, title, heading)
        if not actions:
            continue
        label = title or page_url
        add(
            f"Page loads: {label}"[:150],
            f"Generated from a real browser exploration of {page_url}.",
            actions,
            assertions,
            expected,
            ["smoke", "page-load"],
        )

    # Form interactions.
    for interaction in interactions:
        if len(generated) >= 12:
            break
        if interaction.get("kind") != "form":
            continue
        selector = interaction.get("selector") or ""
        value = interaction.get("value") or ""
        page_url = interaction.get("page_url") or target_url
        label = interaction.get("label") or "form field"
        if not selector or not value:
            continue
        actions = [
            _action_dict(BrowserAction(ActionType.NAVIGATE, value=page_url, description="Open the page")),
            _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=300, description="Let the page settle")),
            _action_dict(
                BrowserAction(
                    ActionType.FILL,
                    selector=selector,
                    value=value,
                    description=f"Fill {label}",
                )
            ),
            _action_dict(
                BrowserAction(
                    ActionType.ASSERT_VALUE,
                    selector=selector,
                    value=value,
                    description=f"{label} keeps the entered value",
                )
            ),
        ]
        assertions = [
            _assertion(3, "value_equals", f"{label} accepts input", value=value),
        ]
        expected = [f'The "{label}" field accepts text', f'The field value is "{value}"']
        add(
            f"Form accepts input: {label}"[:150],
            f"Fills the {label} field on {page_url} and verifies the value.",
            actions,
            assertions,
            expected,
            ["form"],
        )

    # Click interactions that produced a navigation.
    for interaction in interactions:
        if len(generated) >= 12:
            break
        if interaction.get("kind") != "click":
            continue
        selector = interaction.get("selector") or ""
        target = interaction.get("target_url") or ""
        page_url = interaction.get("page_url") or target_url
        label = interaction.get("label") or "control"
        title = _clean(interaction.get("target_title"), 200)
        if not selector or not target or not is_same_origin(target, origin_of(target_url)):
            continue
        actions = [
            _action_dict(BrowserAction(ActionType.NAVIGATE, value=page_url, description="Open the page")),
            _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=300, description="Let the page settle")),
            _action_dict(BrowserAction(ActionType.CLICK, selector=selector, description=f"Click {label}")),
            _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=900, description="Wait for navigation")),
        ]
        assertions: list[dict[str, Any]] = []
        expected: list[str] = [f'The "{label}" control responds']
        if title:
            actions.append(
                _action_dict(
                    BrowserAction(
                        ActionType.ASSERT_TITLE,
                        value=title,
                        description=f"Title contains {title!r}",
                    )
                )
            )
            assertions.append(_assertion(4, "title_contains", "Destination title matches", value=title))
            expected.append(f'Destination title contains "{title}"')
        else:
            actions.append(
                _action_dict(
                    BrowserAction(
                        ActionType.ASSERT_URL,
                        value=target,
                        description=f"URL contains {target}",
                    )
                )
            )
            assertions.append(_assertion(4, "url_contains", "Destination URL matches", value=target))
            expected.append(f'Destination URL contains "{target}"')
        add(
            f"Control opens page: {label}"[:150],
            f"Clicks the {label} control on {page_url} and verifies the destination.",
            actions,
            assertions,
            expected,
            ["navigation", "interaction"],
        )

    # Reproduction tests for observed browser errors. These are expected to
    # fail on a site that currently emits the issue; that is the point.
    console_pages = {}
    network_pages = {}
    for issue in issues:
        if issue.get("category") == "console_error" and issue.get("url"):
            console_pages.setdefault(issue["url"], issue)
        if issue.get("category") in {"network_failure", "http_error"} and issue.get("url"):
            network_pages.setdefault(issue["url"], issue)
    for page_url in list(console_pages)[:2]:
        actions = [
            _action_dict(BrowserAction(ActionType.NAVIGATE, value=page_url, description="Open the page")),
            _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=1200, description="Collect browser errors")),
            _action_dict(BrowserAction(ActionType.ASSERT_NO_CONSOLE_ERRORS, description="No console errors")),
        ]
        add(
            f"Browser console stays clean: {page_url}"[:150],
            f"Reproduces the console error observed on {page_url}.",
            actions,
            [_assertion(2, "no_console_errors", "No browser console errors")],
            ["The page emits no browser console errors"],
            ["issue", "console"],
        )
    for page_url in list(network_pages)[:2]:
        actions = [
            _action_dict(BrowserAction(ActionType.NAVIGATE, value=page_url, description="Open the page")),
            _action_dict(BrowserAction(ActionType.WAIT, timeout_ms=1200, description="Collect failed requests")),
            _action_dict(BrowserAction(ActionType.ASSERT_NO_FAILED_REQUESTS, description="No failed requests")),
        ]
        add(
            f"Network requests succeed: {page_url}"[:150],
            f"Reproduces the failed same-origin request observed on {page_url}.",
            actions,
            [_assertion(2, "no_failed_requests", "No failed same-origin requests")],
            ["The page makes no failed same-origin requests"],
            ["issue", "network"],
        )

    return generated[:12]


# ------------------------------------------------------------------------ explorer


class PublicSiteExplorer:
    """Bounded exploratory QA against a public website."""

    def __init__(
        self,
        *,
        driver: BrowserDriver,
        target_url: str,
        evidence_dir: Path,
        run_id: str = "",
        planner: Any = None,
        max_steps: int = 8,
        progress: ProgressCallback | None = None,
        allow_private: bool = False,
    ) -> None:
        self.driver = driver
        self.target_url = target_url
        self.evidence_dir = Path(evidence_dir)
        self.run_id = run_id
        self.planner = planner
        self.max_steps = max(3, min(int(max_steps), 12))
        self.progress = progress
        self.allow_private = allow_private
        self.issues: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.interactions: list[dict[str, Any]] = []
        self._issue_keys: set[tuple[str, str, str]] = set()
        self._seen_candidates: set[tuple[str, ...]] = set()
        self._visited: set[str] = set()
        self._page_keys: set[str] = set()
        self._shopdemo_verified: bool | None = None
        self.planner_mode = "scripted_heuristic"
        self._planner_failed = False

    # ---------------------------------------------------------------- utilities

    def _log(self, message: str, level: str = "info") -> None:
        self.logs.append({"ts": utcnow(), "level": level, "message": _clean(message, 500)})
        self.logs = self.logs[-200:]

    def _emit(
        self, phase: str, percent: int, message: str, partial: dict[str, Any] | None = None
    ) -> None:
        if self.progress is None:
            return
        payload = partial
        if payload is None:
            payload = {
                "actions": self.actions[-12:],
                "issues": self.issues,
                "screenshots": self.screenshots,
                "logs": self.logs[-30:],
            }
        try:
            self.progress(phase, max(0, min(100, percent)), message, payload)
        except Exception as exc:
            logger.warning("progress callback failed: %s", exc)

    def _add_issue(
        self,
        *,
        category: str,
        title: str,
        description: str,
        severity: str,
        url: str,
        step: int,
        evidence: list[str] | None = None,
    ) -> None:
        key = (category, _canonical(url), _clean(description, 120))
        if key in self._issue_keys:
            return
        self._issue_keys.add(key)
        if len(self.issues) >= MAX_ISSUES:
            return
        self.issues.append(
            {
                "issue_id": _id("iss"),
                "category": category,
                "title": _clean(title, 180),
                "description": _clean(description, 800),
                "severity": severity,
                "url": url,
                "step": step,
                "evidence": evidence or [],
                "detected_at": utcnow(),
            }
        )
        self._log(f"Issue detected: {title}", "warn")

    def _latest_screenshot(self) -> str:
        return self.screenshots[-1]["name"] if self.screenshots else ""

    def _observe_diagnostics(
        self, diagnostics: dict[str, list[dict[str, Any]]], page_url: str, step: int
    ) -> None:
        for row in (diagnostics.get("console_errors") or [])[:10]:
            self._add_issue(
                category="console_error",
                title="Browser console error",
                description=row.get("text") or "A browser console error was emitted.",
                severity="high" if row.get("same_origin", True) else "low",
                url=page_url,
                step=step,
                evidence=[self._latest_screenshot()] if self._latest_screenshot() else [],
            )
        for row in (diagnostics.get("page_errors") or [])[:5]:
            self._add_issue(
                category="page_error",
                title="Uncaught page error",
                description=row.get("error") or "An uncaught JavaScript error was thrown.",
                severity="high",
                url=page_url,
                step=step,
                evidence=[self._latest_screenshot()] if self._latest_screenshot() else [],
            )
        for row in (diagnostics.get("failed_requests") or [])[:10]:
            if not row.get("same_origin"):
                continue
            self._add_issue(
                category="network_failure",
                title="Same-origin request failed",
                description=f"{row.get('method', 'GET')} {row.get('url', '')} failed: {row.get('failure', 'unknown')}",
                severity="medium",
                url=page_url,
                step=step,
                evidence=[self._latest_screenshot()] if self._latest_screenshot() else [],
            )
        for row in (diagnostics.get("http_errors") or [])[:10]:
            if not row.get("same_origin"):
                continue
            self._add_issue(
                category="http_error",
                title=f"HTTP {row.get('status')} response",
                description=f"{row.get('method', 'GET')} {row.get('url', '')} returned HTTP {row.get('status')}.",
                severity="medium",
                url=page_url,
                step=step,
                evidence=[self._latest_screenshot()] if self._latest_screenshot() else [],
            )

    def _observe_page(self, snapshot: dict[str, Any], step: int) -> None:
        url = str(snapshot.get("url") or self.target_url)
        title = _clean(snapshot.get("title"), 300)
        headings = snapshot.get("headings") or []
        images = snapshot.get("images") or {}
        if not title:
            self._add_issue(
                category="missing_title",
                title="Page has no title",
                description="The document title is empty; search engines and assistive technology lose context.",
                severity="low",
                url=url,
                step=step,
            )
        if not _clean(snapshot.get("text_excerpt"), 1200):
            self._add_issue(
                category="blank_page",
                title="Page renders no visible text",
                description="The page body has no visible text after loading.",
                severity="high",
                url=url,
                step=step,
            )
        if not any(int(row.get("level") or 0) == 1 for row in headings):
            self._add_issue(
                category="missing_h1",
                title="Page has no H1 heading",
                description="No level-one heading was found in the rendered page.",
                severity="low",
                url=url,
                step=step,
            )
        if int(images.get("broken") or 0) > 0:
            self._add_issue(
                category="broken_images",
                title="Broken images",
                description=f"{images.get('broken')} image(s) failed to load.",
                severity="low",
                url=url,
                step=step,
            )
        if int(images.get("missing_alt") or 0) > 0:
            self._add_issue(
                category="missing_alt_text",
                title="Images without alt text",
                description=f"{images.get('missing_alt')} image(s) have no alt attribute.",
                severity="low",
                url=url,
                step=step,
            )
        if int(snapshot.get("duplicate_ids") or 0) > 0:
            self._add_issue(
                category="duplicate_ids",
                title="Duplicate element IDs",
                description=f"{snapshot.get('duplicate_ids')} duplicate HTML id(s) were found.",
                severity="low",
                url=url,
                step=step,
            )
        key = _canonical(url)
        if key not in self._page_keys:
            self._page_keys.add(key)
            self.pages.append(
                {
                    "url": url,
                    "title": title,
                    "headings": headings[:8],
                    "links_count": int(snapshot.get("links_count") or 0),
                    "buttons_count": int(snapshot.get("buttons_count") or 0),
                    "inputs_count": int(snapshot.get("inputs_count") or 0),
                    "forms_count": len(snapshot.get("forms") or []),
                    "images": images,
                    "text_excerpt": _clean(snapshot.get("text_excerpt"), 500),
                }
            )

    # --------------------------------------------------------------- candidates

    def _is_demo_checkout(self, href: str) -> bool:
        """Allow the ShopDemo checkout link on a verified ShopDemo target.

        The fixture has no real payments and its checkout page is the only
        place where the deterministic coupon behavior can be exercised. This
        exception is scoped to the bundled loopback fixture or a public host
        that proves it is ShopDemo through ``/shop/api/health``; it is never
        available for arbitrary public sites.
        """
        if not self._is_shopdemo_target():
            return False
        target = urlsplit(self.target_url)
        candidate = urlsplit(href)
        target_path = target.path or "/"
        if target_path != "/shop" and not target_path.startswith("/shop/"):
            return False
        return is_same_origin(href, origin_of(self.target_url)) and (
            candidate.path or "/"
        ).rstrip("/") == "/shop/checkout"

    def _safe_navigation(self, href: str, text: str) -> bool:
        if not href.startswith(("http://", "https://")):
            return False
        if not is_same_origin(href, origin_of(self.target_url)):
            return False
        demo_checkout = self._is_demo_checkout(href)
        if not demo_checkout:
            if UNSAFE_HREF.search(href) or DOWNLOAD_EXT.search(href):
                return False
            if UNSAFE_TEXT.search(text or ""):
                return False
        if _canonical(href) in self._visited:
            return False
        return True

    def _build_candidates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        page_url = str(snapshot.get("url") or self.target_url)
        # Navigation links first: direct navigation is stable, safe, and
        # produces a reusable page-load test.
        for link in (snapshot.get("links") or [])[:60]:
            href = str(link.get("href") or "")
            text = _clean(link.get("text") or link.get("aria_label"), 120)
            if not self._safe_navigation(href, text):
                continue
            signature = ("navigate", _canonical(href))
            if signature in self._seen_candidates:
                continue
            action = BrowserAction(
                ActionType.NAVIGATE,
                value=href,
                description=f"Open {text or href}",
            )
            candidates.append(
                {
                    "actions": [action],
                    "label": f"Open {text or href}"[:140],
                    "kind": "navigation",
                    "href": href,
                    "text": text,
                    "signature": signature,
                }
            )
        # Safe form fields.
        for field in (snapshot.get("inputs") or [])[:40]:
            field_type = str(field.get("type") or "text").lower()
            if str(field.get("tag") or "").lower() == "select":
                continue
            if field_type in {
                "password", "hidden", "file", "submit", "button", "reset", "image",
                "checkbox", "radio", "range", "color", "date", "datetime-local",
                "month", "week", "time",
            }:
                continue
            selector = str(field.get("stable_selector") or "")
            if not selector:
                continue
            value = _sample_value(field)
            if value is None:
                continue
            signature = ("fill", _canonical(page_url), selector)
            if signature in self._seen_candidates:
                continue
            label = (
                _clean(field.get("label"), 100)
                or _clean(field.get("placeholder"), 100)
                or _clean(field.get("name"), 100)
                or _clean(field.get("aria_label"), 100)
                or f"{field_type} field"
            )
            fill = BrowserAction(
                ActionType.FILL,
                selector=selector,
                value=value,
                description=f"Fill {label}",
            )
            candidates.append(
                {
                    "actions": [fill],
                    "label": f"Fill {label}"[:140],
                    "kind": "form",
                    "page_url": page_url,
                    "selector": selector,
                    "value": value,
                    "field": field,
                    "signature": signature,
                }
            )
        # Safe buttons that are not form submissions.
        for button in (snapshot.get("buttons") or [])[:40]:
            if button.get("disabled"):
                continue
            # Do not press controls inside forms during exploration: a
            # "submit" can create an account, send a message, or place an
            # order. Filling a field is enough to exercise the form safely.
            if button.get("in_form"):
                continue
            selector = str(button.get("stable_selector") or "")
            if not selector:
                continue
            text = _clean(button.get("text") or button.get("aria_label"), 100)
            if not text or UNSAFE_TEXT.search(text):
                continue
            if not SAFE_BUTTON_TEXT.search(text) and not button.get("aria_label"):
                continue
            signature = ("click", _canonical(page_url), selector)
            if signature in self._seen_candidates:
                continue
            action = BrowserAction(
                ActionType.CLICK,
                selector=selector,
                description=f"Click {text}",
            )
            candidates.append(
                {
                    "actions": [action],
                    "label": f"Click {text}"[:140],
                    "kind": "click",
                    "page_url": page_url,
                    "selector": selector,
                    "text": text,
                    "signature": signature,
                }
            )
        # Keep the prompt and the run bounded.
        priority = {"navigation": 0, "form": 1, "click": 2}
        candidates.sort(key=lambda row: priority.get(row["kind"], 9))
        return candidates[:12]

    def _observation(
        self, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> str:
        headings = "; ".join(
            _clean(row.get("text"), 80) for row in (snapshot.get("headings") or [])[:4]
        )
        links = "; ".join(
            f"{_clean(row.get('text'), 50)} -> {urlsplit(row.get('href') or '').path}"
            for row in (snapshot.get("links") or [])[:8]
        )
        forms = "; ".join(
            f"{row.get('method')} {urlsplit(row.get('action') or '').path} "
            f"({row.get('input_count')} fields)"
            for row in (snapshot.get("forms") or [])[:4]
        )
        recent = "; ".join(issue["title"] for issue in self.issues[-4:])
        candidate_text = "; ".join(row["label"] for row in candidates[:10])
        return (
            f"URL: {snapshot.get('url')}\nTitle: {snapshot.get('title')}\n"
            f"Headings: {headings}\nLinks: {links}\nForms: {forms}\n"
            f"Candidates: {candidate_text}\nRecent issues: {recent}"
        )[:1500]

    def _choose(
        self, step: int, snapshot: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        chosen: dict[str, Any] | None = None
        if self.planner is not None:
            try:
                action, reason = self.planner.choose(
                    phase=f"explore-step-{step}",
                    goal=(
                        "Choose one safe candidate that explores a new page or exercises a "
                        "form field. Prefer navigation links and search fields; never choose "
                        "destructive, payment, account, or download actions."
                    ),
                    observation=self._observation(snapshot, candidates),
                    candidates=[row["actions"][0] for row in candidates],
                )
                index = next(
                    (
                        idx
                        for idx, row in enumerate(candidates)
                        if row["actions"][0] is action
                    ),
                    None,
                )
                if index is None:
                    # The planner may have returned an equal-valued action.
                    index = next(
                        (
                            idx
                            for idx, row in enumerate(candidates)
                            if row["actions"][0].to_dict() == action.to_dict()
                        ),
                        0,
                    )
                chosen = candidates[index]
                self.planner_mode = "llm" if not self._planner_failed else "llm_with_fallback"
                self._log(
                    f"AI planner chose {chosen['label']} ({reason[:180]})", "info"
                )
            except Exception as exc:
                self._planner_failed = True
                self.planner_mode = "llm_with_fallback"
                self._log(f"Planner unavailable; using deterministic candidate order: {exc}", "warn")
        if chosen is None:
            chosen = candidates[0]
            if self.planner is None:
                self._log(f"Heuristic chose {chosen['label']}", "info")
        self._seen_candidates.add(tuple(chosen["signature"]))
        return chosen

    # ----------------------------------------------------------------- execution

    def _execute(
        self, actions: list[BrowserAction], *, step: int, stop_on_failure: bool = False
    ) -> dict[str, Any]:
        payload = [action.to_dict() for action in actions]
        try:
            return self.driver.execute_actions(payload, stop_on_failure=stop_on_failure)
        except Exception as exc:
            self._log(f"Browser batch failed at step {step}: {type(exc).__name__}: {exc}", "error")
            return {"results": [], "diagnostics": {}, "error": f"{type(exc).__name__}: {exc}"}

    def _record_batch(
        self,
        actions: list[BrowserAction],
        data: dict[str, Any],
        *,
        step: int,
        role: str,
    ) -> None:
        results = data.get("results") or []
        for index, action in enumerate(actions):
            row = results[index] if index < len(results) else {}
            screenshot = str(row.get("screenshot") or "")
            if screenshot:
                self.screenshots.append(
                    {
                        "name": screenshot,
                        "caption": f"Step {step}: {action.description or action.action_type.value}",
                        "step": step,
                        "kind": role,
                    }
                )
                self.screenshots = self.screenshots[-MAX_SCREENSHOTS:]
            output = str(row.get("output") or "")
            if action.action_type == ActionType.SNAPSHOT:
                output = "page snapshot"
            self.actions.append(
                {
                    "step": step,
                    "role": role,
                    "action_type": action.action_type.value,
                    "selector": action.selector,
                    "value": action.value,
                    "attribute": action.attribute,
                    "description": action.description,
                    "success": bool(row.get("success")),
                    "output": output[:2000],
                    "error": str(row.get("error") or "")[:800],
                    "screenshot": screenshot,
                    "wall_ms": float(row.get("wall_ms") or 0),
                    "ts": utcnow(),
                }
            )
            if not row.get("success"):
                self._log(
                    f"{action.action_type.value} failed at step {step}: {row.get('error')}",
                    "error",
                )
        self.actions = self.actions[-120:]

    def _snapshot_from(self, data: dict[str, Any]) -> dict[str, Any] | None:
        for row in reversed(data.get("results") or []):
            if isinstance(row.get("snapshot"), dict):
                return row["snapshot"]
        return None

    def _maybe_screenshot(
        self, actions: list[BrowserAction], *, step: int, caption: str
    ) -> None:
        if len(self.screenshots) >= MAX_SCREENSHOTS:
            return
        if any(action.action_type == ActionType.SCREENSHOT for action in actions):
            return
        actions.append(
            BrowserAction(
                ActionType.SCREENSHOT,
                description=f"Capture evidence: {caption}",
            )
        )

    def _record_interaction(
        self,
        chosen: dict[str, Any],
        results: list[dict[str, Any]],
        snapshot: dict[str, Any],
        page_url: str,
    ) -> None:
        if not results or not all(row.get("success") for row in results):
            return
        new_url = str(snapshot.get("url") or page_url)
        if chosen["kind"] == "form":
            self.interactions.append(
                {
                    "kind": "form",
                    "page_url": page_url,
                    "selector": chosen.get("selector"),
                    "value": chosen.get("value"),
                    "label": chosen.get("label", "").replace("Fill ", "", 1),
                    "submitted": False,
                    "target_url": new_url,
                }
            )
        elif chosen["kind"] == "click" and _canonical(new_url) != _canonical(page_url):
            self.interactions.append(
                {
                    "kind": "click",
                    "page_url": page_url,
                    "selector": chosen.get("selector"),
                    "label": chosen.get("text") or chosen.get("label"),
                    "target_url": new_url,
                    "target_title": _clean(snapshot.get("title"), 200),
                }
            )

    # --------------------------------------------------------------- demo probe

    def _is_shopdemo_target(self) -> bool:
        """Return True only for the bundled ShopDemo fixture, local or deployed.

        Loopback fixtures are trusted directly in explicit mock mode. A public
        target must be on the ShopDemo host allowlist *and* prove its identity
        through the fixture health endpoint. This keeps the checkout/coupon
        probe scoped to ShopDemo instead of enabling payment-path navigation on
        arbitrary public sites.
        """
        if self._shopdemo_verified is not None:
            return self._shopdemo_verified
        self._shopdemo_verified = False
        parsed = urlsplit(self.target_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or "/"
        if path != "/shop" and not path.startswith("/shop/"):
            return False
        if self.allow_private and host in {"127.0.0.1", "localhost", "::1"}:
            self._shopdemo_verified = True
            return True
        if parsed.scheme != "https" or host not in _shopdemo_hosts():
            return False
        health_url = origin_of(self.target_url).rstrip("/") + SHOPDEMO_HEALTH_PATH
        try:
            response = httpx.get(
                health_url,
                timeout=8.0,
                follow_redirects=False,
                headers={"User-Agent": "FlowScoutQA/1.0"},
            )
            payload = response.json() if response.status_code == 200 else {}
        except Exception as exc:
            self._log(
                f"ShopDemo verification failed for {health_url}: {type(exc).__name__}: {exc}",
                "warn",
            )
            return False
        if (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("target") == SHOPDEMO_TARGET_MARKER
        ):
            self._shopdemo_verified = True
            self._log(f"Verified ShopDemo target via {SHOPDEMO_HEALTH_PATH}", "info")
        else:
            self._log("ShopDemo health endpoint did not confirm the fixture", "warn")
        return self._shopdemo_verified

    def _demo_coupon_probe(self) -> dict[str, Any] | None:
        """Exercise the ShopDemo fixture's documented duplicate-coupon defect.

        Scoped to the bundled ShopDemo fixture, local or a verified public
        deployment. Every action still goes through the same validated action
        schema and browser driver as normal exploration. The generated
        regression test encodes the intended one-use coupon policy, so it fails
        while the fixture is in broken mode and passes after the fixture is
        switched to fixed mode.
        """
        if not self._is_shopdemo_target():
            return None
        checkout = origin_of(self.target_url).rstrip("/") + "/shop/checkout"
        step = self.max_steps + 1
        coupon_input = "[data-testid='coupon-input']"
        apply_button = "[data-testid='apply-coupon-btn']"
        actions = [
            BrowserAction(
                ActionType.NAVIGATE,
                value=checkout,
                description="Open the demo checkout",
            ),
            BrowserAction(ActionType.WAIT, timeout_ms=500, description="Let checkout settle"),
            BrowserAction(
                ActionType.FILL,
                selector=coupon_input,
                value="SAVE20",
                description="Enter SAVE20 once",
            ),
            BrowserAction(
                ActionType.CLICK,
                selector=apply_button,
                description="Submit the first coupon",
            ),
            BrowserAction(ActionType.WAIT, timeout_ms=700, description="Wait for the coupon result"),
            BrowserAction(
                ActionType.FILL,
                selector=coupon_input,
                value="SAVE20",
                description="Enter SAVE20 a second time",
            ),
            BrowserAction(
                ActionType.CLICK,
                selector=apply_button,
                description="Submit the duplicate coupon",
            ),
            BrowserAction(ActionType.WAIT, timeout_ms=700, description="Wait for the duplicate result"),
            BrowserAction(
                ActionType.READ_TEXT,
                selector="[data-testid='applied-coupons']",
                description="Read the applied coupon count",
            ),
            BrowserAction(
                ActionType.READ_TEXT,
                selector="[data-testid='cart-total']",
                description="Read the cart total",
            ),
            BrowserAction(
                ActionType.SCREENSHOT,
                description="Capture demo coupon evidence",
            ),
        ]
        self._log("Running ShopDemo duplicate-coupon probe", "info")
        self._emit("probing_demo", 86, "Verifying the ShopDemo coupon policy")
        data = self._execute(actions, step=step)
        self._record_batch(actions, data, step=step, role="probe")
        results = data.get("results") or []
        if len(results) < len(actions) or any(not row.get("success") for row in results):
            self._log("Demo coupon probe did not complete; skipping demo finding", "warn")
            return None
        applied = str(results[8].get("output") or "")
        total = str(results[9].get("output") or "")
        if "SAVE20 x2" not in applied:
            self._log(
                f"Demo coupon probe observed {applied!r}; duplicate coupon was not accepted",
                "info",
            )
            return None
        shot = str(results[10].get("screenshot") or self._latest_screenshot())
        self._add_issue(
            category="coupon_stacking",
            title="One-use coupon SAVE20 can be applied repeatedly",
            description=(
                f"Applying SAVE20 twice left the checkout showing {applied!r} with a total "
                f"of {total!r}. The intended policy rejects a second use of the same coupon."
            ),
            severity="high",
            url=checkout,
            step=step,
            evidence=[shot] if shot else [],
        )
        test = {
            "test_id": _id("tc"),
            "project_id": None,
            "name": "Coupon SAVE20 is rejected after first use",
            "description": (
                "Generated from the bundled ShopDemo defect probe: applies SAVE20 twice and "
                "expects the duplicate to be rejected while the cart keeps one coupon."
            ),
            "target_url": self.target_url,
            "actions": [
                _action_dict(
                    BrowserAction(
                        ActionType.NAVIGATE,
                        value=checkout,
                        description="Open the checkout page",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.WAIT, timeout_ms=500, description="Let the page settle"
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.FILL,
                        selector=coupon_input,
                        value="SAVE20",
                        description="Enter SAVE20",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.CLICK,
                        selector=apply_button,
                        description="Apply the coupon",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.WAIT, timeout_ms=700, description="Wait for the coupon result"
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.FILL,
                        selector=coupon_input,
                        value="SAVE20",
                        description="Enter SAVE20 again",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.CLICK,
                        selector=apply_button,
                        description="Apply the duplicate coupon",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.WAIT,
                        timeout_ms=700,
                        description="Wait for the duplicate result",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.ASSERT_TEXT,
                        selector="[data-testid='coupon-result']",
                        value="already been applied",
                        description="Duplicate coupon is rejected",
                    )
                ),
                _action_dict(
                    BrowserAction(
                        ActionType.ASSERT_TEXT,
                        selector="[data-testid='applied-coupons']",
                        value="SAVE20 x1",
                        description="Cart keeps a single coupon",
                    )
                ),
            ],
            "expected_results": [
                "The second SAVE20 application is rejected",
                "The checkout keeps a single applied coupon",
            ],
            "assertions": [
                _assertion(
                    8,
                    "text_contains",
                    "Duplicate coupon rejection message",
                    value="already been applied",
                ),
                _assertion(
                    9,
                    "text_contains",
                    "Applied coupon count stays at one",
                    value="SAVE20 x1",
                ),
            ],
            "source": "demo-probe",
            "tags": ["demo", "coupon", "regression"],
            "created_at": utcnow(),
        }
        try:
            return validate_test_case(test, allow_private=self.allow_private)
        except (ValueError, InvalidAction) as exc:
            self._log(f"Demo regression test rejected by schema: {exc}", "warn")
            return None

    # --------------------------------------------------------------------- run

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        status = "failed"
        error = ""
        try:
            self._log(f"Starting exploration of {self.target_url}", "info")
            self._emit("launching_browser", 30, "Launching Chromium")
            self.driver.start()
            self._log(f"Browser runtime: {self.driver.runtime}", "info")
            initial_actions = [
                BrowserAction(
                    ActionType.NAVIGATE,
                    value=self.target_url,
                    description="Open the target website",
                ),
                BrowserAction(ActionType.WAIT, timeout_ms=1200, description="Let the page settle"),
                BrowserAction(ActionType.SNAPSHOT, description="Read the page structure"),
            ]
            self._maybe_screenshot(initial_actions, step=1, caption="Homepage")
            self._emit("exploring", 38, "Opening the target website")
            data = self._execute(initial_actions, step=1)
            self._record_batch(initial_actions, data, step=1, role="setup")
            initial_results = data.get("results") or []
            navigation_ok = bool(initial_results) and bool(initial_results[0].get("success"))
            snapshot = self._snapshot_from(data)
            if not navigation_ok or snapshot is None:
                detail = (
                    (initial_results[0].get("error") if initial_results else "")
                    or data.get("error")
                    or "Navigation may have failed."
                )
                error = f"The target website could not be opened: {detail}"
                self._add_issue(
                    category="navigation_failed",
                    title="Target website could not be opened",
                    description=error,
                    severity="high",
                    url=self.target_url,
                    step=1,
                )
                self._emit("failed", 100, error)
                return self._result(status="failed", error=error, started=started)
            self._observe_page(snapshot, 1)
            self._observe_diagnostics(data.get("diagnostics") or {}, snapshot.get("url") or self.target_url, 1)
            self._visited.add(_canonical(snapshot.get("url") or self.target_url))
            self._emit(
                "exploring",
                42,
                f"Homepage captured: {_clean(snapshot.get('title'), 80) or 'untitled'}",
            )

            for step in range(2, self.max_steps + 1):
                candidates = self._build_candidates(snapshot)
                if not candidates:
                    self._log("No safe unexplored candidates remain", "info")
                    break
                chosen = self._choose(step, snapshot, candidates)
                if chosen is None:
                    break
                self._emit(
                    "exploring",
                    42 + int(42 * (step - 1) / self.max_steps),
                    f"Step {step}: {chosen['label']}",
                )
                actions = list(chosen["actions"]) + [
                    BrowserAction(ActionType.WAIT, timeout_ms=900, description="Let the page settle"),
                    BrowserAction(ActionType.SNAPSHOT, description="Read the page structure"),
                ]
                self._maybe_screenshot(actions, step=step, caption=chosen["label"])
                data = self._execute(actions, step=step)
                self._record_batch(actions, data, step=step, role="explore")
                main_results = (data.get("results") or [])[: len(chosen["actions"])]
                if not main_results or any(not row.get("success") for row in main_results):
                    failed_row = next(
                        (row for row in main_results if not row.get("success")), {}
                    )
                    self._add_issue(
                        category="interaction_failed",
                        title="Interactive element failed",
                        description=(
                            f"{chosen['label']} failed: "
                            f"{failed_row.get('error') or 'no result returned'}"
                        ),
                        severity="medium",
                        url=str(snapshot.get("url") or self.target_url),
                        step=step,
                    )
                    self._log(
                        f"Stopping exploration after failed interaction at step {step}",
                        "warn",
                    )
                    break
                snapshot = self._snapshot_from(data)
                if snapshot is None:
                    self._log(f"Step {step} produced no snapshot; stopping exploration", "warn")
                    break
                page_url = str(snapshot.get("url") or self.target_url)
                self._observe_page(snapshot, step)
                self._observe_diagnostics(data.get("diagnostics") or {}, page_url, step)
                self._visited.add(_canonical(page_url))
                self._record_interaction(chosen, main_results, snapshot, page_url)
                self._emit(
                    "exploring",
                    42 + int(42 * step / self.max_steps),
                    f"Step {step} complete: {chosen['label']}",
                )

            self._emit("generating_tests", 88, "Generating structured regression tests")
            demo_test = self._demo_coupon_probe()
            tests = generate_tests(
                project_id=None,
                target_url=self.target_url,
                pages=self.pages,
                interactions=self.interactions,
                issues=self.issues,
                allow_private=self.allow_private,
            )
            if demo_test is not None:
                tests.append(demo_test)
            self._log(f"Generated {len(tests)} validated regression test(s)", "info")
            self._emit(
                "tests_ready",
                94,
                f"{len(tests)} regression test(s) generated",
            )
            status = "succeeded"
            return self._result(status=status, error="", started=started, tests=tests)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"Exploration failed: {error}", "error")
            self._add_issue(
                category="environment_failure",
                title="Exploration failed",
                description=error,
                severity="high",
                url=self.target_url,
                step=0,
            )
            return self._result(status="failed", error=error, started=started)
        finally:
            try:
                self.driver.close()
            except Exception as exc:
                self._log(f"Browser close failed: {type(exc).__name__}: {exc}", "warn")

    def _result(
        self,
        *,
        status: str,
        error: str,
        started: float,
        tests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "error": error,
            "issues": self.issues,
            "actions": self.actions,
            "logs": self.logs,
            "screenshots": self.screenshots,
            "pages": self.pages,
            "tests": tests or [],
            "summary": {
                "pages_visited": len(self.pages),
                "actions": len(self.actions),
                "issues": len(self.issues),
                "tests_generated": len(tests or []),
                "max_steps": self.max_steps,
                "duration_s": round(time.monotonic() - started, 2),
            },
            "planner_mode": self.planner_mode,
            "browser_runtime": self.driver.runtime,
            "duration_s": round(time.monotonic() - started, 2),
        }


# ------------------------------------------------------------------- regression


class RegressionRunner:
    """Execute saved structured tests against a fresh browser."""

    def __init__(
        self,
        *,
        driver: BrowserDriver,
        tests: list[dict[str, Any]],
        evidence_dir: Path,
        previous_results: dict[str, dict[str, Any]] | None = None,
        progress: ProgressCallback | None = None,
        allow_private: bool = False,
    ) -> None:
        self.driver = driver
        self.tests = tests
        self.evidence_dir = Path(evidence_dir)
        self.previous_results = previous_results or {}
        self.progress = progress
        self.allow_private = allow_private
        self.results: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []

    def _log(self, message: str, level: str = "info") -> None:
        self.logs.append({"ts": utcnow(), "level": level, "message": _clean(message, 500)})
        self.logs = self.logs[-200:]

    def _emit(self, percent: int, message: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(
                "regression",
                max(0, min(100, percent)),
                message,
                {"test_results": self.results, "logs": self.logs[-30:]},
            )
        except Exception as exc:
            logger.warning("regression progress callback failed: %s", exc)

    def _run_test(self, test: dict[str, Any], index: int, total: int) -> dict[str, Any]:
        started = time.monotonic()
        test_id = str(test.get("test_id") or _id("tc"))
        name = str(test.get("name") or test_id)
        previous = self.previous_results.get(test_id) or {}
        previous_status = previous.get("status")
        try:
            validated = validate_test_case(test, allow_private=self.allow_private)
        except (ValueError, InvalidAction) as exc:
            return {
                "result_id": _id("tr"),
                "test_id": test_id,
                "test_name": name,
                "status": "failed",
                "duration_s": round(time.monotonic() - started, 2),
                "error": f"Invalid test definition: {exc}",
                "actions": [],
                "assertions": [],
                "screenshots": [],
                "logs": [],
                "previous_status": previous_status,
                "newly_failing": previous_status == "passed",
                "fixed": False,
                "created_at": utcnow(),
            }
        self._emit(
            int(10 + 80 * index / max(1, total)),
            f"Running {index + 1}/{total}: {name}",
        )
        try:
            if hasattr(self.driver, "reset_diagnostics"):
                self.driver.reset_diagnostics()
            data = self.driver.execute_actions(validated["actions"], stop_on_failure=True)
        except Exception as exc:
            data = {
                "results": [],
                "diagnostics": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        rows = data.get("results") or []
        failed_row = next((row for row in rows if not row.get("success")), None)
        error = ""
        screenshots: list[str] = []
        if failed_row is not None:
            error = str(failed_row.get("error") or "Action failed")
            shot = str(failed_row.get("screenshot") or "")
            if shot:
                screenshots.append(shot)
            else:
                try:
                    shot_data = self.driver.execute_actions(
                        [
                            {
                                "action_type": "screenshot",
                                "selector": "",
                                "value": "",
                                "attribute": "",
                                "timeout_ms": 5000,
                                "description": "Failure evidence",
                            }
                        ]
                    )
                    for row in shot_data.get("results") or []:
                        if row.get("screenshot"):
                            screenshots.append(str(row["screenshot"]))
                except Exception:
                    pass
        elif data.get("error"):
            error = str(data["error"])
        status = "passed" if not error and len(rows) == len(validated["actions"]) else "failed"
        if not rows:
            error = error or "The browser returned no action results"
            status = "failed"
        assertion_results: list[dict[str, Any]] = []
        for assertion in validated.get("assertions") or []:
            action_index = assertion.get("action_index")
            row = rows[action_index] if isinstance(action_index, int) and 0 <= action_index < len(rows) else None
            assertion_results.append(
                {
                    **assertion,
                    "status": "passed" if row and row.get("success") else "failed",
                    "actual": str((row or {}).get("output") or "")[:500],
                    "error": str((row or {}).get("error") or "")[:500],
                }
            )
        diagnostics = data.get("diagnostics") or {}
        logs = {
            "console_errors": (diagnostics.get("console_errors") or [])[:20],
            "page_errors": (diagnostics.get("page_errors") or [])[:10],
            "failed_requests": [
                row for row in (diagnostics.get("failed_requests") or []) if row.get("same_origin")
            ][:20],
            "http_errors": [
                row for row in (diagnostics.get("http_errors") or []) if row.get("same_origin")
            ][:20],
        }
        result = {
            "result_id": _id("tr"),
            "test_id": test_id,
            "test_name": name,
            "status": status,
            "duration_s": round(time.monotonic() - started, 2),
            "error": error[:1200],
            "actions": [
                {
                    "index": row.get("index", pos),
                    "action": validated["actions"][pos] if pos < len(validated["actions"]) else {},
                    "success": bool(row.get("success")),
                    "output": str(row.get("output") or "")[:1000],
                    "error": str(row.get("error") or "")[:500],
                    "screenshot": str(row.get("screenshot") or ""),
                    "wall_ms": float(row.get("wall_ms") or 0),
                }
                for pos, row in enumerate(rows)
            ],
            "assertions": assertion_results,
            "screenshots": screenshots,
            "logs": logs,
            "previous_status": previous_status,
            "newly_failing": status == "failed" and previous_status == "passed",
            "fixed": status == "passed" and previous_status == "failed",
            "created_at": utcnow(),
        }
        self._log(
            f"{status.upper()}: {name}" + (f" — {error[:180]}" if error else ""),
            "info" if status == "passed" else "warn",
        )
        return result

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        error = ""
        try:
            self._log(f"Starting regression run with {len(self.tests)} test(s)", "info")
            self._emit(5, "Launching Chromium")
            self.driver.start()
            for index, test in enumerate(self.tests):
                result = self._run_test(test, index, len(self.tests))
                self.results.append(result)
                self._emit(
                    int(10 + 85 * (index + 1) / max(1, len(self.tests))),
                    f"{result['status'].upper()}: {result['test_name']}",
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"Regression run failed: {error}", "error")
        finally:
            try:
                self.driver.close()
            except Exception as exc:
                self._log(f"Browser close failed: {type(exc).__name__}: {exc}", "warn")
        passed = len([row for row in self.results if row["status"] == "passed"])
        failed = len([row for row in self.results if row["status"] == "failed"])
        skipped = len(self.tests) - len(self.results)
        summary = {
            "total": len(self.tests),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "newly_failing": len([row for row in self.results if row["newly_failing"]]),
            "fixed": len([row for row in self.results if row["fixed"]]),
            "duration_s": round(time.monotonic() - started, 2),
        }
        return {
            "status": "succeeded" if not error else "failed",
            "error": error,
            "results": self.results,
            "logs": self.logs,
            "summary": summary,
            "duration_s": round(time.monotonic() - started, 2),
            "browser_runtime": self.driver.runtime,
        }


# --------------------------------------------------------------------- helpers


def _sample_value(field: dict[str, Any]) -> str | None:
    field_type = str(field.get("type") or "text").lower()
    haystack = " ".join(
        str(field.get(key) or "")
        for key in ("name", "placeholder", "aria_label", "label")
    ).lower()
    if field_type in {"email"} or "email" in haystack:
        return "qa@example.com"
    if field_type in {"url"} or "website" in haystack:
        return "https://example.com"
    if field_type in {"tel"} or "phone" in haystack:
        return "5550100"
    if field_type in {"number"}:
        return "1"
    if field_type in {"search", "text", "textarea"} or any(
        word in haystack for word in ("search", "query", "keyword", "name", "message")
    ):
        return "FlowScout QA"
    return None
