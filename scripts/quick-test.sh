#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
GO_BIN="${GO_BIN:-go}"
GUI_LOG="${TMPDIR:-/tmp}/aerodroid-gui-smoke.log"

step() {
  printf '\n==> %s\n' "$1"
}

step "Python compile"
"$PYTHON_BIN" -m compileall -q aerodroid main.py

step "Go test"
"$GO_BIN" test phishadow-droid phishadow-droid/container phishadow-droid/display

step "Build core binary"
"$GO_BIN" build -buildvcs=false -trimpath -ldflags='-s -w' -o bin/shadow-droid-core .
cp bin/shadow-droid-core bin/phishadowd

step "APK metadata smoke"
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
from zipfile import ZipFile
from aerodroid.backend.apk import APKManager

apk = Path("/tmp/aerodroid-metadata-smoke.apk")
manifest = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.example.smoke"
    android:versionCode="7"
    android:versionName="1.2.3">
  <uses-sdk android:minSdkVersion="23" android:targetSdkVersion="35" />
  <application android:label="Smoke App">
    <activity android:name=".MainActivity">
      <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
      </intent-filter>
    </activity>
  </application>
</manifest>
"""
with ZipFile(apk, "w") as zf:
    zf.writestr("AndroidManifest.xml", manifest)

meta = APKManager().extract_metadata(apk)
apk.unlink(missing_ok=True)
assert meta.package_name == "com.example.smoke", meta.to_dict()
assert meta.version_name == "1.2.3", meta.to_dict()
assert meta.version_code == 7, meta.to_dict()
assert meta.main_activity == "com.example.smoke.MainActivity", meta.to_dict()
print(f"{meta.package_name} {meta.version_name} {meta.main_activity}")
PY

step "APK compatibility smoke"
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
from zipfile import ZipFile
from aerodroid.backend.apk import APKManager

manifest = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.example.compat"
    android:versionCode="1"
    android:versionName="1.0">
  <application android:label="Compat">
    <activity android:name=".MainActivity">
      <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
      </intent-filter>
    </activity>
  </application>
</manifest>
"""

manager = APKManager()
arm64_apk = Path("/tmp/aerodroid-arm64-compat.apk")
arm32_apk = Path("/tmp/aerodroid-arm32-compat.apk")
with ZipFile(arm64_apk, "w") as zf:
    zf.writestr("AndroidManifest.xml", manifest)
    zf.writestr("lib/arm64-v8a/libcompat.so", b"\x7fELF")
with ZipFile(arm32_apk, "w") as zf:
    zf.writestr("AndroidManifest.xml", manifest)
    zf.writestr("lib/armeabi-v7a/libcompat.so", b"\x7fELF")

try:
    arm64 = manager.inspect_compatibility(arm64_apk)
    arm32 = manager.inspect_compatibility(arm32_apk)
    assert arm64.supported, arm64.to_dict()
    assert arm64.execution_mode == "native-bridge-arm64", arm64.to_dict()
    assert not arm32.supported, arm32.to_dict()
    assert arm32.execution_mode == "unsupported-native-abi", arm32.to_dict()
    print(f"{arm64.execution_mode} ok; {arm32.execution_mode} blocked")
finally:
    arm64_apk.unlink(missing_ok=True)
    arm32_apk.unlink(missing_ok=True)
PY

step "Runtime profile smoke"
"$PYTHON_BIN" - <<'PY'
from aerodroid.backend.apk import APKCompatibilityReport
from aerodroid.config import Defaults
from aerodroid.run_apk import _profile_resources

report = APKCompatibilityReport(
    apk_path="/tmp/game.apk",
    metadata={},
    native_abis=["arm64-v8a"],
    native_libs_by_abi={},
    runtime_abis=["x86_64", "arm64-v8a"],
    native_bridge="libndk_translation.so",
    zygote="zygote64",
    selected_abi="arm64-v8a",
    execution_mode="native-bridge-arm64",
    profile="arm64-bridge",
    min_memory_mb=3072,
    pids_max=512,
    supported=True,
    warnings=[],
    errors=[],
    recommendations=[],
    permissions=[],
    features=[],
)

assert _profile_resources("auto", report) == (
    Defaults.GAME_MEMORY_LIMIT_MB,
    Defaults.PIDS_MAX,
    Defaults.GAME_CPU_QUOTA_MICROS,
    Defaults.CPU_PERIOD_MICROS,
)
assert _profile_resources("lowmem", report) == (
    Defaults.LOW_MEMORY_LIMIT_MB,
    Defaults.LOW_PIDS_MAX,
    Defaults.LOW_CPU_QUOTA_MICROS,
    Defaults.CPU_PERIOD_MICROS,
)
print("profiles ok")
PY

step "GUI smoke"
set +e
timeout 3s env QT_QPA_PLATFORM=offscreen "$PYTHON_BIN" -m aerodroid --debug >"$GUI_LOG" 2>&1
gui_code=$?
set -e
if [[ "$gui_code" != "0" && "$gui_code" != "124" ]]; then
  sed -n '1,160p' "$GUI_LOG"
  exit "$gui_code"
fi
sed -n '1,40p' "$GUI_LOG"

step "Core status"
./bin/shadow-droid-core status --name default-android

printf '\nOK: quick test passed\n'
