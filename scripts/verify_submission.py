#!/usr/bin/env python3
"""Verify the FlowScout report and evidence tree without regenerating it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.report_schemas import verify_submission


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify FlowScout reports and evidence")
    parser.add_argument(
        "--root",
        default=str(PROJECT_ROOT),
        help="project root containing reports/ and logs/ (default: repository root)",
    )
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    result = verify_submission(Path(args.root))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        status = "OK" if result.ok else "FAILED"
        print(f"FlowScout verification: {status}")
        print(f"checked: {len(result.checked)} files")
        for warning in result.warnings:
            print(f"WARNING: {warning}")
        for error in result.errors:
            print(f"ERROR: {error}")
        if result.ok:
            print("All report, evidence, hash, trace, and live-verification checks passed.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
