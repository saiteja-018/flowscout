"""Read-only view models for the public FlowScout Flight Deck site.

The public site never starts a sandbox and never needs cloud credentials. It
renders committed evidence from ``reports/`` and a recorded mock replay. The
operational dashboard stays at ``/ops``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"

STEP_TITLES = {
    1: "Create the isolated sandbox",
    2: "Verify sandbox readiness",
    3: "Open the shop",
    4: "Read the untrusted review",
    5: "Navigate to checkout",
    6: "Apply the first SAVE20",
    7: "Hit the controlled 503 and recover",
    8: "Apply the next coupon",
    9: "Confirm the negative total",
    10: "Generate tests and clean up",
}

STEP_SUMMARIES = {
    1: "A mock sandbox is created and its lifecycle is recorded.",
    2: "Readiness is checked before any browser work begins.",
    3: "ShopDemo loads and the page title is verified.",
    4: "Products are read. An injected instruction in a review is observed and ignored.",
    5: "Checkout opens with the initial cart total of $129.97.",
    6: "SAVE20 applies once: $129.97 becomes $103.98.",
    7: "Step 7 triggers a controlled HTTP 503. The failed sandbox is replaced and the step is retried on a fresh fixture.",
    8: "The next application takes the total from $77.98 to $51.99.",
    9: "Six applications are recorded. The total crosses zero and lands at -$25.99.",
    10: "Reproduction and regression specs are written. Cleanup is verified.",
}

STATUS_LABELS = {
    "done": "recorded",
    "recovered": "503 recovered",
    "succeeded": "succeeded",
    "deleted": "deleted",
}


def _load_json(relative: str) -> Any:
    try:
        return json.loads((REPORTS_DIR / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_project_json(relative: str | None) -> Any:
    if not relative:
        return None
    try:
        return json.loads((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_jsonl(relative: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with (REPORTS_DIR / relative).open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def report_href(path: str | None) -> str:
    """Map a repository-relative evidence path to its public URL."""
    if not path:
        return ""
    clean = path.replace("\\", "/").lstrip("/")
    if clean.startswith("reports/"):
        clean = clean[len("reports/") :]
    return "/reports/" + clean


def _action_target(action: dict[str, Any]) -> str:
    selector = str(action.get("selector") or "")
    value = str(action.get("value") or "")
    if selector and value:
        return f"{selector} = {value}"
    return selector or value or "—"


def _step_events(trace: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in trace:
        try:
            step = int(event.get("step"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault(step, []).append(event)
    return grouped


def _demo_screenshots(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    for action in actions:
        if action.get("action_type") != "screenshot":
            continue
        output = str(action.get("output") or "")
        if not output:
            continue
        shots.append(
            {
                "step": action.get("step"),
                "path": report_href("reports/evidence/" + output),
                "caption": str(action.get("description") or "Recorded screenshot"),
                "filename": output,
            }
        )
    if shots:
        return shots
    return [
        {
            "step": 4,
            "path": "/reports/evidence/step-04-1794055743070.png",
            "caption": "Shop and poisoned review",
            "filename": "step-04-1794055743070.png",
        },
        {
            "step": 9,
            "path": "/reports/evidence/step-09-1794055744698.png",
            "caption": "Checkout after six applications",
            "filename": "step-09-1794055744698.png",
        },
    ]


def build_demo() -> dict[str, Any]:
    workflow = _load_json("workflow.json") or {}
    primary = workflow.get("primary") or {}
    reliability = _load_json("reliability.json") or {}
    matrix = _load_json("evidence/test-matrix.json") or {}
    trace = _load_jsonl("failure_trace.jsonl")
    actions = primary.get("actions") or []

    grouped = _step_events(trace)
    steps: list[dict[str, Any]] = []
    for number in range(1, 11):
        events = grouped.get(number, [])
        if number == 7:
            status = "recovered"
        elif number == 10:
            status = "succeeded"
        else:
            status = "done"
        steps.append(
            {
                "number": number,
                "title": STEP_TITLES[number],
                "summary": STEP_SUMMARIES[number],
                "status": status,
                "status_label": STATUS_LABELS[status],
                "events": events,
            }
        )

    after_coupon = [
        action
        for action in actions
        if action.get("description") == "Read total after coupon" and action.get("output")
    ]
    coupon_totals = [
        {"value": str(action.get("output")), "step": action.get("step")}
        for action in after_coupon[-6:]
    ]

    cases = matrix.get("cases") or []
    met = sum(1 for case in cases if case.get("expectation_met"))
    defect = primary.get("defect") or {}

    return {
        "run": {
            "sandbox_name": primary.get("sandbox_name") or "not recorded",
            "execution_mode": primary.get("execution_mode") or "mock",
            "browser_runtime": primary.get("browser_runtime") or "host_playwright",
            "planner_mode": primary.get("planner_mode") or "scripted",
            "wall_clock_s": primary.get("wall_clock_s"),
            "agent_wall_clock_s": primary.get("agent_wall_clock_s"),
            "boot_to_ready_s": primary.get("boot_to_ready_s"),
            "workflow_s": primary.get("workflow_s"),
            "cleanup_s": primary.get("cleanup_s"),
            "cleanup_ok": primary.get("cleanup_ok"),
            "success": primary.get("success"),
            "classification": primary.get("classification"),
            "generated_at": workflow.get("generated_at"),
        },
        "steps": steps,
        "trace": trace,
        "actions": [
            {
                "step": action.get("step"),
                "action_type": action.get("action_type"),
                "description": action.get("description"),
                "selector": action.get("selector"),
                "value": action.get("value"),
                "output": action.get("output"),
                "success": action.get("success"),
                "wall_ms": action.get("wall_ms"),
                "target": _action_target(action),
            }
            for action in actions
        ],
        "screenshots": _demo_screenshots(actions),
        "coupon_totals": coupon_totals,
        "defect": defect,
        "result": {
            "success": primary.get("success"),
            "classification": primary.get("classification"),
            "wall_clock_s": primary.get("wall_clock_s"),
            "cleanup_ok": primary.get("cleanup_ok"),
            "test_paths": primary.get("test_paths") or [],
            "test_links": [
                {
                    "label": "reproduction spec",
                    "path": "reports/evidence/coupon_stacking_test.spec.ts",
                    "href": report_href("reports/evidence/coupon_stacking_test.spec.ts"),
                },
                {
                    "label": "regression spec",
                    "path": "reports/evidence/coupon_regression_test.spec.ts",
                    "href": report_href("reports/evidence/coupon_regression_test.spec.ts"),
                },
            ],
        },
        "matrix": {
            "cases": cases,
            "met": met,
            "total": len(cases),
            "all_met": matrix.get("all_expectations_met"),
        },
        "reliability": {
            "run_count": reliability.get("run_count"),
            "success_rate_pct": reliability.get("success_rate_pct"),
            "median_wall_clock_s": reliability.get("median_wall_clock_s"),
            "p90_wall_clock_s": reliability.get("p90_wall_clock_s"),
            "total_cost_usd": reliability.get("total_cost_usd"),
            "evaluation_mode": reliability.get("evaluation_mode"),
            "browser_runtime": reliability.get("browser_runtime"),
            "live_celesto_verified": reliability.get("live_celesto_verified"),
            "note": reliability.get("note"),
        },
        "counts": {
            "actions": len(actions),
            "trace": len(trace),
            "steps": 10,
            "screenshots": len(_demo_screenshots(actions)),
        },
    }


def _record_screenshots(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not record:
        return []
    evidence = record.get("evidence") or {}
    shots: list[dict[str, Any]] = []
    for shot in evidence.get("screenshots") or []:
        path = shot.get("archived_path")
        if not path:
            continue
        shots.append(
            {
                "path": report_href(path),
                "repo_path": path,
                "caption": shot.get("note") or "Recorded live screenshot",
                "width": shot.get("width"),
                "height": shot.get("height"),
                "sha256": shot.get("sha256"),
            }
        )
    return shots


def _pick_shot(shots: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    return next((shot for shot in shots if needle in shot.get("path", "")), None)


def _raw_links(current: dict[str, Any], recovery: dict[str, Any]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = [
        {"label": "Live evidence index", "path": "reports/live-evidence/index.json"},
    ]
    current_evidence = current.get("evidence") or {}
    recovery_evidence = recovery.get("evidence") or {}
    for label, record, key in (
        ("Current live action log", current_evidence, "actions_log"),
        ("Current live command audit", current_evidence, "command_log"),
        ("Recovery action log", recovery_evidence, "actions_log"),
        ("Recovery command audit", recovery_evidence, "command_log"),
    ):
        path = (record.get(key) or {}).get("path")
        if path:
            links.append({"label": label, "path": path})
    current_trace = (current.get("trace") or {}).get("path")
    recovery_trace = (recovery.get("trace") or {}).get("path")
    if current_trace:
        links.append({"label": "Current live lifecycle trace", "path": current_trace})
    if recovery_trace:
        links.append({"label": "Recovery lifecycle trace", "path": recovery_trace})
    links.extend(
        [
            {"label": "Generated reproduction spec", "path": "reports/evidence/coupon_stacking_test.spec.ts"},
            {"label": "Generated regression spec", "path": "reports/evidence/coupon_regression_test.spec.ts"},
            {"label": "Four-case test matrix", "path": "reports/evidence/test-matrix.json"},
            {"label": "Mock reliability report", "path": "reports/reliability.json"},
            {"label": "Recorded mock workflow", "path": "reports/workflow.json"},
        ]
    )
    for link in links:
        link["href"] = report_href(link["path"])
    return links


def build_live() -> dict[str, Any]:
    index = _load_json("live-evidence/index.json") or {}
    current_ref = index.get("current") or {}
    recovery_ref = index.get("recovery") or {}
    current = _load_project_json(current_ref.get("path")) or {}
    recovery = _load_project_json(recovery_ref.get("path")) or {}

    current_sandbox = current.get("sandbox") or {}
    current_browser = current.get("browser") or {}
    current_outcome = current.get("outcome") or {}
    current_evidence = current.get("evidence") or {}
    current_provenance = current.get("provenance") or {}
    current_screenshots = _record_screenshots(current)

    recovery_failure = recovery.get("failure") or {}
    failed_sandbox = recovery.get("failed_sandbox") or {}
    replacement_sandbox = recovery.get("replacement_sandbox") or {}
    recovery_retry = recovery.get("retry") or {}
    recovery_provenance = recovery.get("provenance") or {}
    recovery_screenshots = _record_screenshots(recovery)

    historical = [
        dict(item, href=report_href(item.get("path")))
        for item in (index.get("historical") or [])
    ]
    quarantined = [
        dict(item, href=report_href(item.get("path")))
        for item in (index.get("quarantined") or [])
    ]

    return {
        "index": index,
        "index_generated_at": index.get("generated_at"),
        "current_ref": current_ref,
        "current": current,
        "current_sandbox": current_sandbox,
        "current_browser": current_browser,
        "current_outcome": current_outcome,
        "current_defect": current_outcome.get("defect") or {},
        "current_evidence": current_evidence,
        "current_provenance": current_provenance,
        "current_limitations": current.get("limitations") or [],
        "current_screenshots": current_screenshots,
        "current_hero": _pick_shot(current_screenshots, "step-09"),
        "current_review": _pick_shot(current_screenshots, "step-04"),
        "recovery_ref": recovery_ref,
        "recovery": recovery,
        "recovery_failure": recovery_failure,
        "failed_sandbox": failed_sandbox,
        "replacement_sandbox": replacement_sandbox,
        "recovery_retry": recovery_retry,
        "recovery_events": recovery.get("events") or [],
        "recovery_provenance": recovery_provenance,
        "recovery_limitations": recovery.get("limitations") or [],
        "recovery_screenshots": recovery_screenshots,
        "historical": historical,
        "quarantined": quarantined,
        "raw_links": _raw_links(current, recovery),
    }


def build_landing() -> dict[str, Any]:
    demo = build_demo()
    live = build_live()
    return {
        "demo": demo,
        "live": live,
        "reliability": demo["reliability"],
        "matrix": demo["matrix"],
        "counts": demo["counts"],
    }


def replay_payload() -> dict[str, Any]:
    """JSON payload used by the replay controls and by evidence tooling."""
    demo = build_demo()
    return {
        "run": demo["run"],
        "steps": demo["steps"],
        "actions": demo["actions"],
        "screenshots": demo["screenshots"],
        "coupon_totals": demo["coupon_totals"],
        "defect": demo["defect"],
        "result": demo["result"],
        "matrix": demo["matrix"],
        "reliability": demo["reliability"],
        "counts": demo["counts"],
    }
