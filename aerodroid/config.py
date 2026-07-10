"""Central configuration and path constants for AeroDroid."""

import os
from pathlib import Path


class Paths:
    """Filesystem paths used by the runtime."""

    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    HOME = Path.home()
    XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share"))
    XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))

    CORE_BINARY = Path(os.environ.get("AERODROID_CORE", PROJECT_ROOT / "bin" / "shadow-droid-core"))
    LOCAL_ROOTFS_DIR = PROJECT_ROOT / "rootfs-builder" / "aosp14_google_core"

    # System-level container storage (requires root)
    SYSTEM_LIB = Path("/var/lib/aerodroid")
    CONTAINER_DIR = SYSTEM_LIB / "containers" / "aerodroid"
    ROOTFS_DIR = Path(os.environ["AERODROID_ROOTFS"]) if os.environ.get("AERODROID_ROOTFS") else (
        LOCAL_ROOTFS_DIR if LOCAL_ROOTFS_DIR.exists() else SYSTEM_LIB / "rootfs"
    )

    # User-level app metadata and launchers
    AERODROID_DATA = XDG_DATA_HOME / "aerodroid"
    APPS_DIR = AERODROID_DATA / "apps"
    DESKTOP_DIR = XDG_DATA_HOME / "applications"
    ICONS_DIR = AERODROID_DATA / "icons"
    CONFIG_DIR = XDG_CONFIG_HOME / "aerodroid"
    CORE_STATE_DIR = AERODROID_DATA / "state"

    @classmethod
    def ensure_user_dirs(cls) -> None:
        for d in (cls.APPS_DIR, cls.DESKTOP_DIR, cls.ICONS_DIR, cls.CONFIG_DIR, cls.CORE_STATE_DIR):
            d.mkdir(parents=True, exist_ok=True)


class Defaults:
    """Default runtime settings."""

    CONTAINER_NAME = "default-android"
    HOSTNAME = "aerodroid"
    LOW_MEMORY_LIMIT_MB = 2048
    BALANCED_MEMORY_LIMIT_MB = 2560
    MEMORY_LIMIT_MB = 3072
    GAME_MEMORY_LIMIT_MB = 3072
    CPU_SHARES = 1024
    CPU_PERIOD_MICROS = 100_000
    LOW_CPU_QUOTA_MICROS = 100_000
    BALANCED_CPU_QUOTA_MICROS = 200_000
    CPU_QUOTA_MICROS = BALANCED_CPU_QUOTA_MICROS
    GAME_CPU_QUOTA_MICROS = 300_000
    LOW_PIDS_MAX = 384
    PIDS_MAX = 512
    WAYLAND_SOCKET = "wayland-0"
    ADB_HOST_PORT = 5555
