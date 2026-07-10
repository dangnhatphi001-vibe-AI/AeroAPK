#!/usr/bin/env python3
"""Install and launch an APK through AeroDroid with bounded runtime steps."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from aerodroid.backend.apk import APKCompatibilityReport, APKError, APKManager, APKMetadata
from aerodroid.backend.container import LXCContainer, LXCError
from aerodroid.config import Defaults, Paths


def _wait_for_running(container: LXCContainer, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_state = "UNKNOWN"
    while time.monotonic() < deadline:
        status = container.status()
        last_state = status.get("state", "UNKNOWN")
        if last_state == "RUNNING":
            return
        time.sleep(0.5)
    raise LXCError(f"runtime did not reach RUNNING within {timeout:g}s; last state={last_state}")


def _install(manager: APKManager, apk_path: Path) -> APKMetadata:
    metadata = manager.extract_metadata(apk_path)
    return manager.install_apk(apk_path, metadata)


def _launch(container: LXCContainer, metadata: APKMetadata) -> None:
    result = container.launch_app(metadata.package_name, metadata.main_activity)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LXCError(detail or f"launch failed with exit code {result.returncode}")


def _profile_resources(profile: str, report: APKCompatibilityReport) -> tuple[int, int, int, int]:
    if profile == "lowmem":
        return (
            Defaults.LOW_MEMORY_LIMIT_MB,
            Defaults.LOW_PIDS_MAX,
            Defaults.LOW_CPU_QUOTA_MICROS,
            Defaults.CPU_PERIOD_MICROS,
        )
    if profile == "game":
        return (
            Defaults.GAME_MEMORY_LIMIT_MB,
            Defaults.PIDS_MAX,
            Defaults.GAME_CPU_QUOTA_MICROS,
            Defaults.CPU_PERIOD_MICROS,
        )
    if profile == "balanced":
        memory = max(Defaults.BALANCED_MEMORY_LIMIT_MB, report.min_memory_mb)
        return (
            min(memory, Defaults.GAME_MEMORY_LIMIT_MB),
            max(448, report.pids_max),
            Defaults.BALANCED_CPU_QUOTA_MICROS,
            Defaults.CPU_PERIOD_MICROS,
        )
    if report.execution_mode == "native-bridge-arm64":
        return (
            Defaults.GAME_MEMORY_LIMIT_MB,
            Defaults.PIDS_MAX,
            Defaults.GAME_CPU_QUOTA_MICROS,
            Defaults.CPU_PERIOD_MICROS,
        )
    if report.execution_mode.startswith("direct-"):
        return (
            Defaults.BALANCED_MEMORY_LIMIT_MB,
            max(448, report.pids_max),
            Defaults.BALANCED_CPU_QUOTA_MICROS,
            Defaults.CPU_PERIOD_MICROS,
        )
    return (
        Defaults.LOW_MEMORY_LIMIT_MB,
        Defaults.LOW_PIDS_MAX,
        Defaults.LOW_CPU_QUOTA_MICROS,
        Defaults.CPU_PERIOD_MICROS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Start AeroDroid, install an APK, and launch it")
    parser.add_argument("apk", type=Path, help="APK file to install and launch")
    parser.add_argument("--name", default=Defaults.CONTAINER_NAME, help="runtime container name")
    parser.add_argument("--profile", choices=("auto", "lowmem", "balanced", "game"), default="auto")
    parser.add_argument("--memory", type=int, default=None, help="runtime memory limit in MB")
    parser.add_argument("--pids-max", type=int, default=None, help="runtime task limit")
    parser.add_argument("--cpu-quota", type=int, default=None, help="cgroup CPU quota in microseconds")
    parser.add_argument("--cpu-period", type=int, default=None, help="cgroup CPU period in microseconds")
    parser.add_argument("--start-timeout", type=float, default=35.0, help="seconds to wait for runtime RUNNING state")
    parser.add_argument("--doctor", action="store_true", help="analyze only; do not start runtime")
    parser.add_argument("--no-launch", action="store_true", help="install only")
    parser.add_argument("--stop-after", action="store_true", help="stop runtime after install/launch")
    parser.add_argument("--debug", action="store_true", help="print debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    apk_path = args.apk.expanduser().resolve()
    if not apk_path.exists():
        print(f"APK not found: {apk_path}", file=sys.stderr)
        return 2
    if apk_path.is_dir():
        print(f"APK path is a directory: {apk_path}", file=sys.stderr)
        return 2

    preflight = APKManager(LXCContainer(name=args.name))
    report = preflight.inspect_compatibility(apk_path)
    print("\n".join(report.summary_lines()))
    if args.doctor:
        return 0 if report.supported else 1
    if not report.supported:
        return 1

    auto_memory, auto_pids_max, auto_cpu_quota, auto_cpu_period = _profile_resources(args.profile, report)
    memory_mb = args.memory if args.memory is not None else auto_memory
    pids_max = args.pids_max if args.pids_max is not None else auto_pids_max
    cpu_quota = args.cpu_quota if args.cpu_quota is not None else auto_cpu_quota
    cpu_period = args.cpu_period if args.cpu_period is not None else auto_cpu_period

    container = LXCContainer(
        name=args.name,
        rootfs=Paths.ROOTFS_DIR,
        memory_mb=memory_mb,
        cpu_shares=Defaults.CPU_SHARES,
        cpu_quota_micros=cpu_quota,
        cpu_period_micros=cpu_period,
        pids_max=pids_max,
    )
    manager = APKManager(container)

    started_here = False
    try:
        if not container.is_running():
            cpu_cores = cpu_quota / cpu_period if cpu_period else 0
            print(f"Starting Android runtime ({memory_mb}M, pids={pids_max}, cpu={cpu_cores:.2f} cores)...")
            container.start(wait=True)
            started_here = True
            _wait_for_running(container, args.start_timeout)
        else:
            status = container.status()
            print(f"Using running Android runtime pid={status.get('pid', '-')}")

        print(f"Installing {apk_path.name}...")
        metadata = _install(manager, apk_path)
        print(f"Installed {metadata.package_name} {metadata.version_name}")

        if not args.no_launch:
            print(f"Launching {metadata.package_name}...")
            _launch(container, metadata)
            print("Launched")
    except (APKError, LXCError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.stop_after and (started_here or container.is_running()):
            try:
                print("Stopping Android runtime...")
                container.stop()
            except LXCError as exc:
                print(f"WARNING: stop failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
