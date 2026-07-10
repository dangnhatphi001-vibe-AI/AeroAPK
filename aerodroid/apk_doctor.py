#!/usr/bin/env python3
"""Command-line APK compatibility checker for AeroDroid."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aerodroid.backend.apk import APKError, APKManager
from aerodroid.backend.container import LXCContainer


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze APK compatibility with the AeroDroid runtime")
    parser.add_argument("apk", type=Path, help="APK file to inspect")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    try:
        report = APKManager(LXCContainer()).inspect_compatibility(args.apk)
    except APKError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("\n".join(report.summary_lines()))
    return 0 if report.supported else 1


if __name__ == "__main__":
    sys.exit(main())
