"""Kernel and container performance tuning for AeroDroid."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class Profile(Enum):
    """Predefined performance profiles."""

    LOW_END = "low-end"
    BALANCED = "balanced"
    PERFORMANCE = "performance"


@dataclass
class TuningParams:
    """Kernel parameters for a profile."""

    # VM / memory
    swappiness: int
    dirty_ratio: int
    dirty_background_ratio: int
    vfs_cache_pressure: int
    overcommit_memory: int
    oom_score_adj: int

    # CPU scheduler
    cpu_governor: str
    sched_latency_ns: int
    sched_min_granularity_ns: int
    sched_wakeup_granularity_ns: int

    # I/O
    io_scheduler: str  # mq-deadline, none, bfq
    read_ahead_kb: int

    # cgroup v2 (applied to container cgroup)
    memory_high_pct: int = 90  # memory.high as % of memory.max
    cpu_weight: int = 100      # cgroup2 cpu.weight (1-10000)
    io_weight: int = 100       # cgroup2 io.weight (1-10000)


PROFILES: Dict[Profile, TuningParams] = {
    Profile.LOW_END: TuningParams(
        swappiness=10,
        dirty_ratio=10,
        dirty_background_ratio=3,
        vfs_cache_pressure=100,
        overcommit_memory=1,
        oom_score_adj=-500,  # protect container
        cpu_governor="powersave",
        sched_latency_ns=6_000_000,
        sched_min_granularity_ns=750_000,
        sched_wakeup_granularity_ns=1_000_000,
        io_scheduler="mq-deadline",
        read_ahead_kb=128,
        memory_high_pct=85,
        cpu_weight=50,
        io_weight=50,
    ),
    Profile.BALANCED: TuningParams(
        swappiness=30,
        dirty_ratio=20,
        dirty_background_ratio=10,
        vfs_cache_pressure=50,
        overcommit_memory=1,
        oom_score_adj=-300,
        cpu_governor="schedutil",
        sched_latency_ns=12_000_000,
        sched_min_granularity_ns=1_500_000,
        sched_wakeup_granularity_ns=2_000_000,
        io_scheduler="none",  # noop for NVMe
        read_ahead_kb=256,
        memory_high_pct=90,
        cpu_weight=100,
        io_weight=100,
    ),
    Profile.PERFORMANCE: TuningParams(
        swappiness=60,
        dirty_ratio=40,
        dirty_background_ratio=20,
        vfs_cache_pressure=25,
        overcommit_memory=1,
        oom_score_adj=-100,
        cpu_governor="performance",
        sched_latency_ns=24_000_000,
        sched_min_granularity_ns=3_000_000,
        sched_wakeup_granularity_ns=4_000_000,
        io_scheduler="none",
        read_ahead_kb=512,
        memory_high_pct=95,
        cpu_weight=500,
        io_weight=500,
    ),
}


SYSCTL_PATHS = {
    "swappiness": "/proc/sys/vm/swappiness",
    "dirty_ratio": "/proc/sys/vm/dirty_ratio",
    "dirty_background_ratio": "/proc/sys/vm/dirty_background_ratio",
    "vfs_cache_pressure": "/proc/sys/vm/vfs_cache_pressure",
    "overcommit_memory": "/proc/sys/vm/overcommit_memory",
    "sched_latency_ns": "/proc/sys/kernel/sched_latency_ns",
    "sched_min_granularity_ns": "/proc/sys/kernel/sched_min_granularity_ns",
    "sched_wakeup_granularity_ns": "/proc/sys/kernel/sched_wakeup_granularity_ns",
}


class KernelTuner:
    """Apply and manage kernel tuning profiles."""

    def __init__(self, profile: Profile = Profile.BALANCED):
        self.profile = profile
        self.params = PROFILES[profile]
        self._backup: Dict[str, str] = {}

    def apply(self, container_cgroup: Optional[Path] = None) -> None:
        """Apply profile to kernel and optionally container cgroup."""
        if os.geteuid() != 0:
            logger.warning("Not running as root; kernel parameters may not be applied")
            return

        self._backup = {}
        self._apply_sysctl()
        self._apply_cpu_governor()
        self._apply_io_scheduler()

        if container_cgroup and container_cgroup.exists():
            self._apply_cgroup(container_cgroup)

        logger.info("Applied %s profile", self.profile.value)

    def _apply_sysctl(self) -> None:
        for name, path in SYSCTL_PATHS.items():
            value = getattr(self.params, name)
            self._write_sysctl(path, value)

    def _write_sysctl(self, path: str, value: int) -> None:
        try:
            if Path(path).exists():
                current = Path(path).read_text().strip()
                self._backup[path] = current
                Path(path).write_text(str(value))
                logger.debug("sysctl %s = %d (was %s)", path, value, current)
        except Exception as exc:
            logger.debug("Failed to write %s: %s", path, exc)

    def _apply_cpu_governor(self) -> None:
        governor = self.params.cpu_governor
        cpu_dir = Path("/sys/devices/system/cpu")
        for cpu_path in cpu_dir.glob("cpu*/cpufreq/scaling_governor"):
            try:
                current = cpu_path.read_text().strip()
                self._backup[str(cpu_path)] = current
                cpu_path.write_text(governor)
                logger.debug("Set governor %s for %s", governor, cpu_path)
            except Exception as exc:
                logger.debug("Failed to set governor on %s: %s", cpu_path, exc)

    def _apply_io_scheduler(self) -> None:
        scheduler = self.params.io_scheduler
        read_ahead = self.params.read_ahead_kb

        for block in Path("/sys/block").glob("*"):
            if not block.is_dir():
                continue
            scheduler_path = block / "queue" / "scheduler"
            if scheduler_path.exists():
                try:
                    current = scheduler_path.read_text().strip()
                    self._backup[str(scheduler_path)] = current
                    if f"[{scheduler}]" not in current:
                        scheduler_path.write_text(scheduler)
                        logger.debug("Set I/O scheduler %s for %s", scheduler, block.name)
                except Exception as exc:
                    logger.debug("Failed to set scheduler on %s: %s", block.name, exc)

            ra_path = block / "queue" / "read_ahead_kb"
            if ra_path.exists():
                try:
                    current = ra_path.read_text().strip()
                    self._backup[str(ra_path)] = current
                    ra_path.write_text(str(read_ahead))
                except Exception as exc:
                    logger.debug("Failed to set read_ahead on %s: %s", block.name, exc)

    def _apply_cgroup(self, cgroup_path: Path) -> None:
        """Apply cgroup v2 limits to container."""
        p = self.params

        # memory.high = memory.max * pct / 100
        try:
            mem_max = (cgroup_path / "memory.max").read_text().strip()
            if mem_max != "max":
                mem_high = int(int(mem_max) * p.memory_high_pct / 100)
                (cgroup_path / "memory.high").write_text(str(mem_high))
        except Exception:
            pass

        # cpu.weight
        try:
            (cgroup_path / "cpu.weight").write_text(str(p.cpu_weight))
        except Exception:
            pass

        # io.weight
        try:
            (cgroup_path / "io.weight").write_text(str(p.io_weight))
        except Exception:
            pass

        logger.debug("Applied cgroup limits to %s", cgroup_path)

    def restore(self) -> None:
        """Restore backed-up kernel parameters."""
        if os.geteuid() != 0:
            return

        for path, value in self._backup.items():
            try:
                Path(path).write_text(value)
            except Exception as exc:
                logger.debug("Failed to restore %s: %s", path, exc)
        self._backup.clear()
        logger.info("Restored previous kernel parameters")

    @staticmethod
    def get_current_profile() -> Dict[str, str]:
        """Read current kernel parameters."""
        result = {}
        for name, path in SYSCTL_PATHS.items():
            try:
                result[name] = Path(path).read_text().strip()
            except Exception:
                result[name] = "N/A"
        # CPU governor
        try:
            result["cpu_governor"] = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
        except Exception:
            result["cpu_governor"] = "N/A"
        return result


class SystemdOOMD:
    """Manage systemd-oomd for low-memory scenarios."""

    @staticmethod
    def disable_for_container(container_name: str) -> bool:
        """Prevent systemd-oomd from killing the container."""
        if not shutil.which("systemctl"):
            return False
        try:
            subprocess.run(
                ["systemctl", "set-property", f"lxc@{container_name}.service", "OOMScoreAdjust=-500"],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    @staticmethod
    def enable() -> None:
        subprocess.run(["systemctl", "enable", "--now", "systemd-oomd"], check=False)


def cli() -> int:
    parser = argparse.ArgumentParser(description="AeroDroid kernel tuner")
    parser.add_argument("action", choices=["apply", "restore", "show", "list"])
    parser.add_argument("--profile", choices=[p.value for p in Profile], default=Profile.BALANCED.value)
    parser.add_argument("--cgroup", type=Path, help="Container cgroup path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    profile = Profile(args.profile)
    tuner = KernelTuner(profile)

    if args.action == "apply":
        tuner.apply(args.cgroup)
    elif args.action == "restore":
        tuner.restore()
    elif args.action == "show":
        for k, v in KernelTuner.get_current_profile().items():
            print(f"{k}: {v}")
    elif args.action == "list":
        for p, params in PROFILES.items():
            print(f"\n{p.value}:")
            for field in params.__dataclass_fields__:
                print(f"  {field} = {getattr(params, field)}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(cli())
