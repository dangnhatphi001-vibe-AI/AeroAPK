"""Compatibility launcher for the packaged AeroDroid UI."""

from __future__ import annotations

import sys

from aerodroid.__main__ import main


if __name__ == "__main__":
    sys.exit(main())
