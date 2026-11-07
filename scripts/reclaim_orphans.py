#!/usr/bin/env python3
"""Review and reclaim FlowScout-owned orphaned Celesto computers.

The default is a dry run: list only registry-recorded candidates. Deletion
requires both ``--apply`` and ``--yes``. Unregistered cloud computers are never
considered, and ``CELESTO_KEEP_COMPUTER_IDS`` is always protected.

Usage:
    python scripts/reclaim_orphans.py
    python scripts/reclaim_orphans.py --apply --yes --min-age-s 3600
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv is a core dependency
    pass

from app.ownership import OwnershipRegistry  # noqa: E402
from app.sandbox_runner import (  # noqa: E402
    CelestoSandboxAdapter,
    SandboxUnavailableError,
    create_adapter,
)


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "computer_id": record.get("computer_id"),
        "logical_name": record.get("logical_name"),
        "template_id": record.get("template_id"),
        "size_id": record.get("size_id"),
        "created_at": record.get("created_at"),
        "status": record.get("status"),
    }


def _print_human(result: dict[str, Any]) -> None:
    print(f"registry: {result['registry']}")
    print(f"mode: {result['mode']}")
    print(f"min_age_s: {result['min_age_s']}")
    if result["registry_error"]:
        print(f"registry_error: {result['registry_error']}")
    if result["cloud_error"]:
        print(f"cloud_error: {result['cloud_error']}")
    print(f"candidates: {len(result['candidates'])}")
    for record in result["candidates"]:
        print(
            "  {computer_id}  {logical_name}  template={template_id}  "
            "created={created_at}".format(**_record_summary(record))
        )
    if result["skipped"]:
        print(f"skipped: {len(result['skipped'])}")
        for item in result["skipped"]:
            print(f"  {item['computer_id']}  {item['reason']}")
    if result["mode"] == "apply":
        print(f"reclaimed: {len(result['reclaimed'])}")
        for computer_id in result["reclaimed"]:
            print(f"  {computer_id}")
    else:
        print("dry-run: no cloud resources were deleted")
    if result["registry_error"] or result["cloud_error"]:
        print("fail-closed: no candidates were selected", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List or delete only FlowScout-owned computers recorded in the local "
            "ownership registry. Dry-run is the default."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list candidates without deleting (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="delete eligible candidates; requires --yes",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm that --apply may delete the listed candidates",
    )
    parser.add_argument(
        "--min-age-s",
        type=float,
        default=None,
        help="minimum registry age before a computer is eligible (default: env or 3600)",
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        help="provider id to protect (repeatable, comma-separated)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="ownership registry path override",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    if args.apply and not args.yes:
        parser.error("--apply requires --yes")

    keep: list[str] = []
    for value in args.keep:
        keep.extend(part.strip() for part in value.split(",") if part.strip())

    registry = OwnershipRegistry(args.registry)
    try:
        adapter = create_adapter("live", ownership_registry=registry)
    except SandboxUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(adapter, CelestoSandboxAdapter):  # pragma: no cover - defensive
        print("error: live mode did not return the Celesto adapter", file=sys.stderr)
        return 2

    selected_mode = "apply" if args.apply else "dry-run"
    plan = adapter.plan_reclaim(min_age_s=args.min_age_s, keep=keep)
    result: dict[str, Any] = {
        "mode": selected_mode,
        "registry": str(registry.path),
        "min_age_s": plan["min_age_s"],
        "keep_ids": plan["keep_ids"],
        "candidates": plan["candidates"],
        "skipped": plan["skipped"],
        "registry_error": plan["registry_error"],
        "cloud_error": plan["cloud_error"],
        "reclaimed": [],
    }
    if args.apply and not plan["registry_error"] and not plan["cloud_error"]:
        result["reclaimed"] = adapter.apply_reclaim_plan(plan)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 1 if result["registry_error"] or result["cloud_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
