from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.camera_page_base import CameraPageBase, create_camera_recording_session_dir
from app.disk_guard import RecordingDiskUsage


def disk_status(percent_used: float) -> RecordingDiskUsage:
    total = 1000
    used = int(percent_used * 10)
    return RecordingDiskUsage(
        path=Path.cwd(),
        total_bytes=total,
        used_bytes=used,
        free_bytes=total - used,
        percent_used=percent_used,
    )


class DiskGuardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(50.0),
        ):
            self.page = CameraPageBase()
        self.page.global_state_timer.stop()
        self.page.disk_guard_timer.stop()

    def tearDown(self) -> None:
        for panel in self.page.camera_panels:
            panel.worker = None
        self.page.shutdown()
        self.page.deleteLater()
        self.app.processEvents()

    def test_soft_limit_requests_one_stop_but_keeps_restart_available(self) -> None:
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(90.1),
        ), patch.object(
            self.page,
            "is_any_recording_or_pending",
            return_value=True,
        ), patch.object(self.page, "_stop_all_recording") as stop_recording:
            self.assertTrue(self.page._check_recording_disk())
            self.assertTrue(self.page._check_recording_disk())

        stop_recording.assert_called_once_with()
        self.assertFalse(self.page._disk_recording_blocked)
        self.assertIn("90.1%", self.page.global_status_label.text())
        self.assertTrue(all(not panel._recording_block_reason for panel in self.page.camera_panels))

    def test_buffer_usage_allows_manual_restart_without_second_soft_stop(self) -> None:
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(92.0),
        ), patch.object(
            self.page,
            "is_any_recording_or_pending",
            return_value=False,
        ), patch.object(self.page, "_stop_all_recording") as stop_recording:
            self.assertTrue(self.page._check_recording_disk())
        stop_recording.assert_not_called()
        self.assertFalse(self.page._disk_recording_blocked)
        self.assertFalse(self.page._disk_soft_stop_armed)

    def test_hard_limit_stops_and_blocks_all_recording(self) -> None:
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(98.0),
        ), patch.object(
            self.page,
            "is_any_recording_or_pending",
            return_value=True,
        ), patch.object(self.page, "_stop_all_recording") as stop_recording:
            self.assertFalse(self.page._check_recording_disk())
        stop_recording.assert_called_once_with()
        self.assertTrue(self.page._disk_recording_blocked)
        self.assertTrue(all(panel._recording_block_reason for panel in self.page.camera_panels))

    def test_recovery_unblocks_recording_but_does_not_auto_restart(self) -> None:
        self.page._apply_recording_disk_block(True, "blocked")
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(70.0),
        ), patch.object(self.page, "_start_all_recording") as start_recording:
            self.assertTrue(self.page._check_recording_disk())

        start_recording.assert_not_called()
        self.assertFalse(self.page._disk_recording_blocked)
        self.assertIn("90% 自动暂停已重新启用", self.page.global_status_label.text())

    def test_blocked_panel_cannot_dispatch_start_command(self) -> None:
        panel = self.page.camera_panels[0]
        worker = MagicMock()
        worker.isRunning.return_value = True
        panel.worker = worker
        panel.set_recording_blocked("磁盘已满")

        started = panel.start_recording_to_bag(Path.cwd() / "record" / "camera.bag")

        self.assertFalse(started)
        worker.request_start_recording.assert_not_called()
        panel.worker = None

    def test_global_start_does_not_create_session_when_blocked(self) -> None:
        self.page._disk_recording_blocked = True
        self.page._disk_recording_block_message = "磁盘已满"
        with patch.object(self.page, "_check_recording_disk", return_value=False), patch(
            "app.camera_page_base.create_camera_recording_session_dir",
            wraps=create_camera_recording_session_dir,
        ) as create_session:
            self.page._start_all_recording()
        create_session.assert_not_called()

    def test_disk_limit_does_not_start_post_recording_conversion(self) -> None:
        panel = self.page.camera_panels[0]
        worker = MagicMock()
        worker.isRunning.return_value = True
        panel.worker = worker
        panel._convert_recording_to_mp4 = True
        with patch.object(
            panel,
            "_recording_disk_allows_start",
            return_value=False,
        ), patch.object(panel, "_start_mp4_conversions") as start_conversion:
            panel._on_recording_stopped(str(Path.cwd() / "record" / "camera.bag"))
        start_conversion.assert_not_called()
        panel.worker = None


if __name__ == "__main__":
    unittest.main()
