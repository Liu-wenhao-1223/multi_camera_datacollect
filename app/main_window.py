from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent, QGuiApplication, QShowEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .camera_page import CameraPage
from .camera_page_1280 import CameraPage1280
from .paths import ensure_project_dirs
from .recording_settings import recording_output_dir


class MainWindow(QMainWindow):
    COMPACT_WINDOW_SIZE = (1280, 800)
    STANDARD_WINDOW_SIZE = (1500, 920)
    SCREEN_MARGIN = 32

    def __init__(self):
        super().__init__()
        ensure_project_dirs()
        self.setWindowTitle("Multi Camera Data Collect")
        self.setMinimumSize(760, 520)
        self._startup_geometry_applied = False
        self.shell = QWidget(self)
        self.shell_layout = QHBoxLayout(self.shell)
        self.shell_layout.setContentsMargins(0, 0, 0, 0)
        self.shell_layout.setSpacing(0)

        self.camera_page = CameraPage(
            self,
            show_sync_mode_controls=True,
            default_multi_camera_sync=True,
        )
        self.camera_page_1280 = CameraPage1280(
            self,
            show_sync_mode_controls=True,
            default_multi_camera_sync=True,
        )
        self.camera_stack = QStackedWidget(self)
        self.camera_stack.addWidget(self.camera_page)
        self.camera_stack.addWidget(self.camera_page_1280)
        self.shell_layout.addWidget(self._build_sidebar(), 0)
        self.shell_layout.addWidget(self.camera_stack, 1)
        self.setCentralWidget(self.shell)
        self.camera_stack.setCurrentWidget(self.camera_page_1280)
        self._set_layout_toggle_state(compact=True)
        self._resize_for_layout(compact=True)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame(self)
        sidebar.setObjectName("layoutSidebar")
        sidebar.setFixedWidth(48)
        sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        sidebar.setStyleSheet(
            """
            QFrame#layoutSidebar {
                background: #F3F6FA;
                border-right: 1px solid #D8DEE8;
            }
            QPushButton {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                color: #334155;
                font-size: 11px;
                font-weight: 600;
                min-height: 28px;
                max-height: 28px;
                padding: 0px;
            }
            QPushButton:hover {
                background: #EFF6FF;
                border-color: #93C5FD;
                color: #1D4ED8;
            }
            QPushButton:checked {
                background: #2563EB;
                border-color: #2563EB;
                color: #FFFFFF;
            }
            """
        )

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(5, 8, 5, 8)
        layout.setSpacing(4)
        self.layout_toggle_button = QPushButton("1280", sidebar)
        self.layout_toggle_button.setFixedSize(38, 30)
        self.layout_toggle_button.setCheckable(True)
        self.layout_toggle_button.setToolTip("切换 1280x800 紧凑布局")
        self.layout_toggle_button.clicked.connect(self._toggle_camera_layout)
        layout.addWidget(
            self.layout_toggle_button,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
        )
        layout.addStretch(1)
        return sidebar

    def _set_layout_toggle_state(self, compact: bool) -> None:
        self.layout_toggle_button.blockSignals(True)
        self.layout_toggle_button.setChecked(compact)
        self.layout_toggle_button.setText("标准" if compact else "1280")
        self.layout_toggle_button.setToolTip(
            "切回标准大屏页面" if compact else "切换 1280x800 紧凑布局"
        )
        self.layout_toggle_button.blockSignals(False)

    def _resize_for_layout(self, compact: bool) -> None:
        desired_width, desired_height = (
            self.COMPACT_WINDOW_SIZE if compact else self.STANDARD_WINDOW_SIZE
        )
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(desired_width, desired_height)
            return

        available = screen.availableGeometry()
        max_width = max(640, available.width() - self.SCREEN_MARGIN)
        max_height = max(480, available.height() - self.SCREEN_MARGIN)
        target_width = min(desired_width, max_width)
        target_height = min(desired_height, max_height)
        self.resize(target_width, target_height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def _toggle_camera_layout(self, checked: bool) -> None:
        target_compact = bool(checked)
        current_page = self.current_camera_page()
        if current_page.is_any_camera_running() or current_page.is_any_recording_or_pending():
            current_page.global_status_label.setText("请先停止当前页面相机/录制，再切换页面。")
            self.layout_toggle_button.blockSignals(True)
            self.layout_toggle_button.setChecked(not target_compact)
            self.layout_toggle_button.blockSignals(False)
            return

        target_page = self.camera_page_1280 if target_compact else self.camera_page
        target_page.set_recording_output_dir(recording_output_dir())
        self.camera_stack.setCurrentWidget(target_page)
        self._set_layout_toggle_state(target_compact)
        if not (self.isMaximized() or self.isFullScreen()):
            self._resize_for_layout(target_compact)

    def current_camera_page(self):
        return self.camera_stack.currentWidget()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._startup_geometry_applied:
            self._startup_geometry_applied = True
            self._resize_for_layout(compact=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        shutdown_ok = self.camera_page.shutdown()
        shutdown_ok = self.camera_page_1280.shutdown() and shutdown_ok
        if not shutdown_ok:
            event.ignore()
            return
        super().closeEvent(event)
