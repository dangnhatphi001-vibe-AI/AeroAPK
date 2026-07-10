#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || echo amd64)}"
VERSION="$("$PYTHON_BIN" - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
)"
PACKAGE="aerodroid"
BUILD_ROOT="$ROOT_DIR/dist/deb"
PKG_ROOT="$BUILD_ROOT/${PACKAGE}_${VERSION}_${ARCH}"
OUT_DEB="$ROOT_DIR/dist/${PACKAGE}_${VERSION}_${ARCH}.deb"

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb not found" >&2
  exit 1
fi

if [[ "${SKIP_TEST:-0}" != "1" ]]; then
  "$ROOT_DIR/scripts/quick-test.sh"
fi

rm -rf "$PKG_ROOT"
mkdir -p \
  "$PKG_ROOT/DEBIAN" \
  "$PKG_ROOT/opt/aerodroid/bin" \
  "$PKG_ROOT/usr/bin" \
  "$PKG_ROOT/usr/share/applications" \
  "$PKG_ROOT/usr/share/mime/packages"

cp -a aerodroid "$PKG_ROOT/opt/aerodroid/"
# Compile Python code to bytecode and remove source files for obfuscation
python3 -m compileall -b "$PKG_ROOT/opt/aerodroid/aerodroid"
find "$PKG_ROOT/opt/aerodroid/aerodroid" -type f -name '*.py' -delete
find "$PKG_ROOT/opt/aerodroid/aerodroid" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 bin/shadow-droid-core "$PKG_ROOT/opt/aerodroid/bin/shadow-droid-core"
install -m 0755 bin/phishadowd "$PKG_ROOT/opt/aerodroid/bin/phishadowd"
install -m 0644 data/aerodroid.desktop "$PKG_ROOT/usr/share/applications/aerodroid.desktop"
install -m 0644 data/aerodroid-mime.xml "$PKG_ROOT/usr/share/mime/packages/aerodroid.xml"

if [[ "${AERODROID_INCLUDE_ROOTFS:-0}" == "1" ]]; then
  mkdir -p "$PKG_ROOT/opt/aerodroid/rootfs-builder"
  cp -a rootfs-builder/aosp14_google_core "$PKG_ROOT/opt/aerodroid/rootfs-builder/"
fi

write_wrapper() {
  local name="$1"
  local module="$2"
  local source_rootfs="$ROOT_DIR/rootfs-builder/aosp14_google_core"
  cat >"$PKG_ROOT/usr/bin/$name" <<EOF
#!/bin/sh
export PYTHONPATH=/opt/aerodroid\${PYTHONPATH:+:\$PYTHONPATH}
export AERODROID_CORE=/opt/aerodroid/bin/shadow-droid-core
export NO_AT_BRIDGE=1
export QT_ACCESSIBILITY=0
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=0
export QT_LOGGING_RULES="\${QT_LOGGING_RULES:+\$QT_LOGGING_RULES;}qt.accessibility.atspi=false;qt.accessibility.cache=false"
if [ -z "\${AERODROID_ROOTFS:-}" ] && [ -d /opt/aerodroid/rootfs-builder/aosp14_google_core ]; then
  export AERODROID_ROOTFS=/opt/aerodroid/rootfs-builder/aosp14_google_core
elif [ -z "\${AERODROID_ROOTFS:-}" ] && [ -d "$source_rootfs" ]; then
  export AERODROID_ROOTFS="$source_rootfs"
fi
exec python3 -m $module "\$@"
EOF
  chmod 0755 "$PKG_ROOT/usr/bin/$name"
}

write_wrapper aerodroid aerodroid
write_wrapper aerodroid-run aerodroid.run_app
write_wrapper aerodroid-container aerodroid.backend.container
write_wrapper aerodroid-install-apk aerodroid.backend.apk
write_wrapper aerodroid-apk-doctor aerodroid.apk_doctor
write_wrapper aerodroid-tune aerodroid.tuning.performance
write_wrapper aerodroid-run-apk aerodroid.run_apk

cat >"$PKG_ROOT/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: AeroDroid Local <root@localhost>
Depends: python3 (>= 3.10), python3-pyqt6, util-linux, policykit-1, weston, libcap2-bin
Description: Mini Android container APK runner
 AeroDroid installs and launches Android APK files through the local
 shadow-droid-core namespace runtime.
EOF

cat >"$PKG_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

if command -v setcap >/dev/null 2>&1; then
  setcap cap_sys_admin,cap_net_admin,cap_setuid,cap_setgid,cap_dac_override,cap_chown,cap_fowner,cap_mknod+ep /opt/aerodroid/bin/shadow-droid-core 2>/dev/null || true
  setcap cap_sys_admin,cap_net_admin,cap_setuid,cap_setgid,cap_dac_override,cap_chown,cap_fowner,cap_mknod+ep /opt/aerodroid/bin/phishadowd 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v update-mime-database >/dev/null 2>&1; then
  update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi

exit 0
EOF
chmod 0755 "$PKG_ROOT/DEBIAN/postinst"

cat >"$PKG_ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v update-mime-database >/dev/null 2>&1; then
  update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi

exit 0
EOF
chmod 0755 "$PKG_ROOT/DEBIAN/postrm"

mkdir -p "$ROOT_DIR/dist"
# Use maximum compression (xz -z9) to reduce deb size
dpkg-deb -Zxz -z9 --build --root-owner-group "$PKG_ROOT" "$OUT_DEB"

printf '\nBuilt: %s\n' "$OUT_DEB"
