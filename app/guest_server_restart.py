"""Restart the in-guest ShopDemo server (after dependency changes)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def main() -> int:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "target_saas_app.py" in cmdline and "--serve" in cmdline:
            try:
                os.kill(int(entry), signal.SIGTERM)
            except OSError:
                pass
    time.sleep(1.5)
    log = open("/workspace/flowscout/server.log", "ab")
    subprocess.Popen(
        ["python3", "/workspace/flowscout/target_saas_app.py", "--serve"],
        stdout=log,
        stderr=log,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8090/shop/api/health", timeout=2) as r:
                print(json.dumps({"status": "ok", "body": json.loads(r.read().decode())}))
                return 0
        except Exception:
            time.sleep(1)
    print("restart failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
