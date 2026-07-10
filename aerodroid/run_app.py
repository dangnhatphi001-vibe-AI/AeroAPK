#!/usr/bin/env python3
"""Launcher script invoked by .desktop files to run Android apps via AeroDroid."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from aerodroid.config import Defaults, Paths
from aerodroid.backend.container import LXCContainer, LXCError


def _notify(title: str, message: str) -> None:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return
    subprocess.run([notify_send, title, message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Android app in AeroDroid container")
    parser.add_argument("--package", required=True, help="Android package name")
    parser.add_argument("--activity", help="Specific activity to launch (default: main)")
    args = parser.parse_args()

    container = LXCContainer()
    if not container.is_running():
        _notify("AeroDroid", "Starting Android runtime")
        try:
            container.start(wait=True)
        except LXCError as exc:
            print(f"Launch failed: could not start Android runtime: {exc}", file=sys.stderr)
            _notify("AeroDroid", f"Runtime start failed: {exc}")
            return 1

    core = Path(os.environ.get("AERODROID_CORE", Paths.CORE_BINARY))
    cmd = [
        str(core),
        "launch",
        "--name",
        Defaults.CONTAINER_NAME,
        "--package",
        args.package,
    ]
    if args.activity:
        cmd.extend(["--activity", args.activity])

    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=95)
    except subprocess.TimeoutExpired:
        print("Launch failed: Android runtime did not respond within 95s.", file=sys.stderr)
        _notify("AeroDroid", "Launch timed out")
        return 1
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Launch failed").strip()
        print(detail, file=sys.stderr)
        _notify("AeroDroid", detail[:220])
        return result.returncode
    print((result.stdout or "App launched").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
