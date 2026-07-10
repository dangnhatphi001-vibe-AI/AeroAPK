#!/usr/bin/env bash
#
# debloat_aosp.sh — Micro-AOSP Debloat for AeroDroid Container
#
# Strips a standard AOSP 14 (API 34) root filesystem down to the absolute
# minimum required to run arbitrary .apk files natively inside a container.
#
# Removes:
#   - Google Mobile Services (GMS)
#   - System launchers, UI, settings
#   - Telephony components (dialer / SMS / contacts)
#   - Unnecessary system apps (clock, calendar, calculator, camera, etc.)
#
# Keeps:
#   - Core Android services: PackageManager, SurfaceFlinger, AudioFlinger, Zygote
#   - Base framework libraries
#   - ART / Bionic runtime
#
# Usage:
#   sudo ./scripts/debloat_aosp.sh [--target-dir /path/to/aosp-rootfs] [--dry-run]
#
set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
TARGET_DIR="${TARGET_DIR:-./aosp14_google_core}"
DRY_RUN=0
SHOW_HELP=0

# ─── Colour helpers ──────────────────────────────────────────────────────────
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m'

log_info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
log_warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
log_del()   { printf "${RED}[DEL]${NC}   %s\n" "$*"; }
log_keep()  { printf "${CYAN}[KEEP]${NC}  %s\n" "$*"; }
log_title() { printf "\n${CYAN}═══ %s ═══${NC}\n\n" "$*"; }

# ─── Usage ───────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: sudo $0 [OPTIONS]

Options:
  --target-dir PATH   RootFS directory to debloat (default: ./aosp14_google_core)
  --dry-run           Preview deletions without actually removing files
  -h, --help          Show this help

Description:
  Strips an AOSP 14 rootfs down to a bare Metal execution layer suitable
  for native .apk execution inside AeroDroid LXC containers.

  Safe checks: verifies directory existence, validates system structure
  before removal, prints detailed log of every deleted component.

EOF
}

# ─── Argument parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-dir) TARGET_DIR="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown: $1"; usage; exit 1 ;;
    esac
done

TARGET_DIR="$(realpath "$TARGET_DIR")"

# Verify rootfs looks like an Android system image
check_rootfs() {
    if [[ ! -d "$TARGET_DIR" ]]; then
        echo "ERROR: Target directory does not exist: $TARGET_DIR"
        echo "       Create it first or pass --target-dir with a valid path."
        exit 1
    fi

    local required=(
        "system/build.prop"
        "system/framework"
        "system/lib64"
        "system/bin"
    )
    local missing=()
    local found=0

    for path in "${required[@]}"; do
        if [[ -e "$TARGET_DIR/$path" ]]; then
            found=$((found + 1))
        else
            missing+=("$path")
        fi
    done

    if [[ $found -lt 2 ]]; then
        echo "ERROR: $TARGET_DIR does not look like an AOSP 14 rootfs."
        echo "       Missing: ${missing[*]:-(too few matches)}"
        echo ""
        echo "       Expected structure (minimal):"
        echo "         <rootfs>/system/build.prop"
        echo "         <rootfs>/system/framework/"
        echo "         <rootfs>/system/lib64/"
        echo "         <rootfs>/system/bin/"
        exit 1
    fi

    log_info "RootFS verified at $TARGET_DIR"
}

# ─── Safe delete wrapper ─────────────────────────────────────────────────────
safe_delete() {
    local path="$1"
    local label="${2:-$(basename "$path")}"

    if [[ ! -e "$path" ]]; then
        return 0  # already gone, not an error
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        log_warn "dry-run: would delete $label → $path"
        return 0
    fi

    if rm -rf "$path" 2>/dev/null; then
        log_del "$label"
    else
        log_warn "failed to delete $label at $path (permissions?)"
    fi
}

safe_delete_system_app() {
    local pkg="$1"
    local found=0

    for location in "$TARGET_DIR/system/app/$pkg" "$TARGET_DIR/product/app/$pkg"; do
        if [[ -d "$location" ]]; then
            safe_delete "$location" "system/app/$pkg"
            found=$((found + 1))
        fi
    done
    return 0
}

safe_delete_system_priv() {
    local pkg="$1"
    local found=0

    for location in \
        "$TARGET_DIR/system/priv-app/$pkg" \
        "$TARGET_DIR/system/product/priv-app/$pkg" \
        "$TARGET_DIR/product/priv-app/$pkg"
    do
        if [[ -d "$location" ]]; then
            safe_delete "$location" "system/priv-app/$pkg"
            found=$((found + 1))
        fi
    done
    return 0
}

# ─── Verify a core package STILL exists (fail-safe) ──────────────────────────
assert_kept() {
    local pkg="$1"
    local found=0

    for location in \
        "$TARGET_DIR/system/app/$pkg"       \
        "$TARGET_DIR/system/priv-app/$pkg"
    do
        if [[ -e "$location" ]]; then
            found=$((found + 1))
        fi
    done

    # Also check framework (these are .jar, not dirs)
    local fw_jar="$TARGET_DIR/system/framework/$pkg"
    if [[ -e "$fw_jar" ]]; then
        found=$((found + 1))
    fi 2>/dev/null || true

    if [[ $found -eq 0 ]]; then
        log_warn "Safety check: $pkg was NOT found after debloat — may need manual restore"
        return 1
    else
        log_keep "Verified: $pkg"
        return 0
    fi
}

# ─── Phase 1: GMS (Google Mobile Services) ───────────────────────────────────
phase_gms() {
    log_title "Phase 1 — Google Mobile Services"

    local gms_list=(
        "PrebuiltGmsCore"
        "PrebuiltGmsCorePi"
        "GmsCore"
        "GoogleServicesFramework"
        "Phonesky"
        "GooglePlayServicesUpdater"
        "GoogleFeedback"
        "GoogleOneTimeInitializer"
        "GooglePartnerSetup"
        "GoogleContactsSyncAdapter"
        "GoogleCalendarSyncAdapter"
        "GoogleTTS"
        "PlayAutoInstallConfig"
        "PrebuiltBugle"
    )

    for pkg in "${gms_list[@]}"; do
        safe_delete_system_app "$pkg"
        safe_delete_system_priv "$pkg"
    done

    # Also delete any leftover GMS .jar in framework
    for jar in "$TARGET_DIR/system/framework"/com.google.*.jar "$TARGET_DIR/system/framework"/services.google*; do
        if [[ -f "$jar" ]]; then
            safe_delete "$jar" "framework/$(basename "$jar")"
        fi
    done
}

# ─── Phase 2: Launchers & GUI components ─────────────────────────────────────
phase_gui() {
    log_title "Phase 2 — Launchers, SystemUI, Settings"

    local gui_list=(
        "Launcher3"
        "Launcher3QuickStep"
        "NexusLauncher"
        "SystemUI"
        "SystemUIGoogle"
        "Trebuchet"
        "Settings"
        "SettingsGoogle"
        "SettingsIntelligence"
        "WallpaperCropper"
        "ThemePicker"
        "WallpaperPicker"
        "WallpaperPicker2"
        "Dreams"
        "LiveWallpapers"
        "LiveWallpapersPicker"
    )

    for pkg in "${gui_list[@]}"; do
        safe_delete_system_priv "$pkg"
        safe_delete_system_app "$pkg"
    done

    # Remove launcher-related framework overlays
    for ovl in "$TARGET_DIR"/*/overlay/framework-res__auto_generated_rro.apk \
                "$TARGET_DIR"/*/overlay/*launcher*.apk \
                "$TARGET_DIR"/*/overlay/*systemui*.apk
    do
        if [[ -f "$ovl" ]]; then
            safe_delete "$ovl" "overlay/$(basename "$ovl")"
        fi
    done 2>/dev/null || true
}

# ─── Phase 3: Telephony stack ────────────────────────────────────────────────
#     WARNING: ContactsProvider is NOT telephony — it is a content provider
#     that many apps depend on for contact access. We KEEP it.
phase_telephony() {
    log_title "Phase 3 — Telephony (dialer / SMS / carrier only)"

    local tele_list=(
        "TeleService"
        "Telecom"
        "TelephonyProvider"
        "CarrierConfig"
        "CarrierConfigOverlay"
        "CarrierDefaultApp"
        "CarrierSetup"
        "WfcActivation"
        "Dialer"
        "GoogleDialer"
        "Mms"
        "MmsService"
        "messaging"
        "Messages"
        "WAPPushManager"
        "SimAppDialog"
        "SimContacts"
        "CellBroadcastReceiver"
        "EmergencyInfo"
        "EuiccSupportPixel"
        "EuiccGoogle"
        "Euicc"
    )

    for pkg in "${tele_list[@]}"; do
        safe_delete_system_priv "$pkg"
        safe_delete_system_app "$pkg"
    done
}

# ─── Phase 4: Unnecessary system apps ────────────────────────────────────────
phase_bloatware() {
    log_title "Phase 4 — System bloatware"

    local bloat_list=(
        # Clock & productivity
        "DeskClock"
        "DeskClockGoogle"
        "CalendarGooglePrebuilt"
        "Calendar"
        "PrebuiltCalendar"
        "Calculator"
        "CalculatorGoogle"
        "ExactCalculator"

        # Media & Camera
        "Camera2"
        "Camera"
        "GoogleCamera"
        "Photos"
        "Gallery2"
        "GalleryGoogle"
        "MusicFX"
        "Eleven"
        "Music"
        "GooglePlayMusic"

        # Browser & Search — keep WebView (apps like Zalo need in-app browser)
        "Browser2"
        "Browser"
        "Chrome"
        "Chromium"
        "QuickSearchBox"
        "GoogleQuickSearchBox"
        "Velvet"

        # Email, Maps, etc
        "Email"
        "EmailGoogle"
        "Exchange2"
        "Maps"
        "YouTube"
        "Drive"
        "Keep"
        "Duo"
        "Hangouts"
        "Meet"

        # Misc — safe to remove in container
        "PrintServiceRecommendation"
        "CloudPrint"
        "PartnerBookmarks"
        "HTMLViewer"
        "BookmarkProvider"
        "SoundPicker"
        "SoundRecorder"
        "ScreenRecorder"
        "Traceur"
        "Tag"
        "Tags"
        "Stk2"
        "NfcNci"
        "Nfc"
        "SecureElement"
        "VpnDialogs"
    )

    for pkg in "${bloat_list[@]}"; do
        safe_delete_system_app "$pkg"
        safe_delete_system_priv "$pkg"
    done
}

# ─── Phase 4b: Vendor / ODM bloat (targeted only, NOT wholesale) ──────────
#     We keep critical vendor APKs for WiFi/BT/audio/CameraHAL/connectivity.
phase_vendor_bloat() {
    log_title "Phase 4b — Vendor bloat (targeted only)"

    # Only remove these known-safe vendor bloat packages.
    # Everything else in vendor/app, vendor/overlay is KEPT.
    local known_bloat_vendor=(
        "qcrilmsgtunnel"
        "diagmon"
        "QtiTelephonyService"
        "datastatusnotification"
        "embms"
        "ims"
        "QColorService"
        "uimg"
        "QAS_DVC_MSP"
        "CneApp"
        "Doze"
        "AmbientSense"
        "DeviceHealthServices"
        "Nova"
        "Velvet"
        "Fitness"
        "GoogleTTS"
    )

    for dir in "$TARGET_DIR/vendor/app" "$TARGET_DIR/vendor/overlay"; do
        if [[ ! -d "$dir" ]]; then
            continue
        fi
        for entry in "$dir"/*; do
            local name
            name="$(basename "$entry")"
            name="${name%.apk}"
            for bloat in "${known_bloat_vendor[@]}"; do
                if [[ "$name" == "$bloat" ]]; then
                    safe_delete "$entry" "vendor/$name"
                    break
                fi
            done
        done
    done
}

# ─── Phase 5: Safety verification ────────────────────────────────────────────
#     Checks core runtime, networking, audio, and connectivity components.
phase_verify() {
    log_title "Phase 5 — Safety verification"

    # ── Core Android framework services ──
    local core_services=(
        "framework"       # framework.jar
        "services.jar"    # system_server
        "pm.jar"          # PackageManagerService
        "PackageInstaller"
        "PermissionController"
        "ExtServices"
        "Shell"
    )

    local all_ok=true
    for svc in "${core_services[@]}"; do
        assert_kept "$svc" || all_ok=false
    done

    # ── Networking & Connectivity (REQUIRED for APK network access) ──
    local network_stack=(
        "system/priv-app/NetworkStack"
        "system/product/priv-app/NetworkStackNext"
        "system/app/CertInstaller"
        "system/priv-app/WifiService"
        "system/app/Bluetooth"
        "system/product/priv-app/ServiceConnectivity"
        "system/product/priv-app/ServiceWifi"
    )
    for candidate in "${network_stack[@]}"; do
        local fp="$TARGET_DIR/$candidate"
        if [[ -e "$fp" ]]; then
            log_keep "Network: $candidate"
        else
            log_keep "Network: $candidate (optional — not present in this image)"
        fi
    done

    # ── Audio / Media (REQUIRED for app sound, mic) ──
    local audio_core=(
        "system/lib64/libaudioflinger.so"
        "system/lib64/libaudiopolicyservice.so"
        "system/lib64/libmedia.so"
        "system/lib64/libstagefright.so"
        "system/lib64/libeffects.so"
        "system/lib64/libaudioclient.so"
    )
    for f in "${audio_core[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Audio: $f"
        else
            log_warn "Audio library MISSING: $f — apps may have no sound"
            all_ok=false
        fi
    done

    # ── Audio policy APKs ──
    # These provide audio policy configuration. Missing them means no sound routing.
    local audio_apps=(
        "system/product/priv-app/MediaProvider"
        "system/media"
    )
    for candidate in "${audio_apps[@]}"; do
        if [[ -e "$TARGET_DIR/$candidate" ]]; then
            log_keep "Audio: $candidate"
        fi
    done

    # ── ART / Bionic runtime ──
    local art_files=(
        "$TARGET_DIR/system/lib64/libart.so"
        "$TARGET_DIR/system/bin/app_process"
        "$TARGET_DIR/system/bin/installd"
        "$TARGET_DIR/system/lib64/libc.so"
        "$TARGET_DIR/system/lib64/libm.so"
        "$TARGET_DIR/system/lib64/libdl.so"
    )
    for f in "${art_files[@]}"; do
        if [[ -e "$f" ]]; then
            log_keep "Runtime: $(realpath --relative-to="$TARGET_DIR" "$f" 2>/dev/null || basename "$f")"
        else
            log_warn "Runtime critical file MISSING: $f"
            all_ok=false
        fi
    done

    # ── Container-specific: binder / wayland / surfaceflinger ──
    # servicemanager = binder context manager, bắt buộc để mọi IPC hoạt động
    local container_support=(
        "system/bin/surfaceflinger"
        "system/bin/vold"
        "system/bin/servicemanager"
    )
    for f in "${container_support[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Core: $(basename "$f")"
        else
            log_warn "Core binary MISSING: $f — container may not boot"
        fi
    done

    # ── GPU / OpenGL ES / Vulkan (REQUIRED for games & UI rendering) ──
    local gpu_libs=(
        "system/lib64/libGLESv2.so"
        "system/lib64/libGLESv1_CM.so"
        "system/lib64/libEGL.so"
        "system/lib64/libvulkan.so"
        "system/lib64/libhwui.so"
        "system/lib64/libgui.so"
        "system/lib64/libui.so"
        "system/lib64/libsync.so"
        "system/lib64/libgralloc_cb.so"
        "system/lib64/libhardware.so"
    )
    for f in "${gpu_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "GPU: $(basename "$f")"
        else
            log_warn "GPU library MISSING: $f — apps may have no graphics acceleration"
        fi
    done

    # ── Audio capture / microphone (REQUIRED for Zalo voice, game chat) ──
    local mic_libs=(
        "system/lib64/libaudiorecord.so"
        "system/lib64/libaaudio.so"
        "system/lib64/libopensles.so"
        "system/lib64/libalsautils.so"
        "system/lib64/libtinyalsa.so"
    )
    for f in "${mic_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Mic: $(basename "$f")"
        else
            log_warn "Audio capture library MISSING: $f — mic may not work"
        fi
    done

    # ── Binder IPC (linh hồn của Android — mọi service đều qua Binder) ──
    local binder_libs=(
        "system/lib64/libbinder.so"
        "system/lib64/libhwbinder.so"
    )
    for f in "${binder_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Binder IPC: $(basename "$f")"
        else
            log_warn "Binder library MISSING: $f — toàn bộ IPC Android sẽ sập"
        fi
    done
    # Binder context manager (servicemanager) — đã check ở Core section trên
    # Binder devices (/dev/binder, /dev/hwbinder, /dev/vndbinder) do
    # container runner (Go) mount qua binderfs — không nằm trong rootfs.

    # ── Input / touch / keyboard (REQUIRED for app interaction) ──
    local input_libs=(
        "system/lib64/libinput.so"
        "system/lib64/libinputservice.so"
        "system/lib64/libevdev.so"
    )
    for f in "${input_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Input: $(basename "$f")"
        else
            log_warn "Input library MISSING: $f — touch/keyboard may not work"
        fi
    done

    # ── Sensors (orientation, accelerometer for games) ──
    local sensor_libs=(
        "system/lib64/libsensor.so"
        "system/lib64/libsenservice.so"
    )
    for f in "${sensor_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Sensors: $(basename "$f")"
        else
            log_info "Sensors: $(basename "$f") not found (optional for container)"
        fi
    done

    # ── WebView (apps need in-app browser for login, ads, content) ──
    local webview_paths=(
        "system/app/WebViewGoogle"
        "system/product/app/WebViewGoogle"
        "system/app/webview"
        "system/product/app/webview"
    )
    local wv_found=false
    for wv in "${webview_paths[@]}"; do
        if [[ -e "$TARGET_DIR/$wv" ]]; then
            log_keep "WebView: $wv"
            wv_found=true
        fi
    done
    if [[ "$wv_found" == false ]]; then
        log_warn "WebView NOT found — apps may crash when opening web content"
    fi

    # ── Contacts provider (many apps need Contacts API) ──
    local contacts_paths=(
        "system/priv-app/ContactsProvider"
        "system/product/priv-app/ContactsProvider"
        "system/app/Contacts"
    )
    for cp in "${contacts_paths[@]}"; do
        if [[ -e "$TARGET_DIR/$cp" ]]; then
            log_keep "Contacts: $cp"
        fi
    done

    # ── Camera native libraries ──
    local camera_libs=(
        "system/lib64/libcamera_client.so"
        "system/lib64/libcamera_metadata.so"
        "system/lib64/libcameraservice.so"
    )
    for f in "${camera_libs[@]}"; do
        if [[ -e "$TARGET_DIR/$f" ]]; then
            log_keep "Camera: $(basename "$f")"
        else
            log_info "Camera: $(basename "$f") not found (API-only, HAL in vendor)"
        fi
    done

    # ── Keyboard layout files (REQUIRED for keyboard input in games) ──
    # These are standard Android key layout/config files in /system/usr/.
    # Our debloat script only touches system/app/ and system/priv-app/,
    # so these are inherently preserved. We verify for safety.
    if [[ -d "$TARGET_DIR/system/usr/keylayout" ]]; then
        log_keep "Keyboard: /system/usr/keylayout/ (key layout directory)"
    fi
    if [[ -f "$TARGET_DIR/system/usr/keylayout/Generic.kl" ]]; then
        log_keep "Keyboard: Generic.kl — keyboard mapping for games"
    fi
    if [[ -d "$TARGET_DIR/system/usr/keychars" ]]; then
        log_keep "Keyboard: /system/usr/keychars/ (key character maps)"
    fi

    if [[ "$all_ok" == false ]]; then
        log_warn "Some critical components are missing. The rootfs may be incomplete."
    fi
}

# ─── Main ────────────────────────────────────────────────────────────────────
main() {
    # ─── Sanity check ──────────────────────────────────────────────────────────
    auto_yes=0
    if [[ $EUID -ne 0 ]]; then
        if [[ -n "${FORCE_YES:-}" ]] || [[ ! -t 0 ]]; then
            auto_yes=1
            echo "NOTE: Non-interactive mode — proceeding without root privileges."
        fi
        if [[ $auto_yes -eq 0 ]]; then
            echo "WARNING: Not running as root. Some files may have restricted permissions."
            echo "         Continue? [y/N]"
            read -r answer
            if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
                echo "Aborted."
                exit 1
            fi
        fi
    fi

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║         AeroDroid - Micro-AOSP Debloat Script               ║"
    echo "║         Target: $TARGET_DIR"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "║         Mode:   DRY-RUN (no files will be touched)          ║"
    fi
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    check_rootfs

    phase_gms
    phase_gui
    phase_telephony
    phase_bloatware
    phase_vendor_bloat
    phase_verify

    # Summary
    log_title "Debloat complete!"
    echo ""
    echo "  RootFS: $TARGET_DIR"
    echo ""
    echo "  ✓ GMS removed                    • SystemUI removed"
    echo "  ✓ Launchers removed              • Settings removed"
    echo "  ✓ Telephony (calls/SMS) removed  • ContactsProvider KEPT"
    echo "  ✓ Bloatware removed              • WebView KEPT"
    echo "  ✓ Vendor bloat (targeted)        • Vendor HAL kept intact"
    echo ""
    echo "  ╔═══ Components PRESERVED ═══════════════════════╗"
    echo "  ║  🧵 Binder IPC (libbinder + servicemanager)   ║"
    echo "  ║  🌐 NetworkStack / WifiService / Bluetooth    ║"
    echo "  ║  📜 CertInstaller / WebView / PrintSpooler    ║"
    echo "  ║  👤 ContactsProvider / Contacts               ║"
    echo "  ║  🔊 AudioFlinger / AudioPolicy / libaudiorecord║"
    echo "  ║  🎮 OpenGL ES / EGL / Vulkan / HWUI           ║"
    echo "  ║  🖱️ libinput / libevdev / Generic.kl keymap   ║"
    echo "  ║  📷 libcamera / libsensor                     ║"
    echo "  ║  🖼️ SurfaceFlinger / Binder / Servicemanager  ║"
    echo "  ║  ⚙️ ART / Bionic libc / Zygote / PackageMgr  ║"
    echo "  ╚════════════════════════════════════════════════╝"
    echo ""
    echo "  The rootfs is now ready for AeroDroid container deployment."
    echo ""

    if [[ $DRY_RUN -eq 1 ]]; then
        log_warn "This was a dry-run. No files were actually removed."
        echo "         Run without --dry-run to apply changes."
    fi
}

main "$@"