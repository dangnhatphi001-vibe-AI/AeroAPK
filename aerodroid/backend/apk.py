"""APK handling: metadata extraction, installation, and .desktop launcher generation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from aerodroid.config import Paths

logger = logging.getLogger(__name__)

_ABI_ORDER = ("x86_64", "arm64-v8a", "x86", "armeabi-v7a", "armeabi")
_DIRECT_X86_ABIS = {"x86_64", "x86"}
_ARM64_ABIS = {"arm64-v8a"}
_ARM32_ABIS = {"armeabi-v7a", "armeabi"}


class APKError(Exception):
    pass


@dataclass(frozen=True)
class APKCompatibilityReport:
    """Static APK/runtime compatibility decision."""

    apk_path: str
    metadata: Dict
    native_abis: List[str]
    native_libs_by_abi: Dict[str, List[str]]
    runtime_abis: List[str]
    native_bridge: str
    zygote: str
    selected_abi: Optional[str]
    execution_mode: str
    profile: str
    min_memory_mb: int
    pids_max: int
    supported: bool
    warnings: List[str]
    errors: List[str]
    recommendations: List[str]
    permissions: List[str]
    features: List[str]

    def to_dict(self) -> Dict:
        return {
            "apk_path": self.apk_path,
            "metadata": self.metadata,
            "native_abis": self.native_abis,
            "native_libs_by_abi": self.native_libs_by_abi,
            "runtime_abis": self.runtime_abis,
            "native_bridge": self.native_bridge,
            "zygote": self.zygote,
            "selected_abi": self.selected_abi,
            "execution_mode": self.execution_mode,
            "profile": self.profile,
            "min_memory_mb": self.min_memory_mb,
            "pids_max": self.pids_max,
            "supported": self.supported,
            "warnings": self.warnings,
            "errors": self.errors,
            "recommendations": self.recommendations,
            "permissions": self.permissions,
            "features": self.features,
        }

    def summary_lines(self) -> List[str]:
        meta = self.metadata
        lines = [
            f"APK: {Path(self.apk_path).name}",
            f"Package: {meta.get('package_name', '-')}",
            f"Version: {meta.get('version_name', '-')}",
            f"Activity: {meta.get('main_activity') or '-'}",
            f"Native ABI: {', '.join(self.native_abis) if self.native_abis else 'managed/no-native'}",
            f"Runtime ABI: {', '.join(self.runtime_abis) if self.runtime_abis else 'unknown'}",
            f"Native bridge: {self.native_bridge or 'disabled'}",
            f"Decision: {'SUPPORTED' if self.supported else 'BLOCKED'} ({self.execution_mode})",
            f"Profile: {self.profile} memory={self.min_memory_mb}M pids={self.pids_max}",
        ]
        for item in self.warnings:
            lines.append(f"WARNING: {item}")
        for item in self.errors:
            lines.append(f"ERROR: {item}")
        for item in self.recommendations:
            lines.append(f"RECOMMEND: {item}")
        return lines


class APKMetadata:
    """Metadata extracted from an APK file."""

    def __init__(
        self,
        package_name: str,
        version_name: str,
        version_code: int,
        label: str,
        icon_path: Optional[Path] = None,
        min_sdk: int = 0,
        target_sdk: int = 0,
        main_activity: Optional[str] = None,
        native_abis: Optional[List[str]] = None,
        native_lib_count: int = 0,
        apk_size: int = 0,
    ):
        self.package_name = package_name
        self.version_name = version_name
        self.version_code = version_code
        self.label = label
        self.icon_path = icon_path
        self.min_sdk = min_sdk
        self.target_sdk = target_sdk
        self.main_activity = main_activity
        self.native_abis = native_abis or []
        self.native_lib_count = native_lib_count
        self.apk_size = apk_size

    def to_dict(self) -> Dict:
        return {
            "package_name": self.package_name,
            "version_name": self.version_name,
            "version_code": self.version_code,
            "label": self.label,
            "icon_path": str(self.icon_path) if self.icon_path else None,
            "min_sdk": self.min_sdk,
            "target_sdk": self.target_sdk,
            "main_activity": self.main_activity,
            "native_abis": self.native_abis,
            "native_lib_count": self.native_lib_count,
            "apk_size": self.apk_size,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "APKMetadata":
        return cls(
            package_name=data["package_name"],
            version_name=data["version_name"],
            version_code=data["version_code"],
            label=data["label"],
            icon_path=Path(data["icon_path"]) if data.get("icon_path") else None,
            min_sdk=data.get("min_sdk", 0),
            target_sdk=data.get("target_sdk", 0),
            main_activity=data.get("main_activity"),
            native_abis=list(data.get("native_abis") or []),
            native_lib_count=int(data.get("native_lib_count") or 0),
            apk_size=int(data.get("apk_size") or 0),
        )


class APKManager:
    """Manage APK installation and desktop integration."""

    def __init__(self, container: Optional["LXCContainer"] = None):
        self.container = container
        Paths.ensure_user_dirs()

    def extract_metadata(self, apk_path: Path) -> APKMetadata:
        """Extract package info from APK using aapt or apkutils fallback."""
        apk_path = Path(apk_path).resolve()
        if not apk_path.exists():
            raise APKError(f"APK not found: {apk_path}")

        # Try aapt first
        metadata = self._extract_with_aapt(apk_path)
        if metadata:
            return self._enrich_metadata(metadata, apk_path)

        # Try apkutils (pure Python)
        metadata = self._extract_with_apkutils(apk_path)
        if metadata:
            return self._enrich_metadata(metadata, apk_path)

        metadata = self._extract_with_manifest(apk_path)
        if metadata:
            return self._enrich_metadata(metadata, apk_path)

        raise APKError("Could not extract metadata from APK manifest")

    def _enrich_metadata(self, metadata: APKMetadata, apk_path: Path) -> APKMetadata:
        native_libs = self._apk_native_libs_by_abi(apk_path)
        metadata.native_abis = self._sort_abis(native_libs)
        metadata.native_lib_count = sum(len(libs) for libs in native_libs.values())
        try:
            metadata.apk_size = apk_path.stat().st_size
        except OSError:
            metadata.apk_size = 0
        return metadata

    def _extract_with_aapt(self, apk_path: Path) -> Optional[APKMetadata]:
        if not shutil.which("aapt"):
            return None
        try:
            out = subprocess.run(
                ["aapt", "dump", "badging", str(apk_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                logger.debug("aapt failed: %s", out.stderr)
                return None
            return self._parse_aapt_output(out.stdout, apk_path)
        except Exception as exc:  # pragma: no cover
            logger.debug("aapt exception: %s", exc)
            return None

    def _extract_with_apkutils(self, apk_path: Path) -> Optional[APKMetadata]:
        try:
            from apkutils import APK  # type: ignore
        except ImportError:
            return None
        try:
            apk = APK.from_file(str(apk_path))
            manifest = apk.get_manifest()
            pkg = manifest.get("package", "")
            label = manifest.get("application", {}).get("label", pkg)
            version_name = manifest.get("versionName", "1.0")
            version_code = int(manifest.get("versionCode", 1))
            min_sdk = int(manifest.get("minSdkVersion", 0))
            target_sdk = int(manifest.get("targetSdkVersion", 0))
            main_activity = self._main_activity_from_manifest_dict(manifest, pkg)

            # Extract icon (first one found)
            icon_path = self._extract_icon(apk, apk_path)

            return APKMetadata(
                package_name=pkg,
                version_name=version_name,
                version_code=version_code,
                label=label,
                icon_path=icon_path,
                min_sdk=min_sdk,
                target_sdk=target_sdk,
                main_activity=main_activity,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("apkutils exception: %s", exc)
            return None

    def _parse_aapt_output(self, output: str, apk_path: Path) -> Optional[APKMetadata]:
        """Parse aapt dump badging output."""
        import re

        pkg_match = re.search(r"package: name='([^']+)'", output)
        if not pkg_match:
            return None
        pkg = pkg_match.group(1)

        ver_name = re.search(r"versionName='([^']*)'", output)
        ver_code = re.search(r"versionCode='(\d+)'", output)
        label = re.search(r"application-label:'([^']*)'", output)
        icon = re.search(r"application-icon-\d+:'([^']*)'", output)
        launchable = re.search(r"launchable-activity: name='([^']+)'", output)
        min_sdk_match = re.search(r"sdkVersion:'(\d+)'", output)
        target_sdk_match = re.search(r"targetSdkVersion:'(\d+)'", output)

        version_name = ver_name.group(1) if ver_name else "1.0"
        version_code = int(ver_code.group(1)) if ver_code else 1
        app_label = label.group(1) if label else pkg
        main_activity = self._qualify_activity(pkg, launchable.group(1)) if launchable else None
        min_sdk = int(min_sdk_match.group(1)) if min_sdk_match else 0
        target_sdk = int(target_sdk_match.group(1)) if target_sdk_match else 0

        # Extract icon from APK if found
        icon_path = None
        if icon:
            # aapt gives path inside APK, we'd need to extract it
            pass

        return APKMetadata(
            package_name=pkg,
            version_name=version_name,
            version_code=version_code,
            label=app_label,
            icon_path=icon_path,
            min_sdk=min_sdk,
            target_sdk=target_sdk,
            main_activity=main_activity,
        )

    def _extract_with_manifest(self, apk_path: Path) -> Optional[APKMetadata]:
        """Extract metadata directly from AndroidManifest.xml in the APK."""
        try:
            with zipfile.ZipFile(apk_path) as zf:
                manifest_data = zf.read("AndroidManifest.xml")
        except (KeyError, zipfile.BadZipFile, OSError) as exc:
            logger.debug("manifest read failed: %s", exc)
            return None

        try:
            return self._parse_manifest_bytes(manifest_data)
        except Exception as exc:  # pragma: no cover
            logger.debug("manifest parse failed: %s", exc)
            return None

    def _parse_manifest_bytes(self, data: bytes) -> Optional[APKMetadata]:
        parser = _AXMLParser(data)
        package_name = ""
        version_name = "1.0"
        version_code = 1
        label = ""
        min_sdk = 0
        target_sdk = 0
        main_activity: Optional[str] = None

        stack: List[str] = []
        current_component: Optional[Dict[str, object]] = None
        current_component_depth = -1
        current_intent: Optional[Dict[str, set[str]]] = None
        current_intent_depth = -1
        components: List[Dict[str, object]] = []

        for event, tag, attrs in parser.events():
            if event == "start":
                depth = len(stack)

                if tag == "manifest":
                    package_name = attrs.get("package", "")
                    version_name = self._clean_resource_value(attrs.get("versionName", "")) or "1.0"
                    version_code = self._parse_int(attrs.get("versionCode"), 1)
                elif tag == "uses-sdk":
                    min_sdk = self._parse_int(attrs.get("minSdkVersion"), min_sdk)
                    target_sdk = self._parse_int(attrs.get("targetSdkVersion"), target_sdk)
                elif tag == "application":
                    label_value = self._clean_resource_value(attrs.get("label", ""))
                    if label_value:
                        label = label_value
                elif tag in {"activity", "activity-alias"}:
                    activity_name = attrs.get("name", "")
                    if tag == "activity-alias" and not activity_name:
                        activity_name = attrs.get("targetActivity", "")
                    qualified = self._qualify_activity(package_name, activity_name)
                    if qualified:
                        current_component = {
                            "name": qualified,
                            "enabled": attrs.get("enabled", "true") != "false",
                            "launcher": False,
                        }
                        current_component_depth = depth
                        components.append(current_component)
                elif tag == "intent-filter" and current_component is not None:
                    current_intent = {"actions": set(), "categories": set()}
                    current_intent_depth = depth
                elif tag == "action" and current_intent is not None:
                    action_name = attrs.get("name", "")
                    if action_name:
                        current_intent["actions"].add(action_name)
                elif tag == "category" and current_intent is not None:
                    category_name = attrs.get("name", "")
                    if category_name:
                        current_intent["categories"].add(category_name)

                stack.append(tag)
                continue

            if event == "end":
                depth = len(stack) - 1
                if current_intent is not None and depth == current_intent_depth:
                    if (
                        "android.intent.action.MAIN" in current_intent["actions"]
                        and "android.intent.category.LAUNCHER" in current_intent["categories"]
                        and current_component is not None
                    ):
                        current_component["launcher"] = True
                    current_intent = None
                    current_intent_depth = -1

                if current_component is not None and depth == current_component_depth:
                    current_component = None
                    current_component_depth = -1

                if stack:
                    stack.pop()

        if not package_name:
            return None

        for component in components:
            if component.get("launcher") and component.get("enabled", True):
                main_activity = str(component["name"])
                break

        return APKMetadata(
            package_name=package_name,
            version_name=version_name,
            version_code=version_code,
            label=label or package_name,
            min_sdk=min_sdk,
            target_sdk=target_sdk,
            main_activity=main_activity,
        )

    @staticmethod
    def _clean_resource_value(value: Optional[str]) -> str:
        if not value or value.startswith("@"):
            return ""
        return value

    @staticmethod
    def _parse_int(value: Optional[str], default: int = 0) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value, 0)
        except ValueError:
            return default

    @staticmethod
    def _qualify_activity(package_name: str, activity_name: str) -> Optional[str]:
        if not package_name or not activity_name:
            return None
        if activity_name.startswith("."):
            return f"{package_name}{activity_name}"
        if "." not in activity_name:
            return f"{package_name}.{activity_name}"
        return activity_name

    def _main_activity_from_manifest_dict(self, manifest: Dict, package_name: str) -> Optional[str]:
        """Best-effort launcher extraction for apkutils manifest dictionaries."""
        for node in self._walk_manifest_nodes(manifest):
            if not isinstance(node, dict):
                continue
            name = node.get("name") or node.get("android:name")
            filters = node.get("intent-filter") or node.get("intent_filters") or node.get("intentFilter")
            if not name or not filters:
                continue
            if isinstance(filters, dict):
                filters = [filters]
            for intent_filter in filters:
                if not isinstance(intent_filter, dict):
                    continue
                actions = self._manifest_names(intent_filter.get("action"))
                categories = self._manifest_names(intent_filter.get("category"))
                if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
                    return self._qualify_activity(package_name, str(name))
        return None

    def _walk_manifest_nodes(self, value: object) -> Iterable[object]:
        yield value
        if isinstance(value, dict):
            for child in value.values():
                yield from self._walk_manifest_nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_manifest_nodes(child)

    @staticmethod
    def _manifest_names(value: object) -> set[str]:
        out: set[str] = set()
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                name = item.get("name") or item.get("android:name")
                if name:
                    out.add(str(name))
            elif isinstance(item, str):
                out.add(item)
        return out

    def _extract_icon(self, apk, apk_path: Path) -> Optional[Path]:
        """Extract launcher icon from APK and save to local icons dir."""
        try:
            from zipfile import ZipFile
        except ImportError:
            return None

        icons_dir = Paths.ICONS_DIR
        icons_dir.mkdir(parents=True, exist_ok=True)
        icon_file = icons_dir / f"{apk_path.stem}.png"

        try:
            # This is simplified; real implementation would parse ARSC
            with ZipFile(apk_path) as zf:
                for name in zf.namelist():
                    if name.endswith(".png") and ("mipmap" in name or "drawable" in name):
                        if "launcher" in name or "ic_" in name:
                            zf.extract(name, icons_dir)
                            extracted = icons_dir / name
                            extracted.rename(icon_file)
                            return icon_file
        except Exception as exc:  # pragma: no cover
            logger.debug("Icon extraction failed: %s", exc)
        return None

    def install_apk(self, apk_path: Path, metadata: Optional[APKMetadata] = None) -> APKMetadata:
        """Install APK into container and create desktop entry."""
        apk_path = Path(apk_path).resolve()
        if not apk_path.exists():
            raise APKError(f"APK not found: {apk_path}")

        if metadata is None:
            metadata = self.extract_metadata(apk_path)

        logger.info("Installing %s (%s)", metadata.package_name, metadata.version_name)

        # Install via container if available
        if self.container:
            if not self.container.is_running():
                raise APKError("Container is not running. Start the runtime before installing APKs.")
            self._validate_abi_compatibility(apk_path)
            result = self.container.install_apk_inside(apk_path)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise APKError(f"Install failed: {detail}")

        # Persist metadata
        app_file = Paths.APPS_DIR / f"{metadata.package_name}.json"
        app_file.write_text(json.dumps(metadata.to_dict(), indent=2, ensure_ascii=False))

        # Create .desktop launcher
        self._create_desktop_entry(metadata)

        logger.info("Installed: %s", metadata.package_name)
        return metadata

    def _validate_abi_compatibility(self, apk_path: Path) -> None:
        report = self.inspect_compatibility(apk_path)
        if report.supported:
            return
        detail = "; ".join(report.errors or report.warnings or ["unsupported APK/runtime combination"])
        raise APKError(detail)

    def inspect_compatibility(self, apk_path: Path) -> APKCompatibilityReport:
        apk_path = Path(apk_path).resolve()
        metadata = self.extract_metadata(apk_path)
        native_libs = self._apk_native_libs_by_abi(apk_path)
        native_abis = self._sort_abis(native_libs)
        runtime_abis = self._runtime_supported_abis()
        native_bridge = self._runtime_native_bridge()
        zygote = self._runtime_zygote()
        manifest_details = self._manifest_details(apk_path)

        selected_abi: Optional[str] = None
        execution_mode = "managed"
        profile = "managed"
        min_memory_mb = 2048
        pids_max = 384
        supported = True
        warnings: List[str] = []
        errors: List[str] = []
        recommendations: List[str] = []

        if not native_abis:
            recommendations.append("No native .so libraries found; this is the cheapest APK type for x86 hosts.")
        else:
            direct = [abi for abi in ("x86_64", "x86") if abi in native_abis and abi in runtime_abis]
            if direct:
                selected_abi = direct[0]
                execution_mode = f"direct-{selected_abi}"
                profile = "direct-native"
                min_memory_mb = 2304 if selected_abi == "x86_64" else 2560
                pids_max = 448
                recommendations.append("Direct x86 native libraries are preferred for weak x86 machines.")
            elif "arm64-v8a" in native_abis and "arm64-v8a" in runtime_abis and native_bridge not in {"", "0"}:
                selected_abi = "arm64-v8a"
                execution_mode = "native-bridge-arm64"
                profile = "arm64-bridge"
                min_memory_mb = 3072
                pids_max = 512
                warnings.append("APK uses ARM64 native code through native bridge; expect overhead versus x86_64 APKs.")
                recommendations.append("For best FPS on weak x86 hosts, prefer an x86_64 APK when one exists.")
            else:
                supported = False
                execution_mode = "unsupported-native-abi"
                if _ARM32_ABIS.intersection(native_abis):
                    errors.append("APK only has 32-bit ARM native code, but this rootfs intentionally runs 64-bit zygote.")
                    recommendations.append("Use an arm64-v8a or x86_64 build of the APK; 32-bit ARM was disabled to avoid runtime crash loops.")
                elif "arm64-v8a" in native_abis and native_bridge in {"", "0"}:
                    errors.append("APK has arm64-v8a native code but native bridge is disabled.")
                    recommendations.append("Run scripts/install-native-bridge.sh, then reinstall the deb.")
                elif "x86" in native_abis and "x86" not in runtime_abis:
                    errors.append("APK only has 32-bit x86 native code, but this rootfs runs 64-bit-only Android.")
                    recommendations.append("Use an x86_64 or arm64-v8a build.")
                else:
                    errors.append(
                        "No APK native ABI matches runtime ABI "
                        f"({', '.join(runtime_abis) if runtime_abis else 'unknown'})."
                    )
                    recommendations.append("Use a pure Java/Kotlin, x86_64, or arm64-v8a APK.")

        if manifest_details["features"]:
            gpu_features = [
                f for f in manifest_details["features"]
                if "vulkan" in f.lower() or "opengles" in f.lower() or "gles" in f.lower()
            ]
            if gpu_features:
                warnings.append("APK declares GPU features; performance depends on host Mesa/DRI passthrough.")

        return APKCompatibilityReport(
            apk_path=str(apk_path),
            metadata=metadata.to_dict(),
            native_abis=native_abis,
            native_libs_by_abi=native_libs,
            runtime_abis=runtime_abis,
            native_bridge=native_bridge,
            zygote=zygote,
            selected_abi=selected_abi,
            execution_mode=execution_mode,
            profile=profile,
            min_memory_mb=min_memory_mb,
            pids_max=pids_max,
            supported=supported,
            warnings=warnings,
            errors=errors,
            recommendations=recommendations,
            permissions=manifest_details["permissions"],
            features=manifest_details["features"],
        )

    @staticmethod
    def _apk_native_abis(apk_path: Path) -> List[str]:
        return APKManager._sort_abis(APKManager._apk_native_libs_by_abi(apk_path))

    @staticmethod
    def _apk_native_libs_by_abi(apk_path: Path) -> Dict[str, List[str]]:
        libs: Dict[str, List[str]] = {}
        with zipfile.ZipFile(apk_path) as zf:
            for name in zf.namelist():
                parts = name.split("/")
                if len(parts) >= 3 and parts[0] == "lib" and parts[1] and name.endswith(".so"):
                    libs.setdefault(parts[1], []).append("/".join(parts[2:]))
        return {abi: sorted(values) for abi, values in sorted(libs.items())}

    @staticmethod
    def _sort_abis(value: Dict[str, object] | Iterable[str]) -> List[str]:
        abis = list(value.keys()) if isinstance(value, dict) else list(value)
        order = {abi: idx for idx, abi in enumerate(_ABI_ORDER)}
        return sorted(set(abis), key=lambda abi: (order.get(abi, len(order)), abi))

    def _runtime_supported_abis(self) -> List[str]:
        raw = ""
        if self.container and self.container.is_running():
            try:
                raw = self.container.getprop("ro.product.cpu.abilist")
            except Exception as exc:  # pragma: no cover
                logger.debug("runtime ABI query failed: %s", exc)
        if not raw:
            raw = self._rootfs_prop(
                "ro.product.cpu.abilist",
                "ro.system.product.cpu.abilist",
                "ro.vendor.product.cpu.abilist",
                "ro.odm.product.cpu.abilist",
            )
        if not raw:
            raw = "x86_64,arm64-v8a"
        return [abi.strip() for abi in raw.split(",") if abi.strip()]

    def _runtime_native_bridge(self) -> str:
        if self.container and self.container.is_running():
            try:
                return self.container.getprop("ro.dalvik.vm.native.bridge")
            except Exception as exc:  # pragma: no cover
                logger.debug("native bridge query failed: %s", exc)
        return self._rootfs_prop("ro.dalvik.vm.native.bridge") or "libndk_translation.so"

    def _runtime_zygote(self) -> str:
        if self.container and self.container.is_running():
            try:
                return self.container.getprop("ro.zygote")
            except Exception as exc:  # pragma: no cover
                logger.debug("zygote query failed: %s", exc)
        return self._rootfs_prop("ro.zygote") or "zygote64"

    @staticmethod
    def _rootfs_prop(*names: str) -> str:
        props = APKManager._read_rootfs_props()
        for name in names:
            value = props.get(name, "")
            if value:
                return value
        return ""

    @staticmethod
    def _read_rootfs_props() -> Dict[str, str]:
        props: Dict[str, str] = {}
        rootfs = Paths.ROOTFS_DIR
        prop_files = (
            rootfs / "system" / "build.prop",
            rootfs / "vendor" / "build.prop",
            rootfs / "vendor" / "odm" / "etc" / "build.prop",
        )
        for path in prop_files:
            try:
                lines = path.read_text(errors="surrogateescape").splitlines()
            except OSError:
                continue
            for line in lines:
                raw = line.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                props[key.strip()] = value.strip()
        return props

    def _manifest_details(self, apk_path: Path) -> Dict[str, List[str]]:
        try:
            with zipfile.ZipFile(apk_path) as zf:
                manifest_data = zf.read("AndroidManifest.xml")
        except (KeyError, zipfile.BadZipFile, OSError):
            return {"permissions": [], "features": []}

        permissions: set[str] = set()
        features: set[str] = set()
        try:
            events = _AXMLParser(manifest_data).events()
            for event, tag, attrs in events:
                if event != "start":
                    continue
                if tag == "uses-permission":
                    name = attrs.get("name")
                    if name:
                        permissions.add(name)
                elif tag == "uses-feature":
                    name = attrs.get("name") or attrs.get("glEsVersion")
                    if name:
                        required = attrs.get("required", "true")
                        suffix = "" if required == "true" else " (optional)"
                        features.add(f"{name}{suffix}")
        except Exception as exc:  # pragma: no cover
            logger.debug("manifest detail parse failed: %s", exc)
        return {"permissions": sorted(permissions), "features": sorted(features)}

    def _create_desktop_entry(self, metadata: APKMetadata) -> Path:
        """Generate a .desktop file that launches the app via AeroDroid."""
        desktop_dir = Paths.DESKTOP_DIR
        desktop_dir.mkdir(parents=True, exist_ok=True)

        desktop_file = desktop_dir / f"aerodroid-{metadata.package_name}.desktop"
        icon_str = ""
        if metadata.icon_path and metadata.icon_path.exists():
            icon_str = f"Icon={metadata.icon_path}\n"
        else:
            icon_str = "Icon=android\n"

        exec_line = f"aerodroid-run --package {metadata.package_name}"
        if metadata.main_activity:
            exec_line += f" --activity {metadata.main_activity}"

        content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={metadata.label}
Comment=Android app ({metadata.package_name})
Exec={exec_line}
Terminal=false
{icon_str}Categories=Utility;X-Android;
StartupNotify=true
StartupWMClass=aerodroid-{metadata.package_name}
X-AeroDroid-Package={metadata.package_name}
"""

        desktop_file.write_text(content)
        os.chmod(desktop_file, 0o755)
        logger.info("Created desktop entry: %s", desktop_file)

        # Update desktop database
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(desktop_dir)], capture_output=True)
        return desktop_file

    def uninstall(self, package_name: str) -> bool:
        """Uninstall app and remove desktop entry."""
        app_file = Paths.APPS_DIR / f"{package_name}.json"
        desktop_file = Paths.DESKTOP_DIR / f"aerodroid-{package_name}.desktop"
        icon_file = Paths.ICONS_DIR / f"{package_name}.png"

        removed = False
        if self.container and self.container.is_running():
            result = self.container.uninstall_app_inside(package_name)
            if result.returncode == 0:
                removed = True

        for f in (app_file, desktop_file, icon_file):
            if f.exists():
                f.unlink()
                removed = True

        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(Paths.DESKTOP_DIR)], capture_output=True)
        return removed

    def list_installed(self) -> Dict[str, APKMetadata]:
        """List all installed apps from metadata files."""
        apps: Dict[str, APKMetadata] = {}
        for meta_file in Paths.APPS_DIR.glob("*.json"):
            try:
                data = json.loads(meta_file.read_text())
                apps[data["package_name"]] = APKMetadata.from_dict(data)
            except Exception:  # pragma: no cover
                pass
        return apps


class _AXMLParser:
    """Minimal Android binary XML reader for APK manifests."""

    RES_STRING_POOL_TYPE = 0x0001
    RES_XML_TYPE = 0x0003
    RES_XML_START_ELEMENT_TYPE = 0x0102
    RES_XML_END_ELEMENT_TYPE = 0x0103

    NO_INDEX = 0xFFFFFFFF
    UTF8_FLAG = 0x00000100

    TYPE_REFERENCE = 0x01
    TYPE_ATTRIBUTE = 0x02
    TYPE_STRING = 0x03
    TYPE_INT_DEC = 0x10
    TYPE_INT_HEX = 0x11
    TYPE_INT_BOOLEAN = 0x12

    def __init__(self, data: bytes):
        self.data = data
        self.strings: List[str] = []

    def events(self) -> Iterable[Tuple[str, str, Dict[str, str]]]:
        if len(self.data) < 8:
            return

        chunk_type, header_size, chunk_size = self._chunk_header(0)
        if chunk_type != self.RES_XML_TYPE:
            text = self._try_decode_text_manifest()
            if text is None:
                return
            yield from text
            return

        offset = header_size
        end = min(chunk_size, len(self.data))
        while offset + 8 <= end:
            chunk_type, header_size, chunk_size = self._chunk_header(offset)
            if chunk_size <= 0 or offset + chunk_size > len(self.data):
                return

            if chunk_type == self.RES_STRING_POOL_TYPE:
                self.strings = self._parse_string_pool(offset, header_size, chunk_size)
            elif chunk_type == self.RES_XML_START_ELEMENT_TYPE:
                yield ("start", *self._parse_start_element(offset))
            elif chunk_type == self.RES_XML_END_ELEMENT_TYPE:
                yield ("end", self._parse_end_element(offset), {})

            offset += chunk_size

    def _try_decode_text_manifest(self) -> Optional[List[Tuple[str, str, Dict[str, str]]]]:
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(self.data)
        except Exception:
            return None

        events: List[Tuple[str, str, Dict[str, str]]] = []

        def local_name(name: str) -> str:
            if "}" in name:
                return name.rsplit("}", 1)[1]
            return name

        def walk(element) -> None:
            attrs = {local_name(k): v for k, v in element.attrib.items()}
            tag = local_name(element.tag)
            events.append(("start", tag, attrs))
            for child in list(element):
                walk(child)
            events.append(("end", tag, {}))

        walk(root)
        return events

    def _chunk_header(self, offset: int) -> Tuple[int, int, int]:
        return struct.unpack_from("<HHI", self.data, offset)

    def _parse_string_pool(self, offset: int, header_size: int, chunk_size: int) -> List[str]:
        if header_size < 28:
            return []
        string_count, style_count, flags, strings_start, _styles_start = struct.unpack_from(
            "<IIIII", self.data, offset + 8
        )
        del style_count

        offsets_base = offset + header_size
        strings_base = offset + strings_start
        strings_end = offset + chunk_size
        is_utf8 = bool(flags & self.UTF8_FLAG)
        result: List[str] = []

        for i in range(string_count):
            string_offset = struct.unpack_from("<I", self.data, offsets_base + i * 4)[0]
            pos = strings_base + string_offset
            if pos >= strings_end:
                result.append("")
                continue
            if is_utf8:
                result.append(self._read_utf8_string(pos, strings_end))
            else:
                result.append(self._read_utf16_string(pos, strings_end))
        return result

    def _read_utf8_string(self, pos: int, limit: int) -> str:
        _, pos = self._read_utf8_length(pos)
        byte_len, pos = self._read_utf8_length(pos)
        raw = self.data[pos:min(pos + byte_len, limit)]
        return raw.decode("utf-8", errors="replace")

    def _read_utf8_length(self, pos: int) -> Tuple[int, int]:
        first = self.data[pos]
        pos += 1
        if first & 0x80:
            second = self.data[pos]
            pos += 1
            return ((first & 0x7F) << 8) | second, pos
        return first, pos

    def _read_utf16_string(self, pos: int, limit: int) -> str:
        char_len, pos = self._read_utf16_length(pos)
        byte_len = char_len * 2
        raw = self.data[pos:min(pos + byte_len, limit)]
        return raw.decode("utf-16le", errors="replace")

    def _read_utf16_length(self, pos: int) -> Tuple[int, int]:
        first = struct.unpack_from("<H", self.data, pos)[0]
        pos += 2
        if first & 0x8000:
            second = struct.unpack_from("<H", self.data, pos)[0]
            pos += 2
            return ((first & 0x7FFF) << 16) | second, pos
        return first, pos

    def _parse_start_element(self, offset: int) -> Tuple[str, Dict[str, str]]:
        _line_number, _comment, _ns, name_idx = struct.unpack_from("<IIII", self.data, offset + 8)
        attr_start, attr_size, attr_count, _id_idx, _class_idx, _style_idx = struct.unpack_from(
            "<HHHHHH", self.data, offset + 24
        )
        tag = self._string(name_idx)
        attrs: Dict[str, str] = {}
        attrs_offset = offset + 16 + attr_start

        for i in range(attr_count):
            attr_offset = attrs_offset + i * attr_size
            _ns, attr_name_idx, raw_value_idx, _size, _zero, data_type, data = struct.unpack_from(
                "<IIIHBBI", self.data, attr_offset
            )
            attr_name = self._string(attr_name_idx)
            if not attr_name:
                continue
            attrs[attr_name] = self._typed_value(raw_value_idx, data_type, data)
        return tag, attrs

    def _parse_end_element(self, offset: int) -> str:
        _line_number, _comment, _ns, name_idx = struct.unpack_from("<IIII", self.data, offset + 8)
        return self._string(name_idx)

    def _typed_value(self, raw_value_idx: int, data_type: int, data: int) -> str:
        if raw_value_idx != self.NO_INDEX:
            return self._string(raw_value_idx)
        if data_type == self.TYPE_STRING:
            return self._string(data)
        if data_type in {self.TYPE_REFERENCE, self.TYPE_ATTRIBUTE}:
            return f"@0x{data:08x}"
        if data_type == self.TYPE_INT_BOOLEAN:
            return "true" if data != 0 else "false"
        if data_type == self.TYPE_INT_HEX:
            return f"0x{data:x}"
        if data_type == self.TYPE_INT_DEC:
            return str(data)
        return str(data)

    def _string(self, idx: int) -> str:
        if idx == self.NO_INDEX or idx < 0 or idx >= len(self.strings):
            return ""
        return self.strings[idx]


def cli() -> int:
    parser = argparse.ArgumentParser(description="AeroDroid APK manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Install APK")
    p_install.add_argument("apk", type=Path)
    p_install.add_argument("--no-container", action="store_true")

    p_list = sub.add_parser("list", help="List installed apps")

    p_uninstall = sub.add_parser("uninstall", help="Uninstall app")
    p_uninstall.add_argument("package")

    p_meta = sub.add_parser("meta", help="Extract metadata")
    p_meta.add_argument("apk", type=Path)

    p_doctor = sub.add_parser("doctor", help="Analyze APK/runtime compatibility")
    p_doctor.add_argument("apk", type=Path)
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable report")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        if args.cmd == "install":
            container = None
            if not args.no_container:
                from aerodroid.backend.container import LXCContainer

                container = LXCContainer()
            manager = APKManager(container)
            manager.install_apk(args.apk)
        elif args.cmd == "list":
            manager = APKManager()
            for pkg, meta in manager.list_installed().items():
                print(f"{pkg}  {meta.label}  v{meta.version_name}")
        elif args.cmd == "uninstall":
            from aerodroid.backend.container import LXCContainer

            manager = APKManager(LXCContainer())
            if manager.uninstall(args.package):
                print(f"Uninstalled {args.package}")
            else:
                print(f"Not found: {args.package}")
        elif args.cmd == "meta":
            manager = APKManager()
            meta = manager.extract_metadata(args.apk)
            print(json.dumps(meta.to_dict(), indent=2, ensure_ascii=False))
        elif args.cmd == "doctor":
            from aerodroid.backend.container import LXCContainer

            manager = APKManager(LXCContainer())
            report = manager.inspect_compatibility(args.apk)
            if args.json:
                print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
            else:
                print("\n".join(report.summary_lines()))
            return 0 if report.supported else 1
    except APKError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
