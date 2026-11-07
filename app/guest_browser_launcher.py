"""Launch the guest's bundled Chromium with a CDP debugging port.

Runs inside the sandbox as ``python3 /workspace/flowscout/guest_browser_launcher.py [port]``
(typically under nohup). Uses the browser-agent template's preinstalled
Playwright Chromium and binds the debugging server on 0.0.0.0:<port> so a
Celesto published port can proxy it (9222 is reserved by the platform, so the
default is 9333). If a previous instance was recorded, it is stopped first.
Chromium keeps running after this launcher exits.

By default Chromium runs headed on the sandbox desktop so a live run is
observable. Pass ``--headless`` (or set ``FLOWSCOUT_VISIBLE_BROWSER=0``) for
headless mode. If no X display is available, the launcher logs a warning and
falls back to headless so the workflow still runs.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9333
PROFILE_DIR = "/workspace/flowscout/chrome-profile"
LOG_PATH = Path("/workspace/flowscout/browser.log")
PID_PATH = Path("/workspace/flowscout/browser.pid")


def chromium_executable() -> str:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        return playwright.chromium.executable_path
    finally:
        playwright.stop()


def stop_previous() -> None:
    """Stop any chromium we launched earlier (pid file first, then /proc sweep)."""
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.25)
                except OSError:
                    break
        except (ValueError, OSError):
            pass
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "--remote-debugging-port=" in cmdline and "--user-data-dir=" in cmdline:
            try:
                os.kill(int(entry), signal.SIGTERM)
            except OSError:
                pass
    time.sleep(1.0)


def display_from_environment() -> str | None:
    """Return an X display usable by Chromium, if the sandbox has one."""
    display = os.getenv("DISPLAY", "").strip()
    if display:
        return display
    sockets = sorted(Path("/tmp/.X11-unix").glob("X*"))
    for socket in sockets:
        suffix = socket.name[1:]
        if suffix.isdigit():
            return f":{suffix}"
    return None


def browser_command(executable: str, port: int, *, visible: bool) -> list[str]:
    """Build the Chromium command line without launching it."""
    command = [executable]
    if visible:
        command.extend(
            [
                "--start-maximized",
                "--window-size=1280,900",
                "--window-position=0,0",
            ]
        )
    else:
        command.append("--headless")
    command.extend(
        [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=0.0.0.0",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_DIR}",
            "about:blank",
        ]
    )
    return command


def start(port: int, *, visible: bool) -> int:
    """Start Chromium and wait for its CDP endpoint; 0 means ready."""
    stop_previous()
    executable = chromium_executable()
    env = os.environ.copy()
    if visible:
        display = display_from_environment()
        if display:
            env["DISPLAY"] = display
        else:
            print(
                json.dumps(
                    {
                        "status": "warning",
                        "message": "no X display found; starting Chromium headless",
                    }
                )
            )
            visible = False
    command = browser_command(executable, port, visible=visible)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_PATH.open("ab")
    process = subprocess.Popen(command, stdout=log, stderr=log, env=env)
    PID_PATH.write_text(str(process.pid), encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                body = json.loads(response.read().decode("utf-8"))
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "pid": process.pid,
                            "browser": body.get("Browser", ""),
                            "cdp_port": port,
                            "ws": body.get("webSocketDebuggerUrl", ""),
                            "visible": visible,
                            "display": env.get("DISPLAY", ""),
                        }
                    )
                )
                return 0
        except Exception:
            if process.poll() is not None:
                print(f"chromium exited early with code {process.returncode}; see {LOG_PATH}")
                return 1
            time.sleep(0.5)
    print("chromium did not open the debugging port in time")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run Chromium without a visible window",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="run Chromium headed on the sandbox desktop (default)",
    )
    args = parser.parse_args()

    env_visible = os.getenv("FLOWSCOUT_VISIBLE_BROWSER", "").strip().lower()
    visible = env_visible not in {"0", "false", "no", "off"}
    if args.headless:
        visible = False
    if args.visible:
        visible = True

    if visible:
        code = start(args.port, visible=True)
        if code == 0:
            return 0
        print(
            json.dumps(
                {
                    "status": "warning",
                    "message": "headed Chromium failed to start; retrying headless",
                }
            )
        )
    return start(args.port, visible=False)


if __name__ == "__main__":
    sys.exit(main())
