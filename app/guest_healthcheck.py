"""In-guest health probe for the deployed ShopDemo fixture.

Runs inside the sandbox as ``python3 /workspace/flowscout/guest_healthcheck.py``
and polls the local fixture until it answers or a deadline passes. Uses only
the standard library so it works on a bare guest.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8090/shop/api/health"
TIMEOUT_S = 90.0


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    deadline = time.monotonic() + TIMEOUT_S
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as response:
                body = json.loads(response.read().decode("utf-8"))
                print(json.dumps({"status": body.get("status"), "url": url}))
                return 0
        except Exception as exc:  # noqa: BLE001 - report and retry until deadline
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2.0)
    print(f"healthcheck failed for {url}: {last_error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
