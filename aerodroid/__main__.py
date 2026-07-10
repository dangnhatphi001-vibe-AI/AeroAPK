"""AeroDroid — Mini-Android Container desktop entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys

os.environ.setdefault("NO_AT_BRIDGE", "1")
os.environ.setdefault("QT_ACCESSIBILITY", "0")
os.environ.setdefault("QT_LINUX_ACCESSIBILITY_ALWAYS_ON", "0")


def _append_qt_logging_rules(*rules: str) -> None:
    current = os.environ.get("QT_LOGGING_RULES", "")
    existing = [rule for rule in current.split(";") if rule]
    for rule in rules:
        if rule not in existing:
            existing.append(rule)
    os.environ["QT_LOGGING_RULES"] = ";".join(existing)


_append_qt_logging_rules(
    "qt.accessibility.atspi=false",
    "qt.accessibility.cache=false",
)

from PyQt6.QtCore import QtMsgType, qInstallMessageHandler


_SUPPRESSED_QT_CATEGORIES = {
    "qt.accessibility.atspi",
    "qt.accessibility.cache",
}
_SUPPRESSED_QT_PREFIXES = (
    "qt.accessibility.atspi:",
    "QAccessibleTable::cellAt:",
)


def _qt_message_handler(mode: QtMsgType, context, message: str) -> None:
    category = getattr(context, "category", "") or ""
    if category in _SUPPRESSED_QT_CATEGORIES or message.startswith(_SUPPRESSED_QT_PREFIXES):
        return

    if mode == QtMsgType.QtDebugMsg and not os.environ.get("AERODROID_QT_DEBUG"):
        return

    prefix = {
        QtMsgType.QtDebugMsg: "DEBUG",
        QtMsgType.QtInfoMsg: "INFO",
        QtMsgType.QtWarningMsg: "WARNING",
        QtMsgType.QtCriticalMsg: "CRITICAL",
        QtMsgType.QtFatalMsg: "FATAL",
    }.get(mode, "QT")
    print(f"{prefix}: {message}", file=sys.stderr)


qInstallMessageHandler(_qt_message_handler)

from PyQt6.QtWidgets import QApplication

from aerodroid.ui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser(description="AeroDroid Mini-Android Container")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("apk", nargs="?", help="APK file to install (optional)")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    app = QApplication(sys.argv)
    app.setApplicationName("AeroDroid")
    app.setOrganizationName("AeroDroid")

    window = MainWindow()
    window.show()

    # If an APK path is given on command line, install it directly
    if args.apk:
        window._install_apk(args.apk)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
