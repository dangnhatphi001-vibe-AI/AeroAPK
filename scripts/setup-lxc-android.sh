#!/usr/bin/env bash
#
# setup-lxc-android.sh — Prepare LXC container for Android runtime.
#
# This script:
#   1. Creates the AeroDroid LXC container directory
#   2. Generates a minimal LXC config
#   3. Can prepare a rootfs from Waydroid or Android-x86 images
#
# Usage:
#   sudo ./scripts/setup-lxc-android.sh --rootfs /var/lib/aerodroid/rootfs
#
set -euo pipefail

ROOTFS_DEFAULT="/var/lib/aerodroid/rootfs"
CONTAINER_NAME="aerodroid"
LXC_BASE="/var/lib/aerodroid"
CONFIG_DIR="${LXC_BASE}/containers/${CONTAINER_NAME}"
LOG_FILE="${CONFIG_DIR}/container.log"

# ── Parsing ──────────────────────────────────────────────
ROOTFS="$ROOTFS_DEFAULT"
PREPARE_ROOTFS=0
WAYDROID_IMAGE=""

usage() {
    cat <<EOF
Usage: sudo $0 [OPTIONS]

Options:
  --rootfs PATH         AOSP root filesystem directory (default: $ROOTFS_DEFAULT)
  --prepare-waydroid    Download & extract Waydroid IMG to rootfs
  --waydroid-image PATH Use specific Waydroid image file
  -h, --help            Show this help

Description:
  Prepares LXC container for Mini-Android Container runtime.
  Requires: lxc, binderfs module, CAP_SYS_ADMIN.

EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rootfs) ROOTFS="$2"; shift 2 ;;
        --prepare-waydroid) PREPARE_ROOTFS=1; shift ;;
        --waydroid-image) WAYDROID_IMAGE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# ── Prerequisites ────────────────────────────────────────
check_deps() {
    local missing=()
    for cmd in lxc-start lxc-info modprobe; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "ERROR: missing required commands: ${missing[*]}"
        echo "Install: sudo apt install lxc lxcfs"
        exit 1
    fi
}

check_kernel() {
    # Ensure binderfs is available
    if [[ ! -d /dev/binderfs ]]; then
        if ! lsmod | grep -q '^binder_linux' 2>/dev/null; then
            echo "Loading binder_linux kernel module..."
            modprobe binder_linux || {
                echo "WARNING: binder_linux module not available."
                echo "Install: sudo apt install linux-modules-extra-$(uname -r)"
                echo "Or: sudo modprobe binder_linux"
            }
        fi
        echo "Creating /dev/binderfs mount..."
        mkdir -p /dev/binderfs
        mount -t binder binder /dev/binderfs || {
            echo "WARNING: binderfs mount failed. Container may lack binder devices."
        }
    fi
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: This script must be run as root (sudo)."
        exit 1
    fi
}

# ── LXC Config Generation ────────────────────────────────
generate_lxc_config() {
    local uid="${SUDO_UID:-$(id -u)}"
    local runtime_dir="/run/user/${uid}"
    local wayland_sock="${runtime_dir}/wayland-0"

    mkdir -p "${CONFIG_DIR}"

    cat > "${CONFIG_DIR}/config" <<CONF
# ── AeroDroid LXC Container Config ──────────────────────
lxc.uts.name = aerodroid
lxc.arch = x86_64

# Include standard privileged config
lxc.include = /usr/share/lxc/config/common.conf

# ── Root filesystem ──
lxc.rootfs.path = ${ROOTFS}
lxc.mount.auto = proc:mixed sys:mixed cgroup:mixed

# ── Graphics & input passthrough ──
lxc.mount.entry = /dev/dri dev/dri none bind,optional,create=dir 0 0
lxc.mount.entry = /dev/binderfs dev/binderfs none bind,optional,create=dir 0 0
lxc.mount.entry = /dev/ashmem dev/ashmem none bind,optional,create=file 0 0
lxc.mount.entry = ${runtime_dir} run/user/${uid} none bind,optional,create=dir 0 0

# Wayland socket
lxc.environment = WAYLAND_DISPLAY=wayland-0
lxc.environment = XDG_RUNTIME_DIR=${runtime_dir}
lxc.environment = container=aerodroid
lxc.environment = ANDROID_BINDER_DEVICES=/dev/binder,/dev/hwbinder,/dev/vndbinder

# ── Resource limits (cgroup v2) ──
lxc.cgroup2.memory.max = 1024M
lxc.cgroup2.cpu.weight = 100
lxc.cgroup2.pids.max = 512

# ── Init ──
lxc.init.cmd = /init

# ── Logging ──
lxc.log.file = ${LOG_FILE}
lxc.log.level = 1
CONF

    echo "✓ LXC config written to ${CONFIG_DIR}/config"
}

# ── RootFS Preparation ───────────────────────────────────
prepare_rootfs() {
    if [[ -d "${ROOTFS}" ]] && [[ -f "${ROOTFS}/init" ]]; then
        echo "✓ RootFS already exists at ${ROOTFS}"
        return
    fi

    mkdir -p "${ROOTFS}"

    if [[ -n "${WAYDROID_IMAGE}" ]] && [[ -f "${WAYDROID_IMAGE}" ]]; then
        echo "→ Extracting Waydroid image to ${ROOTFS}..."
        # Waydroid images are ext4 filesystem images
        mount -o loop,ro "${WAYDROID_IMAGE}" /mnt || {
            echo "WARNING: Could not mount Waydroid image. Mount it manually."
        }
        if mountpoint -q /mnt; then
            rsync -a /mnt/ "${ROOTFS}/" || cp -a /mnt/. "${ROOTFS}/"
            umount /mnt
        fi
    elif [[ ${PREPARE_ROOTFS} -eq 1 ]]; then
        echo "→ Downloading Waydroid system image..."
        # Placeholder — real implementation would download __from Waydroid__
        echo "  NOTE: Automatic Waydroid image download is not yet implemented."
        echo "  Please download manually from https://waydro.id or"
        echo "  use --waydroid-image to specify an existing IMG file."
    fi

    # Create essential mount points inside rootfs
    local dirs=(
        proc sys dev dev/pts dev/binderfs dev/dri dev/shm
        run tmp mnt mnt/sdcard data vendor oem odm
        system system/bin system/etc system/lib64
    )
    for d in "${dirs[@]}"; do
        mkdir -p "${ROOTFS}/${d}"
    done

    # Basic symlinks for Android compatibility
    if [[ ! -e "${ROOTFS}/bin" ]]; then
        ln -sf system/bin "${ROOTFS}/bin" 2>/dev/null || true
    fi

    echo "✓ RootFS directory structure prepared at ${ROOTFS}"
    echo "  NOTE: A complete AOSP rootfs is needed. Consider using:"
    echo "  - Waydroid image"
    echo "  - Android-x86 installation"
    echo "  - Custom AOSP build"
}

# ── Main ─────────────────────────────────────────────────
main() {
    check_root
    check_deps
    check_kernel
    mkdir -p "${LXC_BASE}/containers"
    prepare_rootfs
    generate_lxc_config

    echo ""
    echo "══════════════════════════════════════════════════════"
    echo "  AeroDroid LXC setup complete!"
    echo ""
    echo "  Container name: ${CONTAINER_NAME}"
    echo "  RootFS:         ${ROOTFS}"
    echo "  Config:         ${CONFIG_DIR}/config"
    echo ""
    echo "  Start container:"
    echo "    sudo lxc-start -n ${CONTAINER_NAME} -P ${LXC_BASE}"
    echo ""
    echo "  Or via AeroDroid CLI:"
    echo "    aerodroid-container start"
    echo "══════════════════════════════════════════════════════"
}

main "$@"