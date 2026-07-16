#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_exception(exception_type, callback, message: str) -> None:
    try:
        callback()
    except exception_type:
        return
    raise AssertionError(message)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_upgrade.py TARGET_DIR")
    target_dir = Path(sys.argv[1]).expanduser().resolve()
    sys.path.insert(0, str(target_dir))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from app.disk_guard import (
        DISK_AUTO_STOP_PERCENT,
        DISK_CHECK_INTERVAL_SECONDS,
        DISK_START_BLOCK_PERCENT,
        DiskAutoStopReached,
        DiskUsageLimitExceeded,
        RecordingDiskUsage,
        ensure_recording_disk_available,
    )

    require(DISK_AUTO_STOP_PERCENT == 90.0, "automatic stop threshold must be 90%")
    require(DISK_START_BLOCK_PERCENT == 98.0, "hard stop threshold must be 98%")
    require(
        DISK_CHECK_INTERVAL_SECONDS <= 0.25,
        "disk checks must run at least four times per second",
    )

    with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 900, 100)):
        usage = ensure_recording_disk_available(target_dir)
    require(usage.auto_stop_reached, "90% must reach the automatic stop threshold")
    require(not usage.start_block_reached, "90% must still allow a manual restart")

    with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 979, 21)):
        usage = ensure_recording_disk_available(target_dir)
    require(not usage.start_block_reached, "97.9% must still allow recording")

    with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 980, 20)):
        expect_exception(
            DiskUsageLimitExceeded,
            lambda: ensure_recording_disk_available(target_dir),
            "98% did not block recording",
        )

    from app.camera_process import SegmentedBagRecorder

    class FakeRecordDevice:
        def __init__(self, _device, _path: str):
            pass

        def pause(self) -> None:
            pass

    def make_recorder() -> SegmentedBagRecorder:
        return SegmentedBagRecorder(
            record_device_cls=FakeRecordDevice,
            pipeline=object(),
            device=object(),
            camera_index=0,
            stream_mode="RGB-D raw",
            depth_mode="2d",
        )

    with tempfile.TemporaryDirectory() as temp_dir, patch(
        "app.camera_process._write_segment_metadata"
    ), patch("app.camera_process._release_record_device"):
        recorder = make_recorder()
        path = Path(temp_dir) / "soft_stop.bag"
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 899, 101)):
            recorder.start(path)
        recorder._last_disk_check_at = None
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 901, 99)):
            expect_exception(
                DiskAutoStopReached,
                recorder.rotate_if_due,
                "a recording started below 90% did not auto-stop after crossing 90%",
            )
        recorder.close()

        recorder = make_recorder()
        path = Path(temp_dir) / "buffer_restart.bag"
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 920, 80)):
            recorder.start(path)
        recorder._last_disk_check_at = None
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 950, 50)):
            require(recorder.rotate_if_due() is None, "buffer restart stopped again before 98%")
        recorder._last_disk_check_at = None
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 980, 20)):
            expect_exception(
                DiskUsageLimitExceeded,
                recorder.rotate_if_due,
                "buffer restart did not hard-stop at 98%",
            )
        recorder.close()

    from PyQt6.QtWidgets import QApplication
    from app.camera_page_base import CameraPageBase

    app = QApplication.instance() or QApplication([])

    def disk_status(percent: float) -> RecordingDiskUsage:
        return RecordingDiskUsage(target_dir, 1000, int(percent * 10), 0, percent)

    with patch(
        "app.camera_page_base.get_recording_disk_usage",
        return_value=disk_status(50.0),
    ):
        page = CameraPageBase()
    page.global_state_timer.stop()
    page.disk_guard_timer.stop()
    try:
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(90.1),
        ), patch.object(
            page,
            "is_any_recording_or_pending",
            return_value=True,
        ), patch.object(page, "_stop_all_recording") as stop_recording:
            require(page._check_recording_disk(), "GUI treated 90% as a hard block")
            require(page._check_recording_disk(), "GUI changed the 90% buffer state")
        require(stop_recording.call_count == 1, "GUI automatic stop was not requested exactly once")
        require(not page._disk_recording_blocked, "GUI blocked manual restart below 98%")

        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(92.0),
        ), patch.object(page, "is_any_recording_or_pending", return_value=False):
            require(page._check_recording_disk(), "GUI rejected a manual restart in the buffer")

        page._disk_guard_stop_requested = False
        with patch(
            "app.camera_page_base.get_recording_disk_usage",
            return_value=disk_status(98.0),
        ), patch.object(
            page,
            "is_any_recording_or_pending",
            return_value=True,
        ), patch.object(page, "_stop_all_recording") as hard_stop:
            require(not page._check_recording_disk(), "GUI did not hard-block at 98%")
        require(hard_stop.call_count == 1, "GUI did not request a hard stop at 98%")
        require(page._disk_recording_blocked, "GUI recording controls were not blocked at 98%")
    finally:
        for panel in page.camera_panels:
            panel.worker = None
        page.shutdown()
        page.deleteLater()
        app.processEvents()

    print("upgrade verification passed: 90% soft stop, buffer restart, 98% hard stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
