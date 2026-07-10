"""Compact AeroDroid desktop UI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aerodroid.backend.apk import APKError, APKManager, APKMetadata
from aerodroid.backend.container import LXCContainer, LXCError
from aerodroid.config import Defaults, Paths

logger = logging.getLogger(__name__)

STYLE_SHEET = """
QMainWindow {
    background: #101318;
}
QWidget {
    color: #d8dee9;
    font-family: "Inter", "Ubuntu", "Segoe UI", sans-serif;
    font-size: 12px;
}
QLabel#Title {
    font-size: 20px;
    font-weight: 700;
    color: #f4f6f8;
}
QLabel#Subtle,
QLabel#RuntimeDetail {
    color: #9aa4b2;
}
QLabel#StatePill {
    border-radius: 4px;
    padding: 4px 8px;
    font-weight: 700;
}
QFrame#Panel {
    background: #171b22;
    border: 1px solid #2a313b;
    border-radius: 8px;
}
QLineEdit {
    background: #0f1217;
    border: 1px solid #2a313b;
    border-radius: 6px;
    padding: 7px 9px;
    selection-background-color: #2f80ed;
}
QLineEdit:focus {
    border-color: #4aa3ff;
}
QPushButton {
    background: #252c35;
    border: 1px solid #343d49;
    border-radius: 6px;
    padding: 7px 12px;
    font-weight: 700;
}
QPushButton:hover {
    background: #303946;
}
QPushButton:disabled {
    color: #6d7683;
    background: #171b22;
}
QPushButton#Primary {
    background: #2f80ed;
    border-color: #2f80ed;
    color: #ffffff;
}
QPushButton#Start {
    background: #1f8f61;
    border-color: #1f8f61;
    color: #ffffff;
}
QPushButton#Stop {
    background: #9b2c3a;
    border-color: #9b2c3a;
    color: #ffffff;
}
QTableWidget {
    background: #0f1217;
    border: 1px solid #2a313b;
    border-radius: 6px;
    gridline-color: #252c35;
}
QHeaderView::section {
    background: #1f2630;
    border: none;
    border-right: 1px solid #343d49;
    padding: 6px;
    font-weight: 700;
}
QTableWidget::item {
    padding: 5px;
}
QTableWidget::item:selected {
    background: #27384f;
}
QTextEdit {
    background: #0b0e12;
    border: 1px solid #2a313b;
    border-radius: 6px;
    color: #c8d1dc;
    font-family: "JetBrains Mono", "Ubuntu Mono", monospace;
    font-size: 11px;
}
QStatusBar {
    background: #0b0e12;
    color: #9aa4b2;
}
"""


class TaskThread(QThread):
    """Run a blocking backend operation without freezing the Qt event loop."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task: Callable[[], object], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._task = task

    def run(self) -> None:
        try:
            self.succeeded.emit(self._task())
        except Exception as exc:  # pragma: no cover - surfaced in GUI
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    """Single-screen APK installer and launcher."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AeroDroid")
        self.setAcceptDrops(True)
        self.resize(980, 640)
        self.setMinimumSize(760, 500)
        self.setStyleSheet(STYLE_SHEET)

        self.container = LXCContainer(
            rootfs=Paths.ROOTFS_DIR,
            memory_mb=Defaults.MEMORY_LIMIT_MB,
            cpu_shares=Defaults.CPU_SHARES,
            cpu_quota_micros=Defaults.CPU_QUOTA_MICROS,
            cpu_period_micros=Defaults.CPU_PERIOD_MICROS,
            pids_max=Defaults.PIDS_MAX,
        )
        self.apk_manager = APKManager(self.container)
        self._workers: list[TaskThread] = []
        self._selected_apk: Optional[Path] = None

        Paths.ensure_user_dirs()
        self._build_ui()
        self._refresh_status()
        self._refresh_apps()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(3000)

        # Track Android framework readiness separately from container process state
        self._android_ready = False

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title = QLabel("AeroDroid")
        title.setObjectName("Title")
        subtitle = QLabel("APK runtime: install, launch, stop.")
        subtitle.setObjectName("Subtle")
        title_wrap.addWidget(title)
        title_wrap.addWidget(subtitle)
        header.addLayout(title_wrap, stretch=1)

        self.state_pill = QLabel("UNKNOWN")
        self.state_pill.setObjectName("StatePill")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.state_pill)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.btn_refresh.clicked.connect(self._refresh_all)
        header.addWidget(self.btn_refresh)
        layout.addLayout(header)

        runtime_panel = self._panel()
        runtime_grid = QGridLayout(runtime_panel)
        runtime_grid.setContentsMargins(12, 12, 12, 12)
        runtime_grid.setHorizontalSpacing(10)
        runtime_grid.setVerticalSpacing(8)

        self.runtime_detail = QLabel()
        self.runtime_detail.setObjectName("RuntimeDetail")
        self.runtime_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        runtime_grid.addWidget(self.runtime_detail, 0, 0, 1, 5)

        self.btn_start = QPushButton("Start Runtime")
        self.btn_start.setObjectName("Start")
        self.btn_start.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_start.clicked.connect(self._start_runtime)
        runtime_grid.addWidget(self.btn_start, 1, 0)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("Stop")
        self.btn_stop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.btn_stop.clicked.connect(self._stop_runtime)
        runtime_grid.addWidget(self.btn_stop, 1, 1)

        self.package_input = QLineEdit()
        self.package_input.setPlaceholderText("com.example.app")
        runtime_grid.addWidget(self.package_input, 1, 2)

        self.btn_launch = QPushButton("Launch")
        self.btn_launch.setObjectName("Primary")
        self.btn_launch.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.btn_launch.clicked.connect(self._launch_input_package)
        runtime_grid.addWidget(self.btn_launch, 1, 3)

        self.btn_desktop = QPushButton("Launch Desktop")
        self.btn_desktop.setObjectName("Primary")
        self.btn_desktop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.btn_desktop.clicked.connect(self._launch_desktop_ui)
        runtime_grid.addWidget(self.btn_desktop, 1, 4)

        self.btn_shared_folder = QPushButton("Shared Folder")
        self.btn_shared_folder.setObjectName("Primary")
        self.btn_shared_folder.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.btn_shared_folder.clicked.connect(self._open_shared_folder)
        runtime_grid.addWidget(self.btn_shared_folder, 1, 5)

        runtime_grid.setColumnStretch(2, 1)
        layout.addWidget(runtime_panel)

        install_panel = self._panel()
        install_grid = QGridLayout(install_panel)
        install_grid.setContentsMargins(12, 12, 12, 12)
        install_grid.setHorizontalSpacing(10)
        install_grid.setVerticalSpacing(8)

        self.apk_path_input = QLineEdit()
        self.apk_path_input.setPlaceholderText("/path/to/app.apk")
        install_grid.addWidget(self.apk_path_input, 0, 0)

        self.btn_browse = QPushButton("APK")
        self.btn_browse.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.btn_browse.clicked.connect(self._browse_apk)
        install_grid.addWidget(self.btn_browse, 0, 1)

        self.btn_install = QPushButton("Install")
        self.btn_install.setObjectName("Primary")
        self.btn_install.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.btn_install.clicked.connect(self._install_selected_apk)
        install_grid.addWidget(self.btn_install, 0, 2)
        install_grid.setColumnStretch(0, 1)
        layout.addWidget(install_panel)

        apps_panel = self._panel()
        apps_layout = QVBoxLayout(apps_panel)
        apps_layout.setContentsMargins(12, 12, 12, 12)
        apps_layout.setSpacing(8)

        apps_header = QHBoxLayout()
        apps_title = QLabel("Installed Apps")
        apps_title.setStyleSheet("font-size: 14px; font-weight: 700;")
        apps_header.addWidget(apps_title)
        apps_header.addStretch()
        self.app_count_label = QLabel("0")
        self.app_count_label.setObjectName("Subtle")
        apps_header.addWidget(self.app_count_label)
        apps_layout.addLayout(apps_header)

        self.apps_table = QTableWidget()
        self.apps_table.setColumnCount(5)
        self.apps_table.setHorizontalHeaderLabels(["Package", "Label", "Version", "Activity", "Actions"])
        self.apps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.apps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.apps_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.apps_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.apps_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.apps_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.apps_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.apps_table.itemSelectionChanged.connect(self._fill_package_from_selection)
        apps_layout.addWidget(self.apps_table)
        layout.addWidget(apps_panel, stretch=1)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(116)
        layout.addWidget(self.log_view)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

    @staticmethod
    def _panel() -> QFrame:
        panel = QFrame()
        panel.setObjectName("Panel")
        return panel

    def _refresh_all(self) -> None:
        self._refresh_status()
        self._refresh_apps()

    def _refresh_status(self) -> None:
        status = self.container.status()
        state = status.get("state", "UNKNOWN")
        backend = status.get("backend", getattr(self.container, "backend", "unknown"))
        pid = status.get("pid", "-")

        if state == "RUNNING":
            # Check if Android framework is actually ready
            ready, _detail = self.container.android_framework_ready()
            self._android_ready = ready
            if ready:
                color = "#1f8f61"
                display_state = "RUNNING"
            else:
                color = "#c47a1a"
                display_state = "BOOTING…"
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self.btn_install.setEnabled(ready)
            self.btn_launch.setEnabled(ready)
            self.btn_desktop.setEnabled(ready)
        elif state == "STOPPED":
            self._android_ready = False
            color = "#4b5563"
            display_state = "STOPPED"
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_install.setEnabled(False)
            self.btn_launch.setEnabled(False)
            self.btn_desktop.setEnabled(False)
        else:
            self._android_ready = False
            color = "#9b2c3a"
            display_state = state
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(True)
            self.btn_install.setEnabled(False)
            self.btn_launch.setEnabled(False)
            self.btn_desktop.setEnabled(False)

        self.state_pill.setText(display_state)
        self.state_pill.setStyleSheet(f"background: {color}; color: #ffffff;")
        self.runtime_detail.setText(
            f"backend={backend} | pid={pid} | rootfs={self.container.rootfs} | core={self.container.core_path}"
        )

    def _refresh_apps(self) -> None:
        apps = self.apk_manager.list_installed()
        self.apps_table.setRowCount(0)
        self.app_count_label.setText(str(len(apps)))

        for row, (package, meta) in enumerate(sorted(apps.items())):
            self.apps_table.insertRow(row)
            self.apps_table.setItem(row, 0, QTableWidgetItem(package))
            self.apps_table.setItem(row, 1, QTableWidgetItem(meta.label))
            self.apps_table.setItem(row, 2, QTableWidgetItem(meta.version_name))
            self.apps_table.setItem(row, 3, QTableWidgetItem(meta.main_activity or ""))

            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(6)

            run = QPushButton("Run")
            run.clicked.connect(lambda _checked=False, pkg=package: self._launch_package(pkg))
            remove = QPushButton("Remove")
            remove.clicked.connect(lambda _checked=False, pkg=package: self._remove_package(pkg))
            action_layout.addWidget(run)
            action_layout.addWidget(remove)
            self.apps_table.setCellWidget(row, 4, actions)

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.btn_refresh,
            self.btn_start,
            self.btn_stop,
            self.btn_browse,
            self.btn_install,
            self.btn_launch,
            self.btn_desktop,
            self.btn_shared_folder,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._refresh_status()

    def _run_task(
        self,
        label: str,
        task: Callable[[], object],
        on_success: Optional[Callable[[object], None]] = None,
    ) -> None:
        self._log(f"> {label}")
        self.status_bar.showMessage(label)
        self._set_busy(True)

        worker = TaskThread(task, self)
        self._workers.append(worker)

        def finish_ok(result: object) -> None:
            if on_success:
                on_success(result)
            self._log("[OK]")
            self.status_bar.showMessage("OK")
            self._refresh_all()

        def finish_err(message: str) -> None:
            self._log(f"[ERR] {message}")
            self.status_bar.showMessage("Error")
            QMessageBox.critical(self, "AeroDroid", message)

        def cleanup() -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            self._set_busy(False)

        worker.succeeded.connect(finish_ok)
        worker.failed.connect(finish_err)
        worker.finished.connect(cleanup)
        worker.start()

    def _start_runtime(self) -> None:
        self._run_task("Starting runtime", lambda: self.container.start())

    def _stop_runtime(self) -> None:
        self._run_task("Stopping runtime", lambda: self.container.stop())

    def _browse_apk(self) -> None:
        apk, _ = QFileDialog.getOpenFileName(
            self,
            "Select APK",
            "",
            "Android Package (*.apk);;All Files (*)",
        )
        if apk:
            self._set_apk_path(Path(apk))

    def _set_apk_path(self, apk_path: Path) -> None:
        self._selected_apk = apk_path
        self.apk_path_input.setText(str(apk_path))

    def _install_selected_apk(self) -> None:
        apk_text = self.apk_path_input.text().strip()
        if not apk_text:
            QMessageBox.warning(self, "AeroDroid", "Select an APK first.")
            return

        apk_path = Path(apk_text).expanduser()
        if not apk_path.exists():
            QMessageBox.critical(self, "AeroDroid", f"APK not found: {apk_path}")
            return

        if not self.container.is_running():
            ans = QMessageBox.question(
                self, "AeroDroid",
                "Android runtime is not running.\nStart it now? (may take 2-3 minutes to boot)"
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            self._run_task("Starting runtime", lambda: self.container.start(wait=True))
            return

        if not self._android_ready:
            QMessageBox.information(
                self, "AeroDroid",
                "Android is still booting…\nPlease wait until the status shows RUNNING then try again."
            )
            return

        def install() -> APKMetadata:
            metadata = self.apk_manager.extract_metadata(apk_path)
            return self.apk_manager.install_apk(apk_path, metadata)

        def success(result: object) -> None:
            metadata = result if isinstance(result, APKMetadata) else None
            if metadata:
                self.package_input.setText(metadata.package_name)
                self._log(f"installed {metadata.package_name}")

        self._run_task(f"Installing {apk_path.name}", install, success)

    def _launch_input_package(self) -> None:
        package = self.package_input.text().strip()
        if not package:
            QMessageBox.warning(self, "AeroDroid", "Enter a package name.")
            return
        self._launch_package(package)

    def _launch_desktop_ui(self) -> None:
        if not self.container.is_running():
            QMessageBox.warning(self, "AeroDroid", "Runtime is not running.")
            return

        def launch() -> object:
            result = self.container.launch_desktop()
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise LXCError(detail or f"launch desktop failed: {result.returncode}")
            return result.stdout or "desktop launched"

        self._run_task("Launching Android Desktop", launch)

    def _open_shared_folder(self) -> None:
        import subprocess
        shared_dir = Path("/home/dang-nhat-phi/AeroAPK/shared")
        shared_dir.mkdir(parents=True, exist_ok=True)
        try:
            shared_dir.chmod(0o777)
        except Exception:
            pass
        subprocess.Popen(["xdg-open", str(shared_dir)])

    def _launch_package(self, package: str) -> None:
        if not self.container.is_running():
            QMessageBox.warning(self, "AeroDroid", "Runtime is not running.")
            return

        def launch() -> object:
            meta = self.apk_manager.list_installed().get(package)
            activity = meta.main_activity if meta else None
            result = self.container.launch_app(package, activity)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise LXCError(detail or f"launch failed: {result.returncode}")
            return result.stdout or "launched"

        self._run_task(f"Launching {package}", launch)

    def _remove_package(self, package: str) -> None:
        answer = QMessageBox.question(self, "AeroDroid", f"Remove {package}?")
        if answer != QMessageBox.StandardButton.Yes:
            return

        def remove() -> object:
            if not self.apk_manager.uninstall(package):
                raise APKError(f"Package not found: {package}")
            return package

        self._run_task(f"Removing {package}", remove)

    def _fill_package_from_selection(self) -> None:
        selected = self.apps_table.selectedItems()
        if selected:
            self.package_input.setText(selected[0].text())

    def _install_apk(self, path: str) -> None:
        self._set_apk_path(Path(path))
        self._install_selected_apk()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(url.toLocalFile().lower().endswith(".apk") for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local.lower().endswith(".apk"):
                self._install_apk(local)
                break

    def _log(self, message: str) -> None:
        self.log_view.append(message)
        scroll = self.log_view.verticalScrollBar()
        scroll.setValue(scroll.maximum())
