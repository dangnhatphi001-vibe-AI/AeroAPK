#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export AERODROID_CORE="${AERODROID_CORE:-$ROOT_DIR/bin/shadow-droid-core}"
export AERODROID_ROOTFS="${AERODROID_ROOTFS:-$ROOT_DIR/rootfs-builder/aosp14_google_core}"
export NO_AT_BRIDGE=1
export QT_ACCESSIBILITY=0
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0
export QT_LOGGING_RULES="${QT_LOGGING_RULES:+$QT_LOGGING_RULES;}qt.accessibility.atspi=false;qt.accessibility.cache=false"

exec python3 -m aerodroid.run_apk "$@"
