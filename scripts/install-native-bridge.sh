#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ROOTFS="$ROOT_DIR/rootfs-builder/aosp14_google_core"
DEFAULT_SYSTEM_IMG="$ROOT_DIR/rootfs-builder/sys-img/x86_64/system.img"
DEFAULT_EXTRACT_DIR="$ROOT_DIR/rootfs-builder/sys-img/x86_64/extracted"

ROOTFS="${1:-$DEFAULT_ROOTFS}"
SYSTEM_IMG="${2:-$DEFAULT_SYSTEM_IMG}"
EXTRACT_DIR="${3:-$DEFAULT_EXTRACT_DIR}"
SYSTEM_EXT4="$EXTRACT_DIR/system_ext4.img"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

need_cmd python3
need_cmd debugfs
need_cmd cp
need_cmd find
need_cmd install
need_cmd curl

if [[ ! -d "$ROOTFS/system" ]]; then
  echo "rootfs not found or invalid: $ROOTFS" >&2
  exit 1
fi

if [[ ! -f "$SYSTEM_EXT4" ]]; then
  if [[ ! -f "$SYSTEM_IMG" ]]; then
    echo "system image not found: $SYSTEM_IMG" >&2
    exit 1
  fi
  python3 "$ROOT_DIR/rootfs-builder/extract_system.py" "$SYSTEM_IMG" "$EXTRACT_DIR"
fi

if [[ ! -f "$SYSTEM_EXT4" ]]; then
  echo "system ext4 image not found after extraction: $SYSTEM_EXT4" >&2
  exit 1
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/aerodroid-native-bridge.XXXXXX")"
cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

dump_file() {
  local src="$1"
  local dst="$2"
  mkdir -p "$(dirname "$dst")"
  debugfs -R "dump $src $dst" "$SYSTEM_EXT4" >/dev/null 2>&1
  if [[ ! -s "$dst" ]]; then
    echo "failed to extract $src from $SYSTEM_EXT4" >&2
    exit 1
  fi
}

dump_dir() {
  local src="$1"
  local dst_parent="$2"
  mkdir -p "$dst_parent"
  debugfs -R "rdump $src $dst_parent" "$SYSTEM_EXT4" >/dev/null 2>&1
  local name
  name="$(basename "$src")"
  if [[ ! -d "$dst_parent/$name" ]]; then
    echo "failed to extract directory $src from $SYSTEM_EXT4" >&2
    exit 1
  fi
}

SYSTEM_STAGE="$STAGE/system"
mkdir -p "$SYSTEM_STAGE/lib64" "$SYSTEM_STAGE/bin" "$SYSTEM_STAGE/etc/init" "$SYSTEM_STAGE/etc/binfmt_misc"

dump_dir "/system/lib64/arm64" "$SYSTEM_STAGE/lib64"
if [[ -d "$SYSTEM_STAGE/lib64/system/lib64/arm64" ]]; then
  mv "$SYSTEM_STAGE/lib64/system/lib64/arm64" "$SYSTEM_STAGE/lib64/arm64"
  rm -rf "$SYSTEM_STAGE/lib64/system"
fi
dump_dir "/system/bin/arm64" "$SYSTEM_STAGE/bin"
if [[ -d "$SYSTEM_STAGE/bin/system/bin/arm64" ]]; then
  mv "$SYSTEM_STAGE/bin/system/bin/arm64" "$SYSTEM_STAGE/bin/arm64"
  rm -rf "$SYSTEM_STAGE/bin/system"
fi

ndk_libs=(
  libndk_translation.so
  libndk_translation_exec_region.so
  libndk_translation_proxy_libEGL.so
  libndk_translation_proxy_libGLESv1_CM.so
  libndk_translation_proxy_libGLESv2.so
  libndk_translation_proxy_libGLESv3.so
  libndk_translation_proxy_libOpenMAXAL.so
  libndk_translation_proxy_libOpenSLES.so
  libndk_translation_proxy_libaaudio.so
  libndk_translation_proxy_libamidi.so
  libndk_translation_proxy_libandroid.so
  libndk_translation_proxy_libandroid_runtime.so
  libndk_translation_proxy_libbinder_ndk.so
  libndk_translation_proxy_libc.so
  libndk_translation_proxy_libcamera2ndk.so
  libndk_translation_proxy_libjnigraphics.so
  libndk_translation_proxy_libmediandk.so
  libndk_translation_proxy_libnativehelper.so
  libndk_translation_proxy_libnativewindow.so
  libndk_translation_proxy_libneuralnetworks.so
  libndk_translation_proxy_libvulkan.so
  libndk_translation_proxy_libwebviewchromium_plat_support.so
)

for lib in "${ndk_libs[@]}"; do
  dump_file "/system/lib64/$lib" "$SYSTEM_STAGE/lib64/$lib"
done

dump_file "/system/bin/ndk_translation_program_runner_binfmt_misc_arm64" "$SYSTEM_STAGE/bin/ndk_translation_program_runner_binfmt_misc_arm64"
dump_file "/system/etc/init/ndk_translation.rc" "$SYSTEM_STAGE/etc/init/ndk_translation.rc"
dump_file "/system/etc/binfmt_misc/arm64_exe" "$SYSTEM_STAGE/etc/binfmt_misc/arm64_exe"
dump_file "/system/etc/binfmt_misc/arm64_dyn" "$SYSTEM_STAGE/etc/binfmt_misc/arm64_dyn"
dump_file "/system/etc/ld.config.arm64.txt" "$SYSTEM_STAGE/etc/ld.config.arm64.txt"
dump_file "/system/etc/cpuinfo.arm64.txt" "$SYSTEM_STAGE/etc/cpuinfo.arm64.txt"

BACKUP_DIR="$ROOT_DIR/rootfs-builder/native-bridge-backups/$(date +%Y%m%d-%H%M%S)"
backup_rel() {
  local rel="$1"
  if [[ -e "$ROOTFS/$rel" || -L "$ROOTFS/$rel" ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
    cp -a "$ROOTFS/$rel" "$BACKUP_DIR/$rel"
  fi
}

backup_rel "system/build.prop"
backup_rel "vendor/build.prop"
backup_rel "vendor/odm/etc/build.prop"
backup_rel "system/lib64/arm64"
backup_rel "system/bin/arm64"
for lib in "${ndk_libs[@]}"; do
  backup_rel "system/lib64/$lib"
done
backup_rel "system/bin/ndk_translation_program_runner_binfmt_misc_arm64"
backup_rel "system/etc/init/ndk_translation.rc"
backup_rel "system/etc/binfmt_misc/arm64_exe"
backup_rel "system/etc/binfmt_misc/arm64_dyn"
backup_rel "system/etc/ld.config.arm64.txt"
backup_rel "system/etc/cpuinfo.arm64.txt"
backup_rel "system/etc/init/hw/init.zygote64.rc"
backup_rel "vendor/etc/init/android.hardware.graphics.composer@2.1-service.rc"

install -d -m 0755 "$ROOTFS/system/lib64" "$ROOTFS/system/bin" "$ROOTFS/system/etc/init" "$ROOTFS/system/etc/init/hw" "$ROOTFS/system/etc/binfmt_misc"

rm -rf "$ROOTFS/system/lib64/arm64" "$ROOTFS/system/bin/arm64"
cp -a "$SYSTEM_STAGE/lib64/arm64" "$ROOTFS/system/lib64/arm64"
cp -a "$SYSTEM_STAGE/bin/arm64" "$ROOTFS/system/bin/arm64"

for lib in "${ndk_libs[@]}"; do
  install -m 0644 "$SYSTEM_STAGE/lib64/$lib" "$ROOTFS/system/lib64/$lib"
done

install -m 0755 "$SYSTEM_STAGE/bin/ndk_translation_program_runner_binfmt_misc_arm64" "$ROOTFS/system/bin/ndk_translation_program_runner_binfmt_misc_arm64"
install -m 0644 "$SYSTEM_STAGE/etc/init/ndk_translation.rc" "$ROOTFS/system/etc/init/ndk_translation.rc"
install -m 0644 "$SYSTEM_STAGE/etc/binfmt_misc/arm64_exe" "$ROOTFS/system/etc/binfmt_misc/arm64_exe"
install -m 0644 "$SYSTEM_STAGE/etc/binfmt_misc/arm64_dyn" "$ROOTFS/system/etc/binfmt_misc/arm64_dyn"
install -m 0644 "$SYSTEM_STAGE/etc/ld.config.arm64.txt" "$ROOTFS/system/etc/ld.config.arm64.txt"
install -m 0644 "$SYSTEM_STAGE/etc/cpuinfo.arm64.txt" "$ROOTFS/system/etc/cpuinfo.arm64.txt"
cat >"$ROOTFS/system/etc/init/hw/init.zygote64.rc" <<'EOF'
service zygote /system/bin/app_process64 -Xzygote /system/bin --zygote --start-system-server --socket-name=zygote
    class main
    priority -20
    user root
    group root readproc reserved_disk
    socket zygote stream 660 root system
    socket usap_pool_primary stream 660 root system
    onrestart exec_background - system system -- /system/bin/vdc volume abort_fuse
    onrestart write /sys/power/state on
    onrestart restart audioserver
    onrestart restart cameraserver
    onrestart restart media
    onrestart restart media.tuner
    onrestart restart netd
    onrestart restart wificond
    task_profiles ProcessCapacityHigh MaxPerformance
    critical window=${zygote.critical_window.minute:-off} target=zygote-fatal
EOF

chown -R root:root "$ROOTFS/system/lib64/arm64"
find "$ROOTFS/system/lib64/arm64" -type d -exec chmod 0755 {} +
find "$ROOTFS/system/lib64/arm64" -type f -exec chmod 0644 {} +

chown -R root:2000 "$ROOTFS/system/bin/arm64"
find "$ROOTFS/system/bin/arm64" -type d -exec chmod 0751 {} +
find "$ROOTFS/system/bin/arm64" -type f -exec chmod 0755 {} +

chown root:root "$ROOTFS/system"/lib64/libndk_translation*.so
chmod 0644 "$ROOTFS/system"/lib64/libndk_translation*.so
chown root:2000 "$ROOTFS/system/bin/ndk_translation_program_runner_binfmt_misc_arm64"
chmod 0755 "$ROOTFS/system/bin/ndk_translation_program_runner_binfmt_misc_arm64"
chown root:root \
  "$ROOTFS/system/etc/init/ndk_translation.rc" \
  "$ROOTFS/system/etc/binfmt_misc/arm64_exe" \
  "$ROOTFS/system/etc/binfmt_misc/arm64_dyn" \
  "$ROOTFS/system/etc/ld.config.arm64.txt" \
  "$ROOTFS/system/etc/cpuinfo.arm64.txt" \
  "$ROOTFS/system/etc/init/hw/init.zygote64.rc"
chmod 0644 \
  "$ROOTFS/system/etc/init/ndk_translation.rc" \
  "$ROOTFS/system/etc/binfmt_misc/arm64_exe" \
  "$ROOTFS/system/etc/binfmt_misc/arm64_dyn" \
  "$ROOTFS/system/etc/ld.config.arm64.txt" \
  "$ROOTFS/system/etc/cpuinfo.arm64.txt" \
  "$ROOTFS/system/etc/init/hw/init.zygote64.rc"

python3 - "$ROOTFS/system/build.prop" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "ro.system.product.cpu.abilist": "x86_64,arm64-v8a",
    "ro.system.product.cpu.abilist32": "",
    "ro.system.product.cpu.abilist64": "x86_64,arm64-v8a",
    "ro.dalvik.vm.native.bridge": "libndk_translation.so",
    "ro.dalvik.vm.isa.arm64": "x86_64",
    "ro.enable.native.bridge.exec": "1",
    "ro.ndk_translation.version": "0.2.3",
    "ro.ndk_translation.flags": "accurate-sigsegv",
}
lines = path.read_text(errors="surrogateescape").splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY

python3 - "$ROOTFS/vendor/build.prop" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "ro.vendor.product.cpu.abilist": "x86_64,arm64-v8a",
    "ro.vendor.product.cpu.abilist32": "",
    "ro.vendor.product.cpu.abilist64": "x86_64,arm64-v8a",
    "ro.zygote": "zygote64",
}
lines = path.read_text(errors="surrogateescape").splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY

python3 - "$ROOTFS/vendor/odm/etc/build.prop" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "ro.odm.product.cpu.abilist": "x86_64,arm64-v8a",
    "ro.odm.product.cpu.abilist32": "",
    "ro.odm.product.cpu.abilist64": "x86_64,arm64-v8a",
}
lines = path.read_text(errors="surrogateescape").splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY

# Configure HWC service to run as root to allow creating virtual input devices (/dev/input/wl_*_events)
python3 - "$ROOTFS/vendor/etc/init/android.hardware.graphics.composer@2.1-service.rc" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    content = path.read_text(errors="surrogateescape")
    patched = content.replace("    user system", "    user root")
    path.write_text(patched)
PY

# Configure DNS properties in init.waydroid.rc to support network connectivity
python3 - "$ROOTFS/system/etc/init/init.waydroid.rc" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    content = path.read_text(errors="surrogateescape")
    if "setprop net.dns1" not in content:
        patched = content.replace("on boot", "on boot\n    setprop net.dns1 8.8.8.8\n    setprop net.dns2 1.1.1.1")
        path.write_text(patched)
PY

# Backup init.waydroid.rc if needed
backup_rel "system/etc/init/init.waydroid.rc"

chown root:root "$ROOTFS/system/build.prop" "$ROOTFS/vendor/build.prop" "$ROOTFS/vendor/odm/etc/build.prop"
chmod 0600 "$ROOTFS/system/build.prop" "$ROOTFS/vendor/build.prop" "$ROOTFS/vendor/odm/etc/build.prop"

# Download and pre-install FOSS Browser and F-Droid client
CACHE_DIR="$ROOT_DIR/rootfs-builder/cache"
mkdir -p "$CACHE_DIR"

if [[ ! -f "$CACHE_DIR/Browser.apk" ]]; then
  echo "Downloading FOSS Browser (de.baumann.browser)..."
  curl -L -o "$CACHE_DIR/Browser.apk" "https://f-droid.org/repo/de.baumann.browser_158.apk" || true
fi

if [[ ! -f "$CACHE_DIR/FDroid.apk" ]]; then
  echo "Downloading F-Droid client..."
  curl -L -o "$CACHE_DIR/FDroid.apk" "https://f-droid.org/FDroid.apk" || true
fi

if [[ -f "$CACHE_DIR/Browser.apk" ]]; then
  install -d -m 0755 "$ROOTFS/system/app/Browser"
  install -m 0644 "$CACHE_DIR/Browser.apk" "$ROOTFS/system/app/Browser/Browser.apk"
  chown -R root:root "$ROOTFS/system/app/Browser"
  chmod 0755 "$ROOTFS/system/app/Browser"
  chmod 0644 "$ROOTFS/system/app/Browser/Browser.apk"
fi

if [[ -f "$CACHE_DIR/FDroid.apk" ]]; then
  install -d -m 0755 "$ROOTFS/system/app/FDroid"
  install -m 0644 "$CACHE_DIR/FDroid.apk" "$ROOTFS/system/app/FDroid/FDroid.apk"
  chown -R root:root "$ROOTFS/system/app/FDroid"
  chmod 0755 "$ROOTFS/system/app/FDroid"
  chmod 0644 "$ROOTFS/system/app/FDroid/FDroid.apk"
fi

# Configure host user access to Android's internal storage
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"
if [[ -z "$REAL_USER" ]]; then
  REAL_USER="$(stat -c '%U' "$ROOT_DIR")"
fi

MEDIA_PARENT="$ROOTFS/data/media"
if [[ -d "$MEDIA_PARENT" ]]; then
  setfacl -m "u:$REAL_USER:rwx" "$MEDIA_PARENT" || true
fi

MEDIA_DIR="$ROOTFS/data/media/0"
if [[ -d "$MEDIA_DIR" ]]; then
  echo "Exposing Android storage $MEDIA_DIR to host user $REAL_USER..."
  
  # Ensure the directory is accessible and set default ACLs for future files
  setfacl -R -m "u:$REAL_USER:rwx" "$MEDIA_DIR" || true
  setfacl -R -d -m "u:$REAL_USER:rwx" "$MEDIA_DIR" || true
  
  USER_HOME="$(eval echo "~$REAL_USER")"
  if [[ -d "$USER_HOME" ]]; then
    LINK_PATH="$USER_HOME/AeroDroid-Storage"
    if [[ ! -e "$LINK_PATH" ]]; then
      echo "Creating home directory symlink: $LINK_PATH -> $MEDIA_DIR"
      ln -s "$MEDIA_DIR" "$LINK_PATH"
      chown -h "$REAL_USER:$REAL_USER" "$LINK_PATH" || true
    fi
  fi
fi

echo "Installed ARM64 native bridge into: $ROOTFS"
echo "Backup: $BACKUP_DIR"
